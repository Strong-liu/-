"""Request-count and safety regressions for dense-paper translation recovery."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from baidu_academic import BaiduAcademicClient, BaiduAPIError, BaiduConfig
from term_protection import ProtectionError, TOKEN_RE, TermProtector
from test_llm_response import Session


def records_per_line(query):
    if TOKEN_RE.search(query):
        return {'trans_result': [{'src': query, 'dst': TOKEN_RE.sub('', query)}]}
    return {'trans_result': [{'src': line, 'dst': '译文' + line} for line in query.split('\n')]}


class RecoveryPerformanceTests(unittest.TestCase):
    def client(self, handler=records_per_line, cache_path=None, terms=None, sleep=lambda _: None):
        session = Session(handler)
        client = BaiduAcademicClient(
            BaiduConfig(service_mode='llm', api_key='offline-fake-key', qps=10),
            TermProtector(terms or ['EEGNet']), cache_path=cache_path,
            session_factory=lambda: session, sleep=sleep)
        self.addCleanup(client.close)
        return client, session

    def test_dense_paragraph_has_two_requests_and_no_recovery_limit(self):
        client, session = self.client()
        source = ' '.join(f'Segment {index} EEGNet' for index in range(64)) + ' last fragment.'
        translated = client.translate(source)
        self.assertEqual(translated.count('EEGNet'), 64)
        self.assertEqual(translated.count('译文'), 65)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(client.snapshot()['batch_requests'], 1)
        self.assertFalse(TOKEN_RE.search(translated))

    def test_batches_keep_complete_utf8_requests_below_configured_limit(self):
        client, session = self.client()
        source = ' '.join(f'Fragment {index} contains several words EEGNet' for index in range(180))
        result = client.translate(source)
        self.assertEqual(result.count('EEGNet'), 180)
        self.assertTrue(all(len(call['json']['q'].encode('utf-8')) <= client.config.max_query_bytes
                            for _, call in session.calls))
        self.assertLess(len(session.calls), 20)

    def test_same_plain_fragment_is_sent_once_per_batch(self):
        client, session = self.client()
        result = client.translate('The EEGNet improves this. EEGNet improves this. EEGNet improves this.')
        batch = session.calls[1][1]['json']['q'].split('\n')
        self.assertEqual(batch, ['The', 'improves this.'])
        self.assertEqual(result.count('译文improves this.'), 3)

    def test_validated_plain_fragments_reuse_cache_across_policy_changes(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        cache_path = Path(folder) / 'fragments.sqlite3'
        first, first_session = self.client(cache_path=cache_path)
        first.translate('The EEGNet improves this.')
        self.assertEqual(len(first_session.calls), 2)
        second, second_session = self.client(cache_path=cache_path, terms=['OtherNet'])
        translated = second.translate('The OtherNet improves this.')
        self.assertEqual(translated, '译文The OtherNet 译文improves this.')
        self.assertEqual(len(second_session.calls), 1)  # Only the masked whole paragraph.
        self.assertEqual(second.snapshot()['fragment_cache_hits'], 2)

    def test_ignore_cache_also_bypasses_plain_fragment_cache(self):
        client, session = self.client()
        source = 'The EEGNet improves this.'
        expected = client.translate(source)
        self.assertEqual(client.translate(source, ignore_cache=True), expected)
        self.assertEqual(len(session.calls), 4)
        self.assertEqual(client.snapshot()['fragment_cache_hits'], 0)

    def test_partially_untranslated_fragment_is_not_cached_with_other_chinese_results(self):
        def partial(query):
            if TOKEN_RE.search(query):
                return records_per_line(query)
            return {'trans_result': [{'src': line, 'dst': line if line == 'several untranslated body words'
                                      else '译文' + line} for line in query.split('\n')]}
        client, session = self.client(partial)
        with self.assertRaises(ProtectionError):
            client.translate('A good clause EEGNet several untranslated body words')
        self.assertEqual(client.cache.connection.execute('SELECT count(*) FROM translations').fetchone()[0], 0)

    def test_merged_batch_records_are_split_without_guessing_boundaries(self):
        def merged(query):
            if TOKEN_RE.search(query):
                return records_per_line(query)
            return {'trans_result': [{'src': query, 'dst': '译文' + query}]}
        client, session = self.client(merged)
        translated = client.translate('First clause EEGNet last clause.')
        self.assertEqual(translated, '译文First clause EEGNet 译文last clause.')
        self.assertEqual(len(session.calls), 4)
        self.assertEqual(sum(call['json']['q'] == 'First clause\nlast clause.'
                             for _, call in session.calls), 1)

    def test_split_source_records_within_a_fragment_are_joined(self):
        def sentences(query):
            if TOKEN_RE.search(query):
                return records_per_line(query)
            records = []
            for line in query.split('\n'):
                cut = len(line) // 2
                records.extend([{'src': line[:cut], 'dst': '第一部分'},
                                {'src': line[cut:], 'dst': '第二部分'}])
            return {'trans_result': records}
        client, session = self.client(sentences)
        self.assertEqual(client.translate('First clause EEGNet last clause.'),
                         '第一部分第二部分 EEGNet 第一部分第二部分')
        self.assertEqual(len(session.calls), 2)

    def test_bad_batch_source_is_not_retried_or_mapped_by_position(self):
        for bad_kind in ('omitted', 'reordered'):
            def bad_batch(query):
                if TOKEN_RE.search(query):
                    return records_per_line(query)
                lines = query.split('\n')
                if len(lines) > 1:
                    lines = lines[:1] if bad_kind == 'omitted' else list(reversed(lines))
                    return {'trans_result': [{'src': line, 'dst': 'WRONG'} for line in lines]}
                return records_per_line(query)
            with self.subTest(bad_kind=bad_kind):
                client, session = self.client(bad_batch)
                result = client.translate('First clause EEGNet last clause.')
                self.assertEqual(result, '译文First clause EEGNet 译文last clause.')
                self.assertEqual(len(session.calls), 4)
                self.assertNotIn('WRONG', result)

    def test_invalid_single_fragment_never_populates_fragment_or_paragraph_cache(self):
        def missing(query):
            if TOKEN_RE.search(query):
                return records_per_line(query)
            return {'trans_result': [{'src': query[:2], 'dst': '错误'}]}
        client, session = self.client(missing)
        with self.assertRaises(BaiduAPIError):
            client.translate('First clause EEGNet last clause.')
        self.assertEqual(client.cache.connection.execute('SELECT count(*) FROM translations').fetchone()[0], 0)
        self.assertEqual(len(session.calls), 3)  # One bad batch and first singleton, no duplicate batch retry.

    def test_quota_failure_is_not_split_and_is_visible_in_snapshot(self):
        def quota(query):
            return records_per_line(query) if TOKEN_RE.search(query) else {'error_code': 54004}
        client, session = self.client(quota)
        with self.assertRaises(BaiduAPIError):
            client.translate('First clause EEGNet last clause.')
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(client.snapshot()['active_requests'], 0)
        self.assertEqual(client.cache.connection.execute('SELECT count(*) FROM translations').fetchone()[0], 0)

    def test_waiting_request_observes_fatal_error_before_network(self):
        client, session = self.client()
        client.next_request_at = 10**12
        def fatal_during_wait(_):
            with client.counter_lock:
                client.fatal_error = ('54004', '账户余额不足')
        client.sleep = fatal_during_wait
        with self.assertRaises(BaiduAPIError):
            client.translate('A plain body sentence.')
        self.assertEqual(len(session.calls), 0)

    def test_snapshot_reports_active_request_and_clears_it_on_completion(self):
        entered, release = threading.Event(), threading.Event()
        def block(query):
            entered.set()
            release.wait(3)
            return {'trans_result': [{'src': query, 'dst': '完整译文。'}]}
        client, session = self.client(block)
        with mock.patch.object(client, '_wait_for_slot'):
            worker = threading.Thread(target=lambda: client.translate('A complete body sentence.'))
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                snapshot = client.snapshot()
                self.assertEqual(snapshot['active_requests'], 1)
                self.assertEqual(snapshot['requests'], 1)
                self.assertGreaterEqual(snapshot['last_activity_seconds'], 0)
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(client.snapshot()['active_requests'], 0)


if __name__ == '__main__':
    unittest.main()
