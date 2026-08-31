#!/usr/bin/env python3
"""
DSEWebNet → MQTT Bridge for Home Assistant
Connects to DSEWebNet cloud via WebSocket and publishes generator data to HASS.
"""

import asyncio
import json
import logging
import os
import re
import time

import aiohttp
import paho.mqtt.client as mqtt
import yarl

class _ColorFormatter(logging.Formatter):
    _TIME_COLORS = {
        logging.DEBUG:    "\033[32m",   # green
        logging.INFO:     "\033[32m",   # green
        logging.WARNING:  "\033[33m",   # yellow
        logging.ERROR:    "\033[31m",   # red
        logging.CRITICAL: "\033[31m",   # red
    }
    _RESET  = "\033[0m"
    _WHITE  = "\033[97m"
    _CYAN   = "\033[96m"

    def format(self, record):
        time_color = self._TIME_COLORS.get(record.levelno, "\033[32m")
        timestamp  = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        msecs      = int(record.msecs)
        level      = record.levelname
        msg        = record.getMessage()
        if "NEW SESSION" in msg:
            msg_colored = self._CYAN + msg + self._RESET
        else:
            msg_colored = self._WHITE + msg + self._RESET
        return (
            f"{time_color}{timestamp},{msecs:03d}{self._RESET} "
            f"{time_color}{level}{self._RESET} "
            f"{msg_colored}"
        )

