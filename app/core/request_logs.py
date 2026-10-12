"""消费网关请求统计队列；仅持久化模型和计时白名单，不保存凭据或正文。"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime

from . import bridge, identity

_lock = threading.RLock()
_stop = threading.Event()
_worker = None
_pending = []
_error = ''
_unsupported_pid = None


def _number(value):
    return value if type(value) in (int, float) and 0 <= value <= 10**15 else None


def _model(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/() -]{0,255}', value) else None


def normalize(record):
    if not isinstance(record, dict):
        return None
    timestamp = record.get('timestamp')
    if not isinstance(timestamp, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT[\d:.+-]+Z?', timestamp):
        return None
    try:
        datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    except ValueError:
        return None
    duration = _number(record.get('latency_ms'))
    ttft = _number(record.get('ttft_ms'))
    stream = record.get('stream') is True
    failed = record.get('failed') is True
    if not stream or ttft is None or ttft <= 0 or duration is None or ttft >= duration or failed:
        ttft = None
    breakdown = record.get('token_breakdown')
    output = None
    if isinstance(breakdown, dict) and breakdown.get('quality') == 'complete':
        totals = breakdown.get('output')
        if isinstance(totals, dict):
            total = _number(totals.get('total_tokens'))
            text = _number(totals.get('non_reasoning_tokens'))
            reasoning = _number(totals.get('reasoning_tokens'))
            if total is not None and text is not None and reasoning is not None and total == text + reasoning:
                output = total
    speed = round(output * 1000 / (duration - ttft), 2) if output and ttft is not None else None
    failure = record.get('fail')
    status = _number(failure.get('status_code')) if isinstance(failure, dict) else None
    status = status if type(status) is int and 100 <= status <= 599 else None
    request_id = record.get('request_id')
    return {'timestamp': timestamp, 'request_id': request_id if isinstance(request_id, str) and re.fullmatch(r'[a-zA-Z0-9-]{1,64}', request_id) else None,
            'requested_model': _model(record.get('alias')), 'upstream_model': _model(record.get('model')),
            'response_model': _model(record.get('response_model')), 'reasoning_level': record.get('reasoning_effort') if record.get('reasoning_effort') in ('low', 'medium', 'high', 'xhigh', 'max', 'ultra', 'none') else None,
            'failed': failed, 'status': status, 'duration_ms': duration, 'ttft_ms': ttft,
            'output_tokens': output, 'tokens_per_second': speed}


def _connect():
    connection = sqlite3.connect(bridge.ROOT / 'request-history.db', timeout=5)
    try:
        connection.execute('CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, data TEXT NOT NULL)')
    except sqlite3.Error:
        connection.close()
        raise
    return connection


def collect_once():
    global _error, _unsupported_pid
    with _lock:
        who = identity.gateway_identity()
        if who['state'] != 'ok':
            _error = '请求统计：网关未就绪'
            return
        if _unsupported_pid is not None and who.get('pid') == _unsupported_pid:
            return
        # 队列是消费式接口：只允许后台单一采集器调用，页面读取本地快照。
        if not _pending:
            try:
                records = bridge.gateway_get('usage-queue?count=256')
            except bridge.RouteError as exc:
                if 'HTTP 404' in str(exc):
                    _unsupported_pid = who.get('pid')
                    _error = '当前网关不支持请求统计'
                    return
                raise
            if not isinstance(records, list):
                raise bridge.RouteError('网关请求统计格式无效')
            _pending.extend(row for record in records if (row := normalize(record)) is not None)
        if _pending:
            connection = _connect()
            try:
                with connection:
                    connection.executemany('INSERT INTO requests(data) VALUES (?)', [(json.dumps(row),) for row in _pending])
                    connection.execute('DELETE FROM requests WHERE id NOT IN (SELECT id FROM requests ORDER BY id DESC LIMIT 2000)')
                _pending.clear()
            finally:
                connection.close()
        _error = ''


def snapshot(limit=100):
    path = bridge.ROOT / 'request-history.db'
    if not path.is_file():
        return {'requests': [], 'request_error': _error}
    connection = None
    try:
        connection = _connect()
        rows = connection.execute('SELECT data FROM requests ORDER BY id DESC LIMIT ?', (max(1, min(200, limit)),)).fetchall()
        return {'requests': [json.loads(row[0]) for row in rows], 'request_error': _error}
    except (sqlite3.Error, ValueError, OSError):
        return {'requests': [], 'request_error': '请求统计暂不可用'}
    finally:
        if connection:
            connection.close()


def clear():
    with _lock:
        _pending.clear()
        if (bridge.ROOT / 'request-history.db').is_file():
            connection = _connect()
            try:
                with connection:
                    connection.execute('DELETE FROM requests')
            finally:
                connection.close()


def start():
    global _worker, _stop
    with _lock:
        if _worker and _worker.is_alive() and not _stop.is_set():
            return
        _stop = threading.Event()
        stopping = _stop
        def run():
            global _error
            while not stopping.is_set():
                try:
                    collect_once()
                except Exception:
                    _error = '请求统计暂不可用'
                stopping.wait(3 if not _error else 30)
        _worker = threading.Thread(target=run, name='prism-request-logs', daemon=True)
        _worker.start()


def stop():
    _stop.set()
    if _worker and _worker is not threading.current_thread():
        _worker.join(timeout=8)
