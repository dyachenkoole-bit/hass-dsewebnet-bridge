# Changelog

## 2.1.1

Documentation release. README and DOCS rewritten for the current entity set —
the previous text still described the 13-sensor layout of 1.0.x. No code
changes beyond the version number.

Adds the notes that only came out of running against live hardware: which
readings stay unknown and why, why Remote start is preferable to Start in an
automation, how De-Energise output polarity inverts a binary sensor, and the
30-minute session drop that makes `for: minutes: 1` necessary on triggers.

## 2.1.0

Enabling control made the service answer with three groups it had not sent
before. They turned out to hold the Inputs and Outputs pages of the DSEWebNet
interface.

### Added
- **Digital inputs and outputs as binary sensors.** Groups 133 and 134 deliver
  `{"active": ..., "string": "...", "shortName": "A"}` per terminal, so the
  controller names its own terminals and the entities are built from the payload
  rather than from a table. On a DSE4520 that gives inputs A-D (Remote Start On
  Load, Coolant Temperature Switch, Digital Input C, Emergency Stop) and outputs
  A-F (Energise To Stop, Start Relay, Preheat, Close Mains Output, Close Gen
  Output, Audible Alarm). Close Mains Output reading ON while the load sits on
  the mains is a direct read of the contactor.
- **Gateway GPS position** from data block 3, as two diagnostic sensors.

### Changed
- Groups 133, 134 and 136 plus data block 3 are now part of the normal
  subscription instead of being found only by the group sweep.
- Data block 7 is a rolling firmware debug log — an array of GSM and CAN trace
  strings. It is not requested, and is skipped if it arrives anyway, so it never
  becomes an entity.

## 2.0.1

Corrections from the first live run of 2.0.0. Everything the catalogue promised
arrived: engine hours 54.35, energy 411.9 kWh, 420.9 kVAh, 57.6 kvarh, and a
start count of 86 — which the DSEWebNet web interface itself renders as "####".

### Fixed
- Time units. The service spells them "Seconds", "Hours" and "Minutes", none of
  which Home Assistant accepts, so they were being rejected and the table value
  kept. The maintenance countdowns are in seconds, not hours, and are now
  labelled as such.
- Placeholder units ("Litre/Imp Gal/US Gal", "Lookup", "Alarm3") are no longer
  adopted as unit strings.

### Added
- Fuel level in volume units (315), the fuel units selector (316) and the three
  maintenance due timestamps (451/453/455).

### Changed
- IDs 500-516 — the Tier 4 aftertreatment and CAN lamp block (DPF, DEF, SCR) —
  are no longer requested. A 4510/4520 has none of that hardware and answers
  "####" to every one, so they were only adding 17 permanently unknown entities.

## 2.0.0

The DSEWebNet chart series editor contains an "Instrument" dropdown listing
every instrument the service can serve, each with its numeric ID. **Those IDs
are the group 131 sub keys.** They are sparse and run past 1000, which is why
probing a contiguous range only ever found the low block.

### Added
- **Engine hours** (ID 305) and **number of starts** (310).
- **Energy counters**: generator kWh (306), kVAh (308) and kvarh (309), with
  `device_class: energy` and `state_class: total_increasing` — the kWh counter
  can go straight into the Home Assistant Energy Dashboard.
- **Generator totals** that were never subscribed to before: total kW (226),
  total kVA (230), total kvar (234) and average power factor (238).
- Per-phase generator kVA (227-229), kvar (231-233) and power factor (235-237).
- Load unbalance (317) and the three maintenance countdown timers (450/452/454).
- 86 sensors in total, up from 40.

### Fixed
- **Indices 32-51 are the mains side, not the generator.** The catalogue settles
  what a stopped engine could not: 32-34 and 36 are mains watts, 37-40 mains VA,
  41-44 mains kvar, 45-47 and 51 mains power factor. Naming them after the
  generator — the reading a load test might have suggested — would have been
  wrong. The generator equivalents live at 226-238.
- Unit adoption is now checked against the device class. The service reports
  units in its own spelling ("kVAr", "Units"), and Home Assistant drops an
  entity whose unit does not match its device class, so a reported unit is only
  taken when the device class accepts it.

### Changed
- `probe_groups` now defaults to off. It served its purpose; the instruments
  live in group 131 at high IDs, not in neighbouring groups.
- All scale factors are 1.0. DSEWebNet delivers converted values, so the scale
  column only ever applied to bare numbers, which this service does not send.

## 1.8.0

