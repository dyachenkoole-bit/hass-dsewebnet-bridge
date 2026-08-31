## 1.1.0

Generator alarms, missing measurements, and a staleness guard.

### Added
- **Generator alarm entity** (`binary_sensor`, device_class `problem`) fed from protocol
  parameter `129` (`severityID` + text). Until now the add-on published no alarm entity at all,
  although the cloud sends the summary on every update.
- **Seven measurements that were already arriving and being discarded**: starter battery voltage,
  charge alternator voltage, engine speed (RPM), coolant temperature, oil temperature, fuel level,
  and the alarm text. Starter battery voltage matters most — it is the usual reason a standby
  generator fails to start.
- **Staleness watchdog.** MQTT last-will only fires when the MQTT connection itself drops.
  If the *cloud* goes away, the broker stays up, `availability` stayed `online`, and Home Assistant
  kept showing hours-old values as current. The add-on now publishes `offline` when no data has
  arrived for three poll intervals (minimum 120 s). For a generator this was the worst failure
  mode: silence looked like health.

### Fixed
- `_on_connect` ignored the MQTT return code and logged "MQTT connected" even when the broker
  refused the connection, then subscribed into nothing.
- Numeric parsing crashed on the controller's `"----"` (no sensor) and `"####"` (out of range)
  placeholders. They now map to `None` instead of `0` — reporting zero would have claimed a
  healthy sensor reading zero.

### Notes
- Protocol parameter `132` is requested in the subscription but never sent by the cloud;
  alarms come through `129`. Captured live 2026-08-31.

# Changelog

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
