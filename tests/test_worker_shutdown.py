"""Tray shutdown saves/stops local work and leaves durable reports for next start."""
import copy
import json
import subprocess
import unittest
from unittest.mock import Mock, patch

from expman import common, worker_service as service
from expman.launcher import InstanceLock
from tests import test_agent


class WorkerShutdownTests(unittest.TestCase):
    setUp = test_agent.AgentTests.setUp
    tearDown = test_agent.AgentTests.tearDown
    record = test_agent.AgentTests.record
    docker_spec = test_agent.AgentTests.docker_spec

    def owner(self, *, supported=True):
        self.service_root = self.folder / 'client'
        common.atomic_json(self.service_root / 'service.json', {
            'config': str(self.config), 'node_id': 'test', 'backend': 'detached'})
        service._write_status(self.service_root, pid=1234, process_identity='owned-identity',
            status='online', online=True, version='0.3.0rc1' if not supported else 'next',
            shutdown_protocol=1 if supported else None, shutdown_process_identity='owned-identity')

    def checkpoint(self, record):
        path = self.agent._output(record) / 'saved.bin'
        path.write_bytes(b'saved experiment checkpoint')
        common.atomic_json(path.parent / 'checkpoint.json', {'path': path.name, 'sha256': common.sha256_file(path)})

    def test_shutdown_is_asynchronous_and_repeated_requests_are_idempotent(self):
        self.owner()
        with patch.object(service, '_require_linux'), \
                patch.object(service, '_process_identity', return_value='owned-identity'), \
                patch.object(service.os, 'kill') as kill:
            result = service.shutdown(self.service_root)
            request = common.read_json(self.service_root / 'shutdown-request.json')
            again = service.shutdown(self.service_root)
        self.assertEqual(result['status'], 'shutting_down')
        self.assertTrue(result['running'])
        self.assertEqual(again['status'], 'shutting_down')
        self.assertEqual(common.read_json(self.service_root / 'shutdown-request.json'), request)
        kill.assert_not_called()

    def test_old_backend_cannot_silently_stop_the_agent_and_leave_experiments(self):
        self.owner(supported=False)
        with patch.object(service, '_require_linux'), \
                patch.object(service, '_process_identity', return_value='owned-identity'), \
                patch.object(service.os, 'kill') as kill:
            result = service.shutdown(self.service_root)
        self.assertTrue(result['running'])
        self.assertEqual(result['status'], 'exit_failed')
        self.assertTrue(result['manual_shutdown_required'])
        self.assertFalse((self.service_root / 'shutdown-request.json').exists())
        kill.assert_not_called()

    def test_capability_from_previous_process_does_not_authorize_shutdown(self):
        self.owner()
        service._write_status(self.service_root, shutdown_process_identity='old-process')
        with patch.object(service, '_require_linux'), \
                patch.object(service, '_process_identity', return_value='owned-identity'):
            result = service.shutdown(self.service_root)
        self.assertEqual(result['status'], 'exit_failed')
        self.assertTrue(result['manual_shutdown_required'])

    def test_shutdown_during_environment_preparation_requests_owned_cancellation(self):
        self.owner()
        service._write_status(self.service_root, status='preparing')
        with patch.object(service, '_require_linux'), \
                patch.object(service, '_process_identity', return_value='owned-identity'), \
                patch.object(service.os, 'kill') as kill:
            result = service.shutdown(self.service_root)
        self.assertTrue(result['running'])
        self.assertEqual(result['status'], 'shutting_down')
        self.assertTrue((self.service_root / 'shutdown-request.json').exists())
        kill.assert_not_called()

    def test_stopped_supervisor_uses_current_package_only_to_drain_surviving_work(self):
        self.owner()
        self.agent.close()
        service._save_settings(self.service_root, {**service._settings(self.service_root), 'package_dir': '/old-rc1'})
        with patch.object(service, '_require_linux'), patch.object(service.subprocess, 'Popen') as spawn:
            result = service.shutdown(self.service_root)
            again = service.shutdown(self.service_root)
        self.assertEqual(result['status'], 'shutting_down')
        self.assertEqual(again['status'], 'shutting_down')
        self.assertEqual(spawn.call_count, 1)
        self.assertIn('--shutdown-only', spawn.call_args.args[0])
        self.assertNotEqual(spawn.call_args.kwargs['cwd'], '/old-rc1')

    def test_shutdown_only_supervisor_sets_request_before_agent_can_tick(self):
        self.owner()
        self.agent.close()
        service._write_status(self.service_root, stop_requested=True)

        def run(root, config):
            self.assertTrue(service._shutdown_requested(root))

        with patch.object(service, '_require_linux'), \
                patch.object(service, '_process_identity', return_value='owned-identity'), \
                patch.object(service, '_run_agent', side_effect=run):
            self.assertEqual(service.serve(self.service_root, shutdown_only=True), 0)
        self.assertEqual(common.read_json(self.service_root / 'status.json')['status'], 'stopped')

    def test_drain_preserves_unstarted_work_and_never_syncs_or_starts(self):
        self.owner()
        record = self.record('ready')
        with patch.object(self.agent, '_sync') as sync, patch.object(self.agent, '_start') as start, \
                patch.object(self.agent, '_prepare') as prepare:
            self.assertTrue(service._shutdown_step(self.service_root, self.agent))
        saved = self.agent.records()[0]
        self.assertEqual(saved['state'], 'ready')
        self.assertEqual(saved['command_ack'], 0)
        self.assertEqual(saved['seq'], record['seq'])
        self.assertGreater(common.read_json(self.service_root / 'status.json')['shutdown']['pending_reports'], 0)
        sync.assert_not_called()
        start.assert_not_called()
        prepare.assert_not_called()

    def test_running_experiment_waits_for_exit_and_verifies_checkpoint(self):
        self.owner()
        record = self.record('running')
        self.agent._save(record, ever_started=True)
        self.checkpoint(record)
        process = Mock()
        process.poll.side_effect = [None, 0]
        self.agent.processes[self.job_id] = process
        self.assertFalse(service._shutdown_step(self.service_root, self.agent))
        pending = self.agent.records()[0]
        self.assertEqual(pending['state'], 'running')
        self.assertTrue((self.agent._output(record) / 'STOP').exists())
        self.assertTrue(service._shutdown_step(self.service_root, self.agent))
        saved = self.agent.records()[0]
        self.assertEqual(saved['state'], 'paused')
        self.assertEqual(saved['stop_at'], pending['stop_at'])
        self.assertFalse(saved['archive_scanned'])
        self.assertTrue(self.agent._checkpoint(saved))

    def test_unresumable_work_is_interrupted_even_if_checkpoint_file_exists(self):
        self.owner()
        spec = copy.deepcopy(self.spec)
        spec['resume_supported'] = False
        record = self.record('running', spec)
        self.agent._save(record, ever_started=True)
        self.checkpoint(record)
        self.agent.processes[self.job_id] = Mock(poll=Mock(return_value=0))
        self.assertTrue(service._shutdown_step(self.service_root, self.agent))
        saved = self.agent.records()[0]
        self.assertEqual(saved['state'], 'interrupted')
        self.assertIn('no configured native resume', saved['detail'])

    def test_repeated_exit_after_resume_stops_the_new_attempt(self):
        self.owner()
        record = self.record('running')
        self.agent._save(record, ever_started=True)
        self.checkpoint(record)
        self.agent.processes[self.job_id] = Mock(poll=Mock(return_value=0))
        self.assertTrue(service._shutdown_step(self.service_root, self.agent))
        record = self.agent.records()[0]
        self.agent._command(record, 'resume', 1)
        self.agent._save(record, state='running', ever_started=True)
        self.agent.processes[self.job_id] = Mock(poll=Mock(return_value=None))
        self.assertFalse(service._shutdown_step(self.service_root, self.agent))
        saved = self.agent.records()[0]
        self.assertEqual(saved['exit_requested_attempt'], 2)
        self.assertIsNotNone(saved['stop_at'])
        self.assertTrue((self.agent._output(saved) / 'STOP').exists())
        self.agent.processes.clear()

    def test_unresumable_queued_work_can_start_normally_after_shutdown(self):
        self.owner()
        spec = copy.deepcopy(self.spec)
        spec['resume_supported'] = False
        self.record('ready', spec)
        self.assertTrue(service._shutdown_step(self.service_root, self.agent))
        self.assertEqual(self.agent.records()[0]['state'], 'ready')
        with patch.object(self.agent, '_sync'), patch.object(self.agent, '_start') as start, \
                patch.object(self.agent, '_reconcile'), \
                patch('expman.agent.scheduler.select_device', return_value={'gpu_uuid': None, 'environment': None}):
            self.agent.tick()
        start.assert_called_once()

    def test_reconcile_failure_keeps_shutdown_pending_and_retry_can_finish(self):
        self.owner()
        record = self.record('running', self.docker_spec())
        self.agent._save(record, container_name='owned-container', ever_started=True)
        with patch.object(self.agent, '_inspect', side_effect=RuntimeError('Docker unavailable')):
            self.assertFalse(service._shutdown_step(self.service_root, self.agent))
        self.assertEqual(common.read_json(self.service_root / 'status.json')['status'], 'exit_failed')
        with patch.object(self.agent, '_inspect', return_value={'State': {'Status': 'exited', 'ExitCode': 0}}), \
                patch('expman.agent.subprocess.run'), patch.object(service, '_update_containers', return_value=[]):
            self.assertTrue(service._shutdown_step(self.service_root, self.agent))
        self.assertEqual(self.agent.records()[0]['state'], 'interrupted')

    def test_new_assignments_received_in_flight_are_not_started_after_exit_request(self):
        self.owner()
        record = self.record('ready')
        requested = False

        def sync(snapshot):
            nonlocal requested
            requested = True

        with patch.object(self.agent, '_sync', side_effect=sync), patch.object(self.agent, '_start') as start, \
                patch.object(self.agent, '_prepare') as prepare, patch.object(self.agent, 'snapshot', return_value={}):
            self.agent.tick(stop_requested=lambda: requested)
        self.assertEqual(self.agent.records()[0]['state'], 'ready')
        start.assert_not_called()
        prepare.assert_not_called()

    def test_run_loop_drains_without_a_normal_tick_and_persists_reports(self):
        self.owner()
        self.record('ready')
        common.atomic_json(self.service_root / 'shutdown-request.json', {
            'request_id': 'exit', 'pid': 1234, 'process_identity': 'owned-identity'})
        with patch('expman.agent.Agent', return_value=self.agent), patch.object(self.agent, 'tick') as tick:
            service._run_agent(self.service_root, str(self.config))
        tick.assert_not_called()
        self.assertTrue(self.agent.closed)
        self.assertGreater(common.read_json(self.service_root / 'status.json')['shutdown']['pending_reports'], 0)

    def test_terminal_record_with_live_owned_container_is_stopped_before_exit(self):
        self.owner()
        record = self.record('interrupted', self.docker_spec())
        self.agent._save(record, container_name='owned-container', ever_started=True)
        container = {'State': {'Status': 'running', 'Running': True}}
        with patch.object(self.agent, '_inspect', return_value=container):
            self.assertFalse(service._shutdown_step(self.service_root, self.agent))
        self.assertTrue((self.agent._output(record) / 'STOP').exists())
        self.assertEqual(self.agent.records()[0]['state'], 'interrupted')
        container['State'] = {'Status': 'exited', 'Running': False}
        with patch.object(self.agent, '_inspect', return_value=container), \
                patch.object(service, '_update_containers', return_value=[]):
            self.assertTrue(service._shutdown_step(self.service_root, self.agent))

    def test_manual_install_preserves_pending_reports_and_queued_work(self):
        self.owner()
        self.record('ready')
        self.agent.close()
        with patch.object(service, '_update_containers', return_value=[]):
            self.assertTrue(service.install_status(self.service_root)['ready_for_install'])
            self.assertFalse(service.update_status(self.service_root)['ready_for_update'])

    def test_manual_install_blocks_live_processes_and_containers(self):
        self.owner()
        with patch.object(service, '_process_identity', return_value='owned-identity'):
            self.assertFalse(service.install_status(self.service_root)['ready_for_install'])
        self.agent.close()
        with patch.object(service, '_update_containers', return_value=['live-owned-container']):
            self.assertFalse(service.install_status(self.service_root)['ready_for_install'])

    def test_recovered_created_container_does_not_start_when_exit_arrives_during_probe(self):
        self.owner()
        record = self.record('starting', self.docker_spec())
        self.agent._save(record, container_name='owned-container', gpu_uuid='GPU-test')
        requested = False

        def snapshot():
            nonlocal requested
            requested = True
            return {'gpus': [{'uuid': 'GPU-test'}]}

        with patch.object(self.agent, '_inspect', return_value={'State': {'Status': 'created'}}), \
                patch.object(self.agent, 'snapshot', side_effect=snapshot), \
                patch('expman.agent.scheduler.select_device', return_value={}), \
                patch.object(self.agent, '_exec') as execute:
            self.agent._reconcile(record, stop_requested=lambda: requested)
        execute.assert_not_called()
        self.assertEqual(record['state'], 'starting')

    def test_exit_during_docker_create_preserves_unstarted_unresumable_job(self):
        self.owner()
        spec = self.docker_spec()
        spec['resume_supported'] = False
        record = self.record('ready', spec)
        environment = record['spec']['environments'][0]
        record['cached_environments'] = [{**environment, 'image_id': 'sha256:cached'}]
        requested = False
        calls = []

        def execute(argv, **kwargs):
            nonlocal requested
            calls.append(argv)
            if argv[:2] == ['docker', 'create']:
                requested = True
            value = json.dumps([{'Id': 'sha256:cached'}]) if argv[:3] == ['docker', 'image', 'inspect'] else ''
            return subprocess.CompletedProcess(argv, 0, value, '')

        with patch('expman.agent.sys.platform', 'linux'), \
                patch('expman.agent.os.getuid', return_value=1234, create=True), \
                patch('expman.agent.os.getgid', return_value=5678, create=True), \
                patch.object(self.agent, '_verify_checkout'), patch.object(self.agent, '_exec', side_effect=execute):
            self.agent._start(record, {'gpu_uuid': 'GPU-test', 'environment': environment},
                               stop_requested=lambda: requested)
        self.assertFalse(any(call[:2] == ['docker', 'start'] for call in calls))
        self.assertEqual(record['state'], 'starting')
        with patch.object(self.agent, '_inspect', return_value={'State': {'Status': 'created'}}), \
                patch.object(service, '_update_containers', return_value=[]):
            self.assertTrue(service._shutdown_step(self.service_root, self.agent))
        saved = self.agent.records()[0]
        self.assertEqual(saved['state'], 'starting')
        self.assertIsNone(saved.get('stop_at'))
        self.assertFalse(saved.get('ever_started', False))

    def test_interrupted_shutdown_never_reports_successful_exit(self):
        self.owner()
        self.agent.close()

        def interrupted(root, config):
            raise KeyboardInterrupt

        with patch.object(service, '_require_linux'), \
                patch.object(service, '_process_identity', return_value='owned-identity'), \
                patch.object(service, '_run_agent', side_effect=interrupted):
            service.serve(self.service_root, shutdown_only=True)
        self.assertEqual(service.status(self.service_root)['status'], 'exit_failed')

    def test_existing_cancel_is_not_downgraded_to_pause_by_shutdown(self):
        self.owner()
        record = self.record('running')
        self.agent._command(record, 'cancel', 3)
        self.agent.processes[self.job_id] = Mock(poll=Mock(return_value=0))
        self.assertTrue(service._shutdown_step(self.service_root, self.agent))
        self.assertEqual(self.agent.records()[0]['state'], 'canceled')
        self.assertEqual(self.agent.records()[0]['command_ack'], 3)

    def test_preparation_cancellation_reaches_stopped_without_starting_agent(self):
        from expman.worker_setup import SetupCancelled
        self.owner()
        self.agent.close()
        self.config.unlink()
        service._save_settings(self.service_root, {**service._settings(self.service_root),
            'worker_root': str(self.folder / 'worker'), 'pairing': str(self.folder / 'pairing.json')})

        def prepare(args, cancel_requested=None):
            self.assertFalse(cancel_requested())
            requested = service.shutdown(self.service_root)
            self.assertEqual(requested['status'], 'shutting_down')
            self.assertTrue(cancel_requested())
            raise SetupCancelled('owned preparation canceled')

        with patch.object(service, '_require_linux'), \
                patch.object(service, '_process_identity', return_value='owned-identity'), \
                patch('expman.worker_setup.start', side_effect=prepare), \
                patch.object(service, '_run_agent') as run:
            self.assertEqual(service.serve(self.service_root), 0)
        run.assert_not_called()
        saved = common.read_json(self.service_root / 'status.json')
        self.assertEqual(saved['status'], 'stopped')
        self.assertIn('准备已取消', saved['detail'])

    def failed_unconfigured_owner(self):
        self.agent.close()
        self.service_root = self.folder / 'unfinished-client'
        worker = self.folder / 'unfinished-worker'
        service._save_settings(self.service_root, {'config': str(worker / 'node.ready.json'),
                                                 'worker_root': str(worker)})
        service._write_status(self.service_root, status='failed', detail='Earlier setup failed')
        return worker

    def test_failed_unconfigured_preparation_can_finish_exiting(self):
        self.failed_unconfigured_owner()
        with patch.object(service, '_require_linux'), patch.object(service, '_spawn_supervisor') as spawn:
            result = service.shutdown(self.service_root)
        self.assertEqual(result['status'], 'stopped')
        self.assertFalse(result['running'])
        self.assertEqual(service.status(self.service_root)['status'], 'stopped')
        self.assertTrue(common.read_json(self.service_root / 'status.json')['stop_requested'])
        spawn.assert_not_called()

    def test_unconfigured_shutdown_does_not_claim_success_while_lifecycle_locks_are_held(self):
        worker = self.failed_unconfigured_owner()
        for path in (self.service_root / 'supervisor.lock', worker / 'setup.lock', worker / 'runtime' / 'agent.lock'):
            with self.subTest(lock=path.name), InstanceLock(path), patch.object(service, '_require_linux'):
                result = service.shutdown(self.service_root)
            self.assertEqual(result['status'], 'exit_failed')
            self.assertEqual(common.read_json(self.service_root / 'status.json')['status'], 'failed')

    def test_unconfigured_preparing_start_window_is_not_reported_as_stopped(self):
        self.failed_unconfigured_owner()
        service._write_status(self.service_root, status='preparing', pid=None, process_identity=None)
        with patch.object(service, '_require_linux'):
            result = service.shutdown(self.service_root)
        self.assertEqual(result['status'], 'exit_failed')
        self.assertEqual(common.read_json(self.service_root / 'status.json')['status'], 'preparing')

    def test_missing_config_with_existing_experiment_database_is_not_treated_as_fresh_setup(self):
        worker = self.failed_unconfigured_owner()
        database = worker / 'runtime' / 'node.sqlite3'
        database.parent.mkdir(parents=True)
        database.write_bytes(b'preserve existing database')
        with patch.object(service, '_require_linux'):
            result = service.shutdown(self.service_root)
        self.assertEqual(result['status'], 'exit_failed')
        self.assertEqual(database.read_bytes(), b'preserve existing database')


if __name__ == '__main__':
    unittest.main()
