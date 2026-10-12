"""Cursor 的独立选择、等级规则与服务协调。不修改代理端文件或共享模型目录。"""
from __future__ import annotations

import copy
import http.client
import json
import re
import secrets
import threading
from urllib.parse import urlsplit

from . import bridge, cursor_inference, cursor_rules, identity, sources
from .cursor_inference import MODEL, PORT, MAX_BODY
from .cursor_rules import LEVELS, merge_payload, payload_equal, payload_text, rules

_lock = threading.RLock()
_service = None
_error = ''


def _read(name, default=None):
    path = bridge.ROOT / name
    if not path.exists() and default is not None:
        return copy.deepcopy(default)
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise bridge.RouteError(name + ' 读取失败') from None


def _write(name, value):
    bridge._write_text_atomic(bridge.ROOT / name, json.dumps(value, ensure_ascii=False, indent=2))


def _state():
    return _read('cursor-settings.json', {'source': None, 'model': None, 'level': None,
                                         'enabled': False, 'url': '', 'revision': 0})


def _secret_update(values):
    with bridge._named_mutex():
        stored = _read('.local-secrets.json')
        stored.update(values)
        _write('.local-secrets.json', stored)
        return stored


def access_key(body):
    if body.get('action') not in ('copy', 'rotate'):
        raise bridge.RouteError('action 必须为 copy 或 rotate')
    with _lock, bridge._named_mutex():
        stored = _read('.local-secrets.json')
        if body['action'] == 'rotate' or not stored.get('cursor_access_key'):
            stored['cursor_access_key'] = 'sk-prism-cursor-' + secrets.token_urlsafe(32)
            _write('.local-secrets.json', stored)
        return {'access_key': stored['cursor_access_key']}


def _identity():
    if identity.gateway_identity()['state'] != 'ok':
        raise bridge.RouteError('网关未运行或身份不匹配')


def candidates():
    """用运行中的注册映射确定唯一来源，绝不按同名模型猜来源。"""
    _identity()
    plan = sources._read_plan()
    config = bridge.gateway_get('config')
    rows = plan.get('providers', [])
    states, failures = bridge.rs.row_states(config, rows, bridge.rs.selection_of(plan))
    catalog = _read('codex-model-catalog.json', {'models': []})
    directory = {m.get('slug'): m for m in catalog.get('models', []) if isinstance(m, dict)}
    registered = {}
    for section in bridge.rs.SECTIONS:
        for entry in config.get(section, []) or []:
            if bridge.rs.active(entry, section):
                for model in entry.get('models', []) or []:
                    alias = model.get('alias') or model.get('name')
                    registered.setdefault(alias, []).append((section, entry, model.get('name')))
    official_aliases = {a for row in rows if row.get('section') == 'auth-file'
                        and states.get(row.get('id')) for a in row.get('expose', [])}
    out = []
    for row in rows:
        if row.get('group') != 'gpt' or not states.get(row.get('id')) or row.get('blocked'):
            continue
        if row['id'] in failures:
            continue
        section = row.get('section')
        heads = bridge.rs._gateway_heads(plan, row)
        if section == 'auth-file':
            # 官方 OAuth 没有单账号路由标识；只有唯一活动账号时才可确定来源。
            files = bridge.gateway_get('auth-files').get('files', [])
            active = [f for f in files if f.get('provider') == 'codex'
                      and not f.get('disabled') and not f.get('unavailable')]
            if len(active) != 1 or active[0].get('name') != bridge.rs.AUTH_FILE:
                continue
            target = urlsplit(bridge.GATEWAY_BASE)
            if target.scheme != 'http' or target.hostname != '127.0.0.1':
                raise bridge.RouteError('网关必须为本地 HTTP 入口')
            connection = http.client.HTTPConnection(target.hostname, target.port, timeout=15)
            try:
                connection.request('GET', '/v1/models', headers={
                    'Authorization': 'Bearer ' + _read('.local-secrets.json')['api_key']})
                response = connection.getresponse()
                if response.status != 200:
                    raise bridge.RouteError('网关模型目录不可用')
                available = json.loads(response.read(MAX_BODY)).get('data', [])
            except (OSError, ValueError, KeyError, http.client.HTTPException):
                raise bridge.RouteError('网关模型目录不可用') from None
            finally:
                connection.close()
            aliases = {m.get('id') for m in available}
            mappings = [(a, a) for a in row.get('expose', []) if a in aliases and a not in registered]
        else:
            entry = bridge.rs.find_entry(config, row)
            mappings = []
            for alias in row.get('expose', []):
                for head in heads:
                    client = alias if alias.startswith('A/') else sources.client_id(head, alias)
                    hits = registered.get(client, [])
                    if (client not in official_aliases and len(hits) == 1
                            and hits[0][0] == section and hits[0][1] == entry):
                        mappings.append((alias, client))
                        break
        for alias, client in mappings:
            declaration = (row.get('model_settings') or {}).get(alias)
            if isinstance(declaration, dict):
                levels = [v for v in declaration.get('levels', []) if v in LEVELS]
                default = declaration.get('default')
            else:
                item = directory.get(client, {})
                levels = [v.get('effort') for v in item.get('supported_reasoning_levels', [])
                          if isinstance(v, dict) and v.get('effort') in LEVELS]
                default = item.get('default_reasoning_level')
            levels = list(dict.fromkeys(levels))
            default = next((v for v in ('medium', 'high', default) if v in levels), None)
            if levels and default is None:
                # 不臆造默认档；有声明但默认无效时使用声明的第一档。
                default = levels[0]
            out.append({'source': row['id'], 'label': row.get('label') or row['id'],
                        'model': alias, 'gateway_model': client, 'levels': levels, 'default': default})
    return out


