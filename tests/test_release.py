import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import zipfile

from expman.agent import Agent
from expman.common import atomic_json, read_json
from expman.hub import Hub, make_server
from expman.launcher import InstanceLock, browser_url, reopen_controller
from expman.pairing import validate_pairing
from expman.project_setup import register
from expman.worker_setup import WorkerSetup, load_pairing
from expman.worker_setup import start as start_worker
from scripts import build_desktop, build_release
from tests.support import temporary_directory

GPU = {'uuid': 'GPU-11111111-1111-1111-1111-111111111111', 'name': 'Example NVIDIA GPU', 'total_mb': 8192, 'free_mb': 7000}
PAIR = {'schema': 1, 'node_id': 'worker-a', 'hub_url': 'http://127.0.0.1:8765', 'token': 'example-worker-credential-123456789'}
IMAGE = 'localhost:5001/expman-runtime@sha256:' + 'a' * 64


class PairingTests(unittest.TestCase):
    def test_pairing_has_no_administrator_credential_or_machine_paths(self):
        value = validate_pairing(dict(PAIR, admin_token='secret', root='/other/machine', allowed_repos=['private']))
        self.assertEqual(value, PAIR)

    def test_invalid_origins_and_names_rejected(self):
        for url in ('file:///etc/passwd', 'http://user:pass@host', 'http://host/path', 'http://host?token=x',
                    'http://host#x', 'http://host:99999', 'http://host/\n'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_pairing(dict(PAIR, hub_url=url))
        with self.assertRaises(ValueError):
            validate_pairing(dict(PAIR, node_id='../bad'))

    def test_file_discovery_and_repair_identity(self):
        with temporary_directory() as path:
            root = Path(path)
            pairing_path = root / 'worker-a.pairing.json'
            atomic_json(pairing_path, PAIR)
            with patch('pathlib.Path.cwd', return_value=root):
                self.assertEqual(load_pairing(root, None), PAIR)
            atomic_json(root / 'pairing.json', PAIR)
            atomic_json(pairing_path, dict(PAIR, node_id='worker-b'))
            with self.assertRaises(ValueError):
                load_pairing(root, str(pairing_path))


class EnrollmentHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.root = Path(self.temp.__enter__())
        self.hub = Hub(self.root)
        self.server = make_server(self.hub, '127.0.0.1', 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.hub.close()
        self.temp.__exit__(None, None, None)

    def request(self, path, token=None, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(self.url + path, data=data,
            headers={'Content-Type': 'application/json', **({'Authorization': 'Bearer ' + token} if token else {})})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3) as response:
            return json.load(response)

    def test_only_admin_enrolls_worker_and_export_preserves_identity(self):
        payload = {'node_id': 'new-worker', 'hub_url': self.url}
        with self.assertRaises(urllib.error.HTTPError) as failed:
            self.request('/api/enroll', payload=payload)
        self.assertEqual(failed.exception.code, 401)
        self.assertEqual(self.hub.config['nodes'], {})
        admin = self.hub.config['admin_token']
        pairing = self.request('/api/enroll', admin, payload)
        self.assertNotIn(admin, json.dumps(pairing))
        self.assertEqual(self.request('/api/enroll', admin, payload), pairing)
        self.assertEqual(self.request('/api/node-info', pairing['token']), {'node_id': 'new-worker', 'paired': True})
        with self.assertRaises(urllib.error.HTTPError) as failed:
            self.request('/api/enroll', pairing['token'], dict(payload, node_id='other'))
        self.assertEqual(failed.exception.code, 403)
        self.assertNotIn('other', self.hub.config['nodes'])
        with self.assertRaises(urllib.error.HTTPError):
            self.request('/api/state', pairing['token'])

    def test_invalid_enrollment_is_not_persisted(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.request('/api/enroll', self.hub.config['admin_token'], {'node_id': 'valid', 'hub_url': 'file:///x'})
        self.assertEqual(self.hub.config['nodes'], {})

    def test_reopen_only_when_saved_credential_matches_and_fragment_not_query(self):
        atomic_json(self.root / 'launcher.json', {'port': self.server.server_address[1]})
        self.assertTrue(reopen_controller(self.root, False))
        url = browser_url(self.root, self.server.server_address[1])
        self.assertIn('/#token=', url)
        self.assertNotIn('?token=', url)
        with temporary_directory() as other:
            atomic_json(Path(other) / 'launcher.json', {'port': self.server.server_address[1]})
            atomic_json(Path(other) / 'hub.json', {'admin_token': 'incorrect'})
            self.assertFalse(reopen_controller(other, False))

    def test_template_credentials_and_malformed_values_are_not_accepted(self):
        worker = self.hub.add_node('worker')
        for template in (None, 5, {'token': 'private'}, {'backend': 'other'}):
            with self.subTest(template=template), self.assertRaises(urllib.error.HTTPError):
                self.request('/api/sync', worker, {'node_id': 'worker', 'reports': [],
                    'snapshot': {'task_templates': [template]}})


class WorkerSetupTests(unittest.TestCase):
    def configured(self, root):
        setup = WorkerSetup(root, PAIR)
        setup.gpus = [GPU]
        setup.repo = root / 'repo'
        setup.repo.mkdir()
        setup.commit = 'b' * 40
        with patch('expman.agent._free_ram_mb', return_value=10000), patch('os.cpu_count', return_value=8):
            config = setup.configure(IMAGE, [GPU])
        return setup, config

    def test_auto_config_has_local_paths_selected_uuid_and_valid_task(self):
        with temporary_directory() as path:
            root = Path(path)
            _, config_path = self.configured(root)
            config = read_json(config_path)
            self.assertEqual(config['policy']['max_running'], 1)
            self.assertEqual(config['policy']['ram_budget_mb'], 7500)
            self.assertEqual(config['gpu_policy'][GPU['uuid']]['max_jobs'], 1)
            self.assertEqual(config['task_templates'][0]['source']['repo'], str(root / 'repo'))
            self.assertEqual(config['task_templates'][0]['environments'][0]['image'], IMAGE)
            self.assertNotIn(PAIR['token'], json.dumps(config['task_templates']))

    def test_software_upgrade_reuses_enrollment_and_keeps_gpu_and_resource_preferences(self):
        with temporary_directory() as path:
            root = Path(path)
            _, config_path = self.configured(root)
            config = read_json(config_path)
            config['tags'] = ['research', 'do-not-reset']
            config['policy'].update(max_running=3, cpu_budget=7, ram_budget_mb=6000)
            config['gpu_policy'][GPU['uuid']].update(max_jobs=2, reserve_mb=3072)
            atomic_json(config_path, config)
            atomic_json(root / 'pairing.json', PAIR)
            state_path = root / 'setup-state.json'
            state = read_json(state_path)
            state.update(configured_version='0.1.0', docker_endpoint='unix:///var/run/docker.sock')
            atomic_json(state_path, state)
            before_config, before_pairing, before_state = (config_path.read_bytes(),
                (root / 'pairing.json').read_bytes(), state_path.read_bytes())
            args = argparse.Namespace(root=str(root), pairing=None, configure=False, gpu=[], prepare_only=False)
            with patch.dict(os.environ), patch('expman.worker_setup.sys.platform', 'linux'), \
                    patch('os.geteuid', return_value=1000, create=True), \
                    patch('expman.worker_setup.WorkerSetup', side_effect=AssertionError('A software upgrade must not rebuild/reconfigure GPU runtimes')) as setup, \
                    patch('expman.agent.run') as agent:
                start_worker(args)
                setup.assert_not_called()
                agent.assert_called_once_with(str(config_path))
                self.assertEqual(os.environ.get('DOCKER_HOST'), 'unix:///var/run/docker.sock')
            self.assertEqual(config_path.read_bytes(), before_config)
            self.assertEqual((root / 'pairing.json').read_bytes(), before_pairing)
            self.assertEqual(state_path.read_bytes(), before_state)

    def test_registry_uses_push_digest_and_refuses_an_unrelated_container(self):
        with temporary_directory() as path:
            setup = WorkerSetup(path, PAIR)
            setup.source_id = 'c' * 64
            setup.build_root = Path(path)
            calls = []
            def command(argv, **kwargs):
                calls.append(argv)
                if argv[1:3] == ['container', 'inspect']:
                    return 1, ''
                if argv[1] == 'push':
                    return 0, 'tag: digest: sha256:' + 'a' * 64 + ' size: 856'
                return 0, ''
            with patch.object(setup, 'command', side_effect=command):
                self.assertEqual(setup.image(), IMAGE)
            self.assertEqual(calls[-1], ['docker', 'pull', IMAGE])
            unrelated = [{'Config': {'Labels': {}}, 'HostConfig': {}, 'State': {'Running': True}}]
            with patch.object(setup, 'command', return_value=(0, json.dumps(unrelated))), self.assertRaises(ValueError):
                setup.image()

    def test_gpu_check_passes_both_selection_mechanisms_and_rejects_wrong_uuid(self):
        with temporary_directory() as path:
            setup = WorkerSetup(path, PAIR)
            with patch('os.getuid', return_value=1000, create=True), patch('os.getgid', return_value=1000, create=True):
                with patch.object(setup, 'command', return_value=(0, json.dumps({'status': 'passed', 'gpu_uuid': GPU['uuid']}))) as command:
                    setup.verify(IMAGE, [GPU])
                argv = command.call_args.args[0]
                self.assertIn('device=' + GPU['uuid'], argv)
                self.assertIn('CUDA_VISIBLE_DEVICES=' + GPU['uuid'], argv)
                self.assertIn('1000:1000', argv)
                with patch.object(setup, 'command', return_value=(0, '{"status":"passed","gpu_uuid":"wrong"}')), self.assertRaises(ValueError):
                    setup.verify(IMAGE, [GPU])

    def test_register_project_saves_template_without_changing_repository(self):
        with temporary_directory() as path:
            root = Path(path)
            setup, config_path = self.configured(root)
            repository = root / 'science'
            repository.mkdir()
            source = repository / 'train.py'
            source.write_text('print("science")\n')
            def git(argv, **kwargs):
                suffix = argv[3:]
                output = str(repository) if suffix == ['rev-parse', '--show-toplevel'] else ('d' * 40 if suffix == ['rev-parse', 'HEAD'] else '')
                return subprocess.CompletedProcess(argv, 0, output, '')
            with patch('expman.project_setup.subprocess.run', side_effect=git):
                task = register(root, 'science', str(repository), ['python', 'train.py'])
            self.assertEqual(task['source']['commit'], 'd' * 40)
            self.assertEqual(source.read_text(), 'print("science")\n')
            self.assertEqual(len(read_json(config_path)['task_templates']), 2)
            before = config_path.read_bytes()
            with InstanceLock(root / 'runtime' / 'agent.lock'), patch('expman.project_setup.subprocess.run', side_effect=git):
                with self.assertRaises(RuntimeError):
                    register(root, 'second', str(repository), ['python', 'train.py'])
            self.assertEqual(config_path.read_bytes(), before)


class NativeDesktopBuildTests(unittest.TestCase):
    def test_native_compilation_embeds_role_and_optional_payload_without_a_shell(self):
        with temporary_directory() as path:
            root = Path(path)
            compiler = root / 'csc.exe'
            compiler.write_bytes(b'fixture compiler')
            payload = root / 'payload.zip'
            payload.write_bytes(b'fixture zip')
            captured = []
            def run(argv, **kwargs):
                captured.append(argv)
                self.assertNotIn('shell', kwargs)
                resource = next(value for value in argv if value.startswith('/resource:') and value.endswith(',Role'))
                self.assertEqual(Path(resource[len('/resource:'):-len(',Role')]).read_text(encoding='utf-8'), 'worker')
                self.assertIn('/resource:' + str(payload) + ',AppPayload', argv)
                target = Path(next(value[len('/out:'):] for value in argv if value.startswith('/out:')))
                target.write_bytes(b'MZcompiled-test-fixture')
                return subprocess.CompletedProcess(argv, 0, '', '')
            with patch.object(build_desktop.subprocess, 'run', side_effect=run):
                target = build_desktop.compile_desktop(root / 'Worker Setup.exe', 'worker', payload, compiler)
            self.assertEqual(target.read_bytes(), b'MZcompiled-test-fixture')
            self.assertIn('/target:winexe', captured[0])
            self.assertIn('/platform:x64', captured[0])
            self.assertFalse(list(root.glob('.desktop-build-*')))
            with self.assertRaises(FileExistsError):
                build_desktop.compile_desktop(target, 'worker', compiler=compiler)

    def test_compiler_failure_does_not_produce_a_placeholder_executable(self):
        with temporary_directory() as path:
            root = Path(path)
            compiler = root / 'csc.exe'
            compiler.write_bytes(b'fixture compiler')
            target = root / 'Center.exe'
            with patch.object(build_desktop.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'syntax problem')):
                with self.assertRaisesRegex(RuntimeError, 'syntax problem'):
                    build_desktop.compile_desktop(target, 'controller', compiler=compiler)
            self.assertFalse(target.exists())
            self.assertFalse(list(root.glob('.desktop-build-*')))
            with self.assertRaisesRegex(RuntimeError, 'does not exist'):
                build_desktop.find_compiler(root / 'missing-compiler.exe')


class ReleaseArchiveTests(unittest.TestCase):
    def test_public_inputs_exclude_runtime_and_private_documents(self):
        files = build_release.application_files()
        self.assertIn('README.zh-CN.md', files)
        self.assertIn('expman/launcher.py', files)
        self.assertFalse(any('.runtime' in name or '验证报告' in name or name.endswith('.pairing.json') for name in files))
        for data in files.values():
            for forbidden in (b'DESKTOP-PRIVATE', b'private-node-token-for-test', b'PRIVATE_DATASET_CONTENT'):
                self.assertNotIn(forbidden, data)

    def test_runtime_rejects_wrong_hash(self):
        with temporary_directory() as path:
            archive = Path(path) / 'bad.zip'
            archive.write_bytes(b'invalid runtime')
            with self.assertRaisesRegex(ValueError, 'hash'):
                build_release.python_runtime(archive)

    def test_zip_and_installer_assets_have_role_specific_entries_manifests_and_licenses(self):
        with temporary_directory() as path:
            root = Path(path)
            runtime = root / 'runtime.zip'
            with zipfile.ZipFile(runtime, 'w') as archive:
                archive.writestr('python.exe', b'fixture')
                archive.writestr('LICENSE.txt', b'fixture license')
                archive.writestr('python313._pth', b'original')
            calls = []
            def compile_native(output, role, payload=None, compiler=None):
                calls.append((role, payload))
                target = Path(output)
                target.write_bytes(b'MZfixture-' + role.encode() +
                                   (hashlib.sha256(Path(payload).read_bytes()).digest() if payload else b''))
                return target
            with patch.object(build_release, 'PYTHON_SHA256', hashlib.sha256(runtime.read_bytes()).hexdigest()), \
                 patch.object(build_release, 'find_compiler', return_value=root / 'fake-csc.exe'), \
                 patch.object(build_release, 'compile_desktop', side_effect=compile_native):
                build_release.build(root / 'dist', runtime)
            archives = list((root / 'dist').glob('*.zip'))
            self.assertEqual(len(archives), 3)
            for path in archives:
                with zipfile.ZipFile(path) as archive:
                    names = set(archive.namelist())
                    manifest = json.loads(archive.read('manifest.json'))
                    self.assertEqual(names, set(manifest) | {'manifest.json'})
                    for name, expected in manifest.items():
                        data = archive.read(name)
                        self.assertEqual({'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}, expected)
                    self.assertIn('LICENSE', names)
                    if 'controller' in path.name:
                        self.assertIn('ExperimentCenter.exe', names)
                        self.assertIn(b'Entry point: ExperimentCenter.exe', archive.read('START-HERE.txt'))
                        self.assertIn('runtime/LICENSE.txt', names)
                        self.assertIn('Start-Controller.cmd', names)
                        self.assertNotIn('Start-Worker.cmd', names)
                        self.assertIn(b'..\n', archive.read('runtime/python313._pth'))
                    elif 'windows' in path.name:
                        self.assertIn('ExperimentWorker.exe', names)
                        self.assertIn('Client-Worker.sh', names)
                        self.assertIn(b'Entry point: ExperimentWorker.exe', archive.read('START-HERE.txt'))
                        self.assertIn('Start-Worker.cmd', names)
                        self.assertNotIn('runtime/python.exe', names)
                    else:
                        self.assertIn('Install-Worker.sh', names)
                        self.assertIn('Stop-Worker.sh', names)
                        self.assertIn('Worker-Status.sh', names)
                        self.assertIn(b'Entry point: Install-Worker.sh', archive.read('START-HERE.txt'))
                        self.assertIn('Start-Worker.sh', names)
                        self.assertFalse(any(name.endswith('.cmd') for name in names))
            installers = list((root / 'dist').glob('*-Setup.exe'))
            self.assertEqual(len(installers), 2)
            self.assertEqual([role for role, payload in calls if payload], ['controller', 'worker'])
            for role, payload in calls:
                if payload:
                    self.assertTrue(Path(payload).is_file())
            sums = (root / 'dist/SHA256SUMS.txt').read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(sums), 5)
            for row in sums:
                digest, name = row.split('  ', 1)
                self.assertEqual(digest, hashlib.sha256((root / 'dist' / name).read_bytes()).hexdigest())
            info = json.loads((root / 'dist/build-info.json').read_text(encoding='utf-8'))
            self.assertEqual(info['artifact_count'], 5)
            self.assertFalse(info['windows_code_signed'])

    def test_release_fails_before_assets_when_native_compiler_unavailable(self):
        with temporary_directory() as path:
            output = Path(path) / 'dist'
            with patch.object(build_release, 'python_runtime', return_value={}), \
                 patch.object(build_release, 'find_compiler', side_effect=RuntimeError('native compiler unavailable')):
                with self.assertRaisesRegex(RuntimeError, 'native compiler unavailable'):
                    build_release.build(output, 'unused-runtime.zip')
            self.assertEqual(list(output.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
