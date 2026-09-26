import json
from pathlib import Path
import threading
import unittest
import urllib.error
import urllib.request

from expman.hub import Hub, make_server
from scripts.build_release import application_files
from tests.support import temporary_directory
from tests.test_hub import SPEC, SNAPSHOT


class MobileHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.root = Path(self.temp.__enter__())
        self.hub = Hub(self.root)
        self.admin = self.hub.config['admin_token']
        self.worker = self.hub.add_node('worker')
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
        request = urllib.request.Request(self.url + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json', **({'Authorization': 'Bearer ' + token} if token else {})})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5) as response:
            return json.load(response)

    def denied(self, status, path, token=None, payload=None):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(path, token, payload)
        self.assertEqual(error.exception.code, status)

    def enroll(self, permission='monitor'):
        return self.request('/api/mobile/devices', self.admin, {'name': 'Mate 70 Pro', 'permission': permission})

    def job(self):
        identity = self.hub.submit({'spec': SPEC, 'request_id': 'mobile-test'})['ids'][0]
        self.hub.sync('worker', {'node_id': 'worker', 'snapshot': SNAPSHOT, 'reports': []})
        return identity

    def test_only_admin_can_issue_and_revoke_credentials(self):
        for token in (None, self.worker):
            self.denied(401 if token is None else 403, '/api/mobile/devices', token, {'name': 'phone'})
        device = self.enroll()
        for path, payload in [('/api/mobile/devices', None), ('/api/mobile/devices', {'name':'other'}),
                              ('/api/mobile/revoke', {'id': device['id']})]:
            self.denied(401, path, device['token'], payload)
        self.request('/api/mobile/revoke', self.admin, {'id':device['id']})
        self.denied(401, '/api/mobile/state', device['token'])

    def test_mobile_tokens_never_grant_admin_or_worker_access(self):
        for permission in ('monitor', 'control'):
            token = self.enroll(permission)['token']
            for path, payload in [('/api/state', None), ('/api/local/imports', None), ('/api/ai/settings', None),
                                  ('/api/jobs', {'spec': SPEC, 'request_id': 'forbidden'}),
                                  ('/api/action', {}), ('/api/sync', {}), ('/api/enroll', {}),
                                  ('/api/node-policy', {}), ('/api/projects/delete', {})]:
                self.denied(401, path, token, payload)
        self.assertEqual(self.hub.state()['jobs'], [])
        for token in (None, self.admin, self.worker):
            self.denied(401, '/api/mobile/state', token)

    def test_hash_only_storage_expiry_and_restart(self):
        device = self.enroll('control')
        row = dict(self.hub.db.execute('SELECT * FROM mobile_devices').fetchone())
        self.assertNotIn(device['token'], json.dumps(row))
        listing = self.request('/api/mobile/devices', self.admin)
        self.assertNotIn('token', json.dumps(listing))
        other = Hub(self.root)
        try:
            self.assertEqual(other.mobile_authenticate('Bearer ' + device['token'])['id'], device['id'])
            other.db.execute('UPDATE mobile_devices SET expires=0')
            self.denied(401, '/api/mobile/state', device['token'])
        finally:
            other.close()

    def test_monitor_reads_projection_logs_metrics_and_archives(self):
        device = self.enroll()
        identity = self.job()
        self.hub.sync('worker', {'node_id': 'worker', 'snapshot': {**SNAPSHOT, 'allowed_repos':['private-source'], 'task_templates':[SPEC]},
            'reports':[{'id':identity,'seq':1,'state':'running','attempt':1,'metrics':{'step':1,'loss':0.5},'log_tail':'epoch 1\n'}]})
        state = self.request('/api/mobile/state', device['token'])
        self.assertEqual(state['session']['permission'], 'monitor')
        self.assertEqual(state['jobs'][0]['metrics']['loss'], 0.5)
        self.assertNotIn('projects', state)
        self.assertNotIn('allowed_repos', state['nodes'][0]['snapshot'])
        self.assertNotIn('task_templates', state['nodes'][0]['snapshot'])
        self.assertNotIn('source', state['jobs'][0]['spec'])
        detail = self.request('/api/mobile/job?id=' + identity, device['token'])
        self.assertEqual(detail['log_tail'], 'epoch 1\n')
        self.assertTrue(any(e['kind']=='metrics' for e in detail['events']))
        for path,payload in [('/api/mobile/action',{'job_id':identity,'action':'cancel','expected_command_id':0}),
                             ('/api/mobile/node-mode',{'node_id':'worker','mode':'drain','expected_mode':'run'})]:
            self.denied(403, path, device['token'], payload)
        self.assertEqual(self.hub.job(identity)['command_id'], 0)

    def test_control_is_revision_checked_and_reports_pending_not_completed(self):
        token = self.enroll('control')['token']
        identity = self.job()
        payload = {'job_id':identity,'action':'stop','expected_command_id':0}
        result = self.request('/api/mobile/action', token, payload)
        self.assertEqual(result['command_id'], 1)
        detail = self.request('/api/mobile/job?id='+identity, token)
        self.assertEqual(detail['command_ack'], 0)
        self.assertEqual(detail['state'], 'assigned')
        self.denied(409, '/api/mobile/action', token, payload)
        self.assertEqual(self.hub.job(identity)['command_id'], 1)
        self.hub.action({'job_id':identity, 'action':'cancel'})
        self.denied(409, '/api/mobile/action', token, {**payload, 'expected_command_id':1})

    def test_cancel_queued_and_resume_supported_jobs(self):
        token = self.enroll('control')['token']
        queued = self.hub.submit({'spec':SPEC,'request_id':'queued'})['ids'][0]
        self.request('/api/mobile/action', token, {'job_id':queued,'action':'cancel','expected_command_id':0})
        self.assertEqual(self.hub.job(queued)['state'], 'canceled')
        identity = self.job()
        self.hub.sync('worker', {'node_id': 'worker', 'snapshot':SNAPSHOT,'reports':[{'id':identity,'seq':1,'state':'paused','attempt':1}]})
        self.request('/api/mobile/action', token, {'job_id':identity,'action':'resume','expected_command_id':0})
        self.assertEqual(self.hub.job(identity)['action'], 'resume')

    def test_node_mode_revision_check_and_read_routes_cannot_mutate(self):
        token = self.enroll('control')['token']
        payload = {'node_id':'worker','mode':'drain','expected_mode':'run'}
        self.request('/api/mobile/node-mode', token, payload)
        self.denied(409, '/api/mobile/node-mode', token, payload)
        self.request('/api/mobile/node-mode', token, {**payload,'mode':'run','expected_mode':'drain'})
        self.denied(404, '/api/mobile/action', token)
        self.denied(404, '/api/mobile/state', token, {})
        self.denied(404, '/api/mobile/jobs', token, {'spec':SPEC})

    def test_bad_input_cannot_grant_privileges_or_change_state(self):
        for extra in ({'permission':'admin'},{'days':True},{'days':0},{'days':91},{'name':''}):
            self.denied(400, '/api/mobile/devices', self.admin, {'name':'phone', **extra})
        token = self.enroll('control')['token']
        identity = self.job()
        for expected in (None, True, -1, '0'):
            self.denied(400, '/api/mobile/action', token, {'job_id':identity,'action':'stop','expected_command_id':expected})
        self.denied(400, '/api/mobile/job?id='+identity+'&id='+identity, token)
        self.assertEqual(self.hub.job(identity)['command_id'], 0)

    def test_static_mobile_assets_are_packaged_and_cannot_traverse(self):
        files = application_files()
        for path, filename in [('/mobile/','index.html'),('/mobile-access','access.html'),
                               ('/mobile/mobile.js','mobile.js'),('/mobile/model.js','model.js'),
                               ('/mobile/access.js','access.js'),('/mobile/mobile.css','mobile.css')]:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(self.url+path) as response:
                self.assertEqual(response.read(), files['expman/static/mobile/'+filename])
                self.assertIn("connect-src 'self'", response.headers['Content-Security-Policy'])
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.denied(404, '/mobile/../../hub.json')


if __name__ == '__main__':
    unittest.main()