### Added
- **Group sweep** (`probe_groups`, on by default). Groups 129-132 all came from
  a single page of the DSEWebNet interface and carry only instantaneous values,
  but the Engine tab of that same interface displays engine hours — so another
  group holds the accumulated instrumentation. The subscription now also asks
  for groups 120-144 and gateway data blocks 0-15. A group that does not exist
  returns nothing, and the whole subscription is still only ~2 KB.

  Turn it off once the accumulated values have been located, or if the wider
  subscription upsets the server.

## 1.7.1

### Fixed
- Raw diagnostic entities are removed once their parameter gets a proper name.
  Identifying an instrument previously left the old `pNNN_NN` entity behind as a
  retained discovery message, so the device page collected duplicates of
  instruments that were already mapped.

## 1.7.0

### Added
- **Unidentified parameters are now typed.** When the payload carries a value
  and its units, the raw sensor is published as a proper numeric entity with
  that unit and `state_class: measurement`, instead of a JSON blob. An
  unidentified instrument can therefore be graphed and recorded while it is
  still being identified, and display sentinels read as unknown.

### Changed
- Mains currents L1/L2/L3 restored at 131/29-31. The payload labels them amps
  and there are exactly three of them immediately after the six mains voltages.
  This also settles a wider point: past index 28 DSEWebNet no longer follows the
  raw GenComm page 4 register order, so the units in the payload — not the
  standard — are what to trust from there on.
- Indices 32-51 are documented in the registry as kW, kVA, kvar and power factor
  blocks of four, almost certainly L1/L2/L3/total. They stay unnamed: with the
  engine stopped they all read "####" or -0.001, which cannot distinguish the
  generator side from the mains side.

## 1.6.1

### Added
- The first raw value of every unmapped parameter is now logged, not just the
  fact that it exists. Without this the probe added in 1.6.0 reports which
  indices are alive but gives nothing to identify them by.

### Changed
- Indices 131/29-31 are unmapped again. GenComm page 4 places mains voltage
  lag/lead and the two phase-rotation values there, while the payload labels
  them amps — the two disagree, and 1.5.0 picked the payload. With the engine
  stopped every candidate reads zero, so neither can be confirmed. They are left
  raw rather than carrying a name that may be wrong; the mains currents are
  32-bit and most likely sit around 33-35.

## 1.6.0

### Changed
- Widened the subscription index ranges: group 131 is probed to index 63,
  group 130 to 15, groups 129 and 132 to 31 and 15. Indices 0-31 of group 131
  are confirmed and mapped; beyond that this is a search for the accumulated
  instrumentation — engine hours, the kWh counters and the number of starts —
  which has not appeared in any group so far. Indices that do not exist return
  nothing, so the probe costs only the size of the subscription message.
- The "first value" log line no longer prints a scale factor for values that
  DSEWebNet already converted; it says so instead.

## 1.5.0

Adapts to the instrument format DSEWebNet actually sends, now that live data
from a DSE4520 MKII has been captured.

### Fixed
- **Double scaling.** Instruments arrive as
  `{"value": 13.3, "scalar": "0.1", "units": "V", "rawValue": 133}` — the
  `value` member is already converted. Applying the GenComm scale on top of it
  divided everything a second time, so battery voltage read 1.3 V instead of
  13.3 V and mains voltage 22.3 V instead of 223.1 V. The value is now taken as
  it stands; the scale table applies only to bare numbers.
- **Display sentinels.** DSEWebNet renders an unavailable instrument as `----`
  and an out-of-range or not-currently-measurable one as `####`. Both are now
  recognised and published as unknown.
- **Three mis-identified indices**, corrected from the units in the payload:
  131/21 is generator power factor, not current lag/lead, and 131/29-31 are the
  mains currents, not lag/lead and phase rotation.
- Oil pressure is bar and per-phase power is kW, as the controller reports them.

### Added
- **Units are taken from the controller.** Each instrument states its own units,
  so those win over the built-in table. When they disagree the affected entity
  is re-published with the correct unit and the change is logged, which means a
  differently configured controller corrects itself instead of needing a patch.
- Gateway realtime response as a diagnostic entity.

## 1.4.0

Fixes the reason every sensor stayed unknown on a live system, and adapts the
parser to the wire format DSEWebNet actually uses.

### Fixed
- **Subscription.** 65535 is not a wildcard. Groups 129 and the gateway data
  blocks answer to it with one composite object, but groups 130/131/132 need
  explicit index lists — asking them for 65535 returns nothing, which left every
  mapped sensor with no data. Explicit lists are back, widened to cover the
  instruments added in 1.2.0.
- **Composite objects.** Entries that arrive as an object without a `value`
  member are now flattened into one entry per member instead of being discarded.
