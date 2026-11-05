import importlib.util
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('agent', Path(__file__).parents[1] / 'agent' / 'agent.py')
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


class AgentTests(unittest.TestCase):
    def test_denied_action_never_runs(self):
        for action in ['reboot', 'shell', 'install-updates']:
            def forbidden(*args, **kwargs):
                self.fail('Command must not run')
            status, _ = agent.execute({'action': action, 'expires_at': time.time()+100}, {'allowed_actions': []}, forbidden)
            self.assertEqual(status, 'failed')

    def test_expired_job_never_runs(self):
        status, _ = agent.execute({'action': 'reboot', 'expires_at': 1}, {'allowed_actions': ['reboot']}, lambda *a, **kw: self.fail())
        self.assertEqual(status, 'failed')

    def test_commands_have_fixed_arguments_and_no_shell(self):
        def runner(command, **kwargs):
            self.assertEqual(command, ['/usr/bin/apt-get', '-y', '--no-remove', 'upgrade'])
            self.assertNotIn('shell', kwargs)
            return type('Result', (), {'returncode': 0})()
        status, _ = agent.execute({'action': 'install-updates', 'expires_at': time.time()+100}, {'allowed_actions': ['install-updates']}, runner)
        self.assertEqual(status, 'succeeded')

    def test_crash_receipt_is_unknown_and_not_replayed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'state.sqlite')
            first = agent.Agent({'url': 'https://app.test', 'token': 'a'*64}, path)
            first.db.execute("INSERT INTO jobs(id,status) VALUES('job','running')")
            first.db.commit()
            first.db.close()
            second = agent.Agent({'url': 'https://app.test', 'token': 'a'*64}, path)
            self.assertEqual(second.db.execute('SELECT status FROM jobs').fetchone()[0], 'unknown')
            second.flush = lambda: None
            second.post = lambda *args: {'job': {'id': 'job', 'action': 'reboot', 'expires_at': time.time()+100}}
            with patch.object(agent, 'metrics', return_value={}), patch.object(agent, 'execute', side_effect=AssertionError('Replayed')):
                second.tick()
            second.db.close()

    def test_redirects_rejected(self):
        with self.assertRaises(ValueError):
            agent.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.test')


if __name__ == '__main__':
    unittest.main()
