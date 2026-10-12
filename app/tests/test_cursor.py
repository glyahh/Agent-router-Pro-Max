"""Cursor 隔离检查：临时状态、回环假网关，不访问生产网关或服务商。"""
import contextlib
import copy
import http.client
import json
import socket
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import bridge, cursor, cursor_inference, cursor_tunnel, identity, sources


def rejects(run):
    try:
        run()
    except bridge.RouteError:
        return
    raise AssertionError('应拒绝')


seen = []
entered, release = threading.Event(), threading.Event()
canceled = threading.Event()


class Gateway(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        seen.append((copy.deepcopy(body), dict(self.headers)))
        if body.get('disconnect'):
            entered.set()
            self.connection.settimeout(3)
            if self.connection.recv(1) == b'':
                canceled.set()
            return
        if body.get('pause'):
            entered.set(); release.wait(5)
        if body.get('stream'):
            raw = (b'event: response.output_item.added\n'
                   b'data: {"type":"response.output_item.added","item":{"type":"function_call","arguments":"{\\"model\\":\\"keep\\"}"},"model":"real"}\n\n'
                   b'data: {"type":"response.completed","response":{"model":"real","output":[]}}\n\n'
                   b'data: [DONE]\n\n')
            content = 'text/event-stream'
        else:
            raw = json.dumps({'model': 'real', 'choices': [{'message': {'tool_calls': [
                {'function': {'name': 'test', 'arguments': '{"model":"keep"}'}}]}}]}).encode()
            content = 'application/json'
        self.send_response(200); self.send_header('Content-Type', content)
        self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)


def request(server, method='POST', path='/v1/chat/completions', body=None, key='dedicated', extra=None):
    c = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
    headers = {'Authorization': 'Bearer ' + key}
    headers.update(extra or {})
    c.request(method, path, json.dumps(body or {'model': 'client-model'}).encode(), headers)
    r = c.getresponse(); result = r.status, r.read(); c.close()
    return result


