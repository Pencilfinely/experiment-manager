import json
from pathlib import Path
import subprocess
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from expman import common, node_policy, scheduler
from expman.agent import Agent
from expman.hub import APIError, Hub, make_server
from tests.support import temporary_directory


class NodePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.folder = Path(self.temporary.__enter__())
        self.hub = Hub(self.folder / 'center')
        self.token = self.hub.add_node('worker')
        self.config_path = self.folder / 'node.ready.json'
        self.image = 'example/torch@sha256:' + 'a' * 64
        common.atomic_json(self.config_path, {'node_id': 'worker', 'hub_url': 'http://127.0.0.1:8765',
            'root': 'runtime', 'token': self.token, 'allowed_repos': ['/repo'],
            'policy': {'max_running': 1, 'max_prefetch': 2, 'cpu_budget': 4, 'ram_budget_mb': 8192},
            'profiles': {'torch': {'image': self.image, 'verified': True, 'gpu_name_patterns': ['RTX']}},
            'gpu_policy': {'GPU-enabled': {'max_jobs': 1, 'reserve_mb': 1024},
                           'GPU-disabled': {'max_jobs': 0, 'reserve_mb': 1024}}})
        self.agent = Agent(self.config_path)
        self.hub.sync('worker', {'node_id': 'worker', 'snapshot': self.snapshot(), 'reports': []})

    def tearDown(self):
        self.agent.close()
        self.hub.close()
        self.temporary.__exit__(None, None, None)

    def snapshot(self):
        def execute(argv, **kwargs):
            text = ('GPU-enabled, RTX test, 16384, 12000\nGPU-disabled, RTX test, 16384, 12000\n'
                    if argv[0] == 'nvidia-smi' else '25.0')
            return subprocess.CompletedProcess(argv, 0, text, '')
        with patch('expman.agent.sys.platform', 'linux'), \
                patch('expman.agent.os.cpu_count', return_value=8), \
                patch('expman.agent.os.sched_getaffinity', return_value=set(range(8)), create=True), \
                patch('expman.agent._total_ram_mb', return_value=32768), \
                patch('expman.agent._free_ram_mb', return_value=24000), \
                patch.object(self.agent, '_exec', side_effect=execute):
            return self.agent.snapshot()

    def request(self, revision=0, **changes):
        return {'node_id': 'worker', 'revision': revision,
                'policy': {'max_running': 8, 'max_prefetch': 16, 'cpu_budget': 8, 'ram_budget_mb': 24576},
                'gpu_policy': {'GPU-enabled': {'max_jobs': 4, 'reserve_mb': 512}}, **changes}

    def sync(self, lose_response=False):
        def request(url, token, payload, **kwargs):
            result = self.hub.sync('worker', payload)
            if lose_response:
                raise OSError('Response lost after hub commit')
            return result
        with patch('expman.agent.common.api_request', side_effect=request):
            self.agent._sync(self.snapshot())

    def test_offline_save_lost_response_retry_ack_and_restart_persist(self):
        before = self.config_path.read_bytes()
        with self.hub.transaction():
            self.hub.db.execute('UPDATE nodes SET last_seen=0')
        desired = self.hub.set_node_policy(self.request())['resource_policy']
        self.assertEqual(desired['revision'], 1)
        self.assertFalse(self.hub.state()['nodes'][0]['online'])
        self.assertEqual(self.hub.set_node_policy(self.request())['resource_policy'], desired)
        self.sync(lose_response=True)
        self.assertIsNone(self.agent.resource_policy)
        self.sync()
        self.assertEqual(self.agent.resource_policy, desired)
        self.assertEqual(self.snapshot()['policy']['max_running'], 8)
        self.assertEqual(self.snapshot()['gpus'][0]['max_jobs'], 4)
        self.assertEqual(self.hub.state()['nodes'][0]['snapshot']['resource_policy_revision'], 0)
        # Crash before the acknowledgment heartbeat; the effective overlay must
        # survive offline and be acknowledged on the next connection.
        self.agent.close()
        self.agent = Agent(self.config_path)
        self.assertEqual(self.snapshot()['policy']['cpu_budget'], 8)
        self.assertEqual(self.snapshot()['gpus'][0]['max_jobs'], 4)
        self.assertEqual(self.config_path.read_bytes(), before)
        self.sync()
        self.assertEqual(self.hub.state()['nodes'][0]['snapshot']['resource_policy_revision'], 1)
        self.hub.close()
        self.hub = Hub(self.folder / 'center')
        self.assertEqual(self.hub.state()['nodes'][0]['resource_policy'], desired)
        self.sync()
        self.assertEqual(self.agent.resource_policy, desired)

    def test_optimistic_conflict_and_reset_do_not_reenable_locally_disabled_gpu(self):
        self.hub.set_node_policy(self.request())
        with self.assertRaises(APIError) as error:
            self.hub.set_node_policy(self.request(policy={'max_running': 2}))
        self.assertEqual(error.exception.status, 409)
        self.sync()
        disabled = self.hub.set_node_policy(self.request(1, policy={}, gpu_policy={'GPU-enabled': {'max_jobs': 0}}))
        self.sync()
        gpu = self.snapshot()['gpus'][0]
        self.assertEqual(gpu['max_jobs'], 0)
        self.assertTrue(gpu['local_enabled'])
        self.hub.set_node_policy(self.request(disabled['resource_policy']['revision'], policy={}, gpu_policy={}))
        self.sync()
        snapshot = self.snapshot()
        self.assertEqual(snapshot['policy']['max_running'], 1)
        self.assertEqual(snapshot['gpus'][0]['max_jobs'], 1)
        self.assertEqual(snapshot['gpus'][1]['max_jobs'], 0)
        self.assertFalse(snapshot['gpus'][1]['local_enabled'])

    def test_hub_and_worker_reject_bad_types_bounds_and_unauthorized_gpus(self):
        invalid = [self.request(policy={key: value})
                   for key in node_policy.POLICY_LIMITS for value in (-1, True, float('nan'), float('inf'), '2')]
        invalid += [self.request(policy={'max_running': 257}), self.request(policy={'max_prefetch': 1025}),
                    self.request(policy={'max_running': 1.0}), self.request(policy={'cpu_budget': 9}),
                    self.request(policy={'ram_budget_mb': 32769}), self.request(policy={'run_enabled': True}),
                    self.request(revision=True), self.request(revision=-1),
                    self.request(gpu_policy={'GPU-disabled': {'max_jobs': 4}}),
                    self.request(gpu_policy={'GPU-unlisted': {'max_jobs': 4}})]
        invalid += [self.request(gpu_policy={'GPU-enabled': {key: value}})
                    for key in node_policy.GPU_LIMITS for value in (-1, True, 1.5, float('nan'), float('inf'))]
        invalid += [self.request(gpu_policy={'GPU-enabled': {'max_jobs': 257}}),
                    self.request(gpu_policy={'GPU-enabled': {'reserve_mb': 16385}})]
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.hub.set_node_policy(payload)
                value = {key: item for key, item in payload.items() if key != 'node_id'}
                if isinstance(value['revision'], int) and not isinstance(value['revision'], bool) and value['revision'] == 0:
                    value['revision'] = 1
                with self.assertRaises(ValueError):
                    self.agent._accept_resource_policy(value, self.snapshot())
        self.assertIsNone(self.hub.state()['nodes'][0]['resource_policy'])
        self.assertIsNone(self.agent.resource_policy)
        self.agent.config['profiles']['torch']['verified'] = False
        snapshot = self.snapshot()
        self.assertFalse(snapshot['gpus'][0]['local_enabled'])
        with self.assertRaises(ValueError):
            self.agent._accept_resource_policy({'revision': 1, 'policy': {},
                'gpu_policy': {'GPU-enabled': {'max_jobs': 4}}}, snapshot)

    def test_changed_local_authorization_rejects_pending_policy_but_sync_continues(self):
        self.hub.set_node_policy(self.request())
        self.agent.config['gpu_policy']['GPU-enabled']['max_jobs'] = 0
        self.sync()
        self.assertTrue(self.agent.online)
        self.assertIsNone(self.agent.resource_policy)
        self.assertIn('not locally enabled', self.agent.resource_policy_error)
        self.assertEqual(self.snapshot()['gpus'][0]['max_jobs'], 0)
        # Clearing the invalid desired GPU override is delivered normally.
        self.hub.set_node_policy(self.request(1, gpu_policy={}))
        self.sync()
        self.assertEqual(self.agent.resource_policy['revision'], 2)
        self.assertEqual(self.agent.resource_policy_error, '')

    def test_saved_overlay_is_rebounded_when_local_hardware_or_authorization_changes(self):
        self.hub.set_node_policy(self.request())
        self.sync()
        self.agent.config['gpu_policy']['GPU-enabled']['max_jobs'] = 0
        snapshot = self.snapshot()
        self.assertEqual(snapshot['gpus'][0]['max_jobs'], 0)
        snapshot['resource_policy_limits'] = {'cpu_budget': 2, 'ram_budget_mb': 4096}
        node_policy.apply(snapshot, self.agent.resource_policy)
        self.assertEqual(snapshot['policy']['cpu_budget'], 2)
        self.assertEqual(snapshot['policy']['ram_budget_mb'], 4096)

    def test_reduced_limits_block_next_start_without_changing_running_task(self):
        self.hub.set_node_policy(self.request())
        self.sync()
        spec = common.validate_task({'backend': 'docker', 'source': {'repo': '/repo', 'commit': 'a' * 40},
            'command': ['python', 'train.py'], 'environments': [{'profile': 'torch', 'image': self.image}],
            'resources': {'cpu': 1, 'ram_mb': 1024, 'gpu_memory_mb': 2000, 'exclusive': False}})
        record = {'id': 'a' * 32, 'state': 'running', 'spec': spec, 'attempt': 1,
                  'seq': 1, 'gpu_uuid': 'GPU-enabled', 'command_ack': 0, 'metrics': {}}
        self.agent._save(record)
        before = self.agent.records()
        self.assertIsNotNone(scheduler.select_device(spec, self.snapshot(), [record]))
        desired = self.hub.set_node_policy(self.request(1, gpu_policy={'GPU-enabled': {'max_jobs': 1}}))['resource_policy']
        self.agent._accept_resource_policy(desired, self.snapshot())
        self.assertIsNone(scheduler.select_device(spec, self.snapshot(), [record]))
        self.assertEqual(self.agent.records(), before)
        self.assertFalse((self.agent._output(record) / 'STOP').exists())

    def test_legacy_workers_keep_syncing_and_cannot_receive_remote_policy(self):
        self.hub.sync('worker', {'node_id': 'worker', 'snapshot': {}, 'reports': []})
        with self.assertRaises(APIError) as error:
            self.hub.set_node_policy(self.request())
        self.assertEqual(error.exception.status, 409)
        response = self.hub.sync('worker', {'node_id': 'worker', 'snapshot': {}, 'reports': []})
        self.assertNotIn('resource_policy', response)
        self.assertIsNone(self.hub.state()['nodes'][0]['resource_policy'])

    def test_http_admin_only_and_update_stop_fences_resource_mutation(self):
        server = make_server(self.hub, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(token):
            headers = {'Content-Type': 'application/json'}
            if token:
                headers['Authorization'] = 'Bearer ' + token
            return urllib.request.urlopen(urllib.request.Request(
                f'http://127.0.0.1:{server.server_address[1]}/api/node-policy',
                data=json.dumps(self.request()).encode(), headers=headers), timeout=3)
        try:
            for token, status in ((None, 401), (self.token, 403)):
                with self.subTest(token=token), self.assertRaises(urllib.error.HTTPError) as error:
                    request(token)
                self.assertEqual(error.exception.code, status)
                error.exception.close()
            with request(self.hub.config['admin_token']) as response:
                self.assertEqual(json.load(response)['resource_policy']['revision'], 1)
            self.assertTrue(self.hub.update_status(stop=True)['ready_for_update'])
            with self.assertRaises(urllib.error.HTTPError) as error:
                request(self.hub.config['admin_token'])
            self.assertEqual(error.exception.code, 503)
            error.exception.close()
            with self.assertRaises(APIError) as error:
                self.hub.set_node_policy(self.request(1, policy={}))
            self.assertEqual(error.exception.status, 503)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == '__main__':
    unittest.main()
