import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import urllib.error
from unittest.mock import Mock, patch

from expman import common
from expman.agent import Agent
from expman.hub import APIError, Hub
from expman.project_cleanup import execute_cleanup, owned_path, plan_cleanup
from tests.support import temporary_directory
from tests import test_project_delivery as transport


class ProjectDeletionTests(unittest.TestCase):
    tearDown = transport.ProjectDeliveryTests.tearDown
    chunk = transport.ProjectDeliveryTests.chunk
    upload = transport.ProjectDeliveryTests.upload
    deploy = transport.ProjectDeliveryTests.deploy
    install = transport.ProjectDeliveryTests.install
    create_agent = transport.ProjectDeliveryTests.create_agent

    def setUp(self):
        transport.ProjectDeliveryTests.setUp(self)
        self.manifest['bundle_id'] = 'b' * 64
        self.snapshot['capabilities'].append('project-delete-v1')
        self.hub.sync('worker', {'node_id': 'worker', 'snapshot': self.snapshot})

    def sync(self, reports=None, snapshot=None):
        return self.hub.sync('worker', {'node_id': 'worker', 'snapshot': snapshot or self.snapshot,
                                      'project_reports': reports or []})

    def report(self, revision, status):
        return {'digest': self.digest, 'revision': revision, 'status': status, 'detail': status}

    def test_delete_without_jobs_hides_now_replays_after_restart_and_filters_old_worker(self):
        self.upload()
        self.deploy()
        with self.assertRaises(urllib.error.HTTPError) as error:
            common.api_request(self.url + '/api/projects/delete', self.worker_token, {'digest': self.digest})
        self.assertEqual(error.exception.code, 403)
        response = common.api_request(self.url + '/api/projects/delete', self.hub.config['admin_token'], {'digest': self.digest})
        self.assertTrue(response['deleted'])
        self.assertTrue(response['cleanup_pending'])
        self.assertEqual(self.hub.state()['projects'], [])
        self.assertFalse((self.hub.root / 'projects' / (self.digest + '.zip')).exists())
        self.assertEqual(self.sync(snapshot={'capabilities': ['project-bundle-v1']})['project_deployments'], [])
        self.hub.close()
        self.hub = Hub(self.root / 'hub')
        queue = self.sync([self.report(1, 'installed')])['project_deployments']
        self.assertEqual((queue[0]['action'], queue[0]['revision']), ('delete', 2))
        self.assertEqual(queue, self.sync()['project_deployments'])
        self.sync([self.report(2, 'deleted')])
        self.sync([self.report(2, 'deleting')])
        status = self.hub.state()['project_deletions'][0]['deployments'][0]
        self.assertEqual(status['status'], 'deleted')
        self.assertEqual(self.sync()['project_deployments'], [])

    def test_active_job_blocks_but_paused_and_failed_history_is_preserved(self):
        self.upload()
        jobs = []
        for index, state in enumerate(('paused', 'failed', 'running')):
            job = self.hub.submit({'request_id': str(index), 'spec': {'backend': 'demo'}})['ids'][0]
            spec = self.hub.job(job)['spec']
            spec['project_bundle_id'] = self.manifest['bundle_id']
            self.hub.db.execute('UPDATE jobs SET state=?,spec=? WHERE id=?', (state, json.dumps(spec), job))
            jobs.append(job)
        with self.assertRaises(APIError) as error:
            self.hub.project_delete({'digest': self.digest})
        self.assertEqual(error.exception.status, 409)
        self.hub.db.execute("UPDATE jobs SET state='canceled' WHERE id=?", (jobs[-1],))
        self.assertFalse(self.hub.project_delete({'digest': self.digest})['cleanup_pending'])
        self.assertEqual(len(self.hub.state()['jobs']), 3)
        for job in jobs[:2]:
            with self.assertRaises(APIError) as error:
                self.hub.action({'job_id': job, 'action': 'resume'})
            self.assertEqual(error.exception.status, 409)
        with self.assertRaises(APIError):
            self.hub.submit({'request_id': 'deleted', 'spec': {'backend': 'demo',
                'project_bundle_id': self.manifest['bundle_id']}})

    def test_same_bundle_aliases_share_deletion_and_failed_cleanup_requires_new_generation(self):
        self.upload()
        self.deploy()
        alias = 'c' * 64
        self.hub.db.execute('INSERT INTO projects(digest,project_id,name,size,created,bundle_id) VALUES(?,?,?,?,?,?)',
                            (alias, 'algorithm', 'Alias ZIP', 3, common.now(), self.manifest['bundle_id']))
        (self.hub.root / 'projects' / (alias + '.zip')).write_bytes(b'zip')
        self.hub.project_deploy({'digest': alias, 'node_ids': ['worker']})
        self.hub.project_delete({'digest': self.digest})
        self.assertEqual(self.hub.state()['projects'], [])
        self.assertEqual(len(self.sync()['project_deployments']), 2)
        self.sync([self.report(2, 'delete_failed')])
        self.hub.project_delete({'digest': self.digest, 'retry': True})
        queue = self.sync([self.report(2, 'deleted')])['project_deployments']
        self.assertEqual(next(item for item in queue if item['digest'] == self.digest)['revision'], 3)
        with self.assertRaises(APIError):
            self.hub.project_deploy({'digest': alias, 'node_ids': ['worker']})

    def test_controller_unlink_failure_keeps_durable_tombstone_and_recovers(self):
        self.upload()
        target = self.hub.root / 'projects' / (self.digest + '.zip')
        original_unlink = Path.unlink
        def fail(path, *args, **kwargs):
            if path == target:
                raise PermissionError('locked archive')
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail):
            response = self.hub.project_delete({'digest': self.digest})
        self.assertIn('locked archive', response['controller_cleanup_error'])
        self.assertEqual(self.hub.state()['projects'], [])
        self.assertTrue(target.exists())
        self.hub.close()
        self.hub = Hub(self.root / 'hub')
        self.assertFalse(target.exists())
        self.assertEqual(self.hub.state()['project_deletions'][0]['cleanup_error'], '')

    def test_delete_with_unpublished_draft_cleans_only_matching_import_archives(self):
        self.upload()
        imports = self.hub.root / 'imports'
        common.atomic_json(imports / ('1' * 32) / 'state.json', {'project': None})
        published = imports / ('2' * 32)
        common.atomic_json(published / 'state.json', {'project': {'digest': self.digest}})
        archive = published / 'bundle-published.zip'
        archive.write_bytes(self.body)
        draft = published / 'project.json'
        draft.write_text('{}')
        other = imports / ('3' * 32)
        common.atomic_json(other / 'state.json', {'project': {'digest': 'c' * 64}})
        other_archive = other / 'bundle-other.zip'
        other_archive.write_bytes(b'keep')

        response = common.api_request(self.url + '/api/projects/delete', self.hub.config['admin_token'],
                                      {'digest': self.digest})

        self.assertTrue(response['deleted'])
        self.assertFalse(response['cleanup_pending'])
        self.assertEqual(response['controller_cleanup_error'], '')
        self.assertFalse(archive.exists())
        self.assertTrue(draft.exists())
        self.assertEqual(other_archive.read_bytes(), b'keep')
        self.assertEqual(common.read_json(imports / ('1' * 32) / 'state.json'), {'project': None})
        replay = self.hub.project_delete({'digest': self.digest})
        self.assertFalse(replay['cleanup_pending'])

    def test_restart_finishes_deletion_when_an_unpublished_draft_exists(self):
        self.upload()
        archive = self.hub.root / 'projects' / (self.digest + '.zip')
        # Reproduce deletion intent committed before interrupted local cleanup.
        self.hub.db.execute('UPDATE projects SET deleted_at=? WHERE digest=?', (common.now(), self.digest))
        common.atomic_json(self.hub.root / 'imports' / ('1' * 32) / 'state.json', {'project': None})
        root = self.hub.root
        self.hub.close()

        self.hub = Hub(root)

        self.assertFalse(archive.exists())
        self.assertEqual(self.hub.state()['projects'], [])
        self.assertEqual(self.hub.state()['project_deletions'][0]['cleanup_error'], '')

    def test_worker_delete_before_download_survives_restart_and_lost_ack(self):
        self.upload()
        self.deploy()
        agent = self.create_agent()
        agent._sync(self.snapshot)
        self.hub.project_delete({'digest': self.digest})
        agent._sync(self.snapshot)
        for _ in range(4):
            agent.project_delivery.tick()
            if agent.project_delivery.installing:
                agent.project_delivery.installing['thread'].join(3)
        self.assertEqual(agent.project_delivery.reports()[0]['status'], 'deleted')
        agent.close()
        self.agent = Agent(agent.config_path)
        self.agent._sync(self.snapshot)
        self.assertEqual(self.hub.state()['project_deletions'][0]['deployments'][0]['status'], 'deleted')


class OwnedCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.root = Path(self.temporary.__enter__())
        self.node = self.root / 'node'
        self.node.mkdir()
        self.original = self.root / 'original' / 'model.py'
        self.original.parent.mkdir()
        self.original.write_text('original source')
        self.bundle = 'b' * 64
        self.digest = 'd' * 64
        self.job = 'a' * 32
        self.asset = 'hdata-algorithm-dataset-' + 'e' * 16
        self.image = 'localhost:5002/expman-harness@sha256:' + 'f' * 64
        self.profile = {'image': self.image, 'managed_by': 'external-harness-project',
                        'base_profile': 'base', 'image_tag': 'localhost:5002/expman-harness:' + 'f' * 24}
        self.config = {'node_id': 'worker', 'profiles': {'base': {'image': 'base@sha256:' + '1' * 64},
                       'derived': self.profile}, 'assets': {}, 'allowed_repos': [], 'task_templates': []}
        self.create_snapshot('algorithm', self.bundle)
        self.record = {'id': self.job, 'state': 'succeeded', 'attempt': 2,
                       'spec': copy.deepcopy(self.config['task_templates'][0])}
        for relative in ('worktrees/' + self.job + '/source.py', 'runs/' + self.job + '/results.json',
                         'project-downloads/' + self.digest + '.zip'):
            path = self.node / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('owned file')
        self.declaration = {'digest': self.digest, 'project_id': 'algorithm', 'bundle_id': self.bundle}

    def tearDown(self):
        self.temporary.__exit__(None, None, None)

    def create_snapshot(self, project_id, bundle_id):
        snapshot = self.node / 'distributed-projects' / project_id / bundle_id
        repo = snapshot / 'repository'
        repo.mkdir(parents=True)
        (repo / 'source.py').write_text('copied source')
        asset = self.node / 'distributed-projects/assets' / self.asset
        asset.mkdir(parents=True, exist_ok=True)
        (asset / 'dataset.txt').write_text('copied dataset')
        common.atomic_json(snapshot / '.expman-install.json', {'schema': 1, 'project_id': project_id,
            'bundle_id': bundle_id, 'assets': [self.asset], 'profiles': {'derived': self.profile}})
        self.config['assets'][self.asset] = str(asset)
        self.config['allowed_repos'].append(str(repo))
        self.config['task_templates'].append({'project_bundle_id': bundle_id, 'backend': 'docker',
            'source': {'repo': str(repo)}, 'assets': [self.asset],
            'environments': [{'profile': 'derived', 'image': self.image}]})
        cache = self.node / 'repos' / hashlib.sha256(str(repo).encode()).hexdigest()
        cache.mkdir(parents=True)
        (cache / 'objects').write_text('git cache')
        return snapshot

    def docker(self, running=False, wrong_owner=False):
        containers = {'c' * 12, 'd' * 12}
        images = {self.image, self.profile['image_tag']}
        def execute(argv, **kwargs):
            stdout = ''
            if argv[:3] == ['docker', 'ps', '-aq']:
                stdout = '\n'.join(sorted(containers))
            elif argv[:2] == ['docker', 'inspect']:
                stdout = json.dumps([{'Config': {'Labels': {'expman.job': self.job,
                    'expman.node': 'other' if wrong_owner else 'worker'}}, 'State': {'Running': running}}])
            elif argv[:2] == ['docker', 'rm']:
                containers.remove(argv[2])
            elif argv[:3] == ['docker', 'image', 'inspect'] and argv[3] not in images:
                return SimpleNamespace(returncode=1, stdout='', stderr='No such image')
            elif argv[:3] == ['docker', 'image', 'rm']:
                images.remove(argv[3])
            return SimpleNamespace(returncode=0, stdout=stdout, stderr='')
        return Mock(side_effect=execute)

    def test_remove_owned_attempts_snapshots_and_caches_preserves_original_and_results(self):
        planned = plan_cleanup(self.node, self.config, [self.record], self.declaration)
        self.assertNotIn('derived', planned['config']['profiles'])
        docker = self.docker()
        execute_cleanup(self.node, planned['plan'], docker)
        execute_cleanup(self.node, planned['plan'], docker)  # Lost ack replay.
        self.assertEqual(self.original.read_text(), 'original source')
        self.assertTrue((self.node / 'runs' / self.job / 'results.json').exists())
        self.assertFalse((self.node / 'distributed-projects/algorithm' / self.bundle).exists())
        self.assertFalse((self.node / 'worktrees' / self.job).exists())
        self.assertFalse((self.node / 'distributed-projects/assets' / self.asset).exists())
        commands = [call.args[0] for call in docker.call_args_list]
        self.assertEqual(sum(command[:2] == ['docker', 'rm'] for command in commands), 2)
        self.assertFalse(any('prune' in command or '--force' in command or '-v' in command for command in commands))

    def test_shared_assets_profiles_images_survive_until_last_project_is_removed(self):
        other_bundle = '2' * 64
        self.create_snapshot('other', other_bundle)
        other_record = {'id': '4' * 32, 'state': 'succeeded', 'attempt': 0,
                        'spec': copy.deepcopy(self.config['task_templates'][1])}
        records = [self.record, other_record]
        first = plan_cleanup(self.node, self.config, records, self.declaration)
        self.assertIn(self.asset, first['config']['assets'])
        self.assertIn('derived', first['config']['profiles'])
        self.assertEqual(first['plan']['images'], [])
        execute_cleanup(self.node, first['plan'], self.docker())
        second = plan_cleanup(self.node, first['config'], records, {'project_id': 'other',
            'bundle_id': other_bundle, 'digest': '3' * 64})
        self.assertEqual(set(second['plan']['images']), {self.image, self.profile['image_tag']})
        execute_cleanup(self.node, second['plan'], self.docker())
        self.assertFalse((self.node / 'distributed-projects/assets' / self.asset).exists())
        self.assertTrue(self.original.exists())

    def test_running_or_foreign_container_and_changed_source_never_delete_files(self):
        planned = plan_cleanup(self.node, self.config, [self.record], self.declaration)
        for docker in (self.docker(running=True), self.docker(wrong_owner=True)):
            with self.assertRaises(ValueError):
                execute_cleanup(self.node, planned['plan'], docker)
            self.assertFalse(any(call.args[0][:2] == ['docker', 'rm'] for call in docker.call_args_list))
            self.assertTrue((self.node / 'distributed-projects/algorithm' / self.bundle).exists())
        self.config['task_templates'][0]['source']['repo'] = str(self.original.parent)
        with self.assertRaisesRegex(ValueError, 'source mapping'):
            plan_cleanup(self.node, self.config, [self.record], self.declaration)
        with self.assertRaises(ValueError):
            owned_path(self.node, '../original')

    def test_missing_receipt_and_unknown_copy_is_a_failure_not_false_success(self):
        (self.node / 'distributed-projects/algorithm' / self.bundle / '.expman-install.json').unlink()
        with self.assertRaisesRegex(ValueError, 'ownership receipt'):
            plan_cleanup(self.node, self.config, [self.record], self.declaration)

    def test_failed_install_owned_image_receipt_can_reclaim_an_unregistered_runtime(self):
        self.config['profiles'].pop('derived')
        self.config['task_templates'] = []
        snapshot = self.node / 'distributed-projects/algorithm' / self.bundle
        receipt = common.read_json(snapshot / '.expman-install.json')
        receipt['profiles'] = {}
        common.atomic_json(snapshot / '.expman-install.json', receipt)
        common.atomic_json(snapshot / 'environment/environments/owned-images.json', [{
            'image': self.image, 'image_tag': self.profile['image_tag'], 'build_key': 'f' * 64}])
        planned = plan_cleanup(self.node, self.config, [], self.declaration)
        self.assertEqual(set(planned['plan']['images']), {self.image, self.profile['image_tag']})

    def test_worker_two_phase_cleanup_resumes_after_config_commit_without_losing_results(self):
        config_path = self.root / 'node.json'
        common.atomic_json(config_path, {**self.config, 'root': str(self.node),
            'hub_url': 'http://127.0.0.1:1', 'token': 'unused'})
        agent = Agent(config_path)
        try:
            agent._save(self.record)
            agent.project_delivery.accept([{**self.declaration, 'size': 1, 'revision': 1, 'action': 'delete'}])
            agent.project_delivery.tick()
            agent.project_delivery.installing['thread'].join(3)
            agent.project_delivery.tick()
            self.assertEqual(agent.config['task_templates'], [])
            self.assertTrue((self.node / 'distributed-projects/algorithm' / self.bundle).exists())
            agent.close()
            agent = Agent(config_path)
            agent._exec = self.docker()
            agent.project_delivery.tick()
            agent.project_delivery.installing['thread'].join(3)
            agent.project_delivery.tick()
            self.assertEqual(agent.project_delivery.reports()[0]['status'], 'deleted')
            self.assertTrue((self.node / 'runs' / self.job / 'results.json').exists())
            self.assertTrue(self.original.exists())
        finally:
            agent.close()

    def test_worker_cleanup_failure_and_retry_rechecks_new_shared_references(self):
        config_path = self.root / 'node.json'
        common.atomic_json(config_path, {**self.config, 'root': str(self.node),
            'hub_url': 'http://127.0.0.1:1', 'token': 'unused'})
        agent = Agent(config_path)
        try:
            agent._save(self.record)
            agent._exec = Mock(side_effect=RuntimeError('Docker temporarily unavailable'))
            agent.project_delivery.accept([{**self.declaration, 'size': 1, 'revision': 1, 'action': 'delete'}])
            for _ in range(4):
                agent.project_delivery.tick()
                if agent.project_delivery.installing:
                    agent.project_delivery.installing['thread'].join(3)
            self.assertEqual(agent.project_delivery.reports()[0]['status'], 'delete_failed')
            changed = copy.deepcopy(agent.config)
            changed['profiles']['derived'] = self.profile
            changed['assets'][self.asset] = self.config['assets'][self.asset]
            changed['task_templates'] = [{'backend': 'demo', 'assets': [self.asset],
                'environments': [{'profile': 'derived', 'image': self.image}]}]
            common.atomic_json(config_path, changed)
            agent._exec = self.docker()
            agent.project_delivery.accept([{**self.declaration, 'size': 1, 'revision': 2, 'action': 'delete'}])
            for _ in range(4):
                agent.project_delivery.tick()
                if agent.project_delivery.installing:
                    agent.project_delivery.installing['thread'].join(3)
            self.assertEqual(agent.project_delivery.reports()[0]['status'], 'deleted')
            self.assertIn('derived', agent.config['profiles'])
            self.assertTrue((self.node / 'distributed-projects/assets' / self.asset).exists())
            self.assertFalse(any(call.args[0][:3] == ['docker', 'image', 'rm'] for call in agent._exec.call_args_list))
        finally:
            agent.close()

    def test_legacy_derived_image_requires_exact_owned_build_context(self):
        requirements = ['example==1.0']
        key = hashlib.sha256(json.dumps({'base': self.config['profiles']['base']['image'],
            'requirements': requirements}, sort_keys=True).encode()).hexdigest()
        gpu_key = hashlib.sha256(json.dumps(['Example GPU']).encode()).hexdigest()[:6]
        name = 'harness-' + key[:20] + '-' + gpu_key
        legacy = {'image': self.image, 'base_profile': 'base', 'requirements': requirements,
                  'gpu_name_patterns': ['Example GPU'], 'verified': True}
        self.config['profiles'].pop('derived')
        self.config['profiles'][name] = legacy
        self.config['task_templates'][0]['environments'][0]['profile'] = name
        receipt_path = self.node / 'distributed-projects/algorithm' / self.bundle / '.expman-install.json'
        receipt = common.read_json(receipt_path)
        receipt['profiles'] = {name: legacy}
        common.atomic_json(receipt_path, receipt)
        before = plan_cleanup(self.node, self.config, [], self.declaration)
        self.assertEqual(before['plan']['images'], [])
        context = receipt_path.parent / 'environment/environments' / key
        context.mkdir(parents=True)
        (context / 'Dockerfile').write_text('ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\nUSER root\n')
        (context / 'requirements.txt').write_text('example==1.0\n')
        after = plan_cleanup(self.node, self.config, [], self.declaration)
        self.assertIn(self.image, after['plan']['images'])
        self.assertNotIn(name, after['config']['profiles'])

    def test_legacy_never_run_project_reclaims_images_after_restart_and_retry(self):
        requirements = ['example==1.0']
        key = hashlib.sha256(json.dumps({'base': self.config['profiles']['base']['image'],
            'requirements': requirements}, sort_keys=True).encode()).hexdigest()
        gpu_key = hashlib.sha256(json.dumps(['Example GPU']).encode()).hexdigest()[:6]
        name = 'harness-' + key[:20] + '-' + gpu_key
        legacy = {'image': self.image, 'base_profile': 'base', 'requirements': requirements,
                  'gpu_name_patterns': ['Example GPU'], 'verified': True}
        self.config['profiles'].pop('derived')
        self.config['profiles'][name] = legacy
        self.config['task_templates'][0]['environments'][0]['profile'] = name
        self.config['assets'] = {}
        self.config['task_templates'][0]['assets'] = []
        snapshot = self.node / 'distributed-projects/algorithm' / self.bundle
        (snapshot / '.expman-install.json').unlink()
        context = snapshot / 'environment/environments' / key
        context.mkdir(parents=True)
        (context / 'Dockerfile').write_text('ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\nUSER root\n')
        (context / 'requirements.txt').write_text('example==1.0\n')
        archive = self.node / 'project-downloads' / (self.digest + '.zip')
        digest = common.sha256_file(archive)
        archive.rename(archive.with_name(digest + '.zip'))
        declaration = {**self.declaration, 'digest': digest, 'size': 10, 'action': 'delete'}
        manifest = {'project_id': 'algorithm', 'bundle_id': self.bundle, 'assets': {}, 'files': {}}
        config_path = self.root / 'node.json'
        common.atomic_json(config_path, {**self.config, 'root': str(self.node),
            'hub_url': 'http://127.0.0.1:1', 'token': 'unused'})
        agent = Agent(config_path)
        try:
            with patch('expman.harness_project.read_bundle', return_value=manifest):
                agent.project_delivery.accept([{**declaration, 'revision': 1}])
                agent.project_delivery.tick()
                agent.project_delivery.installing['thread'].join(3)
                agent.project_delivery.tick()
                self.assertEqual(agent.config['task_templates'], [])
                self.assertNotIn(name, agent.config['profiles'])
                agent.close()
                agent = Agent(config_path)
                self.assertEqual(agent.records(), [])
                agent._exec = Mock(side_effect=RuntimeError('Docker temporarily unavailable'))
                agent.project_delivery.tick()
                agent.project_delivery.installing['thread'].join(3)
                agent.project_delivery.tick()
                self.assertEqual(agent.project_delivery.reports()[0]['status'], 'delete_failed')
                tag = self.image.split('@', 1)[0] + ':' + key[:24]
                self.profile['image_tag'] = tag
                agent._exec = self.docker()
                agent.project_delivery.accept([{**declaration, 'revision': 2}])
                for _ in range(4):
                    agent.project_delivery.tick()
                    if agent.project_delivery.installing:
                        agent.project_delivery.installing['thread'].join(3)
                self.assertEqual(agent.project_delivery.reports()[0]['status'], 'deleted')
                removed = {call.args[0][3] for call in agent._exec.call_args_list
                           if call.args[0][:3] == ['docker', 'image', 'rm']}
                self.assertEqual(removed, {self.image, tag})
                self.assertFalse(snapshot.exists())
                self.assertTrue(self.original.exists())
        finally:
            agent.close()


if __name__ == '__main__':
    unittest.main()
