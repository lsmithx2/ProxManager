import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from contextlib import redirect_stdout

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('installer', ROOT / 'agent/installer.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def bundle():
    return {name: {'content': base64.b64encode((ROOT / 'agent' / name).read_bytes()).decode(),
                   'sha256': hashlib.sha256((ROOT / 'agent' / name).read_bytes()).hexdigest()}
            for name in ('agent.py', 'proxmanager.service')}


class InstallerTests(unittest.TestCase):
    def test_download_contains_exact_current_sources(self):
        script = (ROOT / 'public/install.sh').read_text()
        encoded = re.search(r"main\(json.loads\(base64.b64decode\('([A-Za-z0-9+/=]+)'", script).group(1)
        self.assertEqual(json.loads(base64.b64decode(encoded)), bundle())
        self.assertIn((ROOT / 'agent/installer.py').read_text(), script)
        self.assertTrue(script.endswith('}\nproxmanager_install "$@"\n'))

    def test_shell_syntax(self):
        bash = os.environ.get('TEST_BASH') or shutil.which('bash')
        if not bash:
            self.skipTest('Bash unavailable on this host')
        for file in ['public/install.sh', 'agent/install.sh']:
            subprocess.run([bash, '-n', (ROOT / file).as_posix()], check=True, capture_output=True)

    def test_corrupt_or_incomplete_payload_is_rejected(self):
        data = bundle()
        data['agent.py']['content'] = base64.b64encode(b'changed').decode()
        with self.assertRaises(ValueError):
            installer.unpack(data)
        with self.assertRaises(ValueError):
            installer.unpack({})

    def test_new_config_uses_domain_and_disables_actions(self):
        with tempfile.TemporaryDirectory() as folder:
            data = installer.config_for_install(Path(folder) / 'config.json', lambda: 'a' * 64)
            self.assertEqual(json.loads(data), {'url': 'https://proxmanager.pages.dev', 'token': 'a' * 64, 'allowed_actions': []})
            with self.assertRaises(ValueError):
                installer.config_for_install(Path(folder) / 'config.json', lambda: 'invalid')

    def test_existing_credentials_and_policy_are_preserved(self):
        path = Mock()
        path.is_symlink.return_value = False
        path.exists.return_value = True
        path.stat.return_value = Mock(st_uid=0, st_mode=stat.S_IFREG | 0o600)
        path.read_text.return_value = json.dumps({'url': 'https://proxmanager.pages.dev', 'token': 'b' * 64, 'allowed_actions': ['reboot']})
        self.assertIsNone(installer.config_for_install(path, lambda: self.fail('Must not prompt for existing config')))
        path.stat.return_value = Mock(st_uid=0, st_mode=stat.S_IFREG | 0o644)
        with self.assertRaises(ValueError):
            installer.config_for_install(path, lambda: self.fail())

    def test_symlink_config_is_rejected(self):
        path = Mock()
        path.is_symlink.return_value = True
        with self.assertRaises(ValueError):
            installer.config_for_install(path, lambda: self.fail())

    def test_running_agent_is_never_interrupted(self):
        with patch.object(installer.os, 'geteuid', return_value=0, create=True), \
             patch.object(installer.os, 'access', return_value=True), \
             patch.object(installer.subprocess, 'run', return_value=Mock(returncode=0)), \
             patch.object(installer, 'atomic_write') as write:
            with self.assertRaisesRegex(ValueError, 'already running'):
                installer.install(bundle())
            write.assert_not_called()

    def test_install_stages_files_and_starts_service_without_exposing_token(self):
        # All host paths, writes, privilege checks and service calls are isolated.
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            writes = []
            output = io.StringIO()
            def target(value):
                return base / str(value).lstrip('/')
            with patch.object(installer, 'Path', side_effect=target), \
                 patch.object(installer.os, 'geteuid', return_value=0, create=True), \
                 patch.object(installer.os, 'access', return_value=True), \
                 patch.object(installer, 'secure_directory'), \
                 patch.object(installer, 'atomic_write', side_effect=lambda *args: writes.append(args)), \
                 patch.object(installer, 'prompt_token', return_value='c' * 64), \
                 patch.object(installer.subprocess, 'run', side_effect=[Mock(returncode=3), Mock(returncode=0), Mock(returncode=0), Mock(returncode=0)]) as run, \
                 redirect_stdout(output):
                installer.install(bundle())
            config_writes = [row for row in writes if row[0].name == 'config.json']
            self.assertEqual(config_writes[0][2], 0o600)
            self.assertEqual(json.loads(config_writes[0][1])['allowed_actions'], [])
            self.assertNotIn('c' * 64, output.getvalue())
            self.assertTrue(any(call.args[0] == ['/usr/bin/systemctl', 'enable', '--now', 'proxmanager'] for call in run.call_args_list))


if __name__ == '__main__':
    unittest.main()
