"""Installer implementation embedded into the downloadable install.sh at build time."""
import base64
import getpass
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
import re
import stat
import subprocess
import sys
import tempfile
import warnings

APP_URL = 'https://proxmanager.pages.dev'


def unpack(bundle):
    expected = {'agent.py', 'proxmanager.service'}
    if set(bundle) != expected:
        raise ValueError('Invalid installer payload')
    result = {}
    for name, item in bundle.items():
        data = base64.b64decode(item['content'], validate=True)
        if hashlib.sha256(data).hexdigest() != item['sha256']:
            raise ValueError('Installer payload is incomplete or corrupt')
        result[name] = data
    compile(result['agent.py'], 'agent.py', 'exec')
    return result


def config_for_install(path, prompt):
    if path.is_symlink():
        raise ValueError('Configuration must not be a symbolic link')
    if path.exists():
        info = path.stat()
        if info.st_uid != 0 or info.st_mode & 0o077 or not stat.S_ISREG(info.st_mode):
            raise ValueError('Existing configuration must be a root-owned regular file with mode 0600')
        # Never change the token, origin or action policy on reinstall.
        config = json.loads(path.read_text())
        if not re.fullmatch('[a-f0-9]{64}', str(config.get('token', ''))):
            raise ValueError('Existing configuration has an invalid token')
        url = urlsplit(config.get('url', ''))
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
            raise ValueError('Existing configuration has an invalid HTTPS origin')
        actions = config.get('allowed_actions', [])
        if not isinstance(actions, list) or any(action not in ('reboot', 'refresh-updates', 'install-updates') for action in actions):
            raise ValueError('Existing configuration has an invalid local action policy')
        return None
    token = prompt().strip()
    if not re.fullmatch('[a-f0-9]{64}', token):
        raise ValueError('Invalid credential. Copy the 64-character token from Add server.')
    return (json.dumps({'url': APP_URL, 'token': token, 'allowed_actions': []}, indent=2) + '\n').encode()


def secure_directory(path, mode):
    if path.is_symlink():
        raise ValueError('Refusing symbolic link: ' + str(path))
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
        raise ValueError('Installation directories must be owned by root')
    path.chmod(mode)


def atomic_write(path, data, mode):
    if path.is_symlink():
        raise ValueError('Refusing symbolic link: ' + str(path))
    fd, temporary = tempfile.mkstemp(prefix='.proxmanager-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            os.fchmod(output.fileno(), mode)
            os.fchown(output.fileno(), 0, 0)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prompt_token():
    # The install script may arrive on stdin from curl; secrets come from the TTY.
    try:
        with open('/dev/tty', 'w+') as terminal:
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                return getpass.getpass('Paste the server token from ProxManager (hidden): ', stream=terminal)
    except (OSError, getpass.GetPassWarning):
        raise ValueError('An interactive terminal is required to enter the server token') from None


def install(bundle):
    if os.geteuid() != 0:
        raise ValueError('Run the installer as root in the Proxmox host shell')
    if sys.version_info < (3, 11) or not os.access('/usr/bin/pveversion', os.X_OK):
        raise ValueError('A Proxmox VE host with Python 3.11+ is required')
    payload = unpack(bundle)  # Validate the complete bundle before making changes.
    if subprocess.run(['/usr/bin/systemctl', 'is-active', '--quiet', 'proxmanager'], check=False).returncode == 0:
        raise ValueError('Agent is already running. After maintenance has finished, stop it before reinstalling.')
    configuration = Path('/etc/proxmanager/config.json')
    if configuration.parent.is_symlink():
        raise ValueError('Configuration directory must not be a symbolic link')
    config = config_for_install(configuration, prompt_token)
    for directory, mode in [('/opt/proxmanager', 0o755), ('/etc/proxmanager', 0o700), ('/var/lib/proxmanager', 0o700)]:
        secure_directory(Path(directory), mode)
    atomic_write(Path('/opt/proxmanager/agent.py'), payload['agent.py'], 0o644)
    atomic_write(Path('/etc/systemd/system/proxmanager.service'), payload['proxmanager.service'], 0o644)
    if config is not None:
        atomic_write(configuration, config, 0o600)
    subprocess.run(['/usr/bin/systemctl', 'daemon-reload'], check=True)
    subprocess.run(['/usr/bin/systemctl', 'enable', '--now', 'proxmanager'], check=True)
    subprocess.run(['/usr/bin/systemctl', 'is-active', '--quiet', 'proxmanager'], check=True)
    print('ProxManager agent installed and started. Check the dashboard for its first heartbeat.')
    print('New installations are monitoring-only. Remote maintenance remains disabled.')


def main(bundle):
    import fcntl
    os.umask(0o077)
    try:
        if os.geteuid() != 0:
            raise ValueError('Run the installer as root in the Proxmox host shell')
        # /run is root-owned, so unprivileged users cannot precreate this lock.
        with open('/run/proxmanager-install.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            install(bundle)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        # Never print the credential or full configuration.
        print('ProxManager installation failed: ' + str(error), file=sys.stderr)
        raise SystemExit(1) from None
