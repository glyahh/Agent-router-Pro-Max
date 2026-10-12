"""Cursor 推理等级规则：安全合并、安装核对及失败恢复。"""
from __future__ import annotations

import copy
import json
import os
import re
import secrets
import time

from . import bridge, sources

LEVELS = sources.REASONING_LEVELS


def rules():
    return [{'models': [{'name': '*', 'protocol': protocol,
                         'headers': {'X-Prism-Client': 'cursor', 'X-Prism-Reasoning-Effort': level}}],
             'params': {field: level}}
            for protocol, field in [('codex', 'reasoning.effort'), ('openai', 'reasoning_effort')]
            for level in LEVELS]


def merge_payload(payload):
    payload = copy.deepcopy(payload or {})
    if not isinstance(payload, dict) or set(payload) - {'default', 'default-raw', 'override', 'override-raw', 'filter'}:
        raise bridge.RouteError('payload 结构无法安全合并')
    owned = rules()
    owned_keys = [_rule_key(e) for e in owned]
    for kind, entries in payload.items():
        if entries is None:
            payload[kind] = []
            continue
        if not isinstance(entries, list):
            raise bridge.RouteError('payload 规则必须为列表')
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get('models'), list):
                raise bridge.RouteError('payload 规则结构无效')
            if _rule_key(entry) in owned_keys and kind == 'override':
                continue
            encoded = json.dumps(entry).lower()
            if ('x-prism-' in encoded or (kind in ('filter', 'override-raw') and 'reasoning' in encoded)
                    or (kind == 'filter' and any(p in ('*', '$', '@this') for p in entry.get('params', [])))):
                raise bridge.RouteError('payload 与 Cursor 规则冲突')
    payload['override'] = [e for e in (payload.get('override') or []) if _rule_key(e) not in owned_keys] + owned
    return payload


def _rule_key(entry):
    entry = copy.deepcopy(entry)
    if isinstance(entry, dict):
        for model in entry.get('models', []) or []:
            if isinstance(model, dict):
                for field in ('from-protocol', 'match', 'not-match', 'exist', 'not-exist', 'headers'):
                    if not model.get(field):
                        model.pop(field, None)
    return entry


def payload_equal(left, right):
    # 网关持久化会把空规则数组省略；读回的 null 与 [] 语义一致。
    def normalize(value):
        return {k: [_rule_key(e) for e in v] for k, v in (value or {}).items() if v}
    return normalize(left) == normalize(right)


def payload_text(text, payload):
    """仅替换顶层 payload。拒绝锚点、合并键、重复顶层键及复杂顶层键。"""
    if re.search(r'(^|\s)(?:[&*][\w-]+|<<\s*:)|^\s*(?:---|\.\.\.)\s*$', text, re.M):
        raise bridge.RouteError('YAML 锚点或多文档无法安全合并')
    lines = text.splitlines(keepends=True)
    keys, start, end = set(), None, len(lines)
    for i, line in enumerate(lines):
        if not line.strip() or line.startswith('#') or line[0].isspace():
            continue
        match = re.match(r'([\w-]+):(?:\s|$)', line)
        if not match or match[1] in keys:
            raise bridge.RouteError('YAML 顶层键复杂或重复，拒绝改写')
        keys.add(match[1])
        if start is not None and end == len(lines):
            end = i
        if match[1] == 'payload':
            start = i
    newline = '\r\n' if '\r\n' in text else '\n'
    block = 'payload: ' + json.dumps(payload, ensure_ascii=False) + newline
    if start is None:
        return text + (newline if text and not text.endswith('\n') else '') + block
    return ''.join(lines[:start]) + block + ''.join(lines[end:])


def install(identity_check):
    identity_check()
    with bridge._named_mutex():
        path = bridge.ROOT / 'config.yaml'
        original = path.read_bytes()
        parsed = bridge.gateway_get('config')
        payload = merge_payload(parsed.get('payload'))
        if payload_equal(parsed.get('payload'), payload):
            return
        bom = b'\xef\xbb\xbf' if original.startswith(b'\xef\xbb\xbf') else b''
        modified = bom + payload_text(original[len(bom):].decode('utf-8'), payload).encode('utf-8')
        # 等待网关读完外部修改后再写；盘与内存不同步时拒绝覆盖。
        if path.read_bytes() != original or bridge.gateway_get('config').get('payload') != parsed.get('payload'):
            raise bridge.RouteError('网关配置正在变化')
        backup = sources._backup('cursor-payload', names=('config.yaml',))
        try:
            bridge.gateway_put_yaml(modified)
            if path.read_bytes() != modified:
                raise bridge.RouteError('网关改写了 payload 之外的格式')
            for _ in range(20):
                identity_check()
                actual = bridge.gateway_get('config').get('payload')
                if payload_equal(actual, payload):
                    return
                time.sleep(.15)
            raise bridge.RouteError('网关未应用 Cursor 等级规则')
        except Exception:
            if path.read_bytes() == original:
                raise
            if path.read_bytes() == modified:
                bridge.gateway_put_yaml(original)
                _write_config_bytes(path, original)
                if (path.read_bytes() != original
                        or not payload_equal(bridge.gateway_get('config').get('payload'), parsed.get('payload'))):
                    raise bridge.RouteError('Cursor 规则恢复核对失败；备份：' + str(backup)) from None
            else:
                raise bridge.RouteError('配置被外部修改，未覆盖；备份：' + str(backup)) from None
            raise


def _write_config_bytes(path, raw):
    # config.yaml 其余字节（含换行/BOM）必须保持；text helper 会转换 Windows 换行。
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
    try:
        temporary.write_bytes(raw)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