with tempfile.TemporaryDirectory() as temporary:
    old_root, old_rs, old_catalog, old_base = bridge.ROOT, bridge.rs.ROOT, bridge.CATALOG_PATH, bridge.GATEWAY_BASE
    bridge.ROOT = bridge.rs.ROOT = Path(temporary)
    bridge.CATALOG_PATH = bridge.ROOT / 'codex-model-catalog.json'
    original_identity, original_get = identity.gateway_identity, bridge.gateway_get
    identity.gateway_identity = lambda: {'state': 'ok'}
    plan = {'providers': [{'id': 'one', 'label': 'One', 'group': 'gpt', 'section': 'codex-api-key',
                          'base_url': 'https://one.invalid', 'tag': 'one', 'head': 'one',
                          'expose': ['gpt-6.1-sol'], 'model_settings': {'gpt-6.1-sol': {
                              'levels': ['low', 'medium', 'high'], 'default': 'high'}}},
                         {'id': 'two', 'label': 'Two', 'group': 'gpt', 'section': 'codex-api-key',
                          'base_url': 'https://two.invalid', 'tag': 'two', 'head': 'two',
                          'expose': ['gpt-6.1-sol'], 'model_settings': {'gpt-6.1-sol': {
                              'levels': ['high'], 'default': 'high'}}}],
            'selected': {'gpt': ['one', 'two']}}
    config = {'codex-api-key': [{'base-url': p['base_url'], 'headers': {'X-Route-Tag': p['tag']},
                                'models': [{'name': 'actual-' + p['id'], 'alias': p['head'] + '/gpt-6.1-sol'}]}
                               for p in plan['providers']], 'payload': {'override': cursor.rules()}}
    bridge.gateway_get = lambda path: copy.deepcopy(config) if path == 'config' else {'files': []}
    cursor._write('routing-plan.json', plan)
    cursor._write('.local-secrets.json', {'api_key': 'inference', 'management_key': 'NEVER-FORWARD',
                                        'cursor_access_key': 'dedicated'})
    gateway = ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    bridge.GATEWAY_BASE = 'http://127.0.0.1:%d' % gateway.server_port
    service = cursor_inference.InferenceServer(
        ('127.0.0.1', 0), cursor_inference.Handler,
        profile=cursor._profile, read_secrets=lambda: cursor._read('.local-secrets.json'))
    threading.Thread(target=service.serve_forever, daemon=True).start()
    try:
        models = cursor.candidates(); assert len(models) == 2 and models[0]['default'] == 'medium'
        saved = cursor.save({'source': 'one', 'model': 'gpt-6.1-sol', 'revision': 0})
        assert saved['level'] == 'medium' and saved['revision'] == 1
        rejects(lambda: cursor.save({'source': 'two', 'model': 'gpt-6.1-sol', 'revision': 0}))
        rejects(lambda: cursor.save({'source': 'one', 'model': 'gpt-6.1-sol', 'level': 'ultra', 'revision': 1}))
        state = cursor._state(); state['enabled'] = True; cursor._write('cursor-settings.json', state)
        assert 'dedicated' not in json.dumps(cursor.snapshot()) and 'NEVER-FORWARD' not in json.dumps(cursor.snapshot())
        assert request(service, 'GET', '/v1/models', key='wrong')[0] == 401
        assert json.loads(request(service, 'GET', '/v1/models')[1])['data'][0]['id'] == 'prism-cursor'
        assert request(service, 'GET', '/api/cursor')[0] == 404
        status, raw = request(service, extra={'X-Prism-Reasoning-Effort': 'ultra', 'X-Prism-Client': 'codex'})
        assert status == 200 and json.loads(raw)['model'] == 'client-model'
        body, headers = seen[-1]
        assert body['model'] == 'one/gpt-6.1-sol' and 'reasoning_effort' not in body
        assert headers['Authorization'] == 'Bearer inference' and headers['X-Prism-Reasoning-Effort'] == 'medium'
        assert json.loads(raw)['choices'][0]['message']['tool_calls'][0]['function']['arguments'] == '{"model":"keep"}'
        status, raw = request(service, path='/v1/responses', body={'model': 'custom-client', 'stream': True})
        assert status == 200 and b'"model": "custom-client"' in raw and b'[DONE]' in raw
        assert 'reasoning' not in seen[-1][0]
        output = []
        thread = threading.Thread(target=lambda: output.append(request(service, body={'model': 'old', 'pause': True})))
        thread.start(); assert entered.wait(3)
        saved = cursor.save({'source': 'two', 'model': 'gpt-6.1-sol', 'revision': 1})
        assert saved['level'] == 'high'
        release.set(); thread.join(5); assert output[0][0] == 200
        assert seen[-1][0]['model'] == 'one/gpt-6.1-sol'
        assert request(service)[0] == 200 and seen[-1][0]['model'] == 'two/gpt-6.1-sol'
        assert cursor._state()['source'] == 'two'
        config['codex-api-key'][1]['excluded-models'] = ['*']
        assert request(service)[0] == 409
        config['codex-api-key'][1].pop('excluded-models')
        config['codex-api-key'][0]['models'].append({'name': 'wrong', 'alias': 'two/gpt-6.1-sol'})
        assert request(service)[0] == 409
        config['codex-api-key'][0]['models'].pop()
        entered.clear()
        client = http.client.HTTPConnection('127.0.0.1', service.server_port, timeout=5)
        client.request('POST', '/v1/chat/completions', json.dumps({'model': 'x', 'disconnect': True}),
                       {'Authorization': 'Bearer dedicated'})
        assert entered.wait(3)
        client.close()
        assert canceled.wait(3), '断连没有关闭网关连接'
        import time
        for _ in range(30):
            if not service.connections: break
            time.sleep(.1)
        assert not service.connections
        config['codex-api-key'][1]['models'] = []
        assert request(service)[0] == 409
        config['codex-api-key'][1]['models'] = [{'name': 'actual-two', 'alias': 'two/gpt-6.1-sol'}]
        before = cursor._state()
        original_write = cursor._write
        def failed_write(name, value):
            if name == 'cursor-settings.json': raise OSError('test disk failure')
            return original_write(name, value)
        cursor._write = failed_write
        try:
            try:
                cursor.save({'source': 'one', 'model': 'gpt-6.1-sol', 'revision': before['revision']})
                raise AssertionError('写失败未上报')
            except OSError: pass
            assert cursor._state() == before
        finally:
            cursor._write = original_write
        identity.gateway_identity = lambda: {'state': 'foreign'}
        assert request(service)[0] == 409
        identity.gateway_identity = lambda: {'state': 'ok'}
        assert request(service, extra={'Content-Length': str(cursor.MAX_BODY + 1)})[0] == 413
        merged = cursor.merge_payload({'default': [{'models': [{'name': '*'}], 'params': {'temperature': .2}}]})
        assert cursor.merge_payload(merged) == merged
        rejects(lambda: cursor.merge_payload({'filter': [{'params': ['reasoning.effort']}]}))
        text = 'port: 123\npayload:\n  override: []\nrequest-retry: 0\n'
        modified = cursor.payload_text(text, merged)
        assert modified.startswith('port: 123\npayload: {') and modified.endswith('request-retry: 0\n')
        rejects(lambda: cursor.payload_text('port: 1\nport: 2\n', merged))
        rejects(lambda: cursor.payload_text('payload: &unsafe {}\n', merged))
        raw_config = b'\xef\xbb\xbfport: 123\r\nrequest-retry: 0\r\n'
        (bridge.ROOT / 'config.yaml').write_bytes(raw_config)
        original_get, original_put = bridge.gateway_get, bridge.gateway_put_yaml
        bridge.gateway_get = lambda path: {'payload': None}
        writes = []
        def put_yaml(raw):
            writes.append(raw); (bridge.ROOT / 'config.yaml').write_bytes(raw); return {'ok': True}
        bridge.gateway_put_yaml = put_yaml
        try:
            rejects(cursor.install_rules)
            assert (bridge.ROOT / 'config.yaml').read_bytes() == raw_config
            assert len(writes) == 2 and writes[0].startswith(raw_config) and writes[1] == raw_config
        finally:
            bridge.gateway_get, bridge.gateway_put_yaml = original_get, original_put
        stored = cursor.access_key({'action': 'rotate'})['access_key']
        assert stored != 'dedicated' and cursor._read('.local-secrets.json')['management_key'] == 'NEVER-FORWARD'
        # 控制台 Cursor 接口不绕开统一安全闸。
        import server
        console = server.ExclusiveServer(('127.0.0.1', 0), server.Handler)
        console.console_port = console.server_port; console.console_token = 'console-only'
        threading.Thread(target=console.serve_forever, daemon=True).start()
        def console_call(method, path, extra=None, body=None):
            c = http.client.HTTPConnection('127.0.0.1', console.server_port, timeout=5)
            headers = {'X-Prism-Token': 'console-only'};headers.update(extra or {})
            c.request(method, path, json.dumps(body or {}).encode(), headers)
            r = c.getresponse(); value = r.status, r.read();c.close();return value
        try:
            assert console_call('GET', '/api/cursor', {'X-Prism-Token': 'wrong'})[0] == 401
            assert console_call('POST', '/api/cursor/access-key', {'Origin': 'https://foreign.invalid'}, {'action': 'copy'})[0] == 409
            status, raw = console_call('POST', '/api/cursor/access-key', body={'action': 'copy'})
            assert status == 200 and json.loads(raw)['data']['access_key'] == stored
            assert console_call('POST', '/api/cursor/tunnel', body={})[0] == 409
        finally:
            console.shutdown();console.server_close()
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0)); occupied.listen()
            old_port = cursor.PORT; cursor.PORT = occupied.getsockname()[1]
            rejects(cursor.start_service); cursor.PORT = old_port
        print('PASS Cursor 状态、唯一来源、协议、工具、等级、隔离、安全与端口冲突')
    finally:
        release.set(); service.shutdown(); service.server_close(); gateway.shutdown(); gateway.server_close()
        identity.gateway_identity, bridge.gateway_get = original_identity, original_get
        bridge.ROOT, bridge.rs.ROOT, bridge.CATALOG_PATH, bridge.GATEWAY_BASE = old_root, old_rs, old_catalog, old_base
