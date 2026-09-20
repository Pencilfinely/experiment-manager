"""Updater admission preserves durable work and uses a cooperative local stop."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from expman import common, desktop, worker_service
from expman.agent import Agent, inspect_update_state as inspect_worker
from expman.hub import APIError, Hub
from expman.launcher import InstanceLock
from expman.update_protocol import supports_update_stop
from tests.support import temporary_directory


class UpdateReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.folder = Path(self.temporary.__enter__())
        self.service_root = self.folder / 'client'
        self.config_path = self.folder / 'worker/node.ready.json'
        self.config = {'node_id': 'worker-one', 'token': 'private-test-token',
                       'hub_url': 'http://127.0.0.1:8765', 'root': 'runtime'}

    def tearDown(self):
        self.temporary.__exit__(None, None, None)

    def worker_database(self):
        common.atomic_json(self.config_path, self.config)
        worker_service.select_configuration(self.service_root, config=str(self.config_path))
        agent = Agent(self.config_path)
        agent.close()
        return self.config_path.parent / 'runtime/node.sqlite3'

    def record(self, database, **changes):
        record = dict(id='a' * 32, state='succeeded', seq=1, archive_scanned=True)
        record.update(changes)
        with closing(sqlite3.connect(database)) as db, db:
            db.execute('INSERT OR REPLACE INTO tasks VALUES (?,?)', (record['id'], json.dumps(record)))
            db.execute('INSERT OR REPLACE INTO report_acks VALUES (?,?)', (record['id'], 1))
        return record

    def test_new_clients_allow_update_without_creating_data(self):
        center = self.folder / 'new-center'
        self.assertTrue(desktop.controller_update_status(center)['ready_for_update'])
        self.assertTrue(worker_service.update_status(self.service_root)['ready_for_update'])
        self.assertFalse(center.exists())
        self.assertFalse(self.service_root.exists())

    def test_update_stop_uses_explicit_capability_and_known_older_releases_only(self):
        for version in ('0.3.0rc2', '0.3.0rc3', '0.3.0-rc.2', '0.3.0-rc.3'):
            with self.subTest(version=version):
                self.assertTrue(supports_update_stop({'version': version}))
                self.assertFalse(supports_update_stop({'version': version, 'update_stop_protocol': 0}))
        for version in (None, '0.3.0rc1', '0.3.0-rc.1', '0.2.9', '0.4.0', 'unknown', {}):
            with self.subTest(version=version):
                self.assertFalse(supports_update_stop({'version': version}))
        self.assertTrue(supports_update_stop({'version': '0.4.0', 'update_stop_protocol': 1}))
        for protocol in (None, True, '1', 2):
            with self.subTest(protocol=protocol):
                self.assertFalse(supports_update_stop({'version': '0.3.0rc2', 'update_stop_protocol': protocol}))

    def test_old_worker_backend_requires_manual_stop_without_wait_or_signal(self):
        self.worker_database()
        worker_service._write_status(self.service_root, pid=1234, process_identity='identity',
                                     status='online', online=True, version='0.3.0rc1')
        with patch.object(worker_service, '_process_identity', return_value='identity'), \
                patch.object(worker_service, '_require_linux'), \
                patch.object(worker_service, '_update_containers') as docker, \
                patch.object(worker_service.time, 'sleep') as sleep, \
                patch.object(worker_service.os, 'kill') as signal:
            result = worker_service.stop_for_update(self.service_root)
            self.assertFalse(result['ready_for_update'])
            self.assertTrue(result['manual_stop_required'])
            self.assertEqual(result['backend_version'], '0.3.0rc1')
            self.assertIn('停止代理', result['detail'])
            self.assertNotIn('同步', result['detail'])
            docker.assert_not_called()
            sleep.assert_not_called()
            signal.assert_not_called()
        self.assertFalse((self.service_root / 'update-request.json').exists())

    def test_worker_ignores_capability_left_by_a_previous_process(self):
        self.worker_database()
        worker_service._write_status(self.service_root, pid=1234, process_identity='new-identity',
            status='online', online=True, version='0.3.0rc1', update_stop_protocol=1,
            update_stop_process_identity='old-identity')
        with patch.object(worker_service, '_process_identity', return_value='new-identity'):
            self.assertNotIn('update_stop_protocol', worker_service.status(self.service_root))
            self.assertTrue(worker_service.update_status(self.service_root)['manual_stop_required'])

    def test_unmarked_rc2_and_rc3_workers_complete_cooperative_stop(self):
        self.worker_database()
        agent = SimpleNamespace(preparation=None, project_delivery=SimpleNamespace(installing=None))
        for version in ('0.3.0rc2', '0.3.0rc3'):
            with self.subTest(version=version):
                worker_service._write_status(self.service_root, pid=1234, process_identity='identity',
                                             status='online', online=True, version=version)
                with patch.object(worker_service, '_process_identity', return_value='identity'), \
                        patch.object(worker_service, '_require_linux'), \
                        patch.object(worker_service, '_update_containers', return_value=[]), \
                        patch.object(worker_service.time, 'sleep', side_effect=lambda _: \
                            worker_service._update_stop_at_boundary(self.service_root, agent)), \
                        patch.object(worker_service.os, 'kill') as signal:
                    result = worker_service.stop_for_update(self.service_root)
                    self.assertTrue(result['ready_for_update'])
                    self.assertEqual(result['status'], 'stopping')
                    signal.assert_not_called()

    def test_old_controller_backend_requires_manual_stop_before_api_or_request(self):
        center = self.folder / 'center'
        backend = dict(running=True, status='running', managed=True, version='0.3.0rc1')
        with patch.object(desktop, 'controller_status', return_value=backend), \
                patch.object(desktop.urllib.request, 'build_opener') as opener, \
                patch.object(desktop.time, 'sleep') as sleep:
            result = desktop.controller_stop_for_update(center)
            self.assertFalse(result['ready_for_update'])
            self.assertTrue(result['manual_stop_required'])
            self.assertEqual(result['backend_version'], '0.3.0rc1')
            self.assertIn('停止主控', result['detail'])
            opener.assert_not_called()
            sleep.assert_not_called()
        self.assertFalse(center.exists())

    def test_unreachable_owned_controller_is_not_treated_as_stopped(self):
        center = self.folder / 'center'
        with InstanceLock(center / 'controller.lock'):
            result = desktop.controller_update_status(center)
        self.assertFalse(result['ready_for_update'])
        self.assertEqual(result['status'], 'unknown')

    def test_stopped_controller_reads_queued_work_without_database_recovery(self):
        center = self.folder / 'center'
        hub = Hub(center)
        with hub.transaction():
            hub.db.execute("INSERT INTO jobs(id,spec,state,created,updated) VALUES (?,'{}','queued',0,0)", ('a' * 32,))
        hub.close()
        before = (center / 'hub.json').read_bytes()
        result = desktop.controller_update_status(center)
        self.assertFalse(result['ready_for_update'])
        self.assertEqual(result['active_jobs'], 1)
        self.assertEqual((center / 'hub.json').read_bytes(), before)

    def test_controller_node_proof_is_diagnostic_and_fences_new_work(self):
        hub = Hub(self.folder / 'center')
        try:
            hub.add_node('worker-one')
            self.assertTrue(hub.update_status()['ready_for_update'])  # unused enrollment
            for last_seen, snapshot in ((time.time(), {}), (time.time() - 60, {'update_quiescent': True}),
                                        (time.time(), {'update_quiescent': False})):
                with hub.transaction():
                    hub.db.execute('UPDATE nodes SET last_seen=?,snapshot=?', (last_seen, json.dumps(snapshot)))
                result = hub.update_status()
                self.assertTrue(result['ready_for_update'])
                self.assertEqual(result['unverified_nodes'], 1)
            with hub.update_request():
                self.assertFalse(hub.update_status(stop=True)['ready_for_update'])
            hub.project_upload_locks['upload'] = object()
            self.assertFalse(hub.update_status(stop=True)['ready_for_update'])
            hub.project_upload_locks.clear()
            hub.local_imports = SimpleNamespace(threads={'import': object()})
            try:
                self.assertFalse(hub.update_status(stop=True)['ready_for_update'])
            finally:
                hub.local_imports = None
            self.assertTrue(hub.update_status(stop=True)['ready_for_update'])
            with self.assertRaises(APIError) as denied:
                with hub.transaction():
                    self.fail('A mutation entered after update stop acceptance')
            self.assertEqual(denied.exception.status, 503)
            with self.assertRaises(APIError):
                with hub.update_request():
                    self.fail('An HTTP mutation entered after update stop acceptance')
        finally:
            hub.close()

    def test_controller_offline_history_survives_accepted_update_and_restart(self):
        center = self.folder / 'center'
        hub = Hub(center)
        try:
            for node_id in ('offline-worker', 'legacy-worker'):
                hub.add_node(node_id)
            with hub.transaction():
                hub.db.execute('UPDATE nodes SET last_seen=?,snapshot=?,mode=? WHERE id=?',
                    (time.time() - 9 * 3600, '{"update_quiescent":true}', 'drain', 'offline-worker'))
                hub.db.execute('UPDATE nodes SET last_seen=?,snapshot=?,mode=? WHERE id=?',
                    (time.time(), '{}', 'drain', 'legacy-worker'))
                for job_id, state, node_id in (('a' * 32, 'succeeded', 'offline-worker'),
                                                ('b' * 32, 'failed', 'legacy-worker')):
                    hub.db.execute('INSERT INTO jobs(id,spec,state,node_id,created,updated) VALUES (?,?,?,?,?,?)',
                                   (job_id, '{"params":{"seed":42}}', state, node_id, 1, 2))
            before_nodes = [dict(row) for row in hub.db.execute('SELECT * FROM nodes ORDER BY id')]
            before_jobs = [dict(row) for row in hub.db.execute('SELECT * FROM jobs ORDER BY id')]
            identity = (center / 'hub.json').read_bytes()
            result = hub.update_status(stop=True)
            self.assertTrue(result['ready_for_update'])
            self.assertEqual(result['unverified_nodes'], 2)
            self.assertEqual(result['active_jobs'], 0)
            self.assertIn('管理端当前', result['detail'])
        finally:
            hub.close()
        # Reopening the same data root models replacing program files followed
        # by a controller restart; enrollment and job history remain unchanged.
        hub = Hub(center)
        try:
            self.assertFalse(hub.updating)
            self.assertEqual([dict(row) for row in hub.db.execute('SELECT * FROM nodes ORDER BY id')], before_nodes)
            self.assertEqual([dict(row) for row in hub.db.execute('SELECT * FROM jobs ORDER BY id')], before_jobs)
            self.assertEqual((center / 'hub.json').read_bytes(), identity)
            self.assertTrue(hub.update_status()['ready_for_update'])
        finally:
            hub.close()

    def test_controller_retains_active_command_upload_and_project_blockers(self):
        hub = Hub(self.folder / 'center')
        try:
            hub.add_node('offline-worker')
            job_id = 'a' * 32
            with hub.transaction():
                hub.db.execute('UPDATE nodes SET last_seen=?,snapshot=?',
                               (time.time() - 3600, '{"update_quiescent":true}'))
                hub.db.execute("INSERT INTO jobs(id,spec,state,node_id,created,updated) "
                               "VALUES (?,'{}','succeeded','offline-worker',0,0)", (job_id,))
                hub.db.execute("INSERT INTO projects(digest,project_id,name,size,created) VALUES ('digest','p','Project',1,0)")
            for state in ('queued', 'assigned', 'preparing', 'ready', 'starting', 'running', 'unknown'):
                with self.subTest(state=state), hub.transaction():
                    hub.db.execute('UPDATE jobs SET state=?', (state,))
                    result = hub.update_status(stop=True)
                    self.assertFalse(result['ready_for_update'])
                    self.assertEqual(result['active_jobs'], 1)
                    self.assertFalse(hub.updating)
            with hub.transaction():
                hub.db.execute("UPDATE jobs SET state='succeeded',command_id=1,command_ack=0")
                result = hub.update_status(stop=True)
                self.assertFalse(result['ready_for_update'])
                self.assertEqual(result['active_jobs'], 1)
                hub.db.execute('UPDATE jobs SET command_ack=command_id')
            cases = (
                ('upload', 'pending_uploads', "INSERT INTO uploads VALUES (?, 'digest', 1)", (job_id,), 'DELETE FROM uploads'),
                ('project-upload', 'pending_projects', "INSERT INTO project_uploads VALUES ('upload',1,NULL,NULL)", (),
                 'DELETE FROM project_uploads'),
            )
            for name, count, insert, params, cleanup in cases:
                with self.subTest(name=name), hub.transaction():
                    hub.db.execute(insert, params)
                    result = hub.update_status(stop=True)
                    self.assertFalse(result['ready_for_update'])
                    self.assertEqual(result[count], 1)
                    self.assertFalse(hub.updating)
                    hub.db.execute(cleanup)
            for state in ('queued', 'downloading', 'installing', 'delete_queued', 'deleting'):
                with self.subTest(deployment=state), hub.transaction():
                    hub.db.execute('INSERT OR REPLACE INTO project_deployments(digest,node_id,revision,status,updated) '
                                   "VALUES ('digest','offline-worker',1,?,0)", (state,))
                    result = hub.update_status(stop=True)
                    self.assertFalse(result['ready_for_update'])
                    self.assertEqual(result['pending_projects'], 1)
                    self.assertFalse(hub.updating)
            with hub.transaction():
                hub.db.execute("UPDATE project_deployments SET status='installed'")
                hub.db.execute("INSERT INTO uploads VALUES (?, 'digest', 1)", (job_id,))
                hub.db.execute("INSERT INTO artifacts VALUES (?, 'result', 'digest', 1, 0)", (job_id,))
                hub.db.execute("INSERT INTO project_uploads VALUES ('upload',1,'digest','digest')")
            result = hub.update_status()
            self.assertTrue(result['ready_for_update'])
            self.assertEqual(result['pending_uploads'], 0)
            self.assertEqual(result['pending_projects'], 0)
        finally:
            hub.close()

    def test_worker_blocks_active_unknown_unscanned_unacknowledged_and_uploading_tasks(self):
        database = self.worker_database()
        cases = ({'state': 'assigned'}, {'state': 'running'}, {'state': 'unrecognized'},
                 {'archive_scanned': False}, {'seq': 2})
        with patch.object(worker_service, '_update_containers') as docker:
            for changes in cases:
                with self.subTest(changes=changes):
                    self.record(database, **changes)
                    self.assertFalse(worker_service.update_status(self.service_root)['ready_for_update'])
            docker.assert_not_called()
        self.record(database)
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("INSERT INTO uploads(job_id,name,sha,size,path,complete) VALUES (?,'result','digest',3,'path',0)", ('a' * 32,))
        result = worker_service.update_status(self.service_root)
        self.assertFalse(result['ready_for_update'])
        self.assertEqual(result['pending_uploads'], 1)

    def test_worker_only_allows_complete_acknowledged_archives_and_no_live_containers(self):
        database = self.worker_database()
        self.record(database, state='paused')
        before = self.config_path.read_bytes()
        with patch.object(worker_service, '_update_containers', return_value=[]):
            ready = worker_service.update_status(self.service_root)
        self.assertTrue(ready['ready_for_update'])
        self.assertEqual(self.config_path.read_bytes(), before)
        with patch.object(worker_service, '_update_containers', return_value=['orphaned-container']):
            blocked = worker_service.update_status(self.service_root)
        self.assertFalse(blocked['ready_for_update'])
        self.assertEqual(blocked['active_containers'], 1)

    def test_docker_probe_preserves_endpoint_and_only_reads_owned_containers(self):
        self.worker_database()
        common.atomic_json(self.config_path.parent / 'setup-state.json', {'docker_endpoint': 'unix:///tmp/test-docker.sock'})
        candidate = worker_service._candidate(self.config_path)
        reply = subprocess.CompletedProcess([], 0, '{"ID":"stopped","State":"exited"}\n{"ID":"paused","State":"paused"}\n', '')
        with patch.object(worker_service.subprocess, 'run', return_value=reply) as run:
            self.assertEqual(worker_service._update_containers(candidate), ['paused'])
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ['docker', 'ps', '--all'])
        self.assertIn('label=expman.node=worker-one', command)
        self.assertIn('label=expman.job', command)
        self.assertEqual(run.call_args.kwargs['env']['DOCKER_HOST'], 'unix:///tmp/test-docker.sock')
        for response in (subprocess.CompletedProcess([], 1, '', 'unavailable'),
                         subprocess.CompletedProcess([], 0, '{}\n', '')):
            with patch.object(worker_service.subprocess, 'run', return_value=response):
                self.assertFalse(worker_service.update_status(self.service_root)['ready_for_update'])

    def test_worker_project_preparation_and_corrupt_database_block_update(self):
        database = self.worker_database()
        with closing(sqlite3.connect(database)) as db, db:
            db.execute('INSERT INTO metadata VALUES (?,?)', ('project_deliveries', '{"digest":{"status":"installing"}}'))
            self.assertFalse(inspect_worker(db)['ready_for_update'])
        self.assertFalse(worker_service.update_status(self.service_root)['ready_for_update'])
        with closing(sqlite3.connect(database)) as db, db:
            db.execute('DELETE FROM metadata')
            db.execute('INSERT INTO tasks VALUES (?,?)', ('a' * 32, '{}'))
        self.assertFalse(worker_service.update_status(self.service_root)['ready_for_update'])

    def test_tick_boundary_rechecks_work_and_never_signals_processes(self):
        database = self.worker_database()
        worker_service._write_status(self.service_root, pid=1234, process_identity='identity',
                                     status='online', online=True, version='0.3.0rc3')
        request = dict(request_id='first-request', pid=1234, process_identity='identity', expires=time.time() + 35)
        common.atomic_json(self.service_root / 'update-request.json', request)
        agent = SimpleNamespace(preparation=None, project_delivery=SimpleNamespace(installing=None))
        self.record(database, state='assigned')  # arrived after the UI's first check
        with patch.object(worker_service, '_process_identity', return_value='identity'), \
                patch.object(worker_service, '_update_containers', return_value=[]), \
                patch.object(worker_service.os, 'kill') as signal:
            self.assertFalse(worker_service._update_stop_at_boundary(self.service_root, agent))
            self.assertEqual(worker_service.status(self.service_root)['status'], 'online')
            self.record(database)
            common.atomic_json(self.service_root / 'update-request.json', {**request, 'request_id': 'second-request'})
            self.assertTrue(worker_service._update_stop_at_boundary(self.service_root, agent))
            signal.assert_not_called()
        reply = common.read_json(self.service_root / 'update-result.json')
        self.assertTrue(reply['ready_for_update'])
        self.assertEqual(reply['status'], 'stopping')


if __name__ == '__main__':
    unittest.main()
