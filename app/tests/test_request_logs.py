"""请求统计白名单、消费与恢复；全程使用临时数据库。"""
import copy
from contextlib import closing
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import bridge, request_logs as logs


def record(**changes):
    row = dict(timestamp='2026-10-13T12:34:56.123+08:00', alias='route/gpt-test',
               model='A/gpt-test', response_model='gpt-test-2026', request_id='request-1',
               reasoning_effort='high', stream=True, failed=False, latency_ms=1200, ttft_ms=200,
               token_breakdown={'quality': 'complete', 'output': {
                   'total_tokens': 100, 'non_reasoning_tokens': 70, 'reasoning_tokens': 30}})
    row.update(changes)
    return row


class RequestLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        p = patch.object(bridge, 'ROOT', self.root); p.start(); self.addCleanup(p.stop)
        p = patch.object(logs.identity, 'gateway_identity', return_value={'state': 'ok', 'pid': 123})
        self.identity = p.start(); self.addCleanup(p.stop)
        logs._pending.clear(); logs._error = ''; logs._unsupported_pid = None

    def test_mapping_metrics_and_privacy(self):
        row = record(api_key='private-key', source='private-account', response_headers={'secret': 'private-header'},
                     fail={'body': 'private-body'}, access_token_sha256='private-hash', prompt='private-prompt')
        with patch.object(bridge, 'gateway_get', return_value=[row]) as get:
            logs.collect_once()
            data = logs.snapshot()['requests'][0]
            logs.snapshot(); get.assert_called_once_with('usage-queue?count=256')
        self.assertEqual((data['requested_model'], data['upstream_model']), ('route/gpt-test', 'A/gpt-test'))
        self.assertEqual((data['ttft_ms'], data['duration_ms'], data['tokens_per_second']), (200, 1200, 100))
        self.assertEqual(data['output_tokens'], 100)
        self.assertNotIn(b'private-', (self.root / 'request-history.db').read_bytes())
        self.assertNotIn('private-', json.dumps(data))

    def test_missing_and_untrustworthy_metrics(self):
        for changes in [dict(stream=False), dict(failed=True), dict(ttft_ms=0), dict(ttft_ms=1200),
                        dict(ttft_ms=float('nan')), dict(latency_ms=True)]:
            data = logs.normalize(record(**changes))
            self.assertIsNone(data['ttft_ms']); self.assertIsNone(data['tokens_per_second'])
        for quality in ('unclassified', 'inconsistent'):
            row = record(); row['token_breakdown']['quality'] = quality
            self.assertIsNone(logs.normalize(row)['tokens_per_second'])
        row = record(); row['token_breakdown']['output']['total_tokens'] = 101
        self.assertIsNone(logs.normalize(row)['tokens_per_second'])
        self.assertIsNone(logs.normalize(record(token_breakdown=None))['tokens_per_second'])
        self.assertIsNone(logs.normalize(record(timestamp='2026-99-13T12:34:56Z')))
        self.assertIsNone(logs.normalize(record(model='<script>'))['upstream_model'])

    def test_database_failure_keeps_consumed_batch(self):
        with patch.object(bridge, 'gateway_get', return_value=[record()]) as get:
            with patch.object(logs, '_connect', side_effect=sqlite3.OperationalError('disk')):
                with self.assertRaises(sqlite3.OperationalError): logs.collect_once()
            self.assertEqual(len(logs._pending), 1)
            logs.collect_once(); get.assert_called_once()
        self.assertEqual(len(logs.snapshot()['requests']), 1)

    def test_retention_clear_and_corrupt_database(self):
        with patch.object(bridge, 'gateway_get', return_value=[record(request_id=str(i)) for i in range(2100)]):
            logs.collect_once()
        data = logs.snapshot(999)['requests']
        self.assertEqual(len(data), 200); self.assertEqual(data[0]['request_id'], '2099')
        with closing(sqlite3.connect(self.root / 'request-history.db')) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM requests').fetchone()[0], 2000)
        logs.clear(); self.assertEqual(logs.snapshot()['requests'], [])
        (self.root / 'request-history.db').write_bytes(b'corrupt')
        self.assertEqual(logs.snapshot()['request_error'], '请求统计暂不可用')

    def test_identity_and_unsupported_gateway(self):
        with patch.object(bridge, 'gateway_get', side_effect=bridge.RouteError('HTTP 404')) as get:
            self.identity.return_value = {'state': 'foreign', 'pid': 123}
            logs.collect_once(); get.assert_not_called()
            self.identity.return_value = {'state': 'ok', 'pid': 123}
            logs.collect_once(); logs.collect_once(); self.assertEqual(get.call_count, 1)
            self.identity.return_value = {'state': 'ok', 'pid': 124}
            get.side_effect = None; get.return_value = []
            logs.collect_once(); self.assertEqual(get.call_count, 2)
            self.assertEqual(logs.snapshot()['request_error'], '')

    def test_worker_restart(self):
        with patch.object(logs, 'collect_once') as collect:
            logs.start(); first = logs._worker
            logs.start(); self.assertIs(logs._worker, first)
            logs.stop(); self.assertFalse(first.is_alive())
            logs.start(); self.assertIsNot(logs._worker, first)
            logs.stop(); self.assertFalse(logs._worker.is_alive())

    def test_read_logs_collects_immediately(self):
        import server
        main_log = self.root / 'main.log'
        main_log.write_text('[2026-10-14 19:00:00] [req-live] [info ] [gin_logger.go:103] 200\n', encoding='utf-8')
        with patch.object(server, 'MAIN_LOG', main_log), \
             patch.object(bridge, 'gateway_get', return_value=[record(request_id='req-live')]) as get:
            payload = server.read_logs(50)
            get.assert_called_once_with('usage-queue?count=256')
        self.assertEqual(payload['requests'][0]['request_id'], 'req-live')


if __name__ == '__main__': unittest.main()