_handler = logging.StreamHandler()
_handler.setFormatter(_ColorFormatter("%(asctime)s %(levelname)s %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger(__name__)

# ── Config (override via environment variables) ───────────────────────────
DSE_LOGIN_URL   = "https://www.dsewebnet.com/login.php"
DSE_WS_URL      = "wss://www.dsewebnet.com/user"
DSE_USERNAME    = os.getenv("DSE_USERNAME", "")
DSE_PASSWORD    = os.getenv("DSE_PASSWORD", "")
GATEWAY_ID      = os.getenv("GATEWAY_ID",  "19128A7F6315501")
MODULE_ID       = os.getenv("MODULE_ID",   "6729830DF6")

MQTT_HOST       = os.getenv("MQTT_HOST",   "192.168.70.7")
MQTT_PORT       = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER       = os.getenv("MQTT_USER",   "")
MQTT_PASS       = os.getenv("MQTT_PASS",   "")

MQTT_PREFIX     = "dse/generator"
HASS_PREFIX     = "homeassistant"
POLL_INTERVAL   = int(os.getenv("POLL_INTERVAL", "30"))  # seconds between status polls
RECONNECT_DELAY = 15   # seconds before reconnect on error

# ── DSEWebNet protocol ────────────────────────────────────────────────────
# Initial subscription message (wildcards for any gateway/module)
SUBSCRIPTION = {
    "1": {
        "*": {
            "modules": {"*": {"129": [65535],
                              "130": [0, 1, 2, 3, 4],
                              "131": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13],
                              "132": [0]}},
            "data":    {"5": [65535], "8": [65535]}
        }
    }
}

# Control command IDs
CMD = {"stop": 35700, "auto": 35701, "manual": 35702, "start": 35705}

# param 130 sub-key → state field name
STATUS_FIELDS = {"0": "engine_state", "1": "mains_state",
                 "2": "load_state", "3": "supervisor_state", "4": "mode_state"}

# param 131 sub-key → (field name, unit)
ELEC_FIELDS = {
    "0":  ("oil_pressure",    "bar"),
    "1":  ("coolant_temp",    "°C"),
    "2":  ("oil_temp",        "°C"),
    "3":  ("fuel_level",      "%"),
    "4":  ("charge_voltage",  "V"),
    "5":  ("battery_voltage", "V"),
    "6":  ("engine_rpm",      "RPM"),
    "7":  ("frequency",       "Hz"),
    "8":  ("voltage_l1n",     "V"),
    "9":  ("voltage_l2n",     "V"),
    "10": ("voltage_l3n",     "V"),
    "11": ("voltage_l1l2",    "V"),
    "12": ("voltage_l2l3",    "V"),
    "13": ("voltage_l3l1",    "V"),
}

# param 129 sub-key 0 → alarm summary: {"severityID": N, "timestamp": ..., "string": "..."}
# severityID 0 = no alarm. This is the ONLY alarm channel the cloud sends;
# param 132 is subscribed by the original protocol capture but never arrives.
ALARM_SEVERITY = {0: "none", 1: "warning", 2: "shutdown", 3: "electrical trip"}

# ── Runtime state ─────────────────────────────────────────────────────────
state = {
    "engine_state": "unknown", "mains_state": "unknown",
    "load_state": "unknown",   "supervisor_state": "unknown",
    "mode_state": "unknown",   "oil_pressure": 0.0,
    "frequency": 0.0,
    "voltage_l1n": 0.0,  "voltage_l2n": 0.0,  "voltage_l3n": 0.0,
    "voltage_l1l2": 0.0, "voltage_l2l3": 0.0, "voltage_l3l1": 0.0,
    "coolant_temp": None, "oil_temp": None, "fuel_level": None,
    "charge_voltage": 0.0, "battery_voltage": 0.0, "engine_rpm": 0,
    "alarm_severity": 0, "alarm_text": "", "alarm_active": "OFF",
}
last_data_ts = 0.0        # когда в последний раз пришли данные из облака
availability = "offline"  # что сейчас объявлено в MQTT
mqttc = None
_loop = None          # asyncio event loop (set in main, used for thread-safe queue puts)
pending_cmd = asyncio.Queue()

# ── MQTT ──────────────────────────────────────────────────────────────────
def mqtt_setup():
    global mqttc
    mqttc = mqtt.Client(client_id="dsewebnet-bridge")
    if MQTT_USER:
        mqttc.username_pw_set(MQTT_USER, MQTT_PASS)
    mqttc.will_set(f"{MQTT_PREFIX}/availability", "offline", retain=True)
    mqttc.on_connect = _on_connect
    mqttc.on_message = _on_message
    mqttc.connect(MQTT_HOST, MQTT_PORT, 60)
    mqttc.loop_start()

def _on_connect(client, userdata, flags, rc):
    # rc != 0 = отказ (неверные креды, брокер не пускает). До 1.1.0 код писал
    # "MQTT connected" при любом исходе и шёл подписываться в никуда.
    if rc != 0:
        log.error(f"MQTT connect refused, rc={rc} — проверь mqtt_user/mqtt_pass и адрес брокера")
        return
    log.info("MQTT connected")
    client.subscribe(f"{MQTT_PREFIX}/command")
    client.publish(f"{MQTT_PREFIX}/availability", "online", retain=True)
    _publish_discovery(client)

def _on_message(client, userdata, msg):
    cmd = msg.payload.decode().strip().lower()
    if cmd in CMD:
        _loop.call_soon_threadsafe(pending_cmd.put_nowait, cmd)
        log.info(f"Command queued: {cmd}")
    else:
        log.warning(f"Unknown command: {cmd}")

def _publish_state():
    mqttc.publish(f"{MQTT_PREFIX}/state", json.dumps(state))
    _set_availability("online")

def _set_availability(value: str):
    """Объявляет доступность только при СМЕНЕ значения.
    Зачем: MQTT-завещание срабатывает лишь при обрыве самого MQTT. Если отвалится ОБЛАКО,
    брокер жив, availability остаётся online — и дом часами показывает последние значения
    как актуальные. Для генератора это худший отказ: тишина выглядит как «всё хорошо»."""
    global availability
    if value != availability:
        availability = value
        mqttc.publish(f"{MQTT_PREFIX}/availability", value, retain=True)
        log.warning(f"availability → {value}") if value == "offline" else log.info(f"availability → {value}")

async def _staleness_watchdog():
    """Нет данных дольше трёх интервалов опроса (но не меньше 120 с) → объявляем offline."""
    limit = max(3 * POLL_INTERVAL, 120)
    while True:
        await asyncio.sleep(15)
        if last_data_ts and (time.time() - last_data_ts) > limit:
            _set_availability("offline")

def _publish_discovery(client):
    device = {
        "identifiers": [f"dse_{MODULE_ID}"],
        "name": "DSE Generator",
        "manufacturer": "Deep Sea Electronics",
        "model": "DSE6110 MKIII",
    }
    avail = f"{MQTT_PREFIX}/availability"
    state_topic = f"{MQTT_PREFIX}/state"

    sensors = [
        ("engine_state",     "Engine State",       None,        None,   "mdi:engine"),
        ("mains_state",      "Mains State",        None,        None,   "mdi:transmission-tower"),
        ("load_state",       "Load State",         None,        None,   "mdi:power-plug"),
        ("mode_state",       "Generator Mode",     None,        None,   "mdi:state-machine"),
        ("supervisor_state", "Supervisor State",   None,        None,   "mdi:shield-check"),
        ("oil_pressure",     "Oil Pressure",       "pressure",  "bar",  "mdi:gauge"),
        ("frequency",        "Gen Frequency",      "frequency", "Hz",   "mdi:sine-wave"),
        ("voltage_l1n",      "Gen Voltage L1-N",   "voltage",   "V",    "mdi:lightning-bolt"),
        ("voltage_l2n",      "Gen Voltage L2-N",   "voltage",   "V",    "mdi:lightning-bolt"),
        ("voltage_l3n",      "Gen Voltage L3-N",   "voltage",   "V",    "mdi:lightning-bolt"),
        ("voltage_l1l2",     "Gen Voltage L1-L2",  "voltage",   "V",    "mdi:lightning-bolt"),
        ("voltage_l2l3",     "Gen Voltage L2-L3",  "voltage",   "V",    "mdi:lightning-bolt"),
        ("voltage_l3l1",     "Gen Voltage L3-L1",  "voltage",   "V",    "mdi:lightning-bolt"),
        ("battery_voltage",  "Starter Battery",    "voltage",   "V",    "mdi:car-battery"),
        ("charge_voltage",   "Charge Alternator",  "voltage",   "V",    "mdi:current-dc"),
        ("engine_rpm",       "Engine Speed",       None,        "RPM",  "mdi:speedometer"),
        ("coolant_temp",     "Coolant Temp",       "temperature","°C",  "mdi:coolant-temperature"),
        ("oil_temp",         "Oil Temp",           "temperature","°C",  "mdi:oil-temperature"),
        ("fuel_level",       "Fuel Level",         None,        "%",    "mdi:fuel"),
        ("alarm_text",       "Alarm Text",         None,        None,   "mdi:alert-circle-outline"),
    ]
    for uid, name, dc, unit, icon in sensors:
        cfg = {
            "unique_id": f"dse_{MODULE_ID}_{uid}",
            "name": name,
            "state_topic": state_topic,
            "value_template": f"{{{{ value_json.{uid} }}}}",
            "availability_topic": avail,
            "device": device,
            "icon": icon,
        }
        if dc:   cfg["device_class"] = dc
        if unit: cfg["unit_of_measurement"] = unit
        client.publish(f"{HASS_PREFIX}/sensor/dse_{MODULE_ID}/{uid}/config",
                       json.dumps(cfg), retain=True)

    # Авария генератора отдельной сущностью: до 1.1.0 мост не публиковал НИ ОДНОГО флага,
    # хотя облако шлёт сводку в параметре 129.
    client.publish(
        f"{HASS_PREFIX}/binary_sensor/dse_{MODULE_ID}/alarm/config",
        json.dumps({
            "unique_id": f"dse_{MODULE_ID}_alarm_active",
            "name": "Generator Alarm",
            "state_topic": state_topic,
            "value_template": "{{ value_json.alarm_active }}",
            "json_attributes_topic": state_topic,
            "json_attributes_template": '{"severity": {{ value_json.alarm_severity }}, "text": "{{ value_json.alarm_text }}"}',
            "payload_on": "ON", "payload_off": "OFF",
            "device_class": "problem",
            "availability_topic": avail,
            "device": device,
        }), retain=True)

    for cmd_name in CMD:
        cfg = {
            "unique_id": f"dse_{MODULE_ID}_btn_{cmd_name}",
            "name": f"Generator {cmd_name.capitalize()}",
            "command_topic": f"{MQTT_PREFIX}/command",
            "payload_press": cmd_name,
            "availability_topic": avail,
            "device": device,
            "icon": "mdi:generator-stationary",
        }
        client.publish(f"{HASS_PREFIX}/button/dse_{MODULE_ID}/{cmd_name}/config",
                       json.dumps(cfg), retain=True)

    log.info("HASS discovery published")

# ── Message parsing ───────────────────────────────────────────────────────
def _safe_num(v):
    """Число или None. Контроллер шлёт "----" (нет датчика) и "####" (вне диапазона) —
    float() на них даёт  ValueError и роняет разбор всего сообщения (до 1.1.0 это был
    путь к обрыву WS-сессии, стоило добавить любое из полей 1/2/3)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def _handle_ws_message(raw: str):
    global last_data_ts
    last_data_ts = time.time()
    try:
        msg = json.loads(raw)
    except Exception:
        log.warning(f"WS non-JSON: {raw[:100]}")
        return
    if "2" not in msg:
        return

    # Find module data (may arrive keyed by actual gateway/module IDs)
    payload2 = msg["2"]
    mod_data = {}
    for gw_val in payload2.values():
        if isinstance(gw_val, dict) and "modules" in gw_val:
            for mod_val in gw_val["modules"].values():
                if isinstance(mod_val, dict):
                    for k, v in mod_val.items():
                        mod_data[k] = v

    changed = False

    # param 130 – status strings
    if "130" in mod_data:
        for key, field in STATUS_FIELDS.items():
            if key in mod_data["130"]:
                state[field] = mod_data["130"][key]
                changed = True

    # param 131 – electrical measurements
    if "131" in mod_data:
        for key, (field, _) in ELEC_FIELDS.items():
            if key in mod_data["131"]:
                val = mod_data["131"][key]
                raw = val.get("value") if isinstance(val, dict) else val
                num = _safe_num(raw)
                if num is None:
                    # Контроллер шлёт "----" (датчика нет) и "####" (вне диапазона).
                    # Это НЕ ноль: ноль соврал бы про исправный датчик с нулевым значением.
                    state[field] = None
                elif isinstance(val, dict):
                    state[field] = round(num, 1)
                else:
                    state[field] = round(num * 0.1, 1)
                changed = True

    # param 129 – сводка аварий. Единственный канал аварий, который шлёт облако:
    # {"severityID": N, "timestamp": ..., "string": "текст"}. 0 = аварий нет.
    if "129" in mod_data:
        a = mod_data["129"].get("0")
        if isinstance(a, dict):
            sev = int(_safe_num(a.get("severityID", 0)) or 0)
            state["alarm_severity"] = sev
            state["alarm_text"] = a.get("string", "") or ALARM_SEVERITY.get(sev, "")
            state["alarm_active"] = "ON" if sev > 0 else "OFF"
            changed = True

    if changed:
        _publish_state()
        log.info(f"engine={state['engine_state']} mode={state['mode_state']} "
                 f"oil={state['oil_pressure']}bar L1={state['voltage_l1n']}V "
                 f"freq={state['frequency']}Hz bat={state['battery_voltage']}V "
                 f"rpm={state['engine_rpm']} alarm={state['alarm_severity']}")

# ── WebSocket loop ────────────────────────────────────────────────────────
async def _send(ws, cmd_id: int, label: str = ""):
    await ws.send_str(json.dumps({"3": {GATEWAY_ID: {"modules": {MODULE_ID: [cmd_id]}}}}))
    log.info(f"→ {label or cmd_id}")

async def _login(session: aiohttp.ClientSession) -> bool:
    try:
        async with session.get(DSE_LOGIN_URL) as resp:
            html = await resp.text()
        csrf_id  = re.search(r'name="login\[_csrfID\]"\s+value="([^"]+)"', html)
        csrf_key = re.search(r'name="login\[_csrfKey\]"\s+value="([^"]+)"', html)
        data = {
            "login[username]":  DSE_USERNAME,
            "login[password]":  DSE_PASSWORD,
            "login[btnLogin]":  "Login",
        }
        if csrf_id:  data["login[_csrfID]"]  = csrf_id.group(1)
        if csrf_key: data["login[_csrfKey]"] = csrf_key.group(1)
        async with session.post(DSE_LOGIN_URL, data=data, allow_redirects=True) as resp:
            cookies = session.cookie_jar.filter_cookies(yarl.URL("https://www.dsewebnet.com"))
            if "sessionKey" in cookies or "login" not in str(resp.url):
                log.info("Login OK")
                return True
            log.error(f"Login failed – status {resp.status}")
            return False
    except Exception as e:
        log.error(f"Login error: {e}")
        return False

async def ws_loop():
    async with aiohttp.ClientSession() as session:
        while True:
            if not await _login(session):
                await asyncio.sleep(RECONNECT_DELAY)
                continue
            try:
                async with session.ws_connect(DSE_WS_URL) as ws:
                    print("\033[36m" + "─" * 60 + "\033[0m")
                    log.info("\033[36m▶ NEW SESSION — WebSocket connected\033[0m")
                    await ws.send_str(json.dumps(SUBSCRIPTION))

                    async def _poller():
                        while True:
                            await asyncio.sleep(POLL_INTERVAL)
                            await ws.send_str(json.dumps(SUBSCRIPTION))

                    async def _cmd_sender():
                        while True:
                            cmd = await pending_cmd.get()
                            if cmd == "start":
                                await _send(ws, CMD["manual"], "manual (pre-start)")
                                await asyncio.sleep(1)
                            await _send(ws, CMD[cmd], cmd)

                    poll_task = asyncio.create_task(_poller())
                    cmd_task  = asyncio.create_task(_cmd_sender())

                    try:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                _handle_ws_message(msg.data)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                log.warning(f"WS closed: {msg.type}")
                                break
                    finally:
                        poll_task.cancel()
                        cmd_task.cancel()

            except Exception as e:
                log.error(f"WS error: {e}")

            log.info(f"Reconnecting in {RECONNECT_DELAY}s…")
            await asyncio.sleep(RECONNECT_DELAY)

# ── Entry point ───────────────────────────────────────────────────────────
async def main():
    global _loop
    _loop = asyncio.get_running_loop()
    # Push previous session logs off screen
    print("\n" * 80, end="", flush=True)
    mqtt_setup()
    await asyncio.sleep(1)
    watchdog = asyncio.create_task(_staleness_watchdog())
    try:
        await ws_loop()
    finally:
        watchdog.cancel()
        mqttc.publish(f"{MQTT_PREFIX}/availability", "offline", retain=True)
        mqttc.loop_stop()
        mqttc.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
