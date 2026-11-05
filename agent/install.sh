#!/bin/sh
set -eu
[ "$(id -u)" = 0 ] || { echo 'Run as root'; exit 1; }
[ -x /usr/bin/pveversion ] || { echo 'This installer requires a Proxmox VE host'; exit 1; }
/usr/bin/python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
agent_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install -d -m 0755 /opt/proxmanager
install -d -m 0700 /etc/proxmanager /var/lib/proxmanager
install -m 0644 "$agent_dir/agent.py" /opt/proxmanager/agent.py
install -m 0644 "$agent_dir/proxmanager.service" /etc/systemd/system/proxmanager.service
if [ ! -f /etc/proxmanager/config.json ]; then
    install -m 0600 "$agent_dir/config.example.json" /etc/proxmanager/config.json
fi
systemctl daemon-reload
echo 'Edit /etc/proxmanager/config.json, then run: systemctl enable --now proxmanager'
