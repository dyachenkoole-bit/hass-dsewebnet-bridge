#!/bin/sh
# Options are read directly from /data/options.json by the bridge itself,
# which avoids quoting problems with passwords containing special characters.
echo "Starting DSEWebNet Bridge..."
exec python3 -u /dsewebnet-bridge.py
