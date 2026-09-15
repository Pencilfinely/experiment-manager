"""Updater admission uses durable queues, positive telemetry, and a cooperative stop."""
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

    def test_controller_needs_fresh_positive_node_proof_and_fences_new_work(self):
        hub = Hub(self.folder / 'center')
        try:
            hub.add_node('worker-one')
            self.assertTrue(hub.update_status()['ready_for_update'])  # unused enrollment
            for last_seen, snapshot in ((time.time(), {}), (time.time() - 60, {'update_quiescent': True}),
                                        (time.time(), {'update_quiescent': False})):
                with hub.transaction():
                    hub.db.execute('UPDATE nodes SET last_seen=?,snapshot=?', (last_seen, json.dumps(snapshot)))
                self.assertFalse(hub.update_status()['ready_for_update'])
            with hub.transaction():
                hub.db.execute('UPDATE nodes SET last_seen=?,snapshot=?', (time.time(), '{"update_quiescent":true}'))
            with hub.update_request():
                self.assertFalse(hub.update_status(stop=True)['ready_for_update'])
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
                                     status='online', online=True)
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
