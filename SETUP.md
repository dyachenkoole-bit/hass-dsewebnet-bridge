# Publishing this repository

Everything is already filled in for `dyachenkoole-bit` — `repository.json`, the
add-on `url` field and the issue links. Nothing needs editing before upload.

## 1. Create the repository

On GitHub: **New repository** → name it `hass-dsewebnet-bridge` → Public →
create it **empty** (no README, no .gitignore, no licence — they are here).

## 2. Upload

**Add file → Upload files**, then drag in everything from this folder, keeping
the structure:

```
hass-dsewebnet-bridge/
├── README.md
├── SETUP.md
├── LICENSE
├── repository.json
├── .gitignore
├── .gitattributes
├── docs/
│   └── dsewebnet-ids.png
└── dsewebnet-bridge/
    ├── config.yaml
    ├── Dockerfile
    ├── run.sh
    ├── dsewebnet-bridge.py
    ├── icon.png
    ├── DOCS.md
    └── CHANGELOG.md
```

The upload page accepts folders by drag and drop, so dragging `docs` and
`dsewebnet-bridge` in one go preserves the layout. Do not paste
`dsewebnet-bridge.py` into the web editor — copying through the clipboard can
break the indentation.

## 3. Point Home Assistant at it

The add-on slug is unchanged, so an older copy has to go first, otherwise two
add-ons claim the same slug.

1. Copy your current configuration out to a text file — it is not carried over
2. Stop and uninstall the existing DSEWebNet Bridge
3. Settings → Add-ons → Add-on Store → ⋮ → **Repositories** — remove the old
   entry, add `https://github.com/dyachenkoole-bit/hass-dsewebnet-bridge`
4. ⋮ → **Check for updates**, then install
5. Paste the configuration back

Entities and their history survive: `unique_id` values have not changed since
1.0.x and the discovery messages are retained in MQTT.

## 4. Configuration after install

```yaml
allow_control: true       # nine buttons and the mode select
expose_unknown: false     # everything a DSE4520 answers is already mapped
probe_groups: false       # the sweep has served its purpose
debug_raw: false
log_level: info
```

Leave the four `mqtt_*` fields empty to take the broker settings from the
Supervisor, or fill them in for an external broker.

## Later updates

Edit files on GitHub, bump `version:` in `dsewebnet-bridge/config.yaml`, commit.
In Home Assistant: ⋮ → Check for updates → the add-on shows an Update button.
The version bump is what makes the button appear.

## Licence

`LICENSE` carries the original author's MIT copyright line. That attribution
stays in a fork — add your own line below it if you want, do not replace it.
The fork notice at the top of README credits the upstream project.
