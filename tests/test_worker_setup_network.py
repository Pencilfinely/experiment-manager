import argparse
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from expman.common import atomic_json, read_json
from expman.worker_setup import WorkerSetup, start
from tests.support import temporary_directory


PAIR = {'schema': 1, 'node_id': 'a6000', 'hub_url': 'http://127.0.0.1:18765',
        'token': 'example-worker-credential-123456789'}
GPU = {'uuid': 'GPU-11111111-1111-1111-1111-111111111111', 'name': 'RTX A6000'}
IMAGE = 'localhost:5001/expman-runtime@sha256:' + 'a' * 64


class WorkerSetupNetworkTests(unittest.TestCase):
    def setup_image(self, path, mode, existing=None):
        setup = WorkerSetup(path, PAIR, setup_network=mode)
        setup.source_id = 'c' * 64
        setup.build_root = Path(path)
        calls = []

        def command(argv, **kwargs):
            calls.append(argv)
            if argv[1:3] == ['container', 'inspect']:
                return (1, '') if existing is None else (0, json.dumps([existing]))
            if argv[1] == 'push':
                return 0, 'tag: digest: sha256:' + 'a' * 64 + ' size: 856'
            return 0, ''

        return setup, calls, command

    def test_host_setup_uses_loopback_registry_and_host_build(self):
        with temporary_directory() as path:
            setup, calls, command = self.setup_image(path, 'host')
            with patch.object(setup, 'command', side_effect=command):
                self.assertEqual(setup.image(), IMAGE)
            registry = next(c for c in calls if c[1] == 'run')
            self.assertEqual(registry[registry.index('--network') + 1], 'host')
            self.assertIn('REGISTRY_HTTP_ADDR=127.0.0.1:5001', registry)
            self.assertIn('REGISTRY_HTTP_DEBUG_ADDR=', registry)
            self.assertNotIn('-p', registry)
            build = next(c for c in calls if c[1] == 'build')
            self.assertEqual(build[build.index('--network') + 1], 'host')
            self.assertEqual(read_json(setup.state_path)['setup_network'], 'host')

    def test_default_setup_retains_bridge_port_mapping(self):
        with temporary_directory() as path:
            setup, calls, command = self.setup_image(path, None)
            with patch.object(setup, 'command', side_effect=command):
                setup.image()
            registry = next(c for c in calls if c[1] == 'run')
            self.assertIn('127.0.0.1:5001:5000', registry)
            self.assertIn('REGISTRY_HTTP_DEBUG_ADDR=', registry)
            self.assertNotIn('--network', next(c for c in calls if c[1] == 'build'))

    def test_conflicting_registry_is_never_started_or_changed(self):
        cases = [
            ('bridge', {}, []),
            ('host', {}, ['REGISTRY_HTTP_ADDR=0.0.0.0:5001']),
            ('host', {}, ['REGISTRY_HTTP_ADDR=127.0.0.1:5001', 'REGISTRY_HTTP_ADDR=0.0.0.0:5001']),
            ('host', {'5000/tcp': [{'HostIp': '', 'HostPort': '5001'}]}, ['REGISTRY_HTTP_ADDR=127.0.0.1:5001']),
        ]
        for network, ports, environment in cases:
            with self.subTest(network=network, environment=environment), temporary_directory() as path:
                existing = {'Config': {'Labels': {'expman.component': 'worker-registry'}, 'Env': environment},
                            'HostConfig': {'NetworkMode': network, 'PortBindings': ports},
                            'State': {'Running': False}}
                setup, calls, command = self.setup_image(path, 'host', existing)
                with patch.object(setup, 'command', side_effect=command), self.assertRaisesRegex(ValueError, 'left unchanged'):
                    setup.image()
                self.assertEqual(calls, [['docker', 'container', 'inspect', 'expman-worker-registry']])

    def test_owned_host_registry_can_restart(self):
        with temporary_directory() as path:
            existing = {'Config': {'Labels': {'expman.component': 'worker-registry'},
                                  'Env': ['REGISTRY_HTTP_ADDR=127.0.0.1:5001', 'REGISTRY_HTTP_DEBUG_ADDR=']},
                        'HostConfig': {'NetworkMode': 'host', 'PortBindings': {}}, 'State': {'Running': False}}
            setup, calls, command = self.setup_image(path, 'host', existing)
            with patch.object(setup, 'command', side_effect=command):
                setup.image()
            self.assertIn(['docker', 'start', 'expman-worker-registry'], calls)
            self.assertFalse(any(c[1] == 'run' for c in calls))

    def test_host_registry_debug_listener_is_rejected_before_downloads(self):
        for debug in ([], ['REGISTRY_HTTP_DEBUG_ADDR=:5001'],
                      ['REGISTRY_HTTP_DEBUG_ADDR=127.0.0.1:5003'],
                      ['REGISTRY_HTTP_DEBUG_ADDR=', 'REGISTRY_HTTP_DEBUG_ADDR=:5001']):
            with self.subTest(debug=debug), temporary_directory() as path:
                existing = {'Config': {'Labels': {'expman.component': 'worker-registry'},
                                      'Env': ['REGISTRY_HTTP_ADDR=127.0.0.1:5001', *debug]},
                            'HostConfig': {'NetworkMode': 'host'}, 'State': {'Running': True}}
                setup, calls, command = self.setup_image(path, 'host', existing)
                with patch.object(setup, 'command', side_effect=command), \
                        self.assertRaisesRegex(ValueError, 'REGISTRY_HTTP_DEBUG_ADDR='):
                    setup.image()
                self.assertEqual(calls, [['docker', 'container', 'inspect', 'expman-worker-registry']])

    def test_restarting_registry_is_rejected_before_downloads(self):
        with temporary_directory() as path:
            existing = {'Config': {'Labels': {'expman.component': 'worker-registry'},
                                  'Env': ['REGISTRY_HTTP_ADDR=127.0.0.1:5001', 'REGISTRY_HTTP_DEBUG_ADDR=']},
                        'HostConfig': {'NetworkMode': 'host'},
                        'State': {'Running': True, 'Restarting': True}}
            setup, calls, command = self.setup_image(path, 'host', existing)
            with patch.object(setup, 'command', side_effect=command), self.assertRaisesRegex(RuntimeError, 'restarting'):
                setup.image()
            self.assertEqual(calls, [['docker', 'container', 'inspect', 'expman-worker-registry']])

    def test_interrupted_preparation_remembers_mode_for_retry(self):
        with temporary_directory() as path:
            args = argparse.Namespace(root=path, pairing='fixture.json', configure=False,
                                      gpu=None, prepare_only=True, setup_network='host')
            with patch('expman.worker_setup.sys.platform', 'linux'), \
                    patch('os.geteuid', return_value=1000, create=True), \
                    patch('expman.worker_setup.load_pairing', return_value=PAIR), \
                    patch('expman.worker_setup.private_connection'), \
                    patch.object(WorkerSetup, 'prerequisites', side_effect=RuntimeError('interrupted')):
                with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                    start(args)
            self.assertEqual(WorkerSetup(path, PAIR).setup_network, 'host')

    def test_reconfigure_keeps_saved_socket_without_environment_override(self):
        for environment, expected in (({}, 'unix:///run/expman-docker.sock'),
                                      ({'DOCKER_HOST': 'unix:///run/explicit.sock'}, 'unix:///run/explicit.sock'),
                                      ({'DOCKER_CONTEXT': 'explicit'}, 'unix:///run/context.sock')):
            with self.subTest(environment=environment), temporary_directory() as path:
                atomic_json(Path(path) / 'setup-state.json',
                            {'node_id': PAIR['node_id'], 'setup_network': 'host',
                             'docker_endpoint': 'unix:///run/expman-docker.sock'})
                setup = WorkerSetup(path, PAIR)
                calls = []

                def command(argv, **kwargs):
                    calls.append(argv)
                    if argv[:3] == ['docker', 'context', 'inspect']:
                        return 0, 'unix:///run/context.sock'
                    if argv[0] == 'nvidia-smi':
                        return 0, GPU['uuid'] + ', RTX A6000, 49140, 49140'
                    return 0, ''

                with patch.dict(os.environ, environment, clear=True), \
                        patch('expman.worker_setup.sys.platform', 'linux'), \
                        patch('expman.worker_setup.shutil.which', return_value='/usr/bin/fixture'), \
                        patch('expman.worker_setup.shutil.disk_usage') as disk, \
                        patch('expman.worker_setup.authenticate'), patch.object(setup, 'command', side_effect=command):
                    disk.return_value.free = 50 * 1024**3
                    setup.prerequisites()
                self.assertEqual(setup.endpoint, expected)
                self.assertEqual(setup.state['docker_endpoint'], expected)
                if 'DOCKER_CONTEXT' not in environment:
                    self.assertFalse(any(c[:3] == ['docker', 'context', 'inspect'] for c in calls))

    def test_mode_persists_into_node_config_and_gpu_check_stays_offline(self):
        with temporary_directory() as path:
            setup = WorkerSetup(path, PAIR, setup_network='host')
            setup.repo = Path(path) / 'repo'
            setup.commit = 'b' * 40
            setup.gpus = [GPU]
            with patch('expman.agent._free_ram_mb', return_value=10000):
                config = read_json(setup.configure(IMAGE, [GPU]))
            self.assertEqual(config['setup_network'], 'host')
            setup.state_path.unlink()
            self.assertEqual(WorkerSetup(path, PAIR).setup_network, 'host')
            with patch('os.getuid', return_value=1000, create=True), patch('os.getgid', return_value=1000, create=True), \
                    patch.object(setup, 'command', return_value=(0, json.dumps({'status': 'passed', 'gpu_uuid': GPU['uuid']}))) as command:
                setup.verify(IMAGE, [GPU])
            argv = command.call_args.args[0]
            self.assertEqual(argv[argv.index('--network') + 1], 'none')

    def test_network_change_requires_explicit_reconfigure(self):
        with temporary_directory() as path:
            atomic_json(Path(path) / 'node.ready.json', {**PAIR, 'setup_network': 'bridge'})
            args = argparse.Namespace(root=path, pairing=None, configure=False, gpu=None,
                                      prepare_only=True, setup_network='host')
            with patch('expman.worker_setup.sys.platform', 'linux'), \
                    patch('os.geteuid', return_value=1000, create=True), \
                    patch('expman.worker_setup.load_pairing', return_value=PAIR), \
                    patch('expman.worker_setup.private_connection'), \
                    patch.object(WorkerSetup, 'prerequisites') as prerequisites:
                with self.assertRaisesRegex(ValueError, '--configure'):
                    start(args)
            prerequisites.assert_not_called()

    def test_invalid_mode_is_rejected_before_setup(self):
        with temporary_directory() as path:
            with self.assertRaisesRegex(ValueError, 'setup_network'):
                WorkerSetup(path, PAIR, setup_network='untrusted-network')
            self.assertFalse((Path(path) / 'setup-state.json').exists())


if __name__ == '__main__':
    unittest.main()
