#!/bin/sh
set -eu
agent_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
/usr/bin/python3 - "$agent_dir" <<'PROXMANAGER_PYTHON'
import base64
import hashlib
from pathlib import Path
import runpy
import sys
source = Path(sys.argv[1])
bundle = {}
for name in ('agent.py', 'proxmanager.service'):
    content = (source / name).read_bytes()
    bundle[name] = {'content': base64.b64encode(content).decode(), 'sha256': hashlib.sha256(content).hexdigest()}
runpy.run_path(str(source / 'installer.py'))['main'](bundle)
PROXMANAGER_PYTHON
