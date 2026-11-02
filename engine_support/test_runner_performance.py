"""Bounded concurrency and live progress must survive slow or failed work."""
import threading
import unittest
from unittest.mock import patch

from run_engine import backend_telemetry, translation_workers
from baidu_academic import BaiduConfig


class RunnerPerformanceTests(unittest.TestCase):
    def test_slow_response_waits_can_overlap_without_raising_account_qps(self):
        config = BaiduConfig(service_mode='llm', api_key='offline-only', qps=1)
        self.assertEqual(translation_workers(config), 8)
        self.assertEqual(config.qps, 1)
        self.assertLessEqual(translation_workers(BaiduConfig(qps=10)), 16)

    def test_heartbeat_reports_network_activity_and_stops_after_error(self):
        received = threading.Event()
        events = []
        class Client:
            def snapshot(self):
                return {'requests': 4, 'active_requests': 2, 'last_activity_seconds': 20,
                        'retries': 1, 'last_retry_code': '30000'}
        def capture(event):
            events.append(event)
            received.set()
        with patch('run_engine.emit', side_effect=capture):
            with self.assertRaisesRegex(RuntimeError, 'synthetic'):
                with backend_telemetry(Client(), interval=.01) as progress:
                    progress()
                    self.assertTrue(received.wait(2))
                    raise RuntimeError('synthetic')
            count = len(events)
            self.assertFalse(any(t.name == 'translation-heartbeat' for t in threading.enumerate()))
        self.assertEqual(len(events), count)
        self.assertEqual(events[0]['type'], 'telemetry')
        self.assertEqual(events[0]['stats']['requests'], 4)
        self.assertEqual(events[0]['active_requests'], 2)
        self.assertLess(events[0]['last_activity_seconds'], 20)


if __name__ == '__main__':
    unittest.main()
