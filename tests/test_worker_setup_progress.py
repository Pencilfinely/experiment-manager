import contextlib
import io
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from expman import worker_setup
from tests.support import temporary_directory


PAIR = {'schema': 1, 'node_id': 'progress-test', 'hub_url': 'http://127.0.0.1:8765',
        'token': 'secret-worker-token-for-progress-tests'}


class WorkerSetupProgressTests(unittest.TestCase):
    def test_requested_cancel_never_launches_a_setup_command(self):
        with temporary_directory() as temporary:
            setup = worker_setup.WorkerSetup(temporary, PAIR, cancel_requested=lambda: True)
            with patch.object(worker_setup.subprocess, 'Popen') as launch:
                with self.assertRaises(worker_setup.SetupCancelled):
                    setup.command(['docker', 'pull', 'unused'], timeout=1)
            launch.assert_not_called()
            self.assertFalse(setup.log_path.exists())

    def test_cancel_callback_stops_quiet_owned_child_and_preserves_other_process(self):
        with temporary_directory() as temporary:
            requested = threading.Event()
            setup = worker_setup.WorkerSetup(temporary, PAIR, cancel_requested=requested.is_set)
            launched = []
            real_popen = subprocess.Popen
            result = []
            unrelated = real_popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, start_new_session=os.name != 'nt')

            def launch(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                launched.append(process)
                return process

            def command():
                try:
                    setup.command([sys.executable, '-u', '-c',
                        "import time; print('before-request', flush=True); time.sleep(60)"], timeout=8)
                except BaseException as error:
                    result.append(error)

            try:
                with patch.object(worker_setup.subprocess, 'Popen', side_effect=launch):
                    thread = threading.Thread(target=command)
                    thread.start()
                    try:
                        deadline = time.monotonic() + 4
                        while time.monotonic() < deadline:
                            if setup.log_path.exists() and '\nbefore-request\n' in setup.log_path.read_text(encoding='utf-8'):
                                break
                            time.sleep(.01)
                        else:
                            self.fail('Setup command did not publish its initial output')
                        requested.set()
                        thread.join(timeout=4)
                        self.assertFalse(thread.is_alive())
                    finally:
                        requested.set()
                        thread.join(timeout=5)
                self.assertEqual(len(result), 1)
                self.assertIsInstance(result[0], worker_setup.SetupCancelled)
                self.assertIsNotNone(launched[0].poll())
                self.assertIsNone(unrelated.poll())
                self.assertIn('\nbefore-request\n', setup.log_path.read_text(encoding='utf-8'))
                self.assertIn('Setup canceled', setup.log_path.read_text(encoding='utf-8'))
            finally:
                unrelated.terminate()
                unrelated.wait(timeout=5)

    def test_start_stops_between_setup_phases_after_exit_request(self):
        with temporary_directory() as temporary:
            requested = threading.Event()
            args = SimpleNamespace(root=temporary, pairing='pairing.json', configure=False,
                                   gpu=None, prepare_only=True)
            with patch.object(worker_setup.sys, 'platform', 'linux'), \
                    patch.object(worker_setup.os, 'geteuid', return_value=1000, create=True), \
                    patch.object(worker_setup, 'load_pairing', return_value=PAIR), \
                    patch.object(worker_setup, 'private_connection'), \
                    patch.object(worker_setup, 'WorkerSetup') as constructor:
                constructor.return_value.prerequisites.side_effect = requested.set
                with self.assertRaises(worker_setup.SetupCancelled):
                    worker_setup.start(args, cancel_requested=requested.is_set)
                self.assertEqual(constructor.call_args.kwargs['cancel_requested'], requested.is_set)
                constructor.return_value.sources.assert_not_called()
                constructor.return_value.configure.assert_not_called()
            self.assertFalse((Path(temporary) / 'node.ready.json').exists())

    def test_command_is_logged_before_launch_and_output_arrives_before_exit(self):
        with temporary_directory() as temporary:
            setup = worker_setup.WorkerSetup(temporary, PAIR)
            release = Path(temporary) / 'release'
            captured, result = io.StringIO(), []
            real_popen = subprocess.Popen

            def launch(*args, **kwargs):
                self.assertIn('$ ', setup.log_path.read_text(encoding='utf-8'))
                return real_popen(*args, **kwargs)

            def command():
                try:
                    result.append(setup.command([sys.executable, '-u', '-c',
                        "import pathlib,sys,time; print('progress-ready', flush=True); "
                        "p=pathlib.Path(sys.argv[1]); end=time.monotonic()+5\n"
                        "while not p.exists() and time.monotonic()<end: time.sleep(.01)\n"
                        "print('done')", str(release)], timeout=8, progress=True))
                except BaseException as error:
                    result.append(error)

            with contextlib.redirect_stdout(captured), patch.object(worker_setup.subprocess, 'Popen', side_effect=launch):
                thread = threading.Thread(target=command)
                thread.start()
                try:
                    deadline = time.monotonic() + 4
                    while '\nprogress-ready\n' not in captured.getvalue() and thread.is_alive() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertIn('\nprogress-ready\n', captured.getvalue())
                    self.assertIn('\nprogress-ready\n', setup.log_path.read_text(encoding='utf-8'))
                    self.assertTrue(thread.is_alive(), result)
                finally:
                    release.touch()
                    thread.join(timeout=5)
            self.assertEqual(result, [(0, 'progress-ready\ndone\n')])

    def test_split_credentials_are_redacted_in_live_output_return_and_log(self):
        with temporary_directory() as temporary:
            setup = worker_setup.WorkerSetup(temporary, PAIR)
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                code, output = setup.command([sys.executable, '-u', '-c',
                    "import sys,time; t=sys.argv[1]; sys.stdout.write('start:'+t[:12]); "
                    "sys.stdout.flush(); time.sleep(.1); sys.stdout.write(t[12:]+':end\\n')",
                    PAIR['token']], timeout=5, progress=True)
            self.assertEqual(code, 0)
            self.assertEqual(output, 'start:[redacted]:end\n')
            for value in (output, captured.getvalue(), setup.log_path.read_text(encoding='utf-8')):
                self.assertNotIn(PAIR['token'], value)
                self.assertIn('[redacted]', value)

    def test_quiet_phase_has_heartbeat_without_polluting_returned_data(self):
        with temporary_directory() as temporary:
            setup = worker_setup.WorkerSetup(temporary, PAIR)
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured), patch.object(worker_setup, 'COMMAND_HEARTBEAT_SECONDS', .03):
                code, output = setup.command([sys.executable, '-c',
                    "import time; time.sleep(.35); print('{\"ok\": true}')"], timeout=5, progress=True)
            self.assertEqual((code, output), (0, '{"ok": true}\n'))
            self.assertIn('last output', captured.getvalue())
            self.assertIn('last output', setup.log_path.read_text(encoding='utf-8'))

    def test_timeout_stops_child_and_preserves_partial_output_and_actionable_log(self):
        with temporary_directory() as temporary:
            setup = worker_setup.WorkerSetup(temporary, PAIR)
            launched = []
            real_popen = subprocess.Popen

            def launch(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                launched.append(process)
                return process

            with patch.object(worker_setup.subprocess, 'Popen', side_effect=launch):
                with self.assertRaisesRegex(RuntimeError, 'Command exceeded') as error:
                    setup.command([sys.executable, '-u', '-c',
                        "import time; print('partial-output', flush=True); time.sleep(60)"], timeout=.5)
            self.assertIsNotNone(launched[0].poll())
            self.assertIn(str(setup.log_path), str(error.exception))
            log = setup.log_path.read_text(encoding='utf-8')
            self.assertIn('\npartial-output\n', log)
            self.assertIn('Command exceeded', log)

    def test_nonzero_status_and_bounded_capture_keep_complete_disk_log(self):
        with temporary_directory() as temporary:
            setup = worker_setup.WorkerSetup(temporary, PAIR)
            with patch.object(worker_setup, 'COMMAND_OUTPUT_LIMIT', 512):
                code, output = setup.command([sys.executable, '-u', '-c',
                    "import sys; print('x'*20000); print('final-error',file=sys.stderr); sys.exit(7)"],
                    timeout=5, check=False)
            self.assertEqual(code, 7)
            self.assertEqual(len(output), 512)
            self.assertTrue(output.endswith('final-error\n'))
            self.assertIn('x' * 20000, setup.log_path.read_text(encoding='utf-8'))
            with self.assertRaisesRegex(RuntimeError, 'failure.*'):
                setup.command([sys.executable, '-c', "import sys; print('failure'); sys.exit(9)"], timeout=5)

    def test_launch_failure_preserves_original_exception_and_command_log(self):
        with temporary_directory() as temporary:
            setup = worker_setup.WorkerSetup(temporary, PAIR)
            with self.assertRaises(FileNotFoundError):
                setup.command([str(Path(temporary) / 'missing-executable')], timeout=1)
            log = setup.log_path.read_text(encoding='utf-8')
            self.assertIn('$ ', log)
            self.assertIn('missing-executable', log)

    def test_cancellation_stops_owned_child_and_retains_output(self):
        with temporary_directory() as temporary:
            setup = worker_setup.WorkerSetup(temporary, PAIR)
            launched = []
            real_popen = subprocess.Popen
            real_queue = queue.Queue

            class InterruptQueue(real_queue):
                received = False

                def get(self, *args, **kwargs):
                    if self.received:
                        self.received = False
                        raise KeyboardInterrupt
                    data = super().get(*args, **kwargs)
                    self.received = bool(data)
                    return data

            def launch(*args, **kwargs):
                self.assertEqual(kwargs['start_new_session'], os.name != 'nt')
                process = real_popen(*args, **kwargs)
                launched.append(process)
                return process

            with patch.object(worker_setup.subprocess, 'Popen', side_effect=launch), \
                    patch.object(worker_setup.queue, 'Queue', InterruptQueue):
                with self.assertRaises(KeyboardInterrupt):
                    setup.command([sys.executable, '-u', '-c',
                        "import time; print('before-cancel',flush=True); time.sleep(60)"], timeout=5)
            self.assertIsNotNone(launched[0].poll())
            self.assertIn('\nbefore-cancel\n', setup.log_path.read_text(encoding='utf-8'))

    def test_pinned_docker_endpoint_keeps_environment_isolated(self):
        with temporary_directory() as temporary:
            setup = worker_setup.WorkerSetup(temporary, PAIR)
            setup.endpoint = 'unix:///var/run/docker.sock'
            real_popen = subprocess.Popen
            observed = []

            def launch(argv, **kwargs):
                observed.append((argv, kwargs['env']))
                return real_popen([sys.executable, '-c', "print('29.0')"], **kwargs)

            with patch.dict(os.environ, {'DOCKER_HOST': 'tcp://wrong:2375', 'DOCKER_CONTEXT': 'wrong'}), \
                    patch.object(worker_setup.subprocess, 'Popen', side_effect=launch):
                code, output = setup.command(['docker', 'info', '--format', '{{.ServerVersion}}'], timeout=5)
                self.assertEqual(os.environ['DOCKER_CONTEXT'], 'wrong')
            self.assertEqual((code, output), (0, '29.0\n'))
            self.assertEqual(observed[0][0][:3], ['docker', '--host', setup.endpoint])
            self.assertNotIn('DOCKER_CONTEXT', observed[0][1])
            self.assertNotIn('DOCKER_HOST', observed[0][1])

    def test_image_phases_show_progress_and_plain_build_output(self):
        with temporary_directory() as temporary:
            setup = worker_setup.WorkerSetup(temporary, PAIR)
            setup.source_id = 'c' * 64
            setup.build_root = Path(temporary)
            calls = []

            def command(argv, **kwargs):
                calls.append((argv, kwargs))
                if argv[1:3] == ['container', 'inspect']:
                    return 1, ''
                if argv[1] == 'push':
                    return 0, 'digest: sha256:' + 'a' * 64
                return 0, ''

            with patch.object(setup, 'command', side_effect=command), contextlib.redirect_stdout(io.StringIO()):
                setup.image()
            for argv, kwargs in calls:
                if argv[1] in ('pull', 'push', 'build', 'run'):
                    self.assertTrue(kwargs.get('progress'), argv)
                if argv[1] == 'build':
                    self.assertIn('--progress=plain', argv)
                    self.assertEqual(kwargs['timeout'], 7200)


if __name__ == '__main__':
    unittest.main()