- **Consistent scaling.** A value delivered as `{"value": n}` is now scaled
  exactly like a bare `n`. Previously the object form skipped scaling, so the
  same instrument read differently depending on which form arrived.

### Added
- **Alarm decoding for the object form.** Group 129 delivers entries shaped
  `{"severityID": n, "timestamp": t, "string": "..."}`, where an entry with
  severity 0 and an empty string is an unused slot. The packed-register decoder
  is kept as a fallback.
- **Gateway telemetry** from the data8 block: signal strength, RSSI, RSRQ, SINR,
  uptime, GSM type and Ethernet flag, all as diagnostic entities. On an Ethernet
  connected gateway the cellular values read zero, which is expected.

## 1.3.1

Cross-checked against DSE training documents 056-051 (Gencomm Control Keys) and
056-080 (Modbus). The control keys, sentinel behaviour and 0.1 scaling on
battery voltage already in the add-on all match; no corrections were needed.

### Added
- Reset mains failure button (key 35710) — useful on an AMF set after an outage.
- Additional control keys accepted on the command topic but deliberately given
  no button, since they drive contactors or change mode outright: Auto with
  manual restore, Transfer to generator, Transfer to mains, Off mode, Lamp test.

### Notes
- Remote Control Outputs (GenComm page 193) are **not** available on the 4xxx
  series — the supported controller list covers E800, P100, 61xx MKIII, 73xx,
  74xx and 8xxx only.
- GenComm page 16 registers 0-7 report which control functions a module
  supports, but DSEWebNet does not expose that page, so an unsupported key is
  silently ignored by the controller rather than reported as an error.

## 1.3.0

### Added
- **Alarm decoding.** Group 129 is decoded as GenComm "Page 154 – Named Alarm
  Conditions": four alarms per 16-bit register, most significant nibble first,
  where the nibble is a condition code (warning, shutdown, electrical trip,
  indication) rather than a severity bit. Adds a `problem` binary sensor, an
  Alarm state sensor and an active alarm count, with the full alarm list and a
  severity breakdown published as entity attributes.
  If the payload does not match the packed-register shape, the add-on logs what
  it actually received and falls back to raw exposure — it never invents alarms.
- **Documented control keys** from GenComm "Page 16 – Control Registers":
  Test mode, Mute alarm, Reset alarms, and Telemetry remote start / cancel.
- **Remote start button.** Telemetry start (key 35732) requests a start while
  leaving the controller in Auto, which is a safer remote-start path than
  forcing Manual mode and pressing Start. Cancel remote start (35733) reverses
  it. The existing Start button is unchanged.
- Test added to the mode select, now that its control key is confirmed.

### Notes
- Register pages 129, 131 and 132 do not exist in the GenComm standard, which
  confirms that DSEWebNet uses its own group numbering while preserving the
  GenComm instrument order inside each group.

## 1.2.0

Parameter group 131 is now mapped from the official DSE GenComm standard
(v2.272) rather than from inference. The sub key is the instrument index on
GenComm "Page 4 – Basic Instrumentation" — the register list with each 32-bit
instrument counted once. Indices 0…13 were already verified against the
DSEWebNet web UI and match the standard exactly.

### Added
- Generator currents L1/L2/L3 and earth current (needs CTs fitted and
  parameters 626-628 configured).
- Per-phase generator power.
- Mains frequency and all six mains voltages — the DSE4520 MKII is an AMF
  controller, so the mains side is instrumented.
- Phase rotation and current lag/lead as diagnostic entities.
- Signed instrument support: coolant temperature, oil temperature, power and
  lag/lead are two's complement in GenComm and now read correctly below zero,
  whichever register width DSEWebNet puts on the wire.
- Commented candidate map for group 132 in case it turns out to be GenComm
  "Page 7 – Accumulated Instrumentation" (engine hours, kWh counters, number of
  starts).

### Changed
- **Oil pressure is now published in kPa**, per the standard, instead of the
  previous bar approximation. Home Assistant can display it in bar — entity
  settings → unit of measurement — without changing the stored value.
- Sentinel detection widened to the full documented range: unimplemented, over
  range, under range, transducer fault, bad data, high digital input, low
  digital input, reserved. Previously the last three were not caught.
- Coolant and oil temperature scale confirmed as whole degrees Celsius, fuel
  level as whole percent, engine speed as whole RPM.

## 1.1.1

