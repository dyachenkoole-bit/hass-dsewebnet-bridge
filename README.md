# DSEWebNet Bridge — Home Assistant App

Connects a DSE generator to Home Assistant via the DSEWebNet cloud WebSocket API and MQTT auto-discovery. Around **110 entities**: full engine and electrical instrumentation for both the generator and the mains side, energy counters, engine hours, named alarms, digital inputs and outputs, and optional control.

> This is a fork of [dmdukr/hass-dsewebnet-bridge](https://github.com/dmdukr/hass-dsewebnet-bridge), rebuilt around the DSEWebNet instrument catalogue. Where the original published 13 sensors from a partly guessed parameter map, this version maps every instrument the service exposes, decodes alarms and digital I/O, and adapts to whatever the controller reports.

> 🤖 Reverse engineering of the DSEWebNet WebSocket protocol, all Python code and this documentation were produced by [Claude](https://claude.ai) (Anthropic). Hardware access and live logs were provided by the repository owner.

---

*[Українська версія нижче / Ukrainian version below](#dsewebnet-bridge--home-assistant-додаток)*

---

## Installation

1. Settings → Add-ons → Add-on Store → ⋮ → **Repositories**
2. Add this repository's URL
3. ⋮ → **Check for updates**, then find **DSEWebNet Bridge** and install
4. Fill in the Configuration tab and start

## Configuration

| Option | Meaning |
|---|---|
| `dse_username`, `dse_password` | Your [dsewebnet.com](https://www.dsewebnet.com) login |
| `gateway_id`, `module_id` | Identify your generator — see below |
| `mqtt_host/port/user/pass` | **Leave empty** with the Mosquitto add-on: taken from the Supervisor |
| `mqtt_topic` | Base topic, empty → `dse/<module_id>` |
| `poll_interval` | Fallback poll in seconds, default `30`, `0` disables |
| `allow_control` | `false` by default — read-only. `true` adds nine buttons and a mode select |
| `expose_unknown` | Publishes anything not in the parameter table as a diagnostic sensor |
| `probe_groups` | Sweeps neighbouring parameter groups. Off by default; everything a DSE4520 answers is already in the normal subscription |
| `filter_sentinels` | Publishes out-of-range and not-fitted readings as unknown instead of 65535 |
| `debug_raw`, `log_level` | Full WebSocket frame dump for protocol work |
| `device_name`, `controller_model` | Naming, e.g. `DSE4520 MKII` |
| `subscription_override` | Advanced: raw JSON subscription message |

### Finding `gateway_id` and `module_id`

Both are visible on the DSEWebNet page — no developer tools needed.

![DSEWebNet IDs location](https://raw.githubusercontent.com/dmdukr/hass-dsewebnet-bridge/main/docs/dsewebnet-ids.png)

- **Gateway ID** → top right: *"Connection made to ID **19XXXXXXXXXXX01** Using Ethernet"*
- **Module ID** → breadcrumb: *WebNet » SiteName » **25XXXXXXXD*** — or left panel: `USB ID:`

---

## Entities

### Engine

| Entity | Unit |
|---|---|
| Engine hours | h, total increasing |
| Number of starts | total increasing |
| Engine speed | rpm |
| Coolant temperature · Oil temperature | °C |
| Oil pressure | bar |
| Fuel level · Fuel level (volume) | % |
| Battery voltage · Charge alternator voltage | V |

### Generator

Frequency (Hz) · voltages L1-N, L2-N, L3-N, L1-L2, L2-L3, L3-L1 (V) · currents L1, L2, L3 (A) · power L1, L2, L3 and total (kW) · apparent power L1, L2, L3 and total (kVA) · reactive power L1, L2, L3 and total (kvar) · power factor L1, L2, L3 and average.

### Mains

The same set for the mains side: frequency, six voltages, three currents, kW, kVA, kvar and power factor per phase plus totals.

### Energy counters

| Entity | Unit |
|---|---|
| Generator energy | kWh — ready for the Energy Dashboard |
| Generator apparent energy | kVAh |
| Generator reactive energy | kvarh |

### Status and alarms

| Entity | Type |
|---|---|
| Engine state · Mains state · Load state · Supervisor state · Generator mode | sensor |
| Engine running · Mains available · Load on generator | binary sensor |
| Problem | binary sensor, `device_class: problem`, with the full alarm list and a severity breakdown as attributes |
| Alarm state · Active alarm count | sensor |
| Last update | timestamp |

### Digital inputs and outputs

Built from the payload — the controller names its own terminals, so renaming a function in the DSE configurator renames the entity. On a DSE4520: inputs A-D and outputs A-F, e.g. *Remote Start On Load*, *Emergency Stop*, *Start Relay*, *Close Mains Output*, *Close Gen Output*, *Audible Alarm*.

### Diagnostic

Earth current · load unbalance · generator current lag/lead · three maintenance countdowns and their due timestamps · gateway signal strength, RSSI, RSRQ, SINR, uptime, GSM type, Ethernet flag and GPS position.

### Controls (only when `allow_control: true`)

Mode select (Stop / Manual / Auto / Test) and nine buttons: Start, Stop, Manual, Auto, Remote start, Cancel remote start, Mute alarm, Reset alarms, Reset mains failure.

Additional keys are accepted on the command topic without a button, because they drive contactors or change mode outright: `auto_manual_restore`, `transfer_generator`, `transfer_mains`, `off`, `lamp_test`.

> ⚠️ These entities start and stop a diesel engine. `allow_control` is `false` by default on purpose.

---

## Notes from a live DSE4520 MKII

**Not every instrument exists on every controller.** DSEWebNet renders an unavailable instrument as `----` and one that cannot be measured right now as `####`; both are published as unknown. On a 4520 with no oil pressure sender, oil pressure stays unknown permanently — this is the controller, not the bridge.

**Mains power is only measured if mains CTs are fitted.** Without them the mains voltages and frequency read correctly while mains kW, kVA and power factor stay `####`.

**Remote start is the safer start path.** The Start button sends Manual → Start, which leaves the controller in Manual; if an automation fails between the two steps the set will not start by itself on the next mains failure. Remote start (control key 35732) requests a start while leaving the controller in Auto, and Cancel remote start reverses it.

**Output polarity matters for automations.** If an output is configured De-Energise — as *Close Mains Output* usually is, so that the mains drops out when the controller loses power — its binary sensor reads ON at rest. Trigger on the transition to OFF.

**The DSEWebNet session drops roughly every 30 minutes** and reconnects within seconds. Entities go unavailable briefly, so use `for: minutes: 1` on triggers to avoid false positives.

---

## Automation examples

```yaml
automation:
  - alias: "Start generator on mains failure"
    trigger:
      - platform: state
        entity_id: binary_sensor.dse_generator_mains_ok
        to: "off"
        for:
          seconds: 30
    action:
      - service: button.press
        target:
          entity_id: button.dse_generator_remote_start

  - alias: "Cancel remote start after mains restore"
    trigger:
      - platform: state
        entity_id: binary_sensor.dse_generator_mains_ok
        to: "on"
        for:
          minutes: 2
    action:
      - service: button.press
        target:
          entity_id: button.dse_generator_cancel_remote_start

  - alias: "Notify on generator alarm"
    trigger:
      - platform: state
        entity_id: binary_sensor.dse_generator_problem
        to: "on"
        for:
          minutes: 1
    action:
      - service: notify.telegram
        data:
          message: >-
            Generator alarm: {{ state_attr('binary_sensor.dse_generator_problem',
            'active_alarms') | map(attribute='name') | join(', ') }}
```

Both control examples require `allow_control: true`.

---

## Adding parameters

Group 131 sub keys are DSEWebNet instrument IDs, taken from the "Instrument" dropdown in the chart series editor on the DSEWebNet site. To map one that is not in the table yet, enable `expose_unknown`, find its ID in the log, and add a line to `PARAMS["131"]` in `dsewebnet-bridge.py`:

```python
"305": Param("engine_run_time", "Engine hours", "h", 1.0, "duration",
             state_class="total_increasing", precision=2, icon="mdi:timer-outline"),
```

Units come from the payload and override the table when the device class accepts them, so the unit column is only a fallback.

---

## Tested on

| Component | Version |
|-----------|---------|
| DSE controller | DSE4520 MKII |
| DSE gateway | DSE890 MKII |
| Home Assistant OS | 17.1 |
| Home Assistant Core | 2026.2.2 |

The original was developed against a DSE6110 MKIII with a DSE 0890-04 gateway.

---
---

# DSEWebNet Bridge — Home Assistant Додаток

Підключає генератор DSE до Home Assistant через хмарний WebSocket API DSEWebNet та MQTT auto-discovery. Близько **110 сутностей**: повна приладова інформація по двигуну та електриці для генератора й мережі, лічильники енергії, моточаси, іменовані аварії, дискретні входи та виходи, опційне керування.

> Це форк [dmdukr/hass-dsewebnet-bridge](https://github.com/dmdukr/hass-dsewebnet-bridge), перебудований навколо каталогу приладів DSEWebNet. Там, де оригінал публікував 13 сенсорів за частково вгаданою картою параметрів, ця версія розбирає кожен прилад, який віддає сервіс, декодує аварії та дискретний ввід-вивід і підлаштовується під те, що повідомляє контролер.

## Встановлення

1. Налаштування → Доповнення → Магазин доповнень → ⋮ → **Repositories**
2. Додай URL цього репозиторію
3. ⋮ → **Перевірити оновлення**, знайди **DSEWebNet Bridge** та встанови
4. Заповни вкладку Конфігурація та запусти

## Налаштування

| Параметр | Призначення |
|---|---|
| `dse_username`, `dse_password` | Логін від [dsewebnet.com](https://www.dsewebnet.com) |
| `gateway_id`, `module_id` | Ідентифікують генератор — див. нижче |
| `mqtt_host/port/user/pass` | **Залиш порожніми** з додатком Mosquitto: дані беруться з Supervisor |
| `mqtt_topic` | Базовий топік, порожній → `dse/<module_id>` |
| `poll_interval` | Резервний опит у секундах, типово `30`, `0` вимикає |
| `allow_control` | Типово `false` — лише читання. `true` додає дев'ять кнопок і select режиму |
| `expose_unknown` | Публікує все, чого немає в таблиці, як діагностичний сенсор |
| `probe_groups` | Сканує сусідні групи параметрів. Типово вимкнено |
| `filter_sentinels` | Публікує позадіапазонні та невстановлені прилади як unknown, а не 65535 |
| `debug_raw`, `log_level` | Повний дамп кадрів WebSocket |
| `device_name`, `controller_model` | Назви, напр. `DSE4520 MKII` |
| `subscription_override` | Для просунутих: власне JSON-повідомлення підписки |

### Де взяти `gateway_id` та `module_id`

Обидва видно прямо на сторінці DSEWebNet.

![Розташування ID в DSEWebNet](https://raw.githubusercontent.com/dmdukr/hass-dsewebnet-bridge/main/docs/dsewebnet-ids.png)

- **Gateway ID** → правий верхній кут: *"Connection made to ID **19XXXXXXXXXXX01** Using Ethernet"*
- **Module ID** → хлібні крихти або ліва панель: `USB ID:`

---

## Сутності

**Двигун:** моточаси (з накопиченням), кількість пусків, оберти, температура ОЖ та масла, тиск масла, рівень палива у відсотках та об'ємі, напруга АКБ та зарядного генератора.

**Генератор:** частота, шість напруг, три струми, кВт / кВА / квар пофазно та сумарно, коефіцієнт потужності пофазно та середній.

**Мережа:** той самий набір для мережевої сторони.

**Лічильники енергії:** кВт·год (готовий для Панелі енергії), кВА·год, квар·год.

**Стан та аварії:** стан двигуна, мережі, навантаження, контролера, режим; бінарні сенсори «двигун працює», «мережа в нормі», «навантаження на генераторі»; сенсор `problem` з повним списком аварій та розбивкою за рівнями в атрибутах; лічильник активних аварій; час останнього оновлення.

**Дискретний ввід-вивід:** будується з payload — контролер сам називає свої клеми. На DSE4520 це входи A-D та виходи A-F, зокрема *Remote Start On Load*, *Emergency Stop*, *Start Relay*, *Close Mains Output*, *Close Gen Output*, *Audible Alarm*.

**Діагностика:** струм витоку, дисбаланс навантаження, кут зсуву фаз, три таймери ТО, рівень сигналу шлюза, RSSI, RSRQ, SINR, час роботи, тип GSM, ознака Ethernet та координати GPS.

**Керування (лише при `allow_control: true`):** select режиму (Stop / Manual / Auto / Test) і дев'ять кнопок: Start, Stop, Manual, Auto, Remote start, Cancel remote start, Mute alarm, Reset alarms, Reset mains failure.

> ⚠️ Ці сутності запускають і зупиняють дизельний двигун. `allow_control` типово вимкнено навмисне.

---

## Спостереження з живого DSE4520 MKII

**Не кожен прилад існує на кожному контролері.** DSEWebNet показує недоступний прилад як `----`, а той, що зараз не вимірюється, як `####`; обидва публікуються як unknown. На 4520 без датчика тиску масла тиск лишається невідомим назавжди — це контролер, а не міст.

**Потужність мережі вимірюється лише за наявності ТТ на вводі.** Без них напруги й частота мережі читаються правильно, а кВт, кВА та cos φ лишаються `####`.

**Remote start — безпечніший спосіб пуску.** Кнопка Start надсилає Manual → Start і лишає контролер у ручному режимі; якщо автоматизація впаде між кроками, генератор сам не запуститься при наступному зникненні мережі. Remote start (ключ 35732) запитує пуск, лишаючи контролер в Auto.

**Полярність виходу впливає на автоматизації.** Якщо вихід налаштований як De-Energise — а *Close Mains Output* зазвичай саме такий, щоб мережа відпадала при втраті живлення контролера — його бінарний сенсор у спокої показує ON. Тригер треба будувати на переході в OFF.

**Сесія DSEWebNet рветься приблизно кожні 30 хвилин** і відновлюється за секунди. Сутності ненадовго стають недоступними, тому в тригерах став `for: minutes: 1`.

---

## Протестовано на

| Компонент | Версія |
|-----------|--------|
| Контролер DSE | DSE4520 MKII |
| Шлюз DSE | DSE890 MKII |
| Home Assistant OS | 17.1 |
| Home Assistant Core | 2026.2.2 |

Оригінал розроблявся на DSE6110 MKIII зі шлюзом DSE 0890-04.
