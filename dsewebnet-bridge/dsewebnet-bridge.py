#!/usr/bin/env python3
"""
DSEWebNet -> MQTT bridge for Home Assistant.

Connects to the DSEWebNet cloud via WebSocket and republishes DSE generator
data to MQTT with Home Assistant auto-discovery.

v1.1.1
  * parameters 131/1..6 identified from the DSE GenComm Page 4 layout
  * GenComm sentinel values (out of range / sensor fault / not measurable)
    are filtered instead of being stored as 65535
  * first value of every parameter is logged as raw -> scaled for verification

v1.1.0
  * declarative parameter registry (units / scale / device_class / state_class)
  * expose_unknown: publishes every unmapped parameter as a diagnostic sensor
    so new values can be identified against the physical controller
  * paho-mqtt 2.x callback API
  * WebSocket heartbeat + stale-data watchdog + exponential backoff
  * two-level availability (MQTT LWT + DSEWebNet session state)
  * read-only by default (allow_control)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import re
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp
import paho.mqtt.client as mqtt
import yarl

VERSION = "2.1.1"


# ── Configuration ─────────────────────────────────────────────────────────
# Values are read from the add-on options file first, then from environment
# variables (useful when running the script standalone / in plain Docker).

_OPTIONS_PATH = os.getenv("OPTIONS_PATH", "/data/options.json")
try:
    with open(_OPTIONS_PATH, encoding="utf-8") as _f:
        _OPTIONS = json.load(_f)
except Exception:
    _OPTIONS = {}


def cfg(key: str, default=None, cast=None):
    val = _OPTIONS.get(key)
    if val is None or val == "":
        env = os.getenv(key.upper())
        val = env if env not in (None, "") else default
    if cast is bool:
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ("1", "true", "yes", "on")
    if cast is not None and val is not None:
        try:
            return cast(val)
        except (TypeError, ValueError):
            return default
    return val


DSE_LOGIN_URL = "https://www.dsewebnet.com/login.php"
DSE_WS_URL = "wss://www.dsewebnet.com/user"

DSE_USERNAME = cfg("dse_username", "")
DSE_PASSWORD = cfg("dse_password", "")
GATEWAY_ID = (cfg("gateway_id", "") or "").strip()
MODULE_ID = (cfg("module_id", "") or "").strip()

MQTT_HOST = cfg("mqtt_host", "")
MQTT_PORT = cfg("mqtt_port", 1883, int)
MQTT_USER = cfg("mqtt_user", "")
MQTT_PASS = cfg("mqtt_pass", "")
MQTT_TOPIC = (cfg("mqtt_topic", "") or "").strip().strip("/")

POLL_INTERVAL = cfg("poll_interval", 30, int)
ALLOW_CONTROL = cfg("allow_control", False, bool)
EXPOSE_UNKNOWN = cfg("expose_unknown", False, bool)
DEBUG_RAW = cfg("debug_raw", False, bool)
LOG_LEVEL = (cfg("log_level", "info") or "info").lower()
CONTROLLER_MODEL = cfg("controller_model", "DSE controller")
DEVICE_NAME = cfg("device_name", "DSE Generator")
SUBSCRIPTION_OVERRIDE = cfg("subscription_override", "")
FILTER_SENTINELS = cfg("filter_sentinels", True, bool)
PROBE_GROUPS = cfg("probe_groups", True, bool)

HASS_PREFIX = "homeassistant"
DEVICE_KEY = f"dse_{MODULE_ID or 'default'}"
MQTT_PREFIX = MQTT_TOPIC or f"dse/{MODULE_ID.lower() if MODULE_ID else 'generator'}"

RECONNECT_BASE = 5          # seconds, doubled on every consecutive failure
RECONNECT_MAX = 300
STALE_FACTOR = 3            # data older than STALE_FACTOR * POLL_INTERVAL -> unavailable
STALE_MIN = 120
WS_HEARTBEAT = 20           # seconds between WebSocket pings
WS_RECEIVE_TIMEOUT = 120    # no frame at all for this long -> drop and reconnect


# ── Logging ───────────────────────────────────────────────────────────────
class _ColorFormatter(logging.Formatter):
    _TIME_COLORS = {
        logging.DEBUG: "\033[36m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[31m",
    }
    _RESET = "\033[0m"
    _WHITE = "\033[97m"
    _CYAN = "\033[96m"

    def format(self, record):
        color = self._TIME_COLORS.get(record.levelno, "\033[32m")
        ts = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        msg = record.getMessage()
        body = self._CYAN if "NEW SESSION" in msg else self._WHITE
        return (
            f"{color}{ts},{int(record.msecs):03d}{self._RESET} "
            f"{color}{record.levelname}{self._RESET} "
            f"{body}{msg}{self._RESET}"
        )


_LEVELS = {
    "trace": logging.DEBUG,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "notice": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "fatal": logging.CRITICAL,
}
_handler = logging.StreamHandler()
_handler.setFormatter(_ColorFormatter())
logging.basicConfig(level=_LEVELS.get(LOG_LEVEL, logging.INFO), handlers=[_handler])
log = logging.getLogger("dsewebnet")


# ── Parameter registry ────────────────────────────────────────────────────
@dataclass(frozen=True)
class Param:
    """One DSEWebNet parameter mapped onto a Home Assistant entity."""
    field: str                       # key in the published JSON state
    name: str                        # friendly name (device name is prepended by HA)
    unit: str | None = None
    scale: float = 1.0               # raw integer * scale = engineering value
    device_class: str | None = None
    state_class: str | None = None   # defaults to "measurement" for numeric params
    precision: int | None = None
    icon: str | None = None
    entity_category: str | None = None
    kind: str = "num"                # "num" | "text"
    bits: int = 16                   # GenComm register width
    signed: bool = False             # GenComm two's complement instrument


# group id -> sub key -> Param
#
# Sub keys that are NOT listed here are still received; enable `expose_unknown`
# to have them published as diagnostic sensors named p<group>_<sub> so they can
# be matched against the readings on the physical controller.
PARAMS: dict[str, dict[str, Param]] = {
    # ── 130: controller state strings ────────────────────────────────────
    "130": {
        "0": Param("engine_state", "Engine state", kind="text", icon="mdi:engine"),
        "1": Param("mains_state", "Mains state", kind="text", icon="mdi:transmission-tower"),
        "2": Param("load_state", "Load state", kind="text", icon="mdi:power-plug"),
        "3": Param("supervisor_state", "Supervisor state", kind="text", icon="mdi:shield-check"),
        "4": Param("mode_state", "Generator mode", kind="text", icon="mdi:state-machine"),
    },
    # ── 131: instrumentation, keyed by DSEWebNet instrument ID ───────────
    #
    # The sub key is the instrument ID from the DSEWebNet catalogue (the list
    # behind "Instrument" in the chart series editor), not a sequential offset.
    # IDs are sparse and run past 1000, which is why a range probe found only
    # the low block. Everything below is the catalogue, transcribed.
    "131": {
        # Engine
        "0": Param("oil_pressure", "Oil pressure", "bar", 1.0, "pressure", precision=2, icon="mdi:gauge"),
        "1": Param("coolant_temp", "Coolant temperature", "°C", 1.0, "temperature", precision=0, signed=True),
        "2": Param("oil_temp", "Oil temperature", "°C", 1.0, "temperature", precision=0, signed=True),
        "3": Param("fuel_level", "Fuel level", "%", 1.0, None, precision=0, icon="mdi:fuel"),
        "4": Param("charge_alt_voltage", "Charge alternator voltage", "V", 1.0, "voltage",
                   precision=1, icon="mdi:alternator"),
        "5": Param("battery_voltage", "Battery voltage", "V", 1.0, "voltage",
                   precision=1, icon="mdi:car-battery"),
        "6": Param("engine_speed", "Engine speed", "rpm", 1.0, None, precision=0, icon="mdi:speedometer"),
        "305": Param("engine_run_time", "Engine hours", "h", 1.0, "duration",
                     state_class="total_increasing", precision=2, icon="mdi:timer-outline"),
        "310": Param("start_count", "Number of starts", None, 1.0, None,
                     state_class="total_increasing", precision=0, icon="mdi:restart"),
        # Generator - electrical
        "7": Param("frequency", "Generator frequency", "Hz", 1.0, "frequency", precision=1, icon="mdi:sine-wave"),
        "8": Param("voltage_l1n", "Generator voltage L1-N", "V", 1.0, "voltage", precision=1),
        "9": Param("voltage_l2n", "Generator voltage L2-N", "V", 1.0, "voltage", precision=1),
        "10": Param("voltage_l3n", "Generator voltage L3-N", "V", 1.0, "voltage", precision=1),
        "11": Param("voltage_l1l2", "Generator voltage L1-L2", "V", 1.0, "voltage", precision=1),
        "12": Param("voltage_l2l3", "Generator voltage L2-L3", "V", 1.0, "voltage", precision=1),
        "13": Param("voltage_l3l1", "Generator voltage L3-L1", "V", 1.0, "voltage", precision=1),
        "14": Param("current_l1", "Generator current L1", "A", 1.0, "current", precision=1),
        "15": Param("current_l2", "Generator current L2", "A", 1.0, "current", precision=1),
        "16": Param("current_l3", "Generator current L3", "A", 1.0, "current", precision=1),
        "17": Param("earth_current", "Generator earth current", "A", 1.0, "current",
                    precision=1, entity_category="diagnostic"),
        "18": Param("power_l1", "Generator power L1", "kW", 1.0, "power", precision=2),
        "19": Param("power_l2", "Generator power L2", "kW", 1.0, "power", precision=2),
        "20": Param("power_l3", "Generator power L3", "kW", 1.0, "power", precision=2),
        "226": Param("power_total", "Generator power", "kW", 1.0, "power", precision=2, icon="mdi:flash"),
        "227": Param("va_l1", "Generator apparent power L1", "kVA", 1.0, "apparent_power", precision=2),
        "228": Param("va_l2", "Generator apparent power L2", "kVA", 1.0, "apparent_power", precision=2),
        "229": Param("va_l3", "Generator apparent power L3", "kVA", 1.0, "apparent_power", precision=2),
        "230": Param("va_total", "Generator apparent power", "kVA", 1.0, "apparent_power", precision=2),
        "231": Param("var_l1", "Generator reactive power L1", "kvar", 1.0, "reactive_power", precision=2),
        "232": Param("var_l2", "Generator reactive power L2", "kvar", 1.0, "reactive_power", precision=2),
        "233": Param("var_l3", "Generator reactive power L3", "kvar", 1.0, "reactive_power", precision=2),
        "234": Param("var_total", "Generator reactive power", "kvar", 1.0, "reactive_power", precision=2),
        "235": Param("pf_l1", "Generator power factor L1", None, 1.0, "power_factor", precision=2),
        "236": Param("pf_l2", "Generator power factor L2", None, 1.0, "power_factor", precision=2),
        "237": Param("pf_l3", "Generator power factor L3", None, 1.0, "power_factor", precision=2),
        "238": Param("power_factor", "Generator power factor", None, 1.0, "power_factor", precision=2),
        "21": Param("gen_lag_lead", "Generator current lag/lead", None, 1.0, None,
                    precision=2, entity_category="diagnostic"),
        "315": Param("fuel_volume", "Fuel level (volume)", None, 1.0, None,
                     precision=1, icon="mdi:fuel"),
        "316": Param("fuel_units", "Fuel level units", None, 1.0, None,
                     state_class=None, precision=0, entity_category="diagnostic"),
        "317": Param("load_unbalance", "Load unbalance", "%", 1.0, None,
                     precision=1, entity_category="diagnostic"),
        # Generator - energy counters. These belong in the Energy Dashboard.
        "306": Param("energy_total", "Generator energy", "kWh", 1.0, "energy",
                     state_class="total_increasing", precision=2),
        "308": Param("energy_kvah", "Generator apparent energy", "kVAh", 1.0, None,
                     state_class="total_increasing", precision=2, entity_category="diagnostic"),
        "309": Param("energy_kvarh", "Generator reactive energy", "kvarh", 1.0, None,
                     state_class="total_increasing", precision=2, entity_category="diagnostic"),
        # Mains
        "22": Param("mains_frequency", "Mains frequency", "Hz", 1.0, "frequency", precision=1, icon="mdi:sine-wave"),
        "23": Param("mains_voltage_l1n", "Mains voltage L1-N", "V", 1.0, "voltage", precision=1),
        "24": Param("mains_voltage_l2n", "Mains voltage L2-N", "V", 1.0, "voltage", precision=1),
        "25": Param("mains_voltage_l3n", "Mains voltage L3-N", "V", 1.0, "voltage", precision=1),
        "26": Param("mains_voltage_l1l2", "Mains voltage L1-L2", "V", 1.0, "voltage", precision=1),
        "27": Param("mains_voltage_l2l3", "Mains voltage L2-L3", "V", 1.0, "voltage", precision=1),
        "28": Param("mains_voltage_l3l1", "Mains voltage L3-L1", "V", 1.0, "voltage", precision=1),
        "29": Param("mains_current_l1", "Mains current L1", "A", 1.0, "current", precision=1),
        "30": Param("mains_current_l2", "Mains current L2", "A", 1.0, "current", precision=1),
        "31": Param("mains_current_l3", "Mains current L3", "A", 1.0, "current", precision=1),
        "32": Param("mains_power_l1", "Mains power L1", "kW", 1.0, "power", precision=2),
        "33": Param("mains_power_l2", "Mains power L2", "kW", 1.0, "power", precision=2),
        "34": Param("mains_power_l3", "Mains power L3", "kW", 1.0, "power", precision=2),
        "36": Param("mains_power_total", "Mains power", "kW", 1.0, "power", precision=2),
        "37": Param("mains_va_l1", "Mains apparent power L1", "kVA", 1.0, "apparent_power", precision=2),
        "38": Param("mains_va_l2", "Mains apparent power L2", "kVA", 1.0, "apparent_power", precision=2),
        "39": Param("mains_va_l3", "Mains apparent power L3", "kVA", 1.0, "apparent_power", precision=2),
        "40": Param("mains_va_total", "Mains apparent power", "kVA", 1.0, "apparent_power", precision=2),
        "41": Param("mains_var_l1", "Mains reactive power L1", "kvar", 1.0, "reactive_power", precision=2),
        "42": Param("mains_var_l2", "Mains reactive power L2", "kvar", 1.0, "reactive_power", precision=2),
        "43": Param("mains_var_l3", "Mains reactive power L3", "kvar", 1.0, "reactive_power", precision=2),
        "44": Param("mains_var_total", "Mains reactive power", "kvar", 1.0, "reactive_power", precision=2),
        "45": Param("mains_pf_l1", "Mains power factor L1", None, 1.0, "power_factor", precision=2),
        "46": Param("mains_pf_l2", "Mains power factor L2", None, 1.0, "power_factor", precision=2),
        "47": Param("mains_pf_l3", "Mains power factor L3", None, 1.0, "power_factor", precision=2),
        "51": Param("mains_power_factor", "Mains power factor", None, 1.0, "power_factor", precision=2),
        # Maintenance timers
        "450": Param("maint_1_remaining", "Maintenance 1 remaining", "s", 1.0, "duration",
                     precision=0, entity_category="diagnostic", icon="mdi:wrench-clock"),
        "451": Param("maint_1_due", "Maintenance 1 due", "s", 1.0, None,
                     state_class=None, precision=0, entity_category="diagnostic", icon="mdi:calendar-clock"),
        "452": Param("maint_2_remaining", "Maintenance 2 remaining", "s", 1.0, "duration",
                     precision=0, entity_category="diagnostic", icon="mdi:wrench-clock"),
        "453": Param("maint_2_due", "Maintenance 2 due", "s", 1.0, None,
                     state_class=None, precision=0, entity_category="diagnostic", icon="mdi:calendar-clock"),
        "454": Param("maint_3_remaining", "Maintenance 3 remaining", "s", 1.0, "duration",
                     precision=0, entity_category="diagnostic", icon="mdi:wrench-clock"),
        "455": Param("maint_3_due", "Maintenance 3 due", "s", 1.0, None,
                     state_class=None, precision=0, entity_category="diagnostic", icon="mdi:calendar-clock"),
    },
    # ── 129 / 132 / gateway data blocks: not decoded yet ──────────────────
    # 129 is very likely an alarm/status bitfield, data5/data8 are gateway
    # level values (link state, signal quality on cellular gateways).
    "129": {},
    # If group 132 turns out to be GenComm "Page 7 – Accumulated Instrumentation"
    # the instrument order is below. Enable once the raw values confirm it —
    # engine hours and the kWh counters are the valuable ones, and the counters
    # belong in the Home Assistant Energy Dashboard.
    #
    #   "3": Param("engine_hours", "Engine hours", "h", 1/3600.0, "duration",
    #              state_class="total_increasing", precision=1, bits=32, icon="mdi:timer-outline"),
    #   "4": Param("energy_exported", "Generator energy", "kWh", 0.1, "energy",
    #              state_class="total_increasing", precision=1, bits=32),
    #   "8": Param("start_count", "Number of starts", None, 1.0, None,
    #              state_class="total_increasing", precision=0, bits=32, icon="mdi:restart"),
    #
    "132": {},
    "data5": {},
    "data3": {
        "latitude": Param("gw_latitude", "Gateway latitude", "°", 1.0, None,
                          precision=4, entity_category="diagnostic", icon="mdi:latitude"),
        "longitude": Param("gw_longitude", "Gateway longitude", "°", 1.0, None,
                           precision=4, entity_category="diagnostic", icon="mdi:longitude"),
    },
    # Gateway-level telemetry. Observed keys of data8: gsmType, signalStrength,
    # rssi, rsrq, sinr, realtimeResponse, gatewayUptime, ethernet.
    "data8": {
        "signalStrength": Param("gw_signal_strength", "Gateway signal strength", "%", 1.0, None,
                                precision=0, entity_category="diagnostic", icon="mdi:signal"),
        "rssi": Param("gw_rssi", "Gateway RSSI", "dBm", 1.0, "signal_strength",
                      precision=0, signed=True, entity_category="diagnostic"),
        "rsrq": Param("gw_rsrq", "Gateway RSRQ", "dB", 1.0, None,
                      precision=0, signed=True, entity_category="diagnostic"),
        "sinr": Param("gw_sinr", "Gateway SINR", "dB", 1.0, None,
                      precision=0, signed=True, entity_category="diagnostic"),
        "gatewayUptime": Param("gw_uptime", "Gateway uptime", "s", 1.0, "duration",
                               state_class="total_increasing", precision=0,
                               entity_category="diagnostic", icon="mdi:timer-outline"),
        "gsmType": Param("gw_gsm_type", "Gateway GSM type", None, 1.0, None,
                         state_class=None, precision=0, entity_category="diagnostic"),
        "realtimeResponse": Param("gw_realtime_response", "Gateway realtime response", None, 1.0, None,
                                  state_class=None, precision=0, entity_category="diagnostic"),
        "ethernet": Param("gw_ethernet", "Gateway on Ethernet", kind="text",
                          entity_category="diagnostic", icon="mdi:ethernet"),
    },
}


# ── GenComm "Page 154 – Named Alarm Conditions" ───────────────────────────
# Four alarms per 16-bit register, most significant nibble first. The nibble
# value is the alarm condition, not a severity bit.
ALARM_NAMES = [
    "Emergency stop", "Low oil pressure", "High coolant temperature", "Low coolant temperature",
    "Under speed", "Over speed", "Generator under frequency", "Generator over frequency",
    "Generator low voltage", "Generator high voltage", "Battery low voltage", "Battery high voltage",
    "Charge alternator failure", "Fail to start", "Fail to stop", "Generator fail to close",
    "Mains fail to close", "Oil pressure sender fault", "Loss of magnetic pick up",
    "Magnetic pick up open circuit",
    "Generator high current", "Calibration lost", "Low fuel level", "CAN ECU warning",
    "CAN ECU shutdown", "CAN ECU data fail", "Low oil level switch", "High temperature switch",
    "Low fuel level switch", "Expansion unit watchdog alarm", "kW overload alarm", None,
    None, None, None, "Maintenance alarm",
    "Loading frequency alarm", "Loading voltage alarm", None, None,
    None, None, None, None,
    None, None, "ECU protect", "ECU malfunction",
    "ECU information", "ECU shutdown", "ECU warning", "ECU HEST",
    None, "ECU water in fuel", None, None,
    None, "High fuel level", "DEF level low", "SCR inducement",
]

# Nibble value -> severity. Codes not listed mean "nothing to report".
ALARM_SEVERITY = {2: "warning", 3: "shutdown", 4: "electrical trip", 10: "indication"}
ALARM_SEVERITY_RANK = {"indication": 0, "warning": 1, "alarm": 2, "electrical trip": 2, "shutdown": 3}


def _decode_alarm_objects(regs: dict) -> list[tuple[str, str]] | None:
    """Decode the object form of group 129.

    Each entry looks like {"severityID": n, "timestamp": t, "string": "..."}.
    An entry with severityID 0 and an empty string is an unused alarm slot.
    Returns None when the payload is not in this form.
    """
    active: list[tuple[str, str]] = []
    matched = False
    for raw in regs.values():
        if not isinstance(raw, dict) or "severityID" not in raw:
            continue
        matched = True
        sev_id = raw.get("severityID") or 0
        text = (raw.get("string") or "").strip()
        if not sev_id and not text:
            continue
        severity = ALARM_SEVERITY.get(sev_id, "alarm" if sev_id else "indication")
        active.append((text or f"Alarm severity {sev_id}", severity))
    return active if matched else None


def _decode_alarm_registers(regs: dict) -> list[tuple[str, str]] | None:
    """Decode page-154 style packed registers into [(name, severity), ...].

    Returns None when the payload does not look like packed alarm registers,
    so an unexpected shape falls through to the raw exposure path instead of
    inventing alarms.
    """
    active: list[tuple[str, str]] = []
    seen_any = False
    for key, raw in regs.items():
        if not str(key).isdigit() or isinstance(raw, bool) or not isinstance(raw, int):
            return None
        if raw < 0 or raw > 0xFFFF:
            return None
        idx = int(key)
        if idx == 0:
            continue                       # register 0 is the alarm count
        seen_any = True
        base = (idx - 1) * 4
        for nib in range(4):
            code = (raw >> (12 - nib * 4)) & 0xF
            pos = base + nib
            severity = ALARM_SEVERITY.get(code)
            if severity is None:
                continue
            name = ALARM_NAMES[pos] if pos < len(ALARM_NAMES) else None
            active.append((name or f"Alarm {pos}", severity))
    return active if seen_any else None


# Derived binary sensors: field -> (name, device_class, icon, source field,
#                                   on-substrings, off-substrings)
BINARY_RULES = [
    ("engine_running", "Engine running", "running", "mdi:engine", "engine_state",
     ("running", "loaded", "crank", "warming", "cooling", "run on"),
     ("not running", "stopped", "at rest", "stop", "off")),
    ("mains_ok", "Mains available", "power", "mdi:transmission-tower", "mains_state",
     ("available", "healthy", "present", "ok", "normal"),
     ("fail", "not available", "absent", "unhealthy", "out of limits", "low", "high")),
    ("load_on_generator", "Load on generator", None, "mdi:power-plug", "load_state",
     ("generator", "gen "),
     ("mains", "off load", "no load", "open", "not on load")),
]

# Control command IDs
# GenComm "Page 16 – Control Registers" system control keys.
CMD = {
    "stop": 35700,
    "auto": 35701,
    "manual": 35702,
    "test": 35703,
    "start": 35705,
    "mute": 35706,
    "reset_alarms": 35707,
    "remote_start": 35732,     # telemetry start, controller stays in Auto
    "remote_stop": 35733,      # cancel telemetry start
    "reset_mains_failure": 35710,
    # Accepted on the command topic but deliberately given no button, because
    # these drive the contactors or change the operating mode outright.
    "auto_manual_restore": 35704,
    "transfer_generator": 35708,
    "transfer_mains": 35709,
    "off": 35776,
    "lamp_test": 35780,
}
# Note: "Reset alarms" exists twice in the standard — function 7 (35707) and
# function 34 (35734). Not every module implements both; if 35707 has no
# effect on this controller, try 35734 here.
# GenComm page 16 registers 0-7 expose which control functions the module
# actually supports, but DSEWebNet does not surface that page, so unsupported
# keys simply do nothing rather than reporting an error.
MODE_OPTIONS = ["Stop", "Manual", "Auto", "Test"]

# Buttons published when allow_control is enabled, and their labels.
BUTTONS = {
    "start": ("Start", "mdi:play"),
    "stop": ("Stop", "mdi:stop"),
    "manual": ("Manual", "mdi:hand-back-right"),
    "auto": ("Auto", "mdi:autorenew"),
    "remote_start": ("Remote start", "mdi:transmission-tower-export"),
    "remote_stop": ("Cancel remote start", "mdi:transmission-tower-off"),
    "mute": ("Mute alarm", "mdi:volume-off"),
    "reset_alarms": ("Reset alarms", "mdi:alarm-light-off"),
    "reset_mains_failure": ("Reset mains failure", "mdi:transmission-tower"),
}


def _default_subscription() -> dict:
    gw = GATEWAY_ID or "*"
    mod = MODULE_ID or "*"
    # 65535 is not a wildcard. Groups 129 and the gateway data blocks answer to
    # it with a single composite object; groups 130/131/132 need explicit index
    # lists. Asking 130/131 for 65535 returns nothing at all.
    modules = {
        "129": [65535] + list(range(0, 32)),
        "130": list(range(0, 16)),
        # Explicit catalogue IDs. They are sparse and reach past 1000, so a
        # contiguous range cannot cover them.
        # IDs 500-516 are the Tier 4 aftertreatment and CAN lamp block (DPF,
        # DEF, SCR). A 4510/4520 has none of it and answers "####" to every
        # one of them, so they are left out rather than published as 17
        # permanently unknown entities.
        "131": sorted({int(k) for k in PARAMS["131"]} | set(range(0, 64))),
        "132": [65535] + list(range(0, 16)),
    }
    modules["133"] = [65535] + list(range(0, 16))   # digital inputs
    modules["134"] = [65535] + list(range(0, 16))   # digital outputs
    modules["136"] = [65535] + list(range(0, 16))
    # data3 is the gateway's GPS fix. data7 is a rolling firmware debug log —
    # a long array of strings that is of no use as an entity, so it is not
    # requested and is skipped if it arrives anyway.
    data = {"3": [65535], "5": [65535], "8": [65535]}

    if PROBE_GROUPS:
        # Groups 129-132 came from one page of the DSEWebNet interface and carry
        # only instantaneous values. Engine hours are visible on the Engine tab
        # of that same interface, so another group holds the accumulated
        # instrumentation. Sweep the neighbourhood to find it: a group that does
        # not exist simply returns nothing.
        for g in range(120, 145):
            modules.setdefault(str(g), [65535] + list(range(0, 16)))
        for d in range(0, 16):
            data.setdefault(str(d), [65535])

    return {"1": {gw: {"modules": {mod: modules}, "data": data}}}


def _load_subscription() -> dict:
    if SUBSCRIPTION_OVERRIDE:
        try:
            sub = json.loads(SUBSCRIPTION_OVERRIDE)
            log.warning("Using subscription_override from configuration")
            return sub
        except Exception as exc:
            log.error(f"subscription_override is not valid JSON ({exc}) - using default")
    return _default_subscription()


SUBSCRIPTION = _load_subscription()


# ── Runtime state ─────────────────────────────────────────────────────────
state: dict[str, object] = {}
for _grp in PARAMS.values():
    for _p in _grp.values():
        state[_p.field] = None
for _f, *_ in BINARY_RULES:
    state[_f] = None
state["mode"] = None
state["last_update"] = None
state["alarm_state"] = None
state["active_alarms"] = None
state["alarm_count"] = None
state["problem"] = None

_unknown_published: set[str] = set()
_first_value_logged: set[str] = set()
_io_published: set[str] = set()
_alarm_shape_logged: set[str] = set()
_alarm_attrs: dict = {}
_unit_overrides: dict[str, str | None] = {}
_dict_shape_logged: set[str] = set()

mqttc: mqtt.Client | None = None
_loop: asyncio.AbstractEventLoop | None = None
pending_cmd: asyncio.Queue = asyncio.Queue()
_refresh_now = asyncio.Event()
_stop_event: asyncio.Event | None = None

_ws_ok = False
_available: bool | None = None
_last_data_ts = 0.0

AVAIL_TOPIC = f"{MQTT_PREFIX}/availability"
STATE_TOPIC = f"{MQTT_PREFIX}/state"
CMD_TOPIC = f"{MQTT_PREFIX}/command"
ATTR_TOPIC = f"{MQTT_PREFIX}/alarms"
STALE_TIMEOUT = max(STALE_MIN, STALE_FACTOR * max(POLL_INTERVAL, 30))


# ── MQTT ──────────────────────────────────────────────────────────────────
async def _supervisor_mqtt() -> dict | None:
    """Ask the Supervisor for MQTT broker credentials (services: mqtt)."""
    token = os.getenv("SUPERVISOR_TOKEN")
    if not token:
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(
                "http://supervisor/services/mqtt",
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status != 200:
                    log.debug(f"Supervisor MQTT service returned {resp.status}")
                    return None
                payload = await resp.json()
                return payload.get("data") or None
    except Exception as exc:
        log.debug(f"Supervisor MQTT lookup failed: {exc}")
        return None


def mqtt_setup(host: str, port: int, user: str, password: str):
    global mqttc
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"dsewebnet-{DEVICE_KEY}")
    if user:
        client.username_pw_set(user, password)
    client.will_set(AVAIL_TOPIC, "offline", retain=True)
    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect
    client.on_message = _on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    mqttc = client
    log.info(f"MQTT broker {host}:{port} (user: {user or 'anonymous'})")
    client.connect_async(host, port, keepalive=60)
    client.loop_start()


def _on_connect(client, userdata, flags, reason_code, properties=None):
    if getattr(reason_code, "is_failure", False):
        log.error(f"MQTT connection refused: {reason_code}")
        return
    log.info("MQTT connected")
    client.subscribe(CMD_TOPIC)
    _publish_discovery(client)
    client.publish(AVAIL_TOPIC, "online" if _ws_ok else "offline", retain=True)
    _publish_state()


def _on_disconnect(client, userdata, flags=None, reason_code=None, properties=None):
    log.warning(f"MQTT disconnected ({reason_code}) - auto-reconnecting")


def _on_message(client, userdata, msg):
    raw = msg.payload.decode(errors="replace").strip()
    cmd = raw.lower()
    if not ALLOW_CONTROL:
        log.warning(f"Command '{raw}' ignored - allow_control is disabled")
        return
    if cmd in CMD:
        if _loop is not None:
            _loop.call_soon_threadsafe(pending_cmd.put_nowait, cmd)
        log.info(f"Command queued: {cmd}")
    else:
        log.warning(f"Unknown command: {raw}")


def _set_available(flag: bool, reason: str = ""):
    global _available
    if flag == _available:
        return
    _available = flag
    if mqttc is not None:
        mqttc.publish(AVAIL_TOPIC, "online" if flag else "offline", retain=True)
    log.info(f"Availability -> {'online' if flag else 'offline'}{f' ({reason})' if reason else ''}")


def _publish_state():
    if mqttc is None:
        return
    mqttc.publish(STATE_TOPIC, json.dumps(state, default=str), retain=True)


def _device() -> dict:
    return {
        "identifiers": [DEVICE_KEY],
        "name": DEVICE_NAME,
        "manufacturer": "Deep Sea Electronics",
        "model": CONTROLLER_MODEL,
        "sw_version": VERSION,
        "configuration_url": "https://www.dsewebnet.com",
    }


ORIGIN = {"name": "DSEWebNet Bridge", "sw_version": VERSION}


def _base_cfg(uid: str, name: str) -> dict:
    return {
        "unique_id": f"{DEVICE_KEY}_{uid}",
        # object_id keeps entity_ids readable (sensor.dse_generator_engine_state);
        # unique_id is unchanged from 1.0.x, so existing entities are preserved.
        "object_id": f"{_slug(DEVICE_NAME)}_{uid}",
        "name": name,
        "has_entity_name": True,
        "state_topic": STATE_TOPIC,
        "availability_topic": AVAIL_TOPIC,
        "device": _device(),
        "origin": ORIGIN,
    }


def _publish_cfg(client, component: str, uid: str, payload: dict | None):
    topic = f"{HASS_PREFIX}/{component}/{DEVICE_KEY}/{uid}/config"
    client.publish(topic, json.dumps(payload) if payload else "", retain=True)


def _sensor_cfg(p: Param) -> dict:
    cfg_ = _base_cfg(p.field, p.name)
    cfg_["value_template"] = (
        "{% set v = value_json." + p.field + " %}"
        "{{ v if v is not none else none }}"
    )
    if p.icon:
        cfg_["icon"] = p.icon
    if p.entity_category:
        cfg_["entity_category"] = p.entity_category
    if p.kind == "num":
        unit = _unit_overrides.get(p.field, p.unit)
        if unit:
            cfg_["unit_of_measurement"] = unit
        if p.device_class:
            cfg_["device_class"] = p.device_class
        cfg_["state_class"] = p.state_class or "measurement"
        if p.precision is not None:
            cfg_["suggested_display_precision"] = p.precision
    return cfg_


def _publish_discovery(client):
    count = 0
    for group in PARAMS.values():
        for p in group.values():
            _publish_cfg(client, "sensor", p.field, _sensor_cfg(p))
            count += 1

    # last update timestamp (diagnostic)
    last = _base_cfg("last_update", "Last update")
    last["value_template"] = "{{ value_json.last_update }}"
    last["device_class"] = "timestamp"
    last["entity_category"] = "diagnostic"
    last["icon"] = "mdi:clock-check-outline"
    _publish_cfg(client, "sensor", "last_update", last)
    count += 1

    for field, name, dev_class, icon, _src, _on, _off in BINARY_RULES:
        cfg_ = _base_cfg(field, name)
        cfg_["value_template"] = (
            "{% set v = value_json." + field + " %}"
            "{% if v is none %}None{% elif v %}ON{% else %}OFF{% endif %}"
        )
        cfg_["payload_on"] = "ON"
        cfg_["payload_off"] = "OFF"
        if dev_class:
            cfg_["device_class"] = dev_class
        if icon and not dev_class:
            cfg_["icon"] = icon
        _publish_cfg(client, "binary_sensor", field, cfg_)
        count += 1

    prob = _base_cfg("problem", "Problem")
    prob["value_template"] = (
        "{% set v = value_json.problem %}"
        "{% if v is none %}None{% elif v %}ON{% else %}OFF{% endif %}"
    )
    prob["payload_on"] = "ON"
    prob["payload_off"] = "OFF"
    prob["device_class"] = "problem"
    prob["json_attributes_topic"] = ATTR_TOPIC
    _publish_cfg(client, "binary_sensor", "problem", prob)

    astate = _base_cfg("alarm_state", "Alarm state")
    astate["value_template"] = "{% set v = value_json.alarm_state %}{{ v if v is not none else none }}"
    astate["icon"] = "mdi:alarm-light"
    astate["json_attributes_topic"] = ATTR_TOPIC
    _publish_cfg(client, "sensor", "alarm_state", astate)

    acount = _base_cfg("alarm_count", "Active alarm count")
    acount["value_template"] = "{% set v = value_json.alarm_count %}{{ v if v is not none else none }}"
    acount["state_class"] = "measurement"
    acount["entity_category"] = "diagnostic"
    acount["icon"] = "mdi:counter"
    _publish_cfg(client, "sensor", "alarm_count", acount)
    count += 3

    for cmd_name, (label, bicon) in BUTTONS.items():
        if ALLOW_CONTROL:
            cfg_ = {
                "unique_id": f"{DEVICE_KEY}_btn_{cmd_name}",
                "object_id": f"{_slug(DEVICE_NAME)}_{cmd_name}",
                "name": label,
                "has_entity_name": True,
                "command_topic": CMD_TOPIC,
                "payload_press": cmd_name,
                "availability_topic": AVAIL_TOPIC,
                "device": _device(),
                "origin": ORIGIN,
                "icon": bicon,
            }
            _publish_cfg(client, "button", cmd_name, cfg_)
            count += 1
        else:
            _publish_cfg(client, "button", cmd_name, None)

    if ALLOW_CONTROL:
        sel = _base_cfg("mode", "Mode")
        sel["command_topic"] = CMD_TOPIC
        sel["value_template"] = "{% set v = value_json.mode %}{{ v if v is not none else none }}"
        sel["command_template"] = "{{ value | lower }}"
        sel["options"] = MODE_OPTIONS
        sel["icon"] = "mdi:state-machine"
        _publish_cfg(client, "select", "mode", sel)
        count += 1
    else:
        _publish_cfg(client, "select", "mode", None)

    # A parameter that used to be unidentified leaves a retained raw entity
    # behind once it gets a name. Clear those so the device page does not
    # accumulate duplicates of instruments that are now properly mapped.
    removed = 0
    for group, params in PARAMS.items():
        for sub in params:
            if not str(sub).isdigit():
                continue
            stale = _slug(f"p{group}_{sub}")
            _publish_cfg(client, "sensor", stale, None)
            removed += 1
    log.debug(f"Cleared {removed} stale raw entity configs")

    log.info(
        f"HASS discovery published: {count} entities "
        f"({'control enabled' if ALLOW_CONTROL else 'read-only'})"
    )


_UNIT_FIXUPS = {
    "Bar": "bar", "RPM": "rpm", "pf": None, "Deg C": "°C", "DegC": "°C",
    "kVAr": "kvar", "KVAr": "kvar", "VAr": "var",
    "kVArh": "kvarh", "KVArh": "kvarh",
    "Units": None, "units": None, "": None,
    "Seconds": "s", "Hours": "h", "Minutes": "min", "Hrs": "h",
    # Placeholder units the service uses when the real one depends on a
    # lookup elsewhere. Better unitless than labelled with all three options.
    "Litre/Imp Gal/US Gal": None, "Lookup": None, "Alarm3": None,
}

# Home Assistant validates the unit against the device class and drops the
# entity if they disagree, so a unit from the payload is only adopted when the
# device class accepts it. Device classes absent from this map take any unit.
_UNITS_FOR_DEVICE_CLASS = {
    "voltage": {"V", "mV", "kV", "MV"},
    "current": {"A", "mA", "kA"},
    "power": {"W", "kW", "MW", "GW", "TW"},
    "apparent_power": {"VA", "kVA", "MVA"},
    "reactive_power": {"var", "kvar", "Mvar"},
    "energy": {"Wh", "kWh", "MWh", "GWh", "TWh"},
    "frequency": {"Hz", "kHz", "MHz", "GHz"},
    "temperature": {"°C", "°F", "K"},
    "pressure": {"Pa", "hPa", "kPa", "bar", "cbar", "mbar", "mmHg", "inHg", "psi"},
    "duration": {"d", "h", "min", "s", "ms"},
    "power_factor": {"%", None},
    "signal_strength": {"dB", "dBm"},
}


def _adopt_unit(p: Param, raw):
    """The payload states the units for each instrument. Trust it over the
    built-in table and re-publish that one entity if they disagree."""
    if not isinstance(raw, dict) or p.kind != "num":
        return
    unit = raw.get("units")
    if not isinstance(unit, str):
        return
    unit = unit.strip()
    unit = _UNIT_FIXUPS.get(unit, unit)
    unit = unit or None
    current = _unit_overrides.get(p.field, p.unit)
    if unit == current:
        return
    allowed = _UNITS_FOR_DEVICE_CLASS.get(p.device_class) if p.device_class else None
    if allowed is not None and unit not in allowed:
        log.debug(f"{p.field}: ignoring reported units '{unit}' — not valid for "
                  f"device_class {p.device_class}, keeping '{current}'")
        return
    _unit_overrides[p.field] = unit
    if mqttc is not None:
        _publish_cfg(mqttc, "sensor", p.field, _sensor_cfg(p))
    log.info(f"{p.field}: controller reports units '{unit or '-'}', "
             f"table said '{current or '-'}' - entity updated")


def _publish_unknown_discovery(field: str, group: str, sub: str,
                               unit: str | None = None, numeric: bool = False):
    if mqttc is None or field in _unknown_published:
        return
    _unknown_published.add(field)
    cfg_ = _base_cfg(field, f"Raw {group}/{sub}")
    cfg_["value_template"] = "{% set v = value_json." + field + " %}{{ v if v is not none else none }}"
    cfg_["entity_category"] = "diagnostic"
    cfg_["icon"] = "mdi:help-circle-outline"
    if numeric:
        # The payload types these itself, so an unidentified parameter can still
        # be graphed and recorded instead of sitting there as a JSON blob.
        cfg_["state_class"] = "measurement"
        if unit:
            cfg_["unit_of_measurement"] = unit
    _publish_cfg(mqttc, "sensor", field, cfg_)
    log.info(f"Unknown parameter exposed: {group}/{sub} -> sensor {field}"
             + (f" [{unit}]" if unit else ""))


# ── Message parsing ───────────────────────────────────────────────────────
# GenComm sentinel values. DSE returns these instead of data when an instrument
# is over/under its measurable range, its transducer is faulty, the value is not
# measurable in the current state (power factor with no load) or the instrument
# is not implemented on this controller. They occupy the top of each integer
# range, so a plain read would store 65535 as a temperature.
# Per GenComm v2.272: unimplemented, over range, under range, transducer fault,
# bad data, high digital input, low digital input, reserved.
_SENTINEL_BANDS = (
    (0xFFF8, 0xFFFF),                    # 16-bit unsigned
    (0x7FF8, 0x7FFF),                    # 16-bit signed
    (0xFFFFFFF8, 0xFFFFFFFF),            # 32-bit unsigned
    (0x7FFFFFF8, 0x7FFFFFFF),            # 32-bit signed
)


def _is_display_sentinel(val) -> bool:
    """DSEWebNet renders unavailable instruments as '----' and out-of-range or
    not-currently-measurable ones as '####'."""
    if not isinstance(val, str):
        return False
    t = val.strip()
    return bool(t) and set(t) <= {"-", "#"}


def _is_sentinel(raw) -> bool:
    if not FILTER_SENTINELS or isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return False
    if raw != int(raw):
        return False
    v = int(raw)
    return any(lo <= v <= hi for lo, hi in _SENTINEL_BANDS)


def _extract(raw, p: Param | None):
    """Convert a raw DSEWebNet value into an engineering value.

    Instrument entries arrive as
        {"value": 13.3, "scalar": "0.1", "units": "V", "unitsID": 2,
         "rawValue": 133}
    where "value" is ALREADY converted and "rawValue" is the register content.
    So the value is taken as it stands — applying our own scale on top would
    divide it a second time. Param.scale only ever applies to a bare number.
    """
    if isinstance(raw, dict):
        val = raw.get("value")
        if _is_display_sentinel(val):
            return None
        for dp_key in ("dp", "decimalPlaces", "decimal_places"):
            if dp_key in raw:
                try:
                    return float(val) / (10 ** int(raw[dp_key]))
                except (TypeError, ValueError):
                    break
        if p is not None and p.kind == "num":
            try:
                return float(val)
            except (TypeError, ValueError):
                return None
        return val

    if _is_display_sentinel(raw):
        return None

    if p is not None and p.kind == "num":
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return None
        if p.signed and val >= 0 and val == int(val):
            iv = int(val)
            width = p.bits if iv < (1 << p.bits) else 32
            if iv < (1 << width) and iv >= (1 << (width - 1)):
                iv -= 1 << width
            val = float(iv)
        return val * p.scale
    return raw


def _tristate(text, on_words, off_words):
    if not text or not isinstance(text, str):
        return None
    t = text.lower()
    for w in off_words:
        if w in t:
            return False
    for w in on_words:
        if w in t:
            return True
    return None


def _derive():
    for field, _name, _dc, _icon, src, on_words, off_words in BINARY_RULES:
        state[field] = _tristate(state.get(src), on_words, off_words)

    mode_text = (state.get("mode_state") or "")
    mode_text = mode_text.lower() if isinstance(mode_text, str) else ""
    if "auto" in mode_text:
        state["mode"] = "Auto"
    elif "manual" in mode_text:
        state["mode"] = "Manual"
    elif "test" in mode_text:
        state["mode"] = "Test"
    elif "stop" in mode_text:
        state["mode"] = "Stop"
    else:
        state["mode"] = None


def _expand(group: str, sub: str, raw):
    """Yield (group, key, value). Composite objects are flattened.

    DSEWebNet delivers some entries as objects rather than scalars. An object
    carrying a "value" member is a single instrument and stays as one entry;
    anything else is a bundle of named members and becomes one entry per member,
    keyed "<sub>.<member>" so a parameter can be mapped by either the full key
    or the bare member name.
    """
    if isinstance(raw, dict) and "value" not in raw:
        for k, v in raw.items():
            yield group, f"{sub}.{k}", v
    else:
        yield group, sub, raw


def _iter_updates(payload2: dict):
    """Yield (group, sub_key, raw_value) for the configured gateway/module."""
    for gw_id, gw_val in payload2.items():
        if not isinstance(gw_val, dict):
            continue
        if GATEWAY_ID and gw_id not in ("*", GATEWAY_ID):
            log.debug(f"Skipping data from foreign gateway {gw_id}")
            continue

        modules = gw_val.get("modules")
        if isinstance(modules, dict):
            for mod_id, mod_val in modules.items():
                if MODULE_ID and mod_id not in ("*", MODULE_ID):
                    log.debug(f"Skipping data from foreign module {mod_id}")
                    continue
                if not isinstance(mod_val, dict):
                    continue
                for group, subs in mod_val.items():
                    if isinstance(subs, dict):
                        for sub, raw in subs.items():
                            yield from _expand(str(group), str(sub), raw)
                    else:
                        yield from _expand(str(group), "0", subs)

        data = gw_val.get("data")
        if isinstance(data, dict):
            for group, subs in data.items():
                if isinstance(subs, dict):
                    for sub, raw in subs.items():
                        yield from _expand(f"data{group}", str(sub), raw)
                else:
                    yield from _expand(f"data{group}", "0", subs)



# Groups 133 and 134 describe the controller's digital inputs and outputs. Each
# entry is {"active": bool, "defaultState": bool, "string": "...",
# "shortName": "A"} — the controller names its own terminals, so the entities
# are built from the payload rather than from a table.
IO_GROUPS = {"133": ("input", "Input", "mdi:import"),
             "134": ("output", "Output", "mdi:export")}


def _handle_io(group: str, block: dict) -> bool | None:
    """Publish digital inputs/outputs. None if the payload is not that shape."""
    entries = []
    for sub, raw in block.items():
        if not isinstance(raw, dict) or "active" not in raw:
            return None
        short = str(raw.get("shortName") or sub).strip()
        label = str(raw.get("string") or "").strip()
        entries.append((short, label, bool(raw.get("active"))))
    if not entries:
        return None

    prefix, kind, icon = IO_GROUPS[group]
    changed = False
    for short, label, active in entries:
        field = _slug(f"{prefix}_{short}")
        if field not in _io_published:
            _io_published.add(field)
            cfg_ = _base_cfg(field, label or f"{kind} {short}")
            cfg_["value_template"] = (
                "{% set v = value_json." + field + " %}"
                "{% if v is none %}None{% elif v %}ON{% else %}OFF{% endif %}"
            )
            cfg_["payload_on"] = "ON"
            cfg_["payload_off"] = "OFF"
            cfg_["icon"] = icon
            if mqttc is not None:
                _publish_cfg(mqttc, "binary_sensor", field, cfg_)
            log.info(f"{kind} {short} discovered: {label or '(unnamed)'}")
        if state.get(field) != active:
            changed = True
        state[field] = active
    return changed


def _collect_group(payload2: dict, group: str) -> dict | None:
    """Return the raw sub-key dict of one group for the configured module."""
    for gw_id, gw_val in payload2.items():
        if not isinstance(gw_val, dict):
            continue
        if GATEWAY_ID and gw_id not in ("*", GATEWAY_ID):
            continue
        for mod_id, mod_val in (gw_val.get("modules") or {}).items():
            if MODULE_ID and mod_id not in ("*", MODULE_ID):
                continue
            if isinstance(mod_val, dict) and isinstance(mod_val.get(group), dict):
                return mod_val[group]
    return None


def _apply_alarms(active: list[tuple[str, str]]) -> bool:
    """Update alarm state fields. Returns True when anything changed."""
    global _alarm_attrs
    worst = None
    for _name, sev in active:
        if worst is None or ALARM_SEVERITY_RANK.get(sev, 0) > ALARM_SEVERITY_RANK.get(worst, 0):
            worst = sev

    if not active:
        summary = "OK"
    else:
        summary = worst.capitalize()

    problem = bool(active) and worst in ("warning", "alarm", "shutdown", "electrical trip")
    listing = ", ".join(f"{n} ({s})" for n, s in active)

    changed = (
        state.get("alarm_state") != summary
        or state.get("alarm_count") != len(active)
        or state.get("problem") != problem
        or state.get("active_alarms") != listing
    )
    state["alarm_state"] = summary
    state["alarm_count"] = len(active)
    state["problem"] = problem
    state["active_alarms"] = listing[:250]

    if changed:
        _alarm_attrs = {
            "active_alarms": [{"name": n, "severity": s} for n, s in active],
            "shutdowns": [n for n, s in active if s == "shutdown"],
            "electrical_trips": [n for n, s in active if s == "electrical trip"],
            "warnings": [n for n, s in active if s == "warning"],
        }
        if mqttc is not None:
            mqttc.publish(ATTR_TOPIC, json.dumps(_alarm_attrs, ensure_ascii=False), retain=True)
        if active:
            log.info(f"Alarms active ({summary}): {listing}")
        else:
            log.info("Alarms cleared")
    return changed


def _slug(text: str) -> str:
    return re.sub(r"\W+", "_", text).strip("_").lower()


def _handle_ws_message(raw: str):
    global _last_data_ts
    try:
        msg = json.loads(raw)
    except Exception:
        log.debug(f"WS non-JSON frame: {raw[:200]}")
        return

    if DEBUG_RAW:
        log.debug(f"RAW {json.dumps(msg, ensure_ascii=False)[:4000]}")

    if "2" not in msg:
        log.debug(f"WS frame without data block: keys={list(msg)[:10]}")
        return

    changed = False

    # Group 129 carries the alarm block. Try the documented page-154 packed
    # format first; anything else falls through to the normal path.
    alarm_block = _collect_group(msg["2"], "129")
    if alarm_block:
        decoded = _decode_alarm_objects(alarm_block)
        if decoded is None:
            decoded = _decode_alarm_registers(alarm_block)
        if decoded is not None:
            if _apply_alarms(decoded):
                changed = True
            alarm_handled = True
        else:
            alarm_handled = False
            if "129" not in _alarm_shape_logged:
                _alarm_shape_logged.add("129")
                log.info(f"Group 129 is not packed alarm registers: "
                         f"{json.dumps(alarm_block, ensure_ascii=False)[:300]}")
    else:
        alarm_handled = False

    io_handled = set()
    for io_group in IO_GROUPS:
        block = _collect_group(msg["2"], io_group)
        if block:
            result = _handle_io(io_group, block)
            if result is not None:
                io_handled.add(io_group)
                changed = changed or result

    for group, sub, raw_val in _iter_updates(msg["2"]):
        if group == "129" and alarm_handled:
            continue
        if group in io_handled:
            continue
        if group == "data7":
            continue
        pgroup = PARAMS.get(group, {})
        p = pgroup.get(sub)
        if p is None and "." in sub:
            p = pgroup.get(sub.split(".", 1)[1])

        if isinstance(raw_val, dict):
            shape_key = f"{group}/{sub}"
            if shape_key not in _dict_shape_logged:
                _dict_shape_logged.add(shape_key)
                log.debug(f"Object-form value {shape_key}: {json.dumps(raw_val)[:200]}")

        if p is not None:
            if _is_sentinel(raw_val):
                # Out of range / sensor fault / not measurable / not fitted.
                if state.get(p.field) is not None:
                    changed = True
                    log.debug(f"{group}/{sub} ({p.field}) sentinel {raw_val} -> unknown")
                state[p.field] = None
                continue

            val = _extract(raw_val, p)
            if p.kind == "num" and val is not None:
                try:
                    val = round(float(val), p.precision if p.precision is not None else 3)
                except (TypeError, ValueError):
                    val = None
            _adopt_unit(p, raw_val)
            if p.field not in _first_value_logged:
                _first_value_logged.add(p.field)
                unit = _unit_overrides.get(p.field, p.unit)
                how = "converted by DSEWebNet" if isinstance(raw_val, dict) else f"scale {p.scale}"
                log.info(f"first value {group}/{sub} {p.field}: raw={raw_val} -> "
                         f"{val}{' ' + unit if unit else ''}  ({how})")
            if state.get(p.field) != val:
                changed = True
            state[p.field] = val
        elif EXPOSE_UNKNOWN:
            field = _slug(f"p{group}_{sub}")
            val = raw_val
            unit = None
            numeric = False
            if isinstance(raw_val, dict) and "value" in raw_val:
                v = raw_val.get("value")
                val = None if _is_display_sentinel(v) else v
                numeric = isinstance(val, (int, float)) or val is None
                u = raw_val.get("units")
                if isinstance(u, str) and u.strip():
                    unit = _UNIT_FIXUPS.get(u.strip(), u.strip())
            elif isinstance(val, (int, float)) and not isinstance(val, bool):
                numeric = True
            elif isinstance(val, (dict, list)):
                val = json.dumps(val, ensure_ascii=False)[:250]
            _publish_unknown_discovery(field, group, sub, unit, numeric)
            if field not in _first_value_logged:
                _first_value_logged.add(field)
                log.info(f"first value {group}/{sub} (unmapped): {json.dumps(raw_val, ensure_ascii=False)[:300]}")
            if state.get(field) != val:
                changed = True
            state[field] = val

    _last_data_ts = time.time()
    if not _ws_ok:
        return

    if changed:
        _derive()
        state["last_update"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _publish_state()
        log.info(
            f"engine={state.get('engine_state')} mode={state.get('mode_state')} "
            f"mains={state.get('mains_state')} oil={state.get('oil_pressure')} "
            f"L1={state.get('voltage_l1n')}V f={state.get('frequency')}Hz"
        )


# ── DSEWebNet session ─────────────────────────────────────────────────────
async def _login(session: aiohttp.ClientSession) -> bool:
    try:
        async with session.get(DSE_LOGIN_URL) as resp:
            html = await resp.text()
        csrf_id = re.search(r'name="login\[_csrfID\]"\s+value="([^"]+)"', html)
        csrf_key = re.search(r'name="login\[_csrfKey\]"\s+value="([^"]+)"', html)
        if not csrf_id or not csrf_key:
            log.debug("CSRF token not found on the login page - continuing anyway")
        data = {
            "login[username]": DSE_USERNAME,
            "login[password]": DSE_PASSWORD,
            "login[btnLogin]": "Login",
        }
        if csrf_id:
            data["login[_csrfID]"] = csrf_id.group(1)
        if csrf_key:
            data["login[_csrfKey]"] = csrf_key.group(1)

        async with session.post(DSE_LOGIN_URL, data=data, allow_redirects=True) as resp:
            body = await resp.text()
            cookies = session.cookie_jar.filter_cookies(yarl.URL("https://www.dsewebnet.com"))
            if "sessionKey" in cookies or "login" not in str(resp.url):
                log.info("Login OK")
                return True
            log.error(f"Login failed - HTTP {resp.status}, landed on {resp.url}")
            log.debug(f"Login response snippet: {body[:400]}")
            return False
    except Exception as exc:
        log.error(f"Login error: {exc}")
        return False


async def _send_cmd(ws, cmd_id: int, label: str = ""):
    await ws.send_str(json.dumps({"3": {GATEWAY_ID: {"modules": {MODULE_ID: [cmd_id]}}}}))
    log.info(f"-> {label or cmd_id}")


async def _session_tasks(ws):
    """Background tasks bound to one WebSocket session."""

    async def poller():
        while True:
            try:
                await asyncio.wait_for(_refresh_now.wait(), timeout=POLL_INTERVAL or 30)
                _refresh_now.clear()
            except asyncio.TimeoutError:
                pass
            if POLL_INTERVAL <= 0 and not _refresh_now.is_set():
                continue
            await ws.send_str(json.dumps(SUBSCRIPTION))
            log.debug("Subscription refreshed")

    async def cmd_sender():
        while True:
            cmd = await pending_cmd.get()
            if not ALLOW_CONTROL:
                continue
            if cmd == "start":
                await _send_cmd(ws, CMD["manual"], "manual (pre-start)")
                await asyncio.sleep(1)
            await _send_cmd(ws, CMD[cmd], cmd)
            await asyncio.sleep(1.5)
            _refresh_now.set()          # pull fresh state instead of waiting a full poll cycle

    async def watchdog():
        while True:
            await asyncio.sleep(10)
            if _last_data_ts and time.time() - _last_data_ts > STALE_TIMEOUT:
                _set_available(False, f"no data for {STALE_TIMEOUT}s")
            elif _ws_ok and _last_data_ts:
                _set_available(True)

    return [
        asyncio.create_task(poller()),
        asyncio.create_task(cmd_sender()),
        asyncio.create_task(watchdog()),
    ]


async def ws_loop():
    global _ws_ok, _last_data_ts
    attempt = 0
    async with aiohttp.ClientSession() as session:
        while _stop_event is None or not _stop_event.is_set():
            try:
                if not await _login(session):
                    raise RuntimeError("login failed")

                async with session.ws_connect(
                    DSE_WS_URL,
                    heartbeat=WS_HEARTBEAT,
                    receive_timeout=WS_RECEIVE_TIMEOUT,
                ) as ws:
                    log.info("=" * 46)
                    log.info("NEW SESSION - WebSocket connected")
                    _ws_ok = True
                    _last_data_ts = time.time()
                    attempt = 0
                    await ws.send_str(json.dumps(SUBSCRIPTION))
                    _set_available(True, "session established")

                    tasks = await _session_tasks(ws)
                    try:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                _handle_ws_message(msg.data)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                              aiohttp.WSMsgType.CLOSING,
                                              aiohttp.WSMsgType.ERROR):
                                log.warning(f"WebSocket closed: {msg.type}")
                                break
                    finally:
                        for t in tasks:
                            t.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await asyncio.gather(*tasks, return_exceptions=True)

            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                log.warning(f"No WebSocket data for {WS_RECEIVE_TIMEOUT}s - reconnecting")
            except Exception as exc:
                log.error(f"Session error: {exc}")

            _ws_ok = False
            _set_available(False, "session lost")

            attempt += 1
            delay = min(RECONNECT_MAX, RECONNECT_BASE * (2 ** min(attempt - 1, 6)))
            delay += random.uniform(0, 3)
            log.info(f"Reconnecting in {delay:.0f}s (attempt {attempt})")
            try:
                if _stop_event is not None:
                    await asyncio.wait_for(_stop_event.wait(), timeout=delay)
                    return
                await asyncio.sleep(delay)
            except asyncio.TimeoutError:
                pass


# ── Entry point ───────────────────────────────────────────────────────────
def _validate() -> bool:
    ok = True
    if not DSE_USERNAME or not DSE_PASSWORD:
        log.error("dse_username / dse_password are not set")
        ok = False
    if not MODULE_ID:
        log.warning("module_id is empty - data from every module in the account "
                    "will be merged into one device and control is not possible")
    if not GATEWAY_ID:
        log.warning("gateway_id is empty - subscribing to every gateway in the account")
    if ALLOW_CONTROL and (not GATEWAY_ID or not MODULE_ID):
        log.error("allow_control requires both gateway_id and module_id - control disabled")
        return ok
    return ok


async def main():
    global _loop, _stop_event
    _loop = asyncio.get_running_loop()
    _stop_event = asyncio.Event()

    log.info(f"DSEWebNet Bridge {VERSION} starting")
    log.info(f"Gateway: {GATEWAY_ID or '(any)'}  Module: {MODULE_ID or '(any)'}")
    log.info(f"Base topic: {MQTT_PREFIX}  poll: {POLL_INTERVAL}s  "
             f"control: {ALLOW_CONTROL}  expose_unknown: {EXPOSE_UNKNOWN}  "
             f"filter_sentinels: {FILTER_SENTINELS}")

    if not _validate():
        log.error("Configuration is incomplete - stopping")
        return

    host, port = MQTT_HOST, MQTT_PORT
    user, password = MQTT_USER, MQTT_PASS
    if not host:
        svc = await _supervisor_mqtt()
        if svc:
            host = svc.get("host", "core-mosquitto")
            port = int(svc.get("port", 1883))
            user = svc.get("username", "")
            password = svc.get("password", "")
            log.info("MQTT settings taken from the Supervisor MQTT service")
        else:
            host = "core-mosquitto"
            log.warning("mqtt_host is empty and no Supervisor service found - "
                        "falling back to core-mosquitto")

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            _loop.add_signal_handler(sig, _stop_event.set)

    mqtt_setup(host, port, user, password)
    await asyncio.sleep(1)

    ws_task = asyncio.create_task(ws_loop())
    stop_task = asyncio.create_task(_stop_event.wait())
    try:
        await asyncio.wait({ws_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (ws_task, stop_task):
            t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(ws_task, stop_task, return_exceptions=True)
        log.info("Shutting down")
        if mqttc is not None:
            mqttc.publish(AVAIL_TOPIC, "offline", retain=True)
            time.sleep(0.3)
            mqttc.loop_stop()
            mqttc.disconnect()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
