# DSEWebNet Bridge

Connects a DSE generator to Home Assistant through the DSEWebNet cloud
WebSocket API and MQTT auto-discovery. Around 110 entities.

## Configuration

### `dse_username` / `dse_password`

Your login for [dsewebnet.com](https://www.dsewebnet.com) — the same email and
password used on the website.

### `gateway_id` / `module_id`

Both IDs are visible on the DSEWebNet page.

![DSEWebNet IDs location](https://raw.githubusercontent.com/dmdukr/hass-dsewebnet-bridge/main/docs/dsewebnet-ids.png)

- **Gateway ID** → top right: *"Connection made to ID **19XXXXXXXXXXX01** Using Ethernet"*
- **Module ID** → breadcrumb at the top, or the left panel: `USB ID:`

Both are required for control, and strongly recommended in any case: with
several sites on one account they keep foreign data out of your device.

### `mqtt_host`, `mqtt_port`, `mqtt_user`, `mqtt_pass`

**Leave all four empty** when using the Mosquitto broker add-on — the settings
are taken from the Supervisor automatically. Fill them in only for an external
broker.

### `mqtt_topic`

Base MQTT topic. Empty means `dse/<module_id>`.

### `poll_interval`

How often (seconds) the add-on re-sends its subscription. Default `30`. Data
also arrives as push updates, so this is only a fallback. `0` disables it.

### `allow_control`

`false` (default) — read-only. `true` publishes a mode select and nine buttons,
and requires both `gateway_id` and `module_id`.

> ⚠️ These entities start and stop a diesel engine.

### `expose_unknown`

Publishes any parameter that arrives but is not in the parameter table as a
diagnostic sensor named `p<group>_<id>`. Values carry the units the payload
states, so an unidentified instrument is still readable and recordable.

### `probe_groups`

Sweeps parameter groups 120-144 and gateway data blocks 0-15 looking for
anything not already subscribed. Off by default — on a DSE4520 everything the
service answers is already in the normal subscription.

### `filter_sentinels`

On by default. GenComm reserves the top of each integer range for "out of
range", "sensor fault", "not fitted" and similar, and DSEWebNet additionally
renders them as `----` and `####`. With this on they are published as unknown
rather than being recorded as 65535.

### `debug_raw` / `log_level`

`debug_raw: true` with `log_level: debug` dumps every WebSocket frame. Useful
for protocol work, noisy otherwise.

### `device_name` / `controller_model`

The device name in Home Assistant and the model shown on its page.

### `subscription_override`

Advanced. A raw JSON subscription message replacing the built-in one.

---

## Entities

**Engine** — engine hours, number of starts, engine speed, coolant and oil
temperature, oil pressure, fuel level in percent and in volume, battery and
charge alternator voltage.

**Generator** — frequency, six voltages, three currents, kW / kVA / kvar per
phase and total, power factor per phase and average.

**Mains** — the same set for the mains side.

**Energy** — kWh, kVAh and kvarh counters. The kWh counter carries
`device_class: energy` and `state_class: total_increasing`, so it can be added
to the Energy Dashboard directly.

**Status** — engine, mains, load, supervisor and mode as text; engine running,
mains available and load on generator as binary sensors.

**Alarms** — a `problem` binary sensor whose attributes carry the full active
alarm list split by severity, plus an alarm state sensor and an active count.

**Digital I/O** — inputs and outputs as binary sensors, named by the controller
itself. Renaming a function in the DSE configurator renames the entity.

**Diagnostic** — earth current, load unbalance, current lag/lead, three
maintenance countdowns and due timestamps, and the gateway's signal strength,
RSSI, RSRQ, SINR, uptime, GSM type, Ethernet flag and GPS position.

**Controls** (with `allow_control: true`) — mode select and nine buttons:
Start, Stop, Manual, Auto, Remote start, Cancel remote start, Mute alarm,
Reset alarms, Reset mains failure.

---

## Things worth knowing

**Unavailable instruments stay unknown.** A controller without an oil pressure
sender reports `----` forever, and mains power needs mains CTs to be fitted.
That is the controller, not the bridge.

**Prefer Remote start over Start** in automations. Start sends Manual → Start
and leaves the controller in Manual; Remote start requests a start while
leaving it in Auto, so a failed automation cannot disable the set.

**Watch output polarity.** An output configured De-Energise — as Close Mains
Output usually is — reads ON at rest, so trigger on the transition to OFF.

**The session drops about every 30 minutes** and reconnects within seconds. Use
`for: minutes: 1` on triggers.

---

## Adding parameters

Sub keys of group 131 are DSEWebNet instrument IDs, listed in the "Instrument"
dropdown of the chart series editor on the DSEWebNet site. Enable
`expose_unknown`, find the ID in the log, then add a line to `PARAMS["131"]`:

```python
"305": Param("engine_run_time", "Engine hours", "h", 1.0, "duration",
             state_class="total_increasing", precision=2, icon="mdi:timer-outline"),
```

Units come from the payload and win over the table whenever the device class
accepts them, so the unit column is only a fallback.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| All entities unavailable | Add-on log: login errors, MQTT connection, availability lines |
| Some sensors permanently unknown | Normal — that instrument is not fitted or not measurable now |
| Values frozen but available | Should not happen; the watchdog marks the device unavailable after ~2 min without data |
| Buttons missing | `allow_control` is off, or `gateway_id` / `module_id` is empty |
| A button does nothing | Not every control key is implemented by every module |
