"""Offline API, credential isolation and review-first workflow checks."""

import importlib.util
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
TEST_DATA = tempfile.TemporaryDirectory(prefix='paper-translator-api-test-')
os.environ['PDF_TRANSLATOR_DATA_DIR'] = TEST_DATA.name
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient
import app as application


class DesktopAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(application.app, base_url='http://127.0.0.1:8766')
        cls.client.__enter__()
        cls.fixture = Path(TEST_DATA.name) / 'synthetic-paper.pdf'
        with application.pymupdf.open() as document:
            page = document.new_page()
            document.set_metadata({'author': 'Alex Gray; Jamie Stone'})
            page.insert_text((50, 40), 'Journal of Neural Engineering')
            page.insert_text((50, 110), 'Synthetic software test paper')
            page.insert_text((50, 150), 'Alex Gray and Jamie Stone')
            page.insert_text((50, 250), 'Abstract: EEGNet and Transformer are protected names in this software test.')
            document.save(cls.fixture)

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        logging.shutdown()
        TEST_DATA.cleanup()

    def setUp(self):
        application.settings.value.update(app_id='', secret_cipher='', api_key_cipher='', service_mode='llm', qps=1, custom_terms=[])

    def upload_fixture(self):
        with self.fixture.open('rb') as stream:
            response = self.client.post('/api/jobs', files={'file': ('paper.pdf', stream, 'application/pdf')})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_health_and_local_origin(self):
        self.assertEqual(self.client.get('/api/health').json()['app'], 'paper-translator-desktop')
        self.assertEqual(self.client.get('/api/health', headers={'Origin': 'https://example.com'}).status_code, 403)

    def test_health_counts_active_jobs_beyond_the_visible_history_window(self):
        history = {'older-active': {'status': 'running'}}
        history.update({f'newer-{index}': {'status': 'review'} for index in range(70)})
        with patch.dict(application.manager.items, history, clear=True):
            self.assertEqual(self.client.get('/api/health').json()['active_jobs'], 1)

    def test_invalid_pdf_is_not_server_error(self):
        result = self.client.post('/api/jobs', files={'file': ('broken.pdf', b'not-pdf', 'application/pdf')})
        self.assertEqual(result.status_code, 400)

    def test_real_pdf_is_local_review_without_credentials(self):
        job = self.upload_fixture()
        self.assertEqual(job['status'], 'review')
        self.assertEqual(job['pages'], 1)
        self.assertTrue(job['terms'])
        self.assertTrue(job['preserve_references'])
        self.assertIn('methods', job['term_groups'])
        self.assertNotIn('policy', job)
        result = self.client.post(f'/api/jobs/{job["id"]}/start', json={'terms': job['terms'], 'preserve_references': True})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.client.get(f'/api/jobs/{job["id"]}').json()['status'], 'review')

    def test_key_saved_encrypted_and_never_returned(self):
        fake_key = 'unit-test-only-not-a-live-api-key'
        result = self.client.put('/api/settings', json={'api_key': fake_key, 'service_mode': 'llm', 'qps': 1, 'custom_terms': ['MyNet']})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()['api_key_configured'])
        self.assertNotIn(fake_key, result.text)
        self.assertNotIn(fake_key, application.settings.path.read_text(encoding='utf-8'))
        self.assertEqual(application.settings.credentials()['BAIDU_API_KEY'], fake_key)
        self.assertNotIn(fake_key, self.client.get('/api/settings').text)

    def test_terms_are_snapshotted_before_queue_without_exposing_key(self):
        self.client.put('/api/settings', json={'api_key': 'offline-only-key', 'service_mode': 'llm', 'custom_terms': ['CustomNet']})
        job = self.upload_fixture()
        with patch.object(application.manager.executor, 'submit') as submit:
            result = self.client.post(f'/api/jobs/{job["id"]}/start', json={'terms': ['CustomNet', 'Pan'], 'preserve_references': True})
        self.assertEqual(result.status_code, 202)
        self.assertEqual(result.json()['status'], 'queued')
        submit.assert_called_once()
        saved = json.loads((application.JOBS / job['id'] / 'job.json').read_text(encoding='utf-8'))
        self.assertTrue(saved['policy']['preserve_texts'])
        self.assertEqual(saved['policy']['terms'], ['CustomNet', 'Pan'])
        self.assertNotIn('offline-only-key', json.dumps(saved))

    def test_legacy_opt_out_cannot_send_references_for_translation(self):
        self.client.put('/api/settings', json={'api_key': 'offline-only-key', 'service_mode': 'llm'})
        job = self.upload_fixture()
        application.manager.items[job['id']]['policy']['preserve_references'] = False
        with patch.object(application.manager.executor, 'submit'):
            result = self.client.post(f'/api/jobs/{job["id"]}/start',
                                     json={'terms': job['terms'], 'preserve_references': False})
        self.assertEqual(result.status_code, 202, result.text)
        self.assertTrue(result.json()['preserve_references'])
        self.assertTrue(application.manager.items[job['id']]['policy']['preserve_references'])

    def test_missing_key_connection_test_does_not_call_process(self):
        with patch.object(application.subprocess, 'run') as run:
            response = self.client.post('/api/service/test')
        self.assertEqual(response.status_code, 400)
        run.assert_not_called()

    def test_download_before_finish_is_rejected(self):
        job = self.upload_fixture()
        self.assertEqual(self.client.get(f'/api/jobs/{job["id"]}/download/mono').status_code, 409)

    def test_temporarily_locked_status_file_retains_old_json_then_saves(self):
        path = Path(TEST_DATA.name) / 'atomic-status.json'
        application.write_json(path, {'status': 'review'})
        replace = Path.replace
        attempts = []

        def blocked_then_replace(source, target):
            attempts.append(source)
            if len(attempts) < 3:
                self.assertEqual(json.loads(path.read_text())['status'], 'review')
                raise PermissionError('synthetic Windows file-sharing conflict')
            return replace(source, target)

        with patch.object(Path, 'replace', new=blocked_then_replace), patch.object(application.time, 'sleep'):
            application.write_json(path, {'status': 'succeeded'})
        self.assertEqual(json.loads(path.read_text())['status'], 'succeeded')
        self.assertEqual(len(attempts), 3)
        self.assertEqual(list(path.parent.glob('.atomic-status.json.*.tmp')), [])

    def test_status_update_failure_terminates_owned_translator(self):
        job = self.upload_fixture()
        application.manager.update(job['id'], status='queued')
        process = Mock()
        process.stdout = io.StringIO('PDFMT_EVENT {"type":"progress_start","stage":"Subset font"}\n')
        original_update = application.manager.update

        def fail_progress(identifier, **values):
            if values.get('phase') == 'Subset font':
                raise PermissionError('synthetic status write failure')
            return original_update(identifier, **values)

        with patch.object(application, 'prepare_assets'), \
                patch.object(application.settings, 'credentials', return_value={'BAIDU_API_KEY': 'fake-test-only'}), \
                patch.object(application.subprocess, 'Popen', return_value=process), \
                patch.object(application, 'kill_owned_process') as terminate, \
                patch.object(application.manager, 'update', side_effect=fail_progress):
            application.manager.run(job['id'])
        terminate.assert_called_once_with(process)
        self.assertEqual(application.manager.items[job['id']]['status'], 'failed')
        self.assertTrue(process.stdout.closed)

    def test_engine_telemetry_and_progress_survive_failure_and_reload(self):
        job = self.upload_fixture()
        application.manager.update(job['id'], status='queued')
        process = Mock(returncode=1)
        process.stdout = io.StringIO(
            'PDFMT_EVENT {"type":"progress_update","stage":"Translate Paragraphs","stage_current":48,"stage_total":496,"overall_progress":43.05}\n'
            'PDFMT_EVENT {"type":"telemetry","stats":{"requests":76,"cache_hits":12,"translated_paragraphs":48},"active_requests":1,"retries":4,"last_retry_code":"30000","elapsed_seconds":650}\n'
            'PDFMT_EVENT {"type":"error","error":"upstream unavailable"}\n')
        with patch.object(application, 'prepare_assets'), \
                patch.object(application.settings, 'credentials', return_value={'BAIDU_API_KEY': 'fake-test-only'}), \
                patch.object(application.subprocess, 'Popen', return_value=process):
            application.manager.run(job['id'])
        result = self.client.get(f'/api/jobs/{job["id"]}').json()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual((result['stage_current'], result['stage_total']), (48, 496))
        self.assertEqual(result['telemetry']['requests'], 76)
        self.assertEqual(result['telemetry']['retries'], 4)
        self.assertEqual(result['telemetry']['last_retry_code'], '30000')
        saved = json.loads((application.JOBS / job['id'] / 'job.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['telemetry'], result['telemetry'])

    def test_cancelled_queued_job_never_starts_a_process_and_can_retry(self):
        job = self.upload_fixture()
        application.manager.update(job['id'], status='queued')
        response = self.client.post(f'/api/jobs/{job["id"]}/cancel')
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()['phase'], 'cancelled')
        with patch.object(application.subprocess, 'Popen') as spawn:
            application.manager.run(job['id'])
        spawn.assert_not_called()
        with patch.object(application.settings, 'credentials', return_value={'BAIDU_API_KEY': 'fake-test-only'}), \
                patch.object(application.manager.executor, 'submit'):
            retried = self.client.post(f'/api/jobs/{job["id"]}/retry')
        self.assertEqual(retried.status_code, 202, retried.text)
        self.assertEqual(retried.json()['status'], 'queued')

    def test_cancel_other_queued_job_never_kills_active_process(self):
        job = self.upload_fixture()
        application.manager.update(job['id'], status='queued')
        process = Mock()
        with patch.object(application.manager, 'process', process), \
                patch.object(application.manager, 'process_job_id', 'another-task'), \
                patch.object(application, 'kill_owned_process') as terminate:
            response = self.client.post(f'/api/jobs/{job["id"]}/cancel')
        self.assertEqual(response.status_code, 202, response.text)
        terminate.assert_not_called()

    def test_cancel_running_task_kills_only_its_owned_process(self):
        job = self.upload_fixture()
        application.manager.update(job['id'], status='running')
        process = Mock()
        with patch.object(application.manager, 'process', process), \
                patch.object(application.manager, 'process_job_id', job['id']), \
                patch.object(application, 'kill_owned_process') as terminate:
            response = self.client.post(f'/api/jobs/{job["id"]}/cancel')
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()['phase'], 'cancelling')
        terminate.assert_called_once_with(process)
        application.manager.cancel_requested.discard(job['id'])

    def test_watchdog_uses_silence_instead_of_total_runtime(self):
        process = Mock()
        process.poll.return_value = None
        with patch.object(application.time, 'monotonic', return_value=100):
            watchdog = application.EngineWatchdog(process, silence_seconds=180)
        with patch.object(application.time, 'monotonic', return_value=7300):
            watchdog.touch()
        with patch.object(application.time, 'monotonic', return_value=7479), \
                patch.object(application, 'kill_owned_process') as terminate:
            self.assertFalse(watchdog.check())
            terminate.assert_not_called()
        with patch.object(application.time, 'monotonic', return_value=7480), \
                patch.object(application, 'kill_owned_process') as terminate:
            self.assertTrue(watchdog.check())
            terminate.assert_called_once_with(process)
        self.assertTrue(watchdog.timed_out.is_set())

    def test_idle_telemetry_stops_engine_with_clear_error(self):
        job = self.upload_fixture()
        application.manager.update(job['id'], status='queued')
        process = Mock(returncode=1)
        process.stdout = io.StringIO('PDFMT_EVENT {"type":"telemetry","last_activity_seconds":300,"active_requests":0}\n')
        with patch.object(application, 'prepare_assets'), \
                patch.object(application.settings, 'credentials', return_value={'BAIDU_API_KEY': 'fake-test-only'}), \
                patch.object(application.subprocess, 'Popen', return_value=process), \
                patch.object(application, 'kill_owned_process') as terminate:
            application.manager.run(job['id'])
        terminate.assert_called_once_with(process)
        saved = application.manager.items[job['id']]
        self.assertEqual(saved['status'], 'failed')
        self.assertIn('5 分钟', saved['message'])
        self.assertIn('缓存会保留', saved['message'])

    def test_cancellation_during_preparation_never_starts_engine(self):
        job = self.upload_fixture()
        application.manager.update(job['id'], status='queued')
        with patch.object(application, 'prepare_assets', side_effect=lambda: application.manager.cancel(job['id'])), \
                patch.object(application.settings, 'credentials', return_value={'BAIDU_API_KEY': 'fake-test-only'}), \
                patch.object(application.subprocess, 'Popen') as spawn:
            application.manager.run(job['id'])
        spawn.assert_not_called()
        self.assertEqual(application.manager.items[job['id']]['phase'], 'cancelled')

    def test_heartbeat_silence_timeout_is_reported_as_timeout(self):
        job = self.upload_fixture()
        application.manager.update(job['id'], status='queued')
        process = Mock(returncode=-1)
        process.stdout = io.StringIO('')
        with patch.object(application, 'prepare_assets'), \
                patch.object(application.settings, 'credentials', return_value={'BAIDU_API_KEY': 'fake-test-only'}), \
                patch.object(application.subprocess, 'Popen', return_value=process), \
                patch.object(application, 'EngineWatchdog') as monitor, \
                patch.object(application, 'kill_owned_process'):
            monitor.return_value.timed_out.is_set.return_value = True
            application.manager.run(job['id'])
        self.assertEqual(application.manager.items[job['id']]['status'], 'failed')
        self.assertIn('180 秒没有心跳', application.manager.items[job['id']]['message'])


if __name__ == '__main__':
    unittest.main()
