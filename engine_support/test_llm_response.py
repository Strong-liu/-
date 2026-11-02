"""Synthetic regressions for complete official Baidu API result handling."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from baidu_academic import BaiduAcademicClient, BaiduAPIError, BaiduConfig, LLM_ENDPOINT, parse_complete_llm_response
from term_protection import ProtectionError, TOKEN_RE, TermProtector


class Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class Session:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.trust_env = None

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response(self.handler(kwargs['json']['q']))


class LLMResponseTests(unittest.TestCase):
    def client(self, handler, cache_path=None):
        session = Session(handler)
        client = BaiduAcademicClient(BaiduConfig(service_mode='llm', api_key='fake-only-test-key', qps=10),
            TermProtector(['EEGNet']), cache_path=cache_path,
            session_factory=lambda: session, sleep=lambda _: None)
        self.addCleanup(client.close)
        return client, session

    def test_all_records_survive_even_when_first_is_only_a_number(self):
        def handler(query):
            return {'trans_result': [{'src': query[:4], 'dst': '(7).'},
                {'src': query[4:], 'dst': query[4:].replace('Our', '我们的').replace('is useful', '很有用')}]}
        client, session = self.client(handler)
        result = client.translate('(7). Our EEGNet is useful.')
        self.assertEqual(result, '(7). 我们的 EEGNet 很有用.')
        url, request = session.calls[0]
        self.assertEqual(url, LLM_ENDPOINT)
        self.assertEqual(request['headers'], {'Authorization': 'Bearer fake-only-test-key'})
        self.assertNotIn('fake-only-test-key', json.dumps(request['json']))
        self.assertEqual(request['timeout'], (10, 75))
        self.assertFalse(session.trust_env)

    def test_omitted_or_reordered_source_records_are_refused(self):
        for records in ([{'src': 'First.', 'dst': '第一句。'}],
                        [{'src': 'Second.', 'dst': '第二句。'}, {'src': 'First.', 'dst': '第一句。'}]):
            with self.subTest(records=records):
                with self.assertRaises(BaiduAPIError) as caught:
                    parse_complete_llm_response({'trans_result': records}, 'First. Second.')
                self.assertEqual(caught.exception.code, 'SOURCE_COVERAGE')

    def test_whitespace_normalization_is_accepted_without_losing_source(self):
        self.assertEqual(parse_complete_llm_response({'trans_result': [
            {'src': 'First.\nSecond.', 'dst': '第一句。第二句。'}]}, 'First. Second.'), '第一句。第二句。')

    def test_empty_later_translation_is_not_success(self):
        with self.assertRaises(BaiduAPIError):
            parse_complete_llm_response({'trans_result': [
                {'src': 'First.', 'dst': '第一句。'}, {'src': 'Second.', 'dst': ''}]}, 'First. Second.')

    def test_incomplete_response_retries_then_fails_without_cache(self):
        client, session = self.client(lambda q: {'trans_result': [{'src': q[:3], 'dst': '短句'}]})
        with self.assertRaises(BaiduAPIError):
            client.translate('An entire paragraph must be present.')
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(client.cache.connection.execute('SELECT count(*) FROM translations').fetchone()[0], 0)

    def test_lost_marker_retries_with_validation_then_recovers(self):
        attempts = []
        def handler(query):
            attempts.append(query)
            if len(attempts) == 1:
                return {'trans_result': [{'src': query, 'dst': TOKEN_RE.sub('', query)}]}
            return {'trans_result': [{'src': line, 'dst': line.replace('Our', '我们的')}
                                     for line in query.split('\n')]}
        client, session = self.client(handler)
        self.assertEqual(client.translate('Our EEGNet works.'), '我们的 EEGNet works.')
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(client.stats['protection_retries'], 1)
        self.assertEqual(client.stats['fragment_recoveries'], 1)
        self.assertEqual(client.errors, [])

    def test_fragment_recovery_inserts_protected_tokens_only_locally(self):
        def handler(query):
            if TOKEN_RE.search(query):
                translated = TOKEN_RE.sub('', query)
            else:
                translated = '译文' + query
            return {'trans_result': [{'src': query, 'dst': translated}]}
        client, session = self.client(handler)
        source = 'The EEGNet method remains unchanged; EEGNet works in this example.'
        result = client.translate(source)
        self.assertEqual(result.count('EEGNet'), 2)
        self.assertEqual(result, '译文The EEGNet 译文method remains unchanged; EEGNet 译文works in this example.')
        self.assertTrue(all(not TOKEN_RE.search(request['json']['q']) for _, request in session.calls[1:]))
        self.assertEqual(client.stats['fragment_recoveries'], 1)

    def test_more_than_forty_fragments_are_all_recovered_and_terms_remain_english(self):
        def handler(query):
            if TOKEN_RE.search(query):
                return {'trans_result': [{'src': query, 'dst': TOKEN_RE.sub('', query)}]}
            return {'trans_result': [{'src': line, 'dst': '译文' + line} for line in query.split('\n')]}
        client, session = self.client(handler)
        source = ' '.join(f'Segment {index} EEGNet' for index in range(64)) + ' final fragment.'
        expected = ' '.join(f'译文Segment {index} EEGNet' for index in range(64)) + ' 译文final fragment.'
        result = client.translate(source)
        self.assertEqual(result, expected)
        self.assertEqual(result.count('EEGNet'), 64)
        self.assertFalse(TOKEN_RE.search(result))
        self.assertEqual(len(session.calls), 2)  # One full request, then one verified 65-fragment batch.
        self.assertTrue(all(not TOKEN_RE.search(request['json']['q']) for _, request in session.calls[1:]))
        self.assertEqual(client.stats['fragment_recoveries'], 1)
        self.assertEqual(client.errors, [])
        self.assertEqual(client.translate(source), expected)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(client.stats['cache_hits'], 1)

    def test_quota_failure_during_many_fragment_recovery_stops_remaining_requests(self):
        def handler(query):
            if TOKEN_RE.search(query):
                return {'trans_result': [{'src': query, 'dst': TOKEN_RE.sub('', query)}]}
            return {'error_code': 54004}
        client, session = self.client(handler)
        source = ' '.join(f'Segment {index} EEGNet' for index in range(64)) + ' final fragment.'
        with self.assertRaises(BaiduAPIError) as caught:
            client.translate(source)
        self.assertEqual(caught.exception.code, '54004')
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(client.stats['fragment_recoveries'], 0)
        with self.assertRaises(BaiduAPIError):
            client.translate('Another paragraph after the quota failure.')
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(client.cache.connection.execute('SELECT count(*) FROM translations').fetchone()[0], 0)

    def test_unchanged_english_fragment_recovery_is_rejected_without_cache(self):
        client, session = self.client(lambda q: {'trans_result': [
            {'src': line, 'dst': TOKEN_RE.sub('', line)} for line in q.split('\n')]})
        with self.assertRaises(ProtectionError):
            client.translate('Our EEGNet is a method. ' * 3)
        self.assertEqual(len(session.calls), 5)  # Split once more to verify the unchanged singleton fails too.
        self.assertEqual(client.cache.connection.execute('SELECT count(*) FROM translations').fetchone()[0], 0)

    def test_old_cli_cache_is_never_reused_as_complete_translation(self):
        with tempfile.TemporaryDirectory() as folder:
            client, session = self.client(lambda q: {'trans_result': [{'src': q, 'dst': '完整的新译文。'}]}, Path(folder) / 'cache.sqlite3')
            text = 'First sentence. Second sentence without named entities.'
            old_key = hashlib.sha256(json.dumps(['baidu-desktop-v2', 'llm', 'academic-style', 'en', 'zh',
                client.protector.fingerprint, text], ensure_ascii=False).encode('utf-8')).hexdigest()
            client.cache.set(old_key, '第一句，旧客户端截断。')
            self.assertEqual(client.translate(text), '完整的新译文。')
            self.assertEqual(client.translate(text), '完整的新译文。')
            self.assertEqual(len(session.calls), 1)
            self.assertEqual(client.stats['cache_hits'], 1)
            client.close()
            self._cleanups.pop()

    def test_quota_failure_never_falls_back_or_exposes_response_secrets(self):
        client, session = self.client(lambda q: {'error_code': 54004, 'error_msg': 'fake-only-test-key'})
        with self.assertRaises(BaiduAPIError) as caught:
            client.translate('Hello world.')
        self.assertEqual(len(session.calls), 1)
        self.assertNotIn('fake-only-test-key', str(caught.exception))

    def test_fatal_quota_error_stops_subsequent_network_calls(self):
        client, session = self.client(lambda q: {'error_code': 54004, 'error_msg': 'private diagnostic'})
        for sentence in ('First sentence.', 'Second sentence.'):
            with self.assertRaises(BaiduAPIError) as caught:
                client.translate(sentence)
            self.assertEqual(caught.exception.code, '54004')
            self.assertIn('余额不足', str(caught.exception))
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(client.stats['fragment_recoveries'], 0)

    def test_transient_incomplete_response_does_not_latch_a_fatal_error(self):
        client, session = self.client(lambda q: {'trans_result': [{'src': q[:3], 'dst': '不完整。'}]})
        with self.assertRaises(BaiduAPIError):
            client.translate('The entire source is required.')
        self.assertIsNone(client.fatal_error)

    def test_intermittent_business_error_retries_and_redacts_credentials(self):
        attempts = []
        def handler(query):
            attempts.append(query)
            if len(attempts) == 1:
                return {'error_code': 30000, 'error_msg': 'temporary fake-only-test-key ' + query}
            return {'trans_result': [{'src': query, 'dst': '完整译文。'}]}
        client, session = self.client(handler)
        with self.assertLogs('baidu_academic', level='WARNING') as captured:
            self.assertEqual(client.translate('An entire controlled sentence.'), '完整译文。')
        self.assertEqual(len(session.calls), 2)
        self.assertIsNone(client.fatal_error)
        diagnostics = '\n'.join(captured.output)
        self.assertIn('30000', diagnostics)
        self.assertNotIn('fake-only-test-key', diagnostics)
        self.assertNotIn('An entire controlled sentence.', diagnostics)

    def test_persistent_business_error_stops_after_three_attempts_without_cache(self):
        client, session = self.client(lambda q: {'error_code': '30000', 'error_msg': 'temporary error'})
        with self.assertLogs('baidu_academic', level='WARNING'):
            with self.assertRaises(BaiduAPIError) as caught:
                client.translate('A controlled sentence with no entities.')
        self.assertEqual(caught.exception.code, '30000')
        self.assertEqual(len(session.calls), 3)
        self.assertIsNone(client.fatal_error)
        self.assertEqual(client.stats['fragment_recoveries'], 0)
        self.assertEqual(client.cache.connection.execute('SELECT count(*) FROM translations').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
