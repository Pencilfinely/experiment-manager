import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from expman import common, worker_service as service
from expman.launcher import InstanceLock
from tests.support import temporary_directory


class WorkerServiceTests(unittest.TestCase):
    def setUp(self):
        # WSL /mnt/c and /mnt/e may not implement chmod; verify private files
        # on the Linux filesystem, just like the real per-user installation.
        self.temporary = (tempfile.TemporaryDirectory(prefix='expman-worker-test-')
                          if sys.platform == 'linux' else temporary_directory())
        self.folder = Path(self.temporary.__enter__())
        self.root = self.folder / 'client'
        self.root.mkdir()
        self.home = self.folder / 'home'
        self.home.mkdir()
        self.config_path = self.home / 'expman-first-run/single-gpu/node.ready.json'
        self.config = {'node_id': 'old2080', 'token': 'private-worker-credential-secret',
                       'hub_url': 'http://127.0.0.1:8765', 'root': 'runtime',
                       'policy': {'run_enabled': False, 'max_running': 3},
                       'gpu_policy': {'GPU-old': {'max_jobs': 2}},
                       'allowed_repos': ['/my/source'], 'assets': {'data': '/my/data'}}
        common.atomic_json(self.config_path, self.config)

    def tearDown(self):
        self.temporary.__exit__(None, None, None)

    def select(self):
        return service.select_configuration(self.root, config=str(self.config_path))

    def test_existing_configuration_reused_without_policy_or_identity_changes(self):
        before = self.config_path.read_bytes()
        selected = self.select()
        self.assertEqual(selected['config'], str(self.config_path))
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertNotIn(self.config['token'], json.dumps(service.status(self.root)))
        self.assertEqual(service.select_configuration(self.root)['config'], str(self.config_path))

    def test_ambiguous_configuration_is_noninteractive_and_credential_free(self):
        candidate = service._candidate(self.config_path)
        with patch.object(service, 'discover_configs', return_value=[candidate, {**candidate, 'node_id': 'other'}]):
            result = service.select_configuration(self.root)
        self.assertEqual(result['status'], 'selection_required')
        self.assertEqual(len(result['candidates']), 2)
        self.assertNotIn(self.config['token'], json.dumps(result))
        self.assertFalse((self.root / 'service.json').exists())

    def test_pairing_required_without_existing_node(self):
        with patch.object(service, 'discover_configs', return_value=[]):
            result = service.select_configuration(self.root)
        self.assertEqual(result['status'], 'pairing_required')

    def test_new_pairing_is_copied_privately_and_not_returned(self):
        credentials = self.folder / 'new.pairing.json'
        pairing = {'schema': 1, 'node_id': 'newnode', 'token': 'a' * 30,
                   'hub_url': 'http://127.0.0.1:8765'}
        common.atomic_json(credentials, pairing)
        with patch.object(service, 'discover_configs', return_value=[]):
            result = service.select_configuration(self.root, pairing=str(credentials), worker_root=str(self.folder / 'data'))
        self.assertEqual(common.read_json(self.root / 'pairing.json'), pairing)
        self.assertEqual(result['config'], str(self.folder / 'data/node.ready.json'))
        self.assertNotIn(pairing['token'], json.dumps(result))
        self.assertNotIn(pairing['token'], json.dumps(service.status(self.root)))
        self.assertFalse(Path(result['config']).exists())
        if os.name != 'nt':
            self.assertEqual((self.root / 'pairing.json').stat().st_mode & 0o777, 0o600)

    def test_mismatched_pairing_preserves_existing_configuration(self):
        credentials = self.folder / 'wrong.json'
        common.atomic_json(credentials, {'schema': 1, 'node_id': 'wrong', 'token': 'a' * 30,
                                        'hub_url': self.config['hub_url']})
        before = self.config_path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'does not match'):
            service.select_configuration(self.root, config=str(self.config_path), pairing=str(credentials))
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_old_agent_is_detected_and_not_signaled(self):
        self.select()
        with InstanceLock(self.config_path.parent / 'runtime/agent.lock'), \
             patch.object(service, '_require_linux'), patch.object(service.os, 'kill') as kill:
            result = service.stop(self.root)
        self.assertTrue(result['running'])
        self.assertEqual(result['status'], 'external_running')
        kill.assert_not_called()

    def test_start_is_detached_noninteractive_and_duplicate_launch_avoided(self):
        self.select()
        with patch.object(service, '_require_linux'), patch.object(service.subprocess, 'Popen') as spawn:
            result = service.start(self.root)
            self.assertEqual(result['status'], 'starting')
            self.assertEqual(spawn.call_count, 1)
            argv = spawn.call_args.args[0]
            self.assertIn('_serve', argv)
            self.assertNotIn(self.config['token'], ' '.join(argv))
            self.assertTrue(spawn.call_args.kwargs['start_new_session'])
            self.assertEqual(spawn.call_args.kwargs['stdin'], subprocess.DEVNULL)
            service.start(self.root)
            self.assertEqual(spawn.call_count, 1)

    def test_stop_refuses_recycled_pid_and_signals_only_owned_supervisor(self):
        self.select()
        service._write_status(self.root, pid=4137, process_identity='100', status='online')
        with patch.object(service, '_require_linux'), \
             patch.object(service, '_process_identity', return_value='different'), \
             patch.object(service.os, 'kill') as kill:
            service.stop(self.root)
            kill.assert_not_called()
        with patch.object(service, '_require_linux'), \
             patch.object(service, '_process_identity', return_value='100'), \
             patch.object(service.os, 'kill') as kill:
            result = service.stop(self.root)
            self.assertEqual(result['status'], 'stopping')
            kill.assert_called_once_with(4137, service.signal.SIGTERM)

    def test_log_api_redacts_credentials_and_limits_lines(self):
        self.select()
        (self.root / 'worker.log').write_text('first\nsecond\n' + self.config['token'] + '\nlast\n', encoding='utf-8')
        result = service.logs(self.root, lines=2)
        self.assertEqual(result['lines'], ['[redacted]', 'last'])

    def test_install_copies_versioned_software_without_altering_config(self):
        before = self.config_path.read_bytes()
        with patch.object(service, '_require_linux'), patch.object(service.Path, 'home', return_value=self.home):
            result = service.install(self.root, config=str(self.config_path), backend='detached', start_now=False)
        settings = common.read_json(self.root / 'service.json')
        package = Path(settings['package_dir'])
        self.assertTrue((package / 'expman/worker_service.py').is_file())
        self.assertIn('worker', common.read_json(package / 'release-role.json')['role'])
        self.assertTrue((self.root / 'Start-Worker.sh').is_file())
        self.assertEqual(len(list((self.home / '.local/share/applications').glob('*.desktop'))), 1)
        self.assertEqual(result['backend'], 'detached')
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_running_install_reuses_identical_bits_but_requires_stop_for_new_code_or_role(self):
        incoming = self.folder / 'incoming'
        package = incoming / 'expman'
        package.mkdir(parents=True)
        source = package / 'worker_service.py'
        source.write_text('# release one\n', encoding='utf-8')
        marker = {'role': 'windows-worker-x64', 'version': '0.3.0-rc.1'}
        common.atomic_json(incoming / 'release-role.json', marker)
        with patch.object(service, '_require_linux'), patch.object(service.Path, 'home', return_value=self.home), \
             patch.object(service, '__file__', str(source)):
            service.install(self.root, config=str(self.config_path), backend='detached', start_now=False)
            before = (self.root / 'service.json').read_bytes()
            installed = Path(common.read_json(self.root / 'service.json')['package_dir'])
            service._write_status(self.root, pid=4217, process_identity='1234', status='online',
                                  detail='Connected to controller', online=True)
            with patch.object(service, '_process_identity', return_value='1234'):
                current = service.status(self.root)
                self.assertEqual(service.install(self.root, backend='detached'), current)
                self.assertEqual((self.root / 'service.json').read_bytes(), before)
                source.write_text('# release two\n', encoding='utf-8')
                changed = service.install(self.root, backend='detached')
                self.assertIn('before installing an update', changed['detail'])
                self.assertTrue(changed['running'])
                self.assertEqual((installed / 'expman/worker_service.py').read_text(encoding='utf-8'), '# release one\n')
                source.write_text('# release one\n', encoding='utf-8')
                common.atomic_json(incoming / 'release-role.json', {**marker, 'version': '0.3.0-rc.2'})
                self.assertIn('before installing an update', service.install(self.root, backend='detached')['detail'])
                self.assertEqual((self.root / 'service.json').read_bytes(), before)
                self.assertEqual(len(list((self.root / 'software').iterdir())), 1)

    def test_user_service_never_uses_sudo_and_signals_process_only(self):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, '', '')
        with patch.object(service, '_require_linux'), patch.object(service.Path, 'home', return_value=self.home), \
             patch.object(service, '_systemd_available', return_value=True), \
             patch.object(service.subprocess, 'run', side_effect=run):
            service.install(self.root, config=str(self.config_path), backend='systemd', start_now=False)
        unit = next((self.home / '.config/systemd/user').glob('*.service')).read_text(encoding='utf-8')
        self.assertIn('KillMode=process', unit)
        self.assertIn('Restart=no', unit)
        self.assertNotIn(self.config['token'], unit)
        self.assertEqual(calls[0], ['systemctl', '--user', 'daemon-reload'])
        self.assertTrue(all(call[0] != 'sudo' for call in calls))

    def test_pid_identity_checks_process_command_and_start_ticks(self):
        proc = self.folder / 'proc/123'
        proc.mkdir(parents=True)
        fields = ['S'] + ['0'] * 18 + ['4321'] + ['0'] * 10
        (proc / 'stat').write_text('123 (python worker) ' + ' '.join(fields), encoding='utf-8')
        (proc / 'cmdline').write_bytes(b'python\0-m\0expman.worker_service\0_serve\0')
        self.assertEqual(service._process_identity(123, self.folder / 'proc'), '4321')
        (proc / 'cmdline').write_bytes(b'python\0unrelated.py\0')
        self.assertIsNone(service._process_identity(123, self.folder / 'proc'))

    def test_serve_does_not_reconfigure_an_existing_worker(self):
        self.select()
        before = self.config_path.read_bytes()
        with patch.object(service, '_require_linux'), patch.object(service, '_process_identity', return_value='100'), \
             patch.object(service.signal, 'signal'), \
             patch.object(service, '_run_agent', side_effect=KeyboardInterrupt) as run, \
             patch('expman.worker_setup.start', side_effect=AssertionError('must reuse existing config')):
            self.assertEqual(service.serve(self.root), 0)
        run.assert_called_once_with(self.root, str(self.config_path))
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(common.read_json(self.root / 'status.json')['status'], 'stopped')

    def test_worker_cli_refuses_controller_release_before_any_lifecycle_action(self):
        output = io.StringIO()
        with patch.object(service, '_release_role', return_value={'role': 'windows-controller-x64'}), \
             patch.object(service, 'status') as status, redirect_stdout(output):
            result = service.main(['status', '--service-root', str(self.root)])
        self.assertEqual(result, 2)
        self.assertEqual(json.loads(output.getvalue())['status'], 'failed')
        self.assertIn('controller edition', json.loads(output.getvalue())['detail'])
        status.assert_not_called()

    def test_worker_cli_accepts_worker_release_and_development_checkout(self):
        for marker in ({'role': 'ubuntu-worker-x64'}, {'role': 'windows-worker-x64'}, {}):
            with self.subTest(marker=marker), patch.object(service, '_release_role', return_value=marker), \
                 redirect_stdout(io.StringIO()) as output:
                self.assertEqual(service.main(['status', '--service-root', str(self.root)]), 0)
                self.assertEqual(json.loads(output.getvalue())['status'], 'stopped')

    def test_session_holder_reads_only_status_and_waits_through_startup(self):
        snapshots = [{'running': False, 'status': 'starting'},
                     {'running': True, 'status': 'preparing'},
                     {'running': True, 'status': 'online'},
                     {'running': False, 'status': 'stopped'}]
        with patch.object(service, '_require_linux'), \
             patch.object(service, 'status', side_effect=snapshots), \
             patch.object(service.time, 'sleep') as sleep, \
             patch.object(service.os, 'kill') as kill:
            self.assertEqual(service.hold(self.root), 0)
        self.assertEqual(sleep.call_count, 3)
        kill.assert_not_called()
        self.assertEqual(sorted(item.name for item in self.root.iterdir()), ['hold.lock'])

    def test_duplicate_session_holder_returns_without_reading_or_changing_agent_state(self):
        with InstanceLock(self.root / 'hold.lock'), patch.object(service, '_require_linux'), \
             patch.object(service, 'status') as status:
            self.assertEqual(service.hold(self.root), 0)
            status.assert_not_called()

    @unittest.skipUnless(sys.platform == 'linux' and getattr(os, 'geteuid', lambda: 0)() != 0,
                         'Linux same-user process lifecycle fixture')
    def test_real_owned_process_status_and_stop_leave_unrelated_process_alive(self):
        self.select()
        before = self.config_path.read_bytes()
        code = (
            'import sys,time\nfrom expman import worker_service as s\n'
            'def fixture(root, config):\n'
            ' s._write_status(root,status="online",online=True,detail="fixture running")\n'
            ' while True: time.sleep(0.05)\n'
            's._run_agent=fixture\nraise SystemExit(s.serve(sys.argv[-1]))\n')
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(20)'])
        child = subprocess.Popen([sys.executable, '-c', code, 'expman.worker_service', '_serve', str(self.root)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        holder = None
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and service.status(self.root)['status'] != 'online':
                self.assertIsNone(child.poll(), 'Fixture supervisor exited unexpectedly')
                time.sleep(0.05)
            current = service.status(self.root)
            self.assertTrue(current['running'])
            self.assertEqual(current['pid'], child.pid)
            self.assertEqual(service.start(self.root)['pid'], child.pid)
            holder = subprocess.Popen([sys.executable, '-m', 'expman.worker_service', '_hold',
                                       '--service-root', str(self.root)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 5
            while not (self.root / 'hold.lock').exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue((self.root / 'hold.lock').exists())
            self.assertIsNone(holder.poll())
            duplicate = subprocess.run([sys.executable, '-m', 'expman.worker_service', '_hold',
                                        '--service-root', str(self.root)], capture_output=True, timeout=5)
            self.assertEqual(duplicate.returncode, 0)
            self.assertIsNone(holder.poll())
            service.stop(self.root)
            self.assertEqual(child.wait(timeout=8), 0)
            self.assertEqual(holder.wait(timeout=5), 0)
            self.assertIsNone(unrelated.poll())
            self.assertEqual(service.status(self.root)['status'], 'stopped')
            self.assertEqual(self.config_path.read_bytes(), before)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            if holder is not None and holder.poll() is None:
                holder.kill()
                holder.wait(timeout=5)
            unrelated.terminate()
            unrelated.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
from contextlib import redirect_stdout
import io