### Added
- **Parameters 131/1…6 identified.** The sub key in group 131 is the instrument
  index of DSE GenComm "Page 4 – Basic Instrumentation": the already verified
  entries (0 = oil pressure, 7 = generator frequency, 8…13 = the six generator
  voltages) land exactly on the GenComm layout, which fixes the remaining six as
  coolant temperature, oil temperature, fuel level, charge alternator voltage,
  battery voltage and engine speed. Six new sensors, names confirmed, scales
  still to be verified against the controller display.
- `filter_sentinels` (default on) — GenComm returns reserved values at the top of
  the integer range when an instrument is out of range, its sensor is faulty, the
  value is not measurable in the current state or the instrument is not fitted.
  These are now published as unknown instead of being recorded as 65535, which
  would otherwise poison long-term statistics on the first run.
- The first value of every parameter is logged as `raw -> scaled`, so scales can
  be checked against the controller without enabling the full frame dump.
- Commented-out candidates for the GenComm continuation of page 4 (currents,
  per-phase power) ready to enable once CTs are configured.

### Changed
- `controller_model` now defaults to `DSE4520 MKII`.

## 1.1.0

### Breaking / behaviour changes
- **Control is disabled by default.** Start/Stop/Manual/Auto buttons are only
  published when `allow_control: true`. Existing users who rely on the buttons
  must enable it after upgrading.
- **Base MQTT topic is now `dse/<module_id>`** instead of the fixed
  `dse/generator`, so several generators no longer collide. Set `mqtt_topic`
  to `dse/generator` to keep the old topic. Entity IDs are unaffected —
  `unique_id` values are unchanged.
- `gateway_id` / `module_id` no longer ship with example values; both are empty
  by default.

### Added
- Declarative parameter registry: unit, scale, `device_class`, `state_class`
  and display precision are defined per parameter in one table.
- `expose_unknown` — every parameter that is received but not yet mapped is
  published as a diagnostic sensor `p<group>_<sub>` (raw, unscaled), which makes
  identifying new values against the physical controller straightforward.
- `debug_raw` + `log_level` — full WebSocket frame dump for protocol work.
- `state_class: measurement` on all numeric sensors → long-term statistics.
- Derived binary sensors: Engine running, Mains available, Load on generator.
- `select` entity for the operating mode (Stop / Manual / Auto).
- `Last update` diagnostic timestamp sensor.
- MQTT broker settings are taken from the Supervisor MQTT service when
  `mqtt_host` is left empty — no need to fill in host/port/user/password.
- `device_name`, `controller_model` and `subscription_override` options.
- Subscription now also covers parameter groups 132 and the gateway `data`
  blocks, and requests all sub keys instead of a fixed list.

### Fixed
- **paho-mqtt 2.x compatibility.** The previous release used the v1 callback
  API and would fail to start on any freshly built image.
- WebSocket heartbeat (20 s) and receive timeout (120 s) — a silently dead TCP
  connection is now detected and reconnected instead of hanging forever. This
  mainly affects cellular gateways.
- Stale-data watchdog: entities go unavailable when no data arrives, instead of
  showing stale values as if they were live.
- Availability now reflects the DSEWebNet session, not just the MQTT link.
- Data from other gateways/modules in the same account is no longer merged into
  one device.
- State is published with `retain`, so values survive a Home Assistant restart.
- Unknown values are published as `null` (unknown) instead of a fake `0.0`.
- MQTT connection no longer blocks startup and retries on its own if the broker
  is not up yet.
- Exponential backoff with jitter on reconnect instead of a fixed 15 s.
- Graceful SIGTERM handling — availability is set to `offline` when the add-on
  is stopped.
- A command now triggers an immediate state refresh instead of waiting for the
  next poll cycle.
- Options are read directly from `/data/options.json`, fixing passwords that
  contain shell metacharacters.

## 1.0.1
- Fixed Auto command ID (35701, verified from browser traffic)
- Start button now sends Manual → Start sequence automatically
- Fixed command thread-safety (paho callback → asyncio queue)
- Cleaned up logs: status updates, commands, connection events only
- Logs cleared on each restart

## 1.0.0

Initial release.

- DSEWebNet cloud WebSocket → MQTT bridge for Home Assistant
- HASS auto-discovery: 13 sensors + 4 control buttons grouped under one device
- Sensors: Engine State, Mains State, Load State, Generator Mode, Supervisor State, Oil Pressure, Frequency, Voltage L1-N/L2-N/L3-N/L1-L2/L2-L3/L3-L1
- Buttons: Start (auto-sends Manual → Start sequence), Stop, Auto, Manual
- Real-time push updates via WebSocket + configurable polling interval
- Reverse-engineered DSEWebNet WebSocket protocol: CSRF login, subscription, push data, commands
