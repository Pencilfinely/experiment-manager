import copy
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from expman import common, worker_gpu, worker_service as service
from expman.launcher import InstanceLock
from tests.support import temporary_directory


class WorkerGpuTests(unittest.TestCase):
    gpu = 'GPU-738c5e76-1914-9b77-77df-7a57a48b996c'
    other = 'GPU-5647b081-f9d7-1019-0a1d-a026a2a4da30'

    def setUp(self):
        self.temporary = temporary_directory()
        self.folder = Path(self.temporary.__enter__())
        self.root = self.folder / 'client'
        self.root.mkdir()
        self.runtime = self.folder / 'runtime'
        self.runtime.mkdir()
        self.path = self.folder / 'node.ready.json'
        self.config = dict(node_id='win2080', root=str(self.runtime), token='private-test-credential',
                           hub_url='http://127.0.0.1:8765', assets={'keep': '/original/data'},
                           allowed_repos=['/original/source'], tags=['old'],
                           policy={'max_running': 1, 'ram_budget_mb': 8192},
                           gpu_policy={self.gpu: {'max_jobs': 0, 'reserve_mb': 2048},
                                       self.other: {'max_jobs': 1, 'reserve_mb': 1024}},
                           profiles={'torch': {'verified': True, 'image': 'localhost:5000/runtime@sha256:'+'a'*64,
                                               'gpu_name_patterns': ['RTX 2080 Ti']}})
        common.atomic_json(self.path, self.config)
        service.select_configuration(self.root, config=str(self.path))
        self.original = self.path.read_bytes()
        self.commands = []
        self.fail = False
        self.receipt_uuid = self.gpu
        self.ready = dict(running=False, status='stopped', ready_for_update=True, detail='ready')
        self.patchers = [patch.object(service, '_require_linux'),
                         patch.object(service, 'update_status', side_effect=lambda *a: dict(self.ready)),
                         patch.object(worker_gpu.subprocess, 'run', side_effect=self.command),
                         patch.object(os, 'getuid', return_value=1000, create=True),
                         patch.object(os, 'getgid', return_value=1000, create=True)]
        for item in self.patchers:
            item.start()

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.temporary.__exit__(None, None, None)

    def command(self, argv, **kwargs):
        self.commands.append((argv, kwargs))
        if argv[0] == 'nvidia-smi':
            return subprocess.CompletedProcess(argv, 0,
                f'{self.gpu}, NVIDIA GeForce RTX 2080 Ti, 11264, 10592\n'
                f'{self.other}, NVIDIA GeForce RTX 2080 Ti, 11264, 10973\n')
        if argv[:2] == ['docker', 'run']:
            return subprocess.CompletedProcess(argv, 1 if self.fail else 0,
                json.dumps(dict(status='passed', gpu_uuid=self.receipt_uuid, gpu_name='RTX 2080 Ti')),
                'CUDA failed '+self.config['token'] if self.fail else '')
        if argv[:2] == ['docker', 'inspect']:
            return subprocess.CompletedProcess(argv, 1, '', 'No such container')
        raise AssertionError(argv)

    def enable(self):
        return worker_gpu.gpu_enable(self.root, gpu=self.gpu)

    def test_inventory_distinguishes_disabled_from_missing_runtime_without_credentials(self):
        value = worker_gpu.gpu_status(self.root)
        first, second = value['gpus']
        self.assertEqual(first['reason'], '本地未启用')
        self.assertTrue(first['can_enable'])
        self.assertFalse(first['local_enabled'])
        self.assertTrue(second['local_enabled'])
        self.assertNotIn(self.config['token'], json.dumps(value))
        self.config['profiles']['torch']['verified'] = False
        common.atomic_json(self.path, self.config)
        self.assertFalse(worker_gpu.gpu_status(self.root)['gpus'][0]['can_enable'])

    def test_success_preserves_other_configuration_and_stopped_worker(self):
        with patch.object(service, 'start') as start:
            result = self.enable()
        self.assertEqual(result['status'], 'succeeded')
        self.assertTrue(result['configuration_saved'])
        start.assert_not_called()
        expected = copy.deepcopy(self.config)
        expected['gpu_policy'][self.gpu]['max_jobs'] = 1
        self.assertEqual(common.read_json(self.path), expected)
        backups = list(self.folder.glob('node.ready.json.before-gpu-*.json'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(common.read_json(backups[0]), self.config)
        receipt = common.read_json(self.root / 'gpu-verification.json')
        self.assertEqual(receipt['gpu_uuid'], self.gpu)
        self.assertEqual(receipt['receipts'][0]['gpu_uuid'], self.gpu)
        run, options = next((a, k) for a, k in self.commands if a[:2] == ['docker', 'run'])
        self.assertIn('device='+self.gpu, run)
        self.assertIn('CUDA_VISIBLE_DEVICES='+self.gpu, run)
        self.assertIn('--pull=never', run)
        self.assertEqual(run[run.index('--network')+1], 'none')
        self.assertIn('--read-only', run)
        self.assertNotIn(self.config['token'], json.dumps(result))

    def test_all_distinct_matching_images_must_pass(self):
        self.config['profiles']['duplicate'] = copy.deepcopy(self.config['profiles']['torch'])
        self.config['profiles']['harness'] = {**self.config['profiles']['torch'],
                                             'image': 'localhost:5000/harness@sha256:'+'b'*64}
        common.atomic_json(self.path, self.config)
        self.assertEqual(self.enable()['status'], 'succeeded')
        self.assertEqual(sum(a[:2] == ['docker', 'run'] for a, k in self.commands), 2)

    def test_failed_check_preserves_config_and_redacts_error(self):
        self.fail = True
        result = self.enable()
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['configuration_saved'])
        self.assertNotIn(self.config['token'], json.dumps(result))
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse(list(self.folder.glob('*.before-gpu-*.json')))

    def test_wrong_uuid_and_missing_runtime_never_enable(self):
        self.receipt_uuid = self.other
        self.assertEqual(self.enable()['status'], 'failed')
        self.assertEqual(self.path.read_bytes(), self.original)
        self.config['profiles'] = {}
        common.atomic_json(self.path, self.config)
        self.commands.clear()
        self.assertEqual(self.enable()['status'], 'failed')
        self.assertFalse(any(a[:2] == ['docker', 'run'] for a, k in self.commands))

    def test_running_work_is_rejected_before_lifecycle_or_configuration_changes(self):
        self.ready.update(running=True, ready_for_update=False, detail='仍有实验和文件回传')
        with patch.object(service, 'stop_for_update') as stop, patch.object(service, 'start') as start:
            with self.assertRaisesRegex(RuntimeError, '实验和文件回传'):
                self.enable()
        stop.assert_not_called()
        start.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.commands, [])

    def test_idle_running_worker_is_restored_after_success_and_cuda_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                common.atomic_json(self.path, self.config)
                self.fail = fail
                running = {**self.ready, 'running': True, 'status': 'online'}
                with patch.object(service, 'update_status', side_effect=[running, self.ready]), \
                        patch.object(service, 'stop_for_update', return_value={**self.ready, 'status': 'stopping'}) as stop, \
                        patch.object(service, 'start', return_value={'status': 'starting', 'running': False}) as start:
                    result = self.enable()
                stop.assert_called_once_with(self.root)
                start.assert_called_once_with(self.root)
                self.assertEqual(result['status'], 'failed' if fail else 'succeeded')

    def test_active_agent_lock_prevents_changes(self):
        with InstanceLock(self.runtime / 'agent.lock'):
            result = self.enable()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_restart_failure_reports_saved_gpu_and_manual_start(self):
        running = {**self.ready, 'running': True, 'status': 'online'}
        with patch.object(service, 'update_status', side_effect=[running, self.ready]), \
                patch.object(service, 'stop_for_update', return_value={**self.ready, 'status': 'stopping'}), \
                patch.object(service, 'start', side_effect=RuntimeError('cannot start')):
            result = self.enable()
        self.assertTrue(result['configuration_saved'])
        self.assertIn('启动后台代理', result['detail'])
        self.assertEqual(common.read_json(self.path)['gpu_policy'][self.gpu]['max_jobs'], 1)

    def test_work_arriving_before_stop_prevents_verification(self):
        self.ready.update(running=True, status='online')
        with patch.object(service, 'stop_for_update', return_value={**self.ready, 'ready_for_update': False,
                                                                   'detail': 'new work arrived'}), \
                patch.object(service, 'status', return_value={'running': True, 'status': 'online'}), \
                patch.object(service, 'start') as start:
            result = self.enable()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.commands, [])
        start.assert_not_called()

    def test_config_change_during_check_is_preserved(self):
        def changed(*args):
            common.atomic_json(self.path, {**self.config, 'tags': ['changed']})
            return {'status': 'passed'}
        with patch.object(worker_gpu, '_verify', side_effect=changed):
            result = self.enable()
        self.assertEqual(result['status'], 'failed')
        changed_config = common.read_json(self.path)
        self.assertEqual(changed_config['tags'], ['changed'])
        self.assertEqual(changed_config['gpu_policy'][self.gpu]['max_jobs'], 0)

    def test_check_uses_saved_docker_endpoint(self):
        common.atomic_json(self.folder / 'setup-state.json', {'docker_endpoint': 'unix:///saved/docker.sock'})
        with patch.dict(os.environ, {'DOCKER_CONTEXT': 'unrelated'}):
            self.assertEqual(self.enable()['status'], 'succeeded')
        options = next(k for a, k in self.commands if a[:2] == ['docker', 'run'])
        self.assertEqual(options['env']['DOCKER_HOST'], 'unix:///saved/docker.sock')
        self.assertNotIn('DOCKER_CONTEXT', options['env'])

    def test_timeout_cleans_only_owned_probe_container(self):
        calls = []
        def timed_out(argv, **kwargs):
            calls.append(argv)
            if argv[:2] == ['docker', 'run']:
                raise subprocess.TimeoutExpired(argv, 60)
            if argv[:2] == ['docker', 'inspect']:
                return subprocess.CompletedProcess(argv, 0, 'gpu-settings-check\n')
            if argv[:3] == ['docker', 'rm', '-f']:
                return subprocess.CompletedProcess(argv, 0, '')
            return self.command(argv, **kwargs)
        with patch.object(worker_gpu.subprocess, 'run', side_effect=timed_out):
            result = self.enable()
        self.assertEqual(result['status'], 'failed')
        run = next(a for a in calls if a[:2] == ['docker', 'run'])
        name = run[run.index('--name')+1]
        self.assertIn(['docker', 'rm', '-f', name], calls)
        self.assertEqual(self.path.read_bytes(), self.original)


if __name__ == '__main__':
    unittest.main()