def snapshot():
    with _lock:
        state = _state()
        error = _error
    try:
        models = candidates()
        if state['model'] and not any(m['source'] == state['source'] and m['model'] == state['model']
                                      for m in models):
            error = '当前来源或模型不可用'
    except bridge.RouteError as exc:
        models, error = [], str(exc)
    from . import cursor_tunnel
    return dict(state, candidates=models, error=error or cursor_tunnel.error(),
                model_name=MODEL, running=_service is not None, starting=cursor_tunnel.starting())


def save(body):
    with _lock, bridge._named_mutex():
        state = _state()
        if type(body.get('revision')) is not int or body['revision'] != state['revision']:
            raise bridge.RouteError('Cursor 选择已变化，请刷新')
        models = candidates()
        selected = next((m for m in models if m['source'] == body.get('source')
                         and m['model'] == body.get('model')), None)
        if selected is None:
            raise bridge.RouteError('来源未启用或模型未暴露')
        changed = (state['source'], state['model']) != (selected['source'], selected['model'])
        level = body.get('level', selected['default'] if changed else state['level'])
        if level not in selected['levels'] and not (level is None and not selected['levels']):
            raise bridge.RouteError('模型未声明该推理等级')
        state.update(source=selected['source'], model=selected['model'], level=level,
                     revision=state['revision'] + 1)
        _write('cursor-settings.json', state)
        return dict(state, candidates=models, model_name=MODEL, error='')


def install_rules():
    cursor_rules.install(_identity)


def _profile():
    with _lock:
        state = _state()
        if not state['enabled']:
            raise bridge.RouteError('Cursor 未启用')
        selected = next((m for m in candidates() if m['source'] == state['source']
                         and m['model'] == state['model']), None)
        if selected is None or (state['level'] is not None and state['level'] not in selected['levels']):
            raise bridge.RouteError('当前来源、模型或等级不可用')
        if state['level']:
            payload = bridge.gateway_get('config').get('payload')
            if not payload_equal(payload, merge_payload(payload)):
                raise bridge.RouteError('Cursor 等级规则缺失')
        return dict(selected, level=state['level'])


def start_service():
    global _service
    with _lock:
        if _service is None:
            try:
                _service = cursor_inference.InferenceServer(
                    ('127.0.0.1', PORT), cursor_inference.Handler,
                    profile=_profile, read_secrets=lambda: _read('.local-secrets.json'))
            except OSError:
                raise bridge.RouteError('Cursor 端口 8319 被占用') from None
            threading.Thread(target=_service.serve_forever, name='prism-cursor', daemon=True).start()


def stop(wait=True):
    global _service
    from . import cursor_tunnel
    cursor_tunnel.stop(wait=wait)
    with _lock:
        service, _service = _service, None
    if service:
        service.close_connections()
        service.shutdown()
        service.server_close()


def tunnel(body):
    if type(body.get('on')) is not bool:
        raise bridge.RouteError('on 必须为 true 或 false')
    from . import cursor_tunnel
    if body['on']:
        if body.get('authtoken'):
            token = body['authtoken']
            if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,200}', token):
                raise bridge.RouteError('ngrok 授权信息格式无效')
            _secret_update({'cursor_ngrok_authtoken': token})
        cursor_tunnel.start()
    else:
        stop(wait=False)
        with _lock, bridge._named_mutex():
            state = _state()
            state.update(enabled=False, revision=state['revision'] + 1)
            _write('cursor-settings.json', state)
    return snapshot()


def restore():
    global _error
    try:
        if _state()['enabled']:
            from . import cursor_tunnel
            cursor_tunnel.start()
    except bridge.RouteError:
        _error = 'Cursor 启动失败'
