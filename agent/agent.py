#!/usr/bin/env python3
"""Outbound-only ProxManager agent. Python 3.11+, standard library only."""
import argparse
import json
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import ssl
import subprocess
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit

COMMANDS = {
    'refresh-updates': ['/usr/bin/apt-get', 'update'],
    'install-updates': ['/usr/bin/apt-get', '-y', '--no-remove', 'upgrade'],
    'reboot': ['/usr/bin/systemd-run', '--collect', '--unit=proxmanager-reboot', '--on-active=10s', '/usr/sbin/shutdown', '-r', 'now'],
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Redirects are forbidden')


def load_config(path):
    info = path.stat()
    if info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError('Configuration must be root-owned, mode 0600')
    config = json.loads(path.read_text())
    url = urlsplit(config['url'])
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
        raise ValueError('url must be an HTTPS origin')
    if not re.fullmatch('[a-f0-9]{64}', config['token']):
        raise ValueError('Invalid agent token')
    config['url'] = config['url'].rstrip('/')
    if not isinstance(config.get('allowed_actions', []), list) or not set(config.get('allowed_actions', [])) <= COMMANDS.keys():
        raise ValueError('Invalid local action policy')
    return config


def metrics():
    memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    total = int(memory['MemTotal'].split()[0])
    available = int(memory['MemAvailable'].split()[0])
    disk = shutil.disk_usage('/')
    version = subprocess.run(['/usr/bin/pveversion'], capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    return {'hostname': socket.gethostname(), 'uptime': float(Path('/proc/uptime').read_text().split()[0]),
            'load': list(os.getloadavg()), 'memory_percent': round(100 * (1 - available / total), 1),
            'disk_percent': round(100 * disk.used / disk.total, 1), 'version': version[:200],
            'reboot_required': Path('/var/run/reboot-required').exists()}


def execute(job, config, runner=subprocess.run):
    action = job.get('action')
    if action not in COMMANDS or action not in config.get('allowed_actions', []):
        return 'failed', 'Action denied by local policy'
    if not isinstance(job.get('expires_at'), (float, int)) or job['expires_at'] <= time.time():
        return 'failed', 'Action expired before execution'
    try:
        # File output avoids holding unbounded package-manager logs in memory.
        with tempfile.TemporaryFile() as output:
            result = runner(COMMANDS[action], stdout=output, stderr=subprocess.STDOUT,
                            timeout=1800, check=False,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8', 'DEBIAN_FRONTEND': 'noninteractive'})
            output.seek(0, 2)
            length = output.tell()
            output.seek(max(0, length - 3500))
            tail = output.read().decode('utf-8', errors='replace')
        message = 'Reboot scheduled; completion is not verified.' if action == 'reboot' and result.returncode == 0 else tail
        return ('succeeded' if result.returncode == 0 else 'failed'), message
    except subprocess.TimeoutExpired:
        return 'unknown', 'Command exceeded timeout; inspect host before retrying'
    except OSError:
        return 'failed', 'Unable to execute local command'


class Agent:
    def __init__(self, config, state):
        self.config = config
        self.db = sqlite3.connect(state)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,status TEXT NOT NULL,result TEXT,sent INTEGER NOT NULL DEFAULT 0)')
        # Never replay an action interrupted by a crash/reboot.
        self.db.execute("UPDATE jobs SET status='unknown',result='Agent restarted during execution; inspect host' WHERE status='running'")
        self.db.commit()
        self.http = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def post(self, route, data):
        request = urllib.request.Request(self.config['url'] + '/api/agent/' + route,
            data=json.dumps(data).encode(), method='POST',
            headers={'Authorization': 'Bearer ' + self.config['token'], 'Content-Type': 'application/json'})
        with self.http.open(request, timeout=30) as response:
            raw = response.read(32769)
            if len(raw) > 32768:
                raise ValueError('Response too large')
            return json.loads(raw)

    def flush(self):
        for job_id, status, result in self.db.execute("SELECT id,status,result FROM jobs WHERE sent=0 AND status!='running'").fetchall():
            self.post('result', {'id': job_id, 'status': status, 'result': result})
            self.db.execute('UPDATE jobs SET sent=1 WHERE id=?', (job_id,))
            self.db.commit()

    def tick(self):
        self.flush()
        job = self.post('poll', {'metrics': metrics()}).get('job')
        if not job:
            return
        if not isinstance(job.get('id'), str) or len(job['id']) > 100:
            raise ValueError('Invalid job identifier')
        try:
            self.db.execute("INSERT INTO jobs(id,status) VALUES(?,'running')", (job['id'],))
            self.db.commit()  # Durable receipt before any side effect.
        except sqlite3.IntegrityError:
            return
        status, result = execute(job, self.config)
        self.db.execute('UPDATE jobs SET status=?,result=? WHERE id=?', (status, result, job['id']))
        self.db.commit()
        self.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, default=Path('/etc/proxmanager/config.json'))
    parser.add_argument('--state', default='/var/lib/proxmanager/state.sqlite')
    args = parser.parse_args()
    os.umask(0o077)
    config = load_config(args.config)
    agent = Agent(config, args.state)
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            agent.tick()
        except Exception as error:
            # Do not log credentials, payloads or URLs.
            logging.warning('Agent cycle failed (%s)', type(error).__name__)
        time.sleep(20)


if __name__ == '__main__':
    main()
