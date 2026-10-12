"""当前 CLIProxyAPI 二进制隔离回放；只启动随机端口和临时配置。"""
import copy
import http.client
import json
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'app'))
from core import bridge, cursor, cursor_inference, identity

seen = []


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        seen.append((self.path, body, self.headers.get('Authorization')))
        if self.path == '/v1/responses':
            output = [{'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                       'content': [{'type': 'output_text', 'text': 'hello', 'annotations': []}]}]
            if body.get('tools'):
                output = [{'id': 'fc_1', 'type': 'function_call', 'status': 'completed',
                           'call_id': 'call_1', 'name': 'check', 'arguments': '{"model":"keep"}'}]
            response = {'id': 'resp_1', 'object': 'response', 'status': 'completed',
                        'model': body['model'], 'output': output,
                        'usage': {'input_tokens': 1, 'output_tokens': 2, 'total_tokens': 3}}
            events = [{'type': 'response.created', 'response': dict(response, status='in_progress', output=[])},
                      {'type': 'response.output_item.added', 'output_index': 0, 'item': output[0]},
                      {'type': 'response.output_text.delta', 'item_id': 'msg_1', 'output_index': 0,
                       'content_index': 0, 'delta': 'hello'},
                      {'type': 'response.output_item.done', 'output_index': 0, 'item': output[0]},
                      {'type': 'response.completed', 'response': response}]
            raw = ''.join('event: ' + e['type'] + '\ndata: ' + json.dumps(e) + '\n\n' for e in events).encode()
            content = 'text/event-stream'
        else:
            message = {'role': 'assistant', 'content': 'hello'}
            if body.get('tools'):
                message = {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'call_1', 'type': 'function', 'function': {'name': 'check', 'arguments': '{"model":"keep"}'}}]}
            obj = {'id': 'chat_1', 'object': 'chat.completion', 'created': 1, 'model': body['model'],
                   'choices': [{'index': 0, 'message': message, 'finish_reason': 'tool_calls' if body.get('tools') else 'stop'}],
                   'usage': {'prompt_tokens': 1, 'completion_tokens': 2, 'total_tokens': 3}}
            if body.get('stream'):
                obj['object'] = 'chat.completion.chunk'
                obj['choices'][0]['delta'] = obj['choices'][0].pop('message')
                raw = ('data: ' + json.dumps(obj) + '\n\ndata: [DONE]\n\n').encode()
                content = 'text/event-stream'
            else:
                raw = json.dumps(obj).encode(); content = 'application/json'
        self.send_response(200); self.send_header('Content-Type', content)
        self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)


def call(port, path, body, key):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
    connection.request('POST', path, json.dumps(body).encode(), {'Authorization': 'Bearer ' + key,
                                                              'Content-Type': 'application/json'})
    r = connection.getresponse(); result = r.status, r.read(); connection.close(); return result


with tempfile.TemporaryDirectory(prefix='prism-cursor-test-') as temporary:
    root = Path(temporary)
    from socket import socket
    with socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    base = 'http://127.0.0.1:%d/v1' % upstream.server_port
    config = {'host': '127.0.0.1', 'port': port, 'auth-dir': str(root / 'auth'),
              'api-keys': ['local-test'], 'remote-management': {'secret-key': 'management-test'},
              'request-retry': 0, 'max-retry-credentials': 0,
              'codex-api-key': [{'api-key': 'upstream-codex', 'base-url': base,
                                 'headers': {'X-Route-Tag': 'codex'},
                                 'models': [{'name': 'gpt-6.1-sol', 'alias': 'codex/gpt-6.1-sol'}]}],
              'openai-compatibility': [{'name': 'openai', 'base-url': base, 'headers': {'X-Route-Tag': 'openai'},
                                        'api-key-entries': [{'api-key': 'upstream-openai'}],
                                        'models': [{'name': 'gpt-6.1-sol', 'alias': 'openai/gpt-6.1-sol'}]}],
              'payload': {'default': [{'models': [{'name': '*', 'protocol': 'codex'}],
                                       'params': {'temperature': .2}}]}}
    (root / 'auth').mkdir()
    # 顶层 YAML 独立行，便于核对仅 payload 被改写。
    text = ''.join(k + ': ' + json.dumps(v) + '\n' for k, v in config.items())
    (root / 'config.yaml').write_text(text, encoding='utf-8')
    (root / '.local-secrets.json').write_text(json.dumps({'api_key': 'local-test', 'management_key': 'management-test',
                                                       'cursor_access_key': 'cursor-test'}))
    rows = [{'id': name, 'label': name, 'group': 'gpt', 'base_url': base, 'tag': name,
             'head': name, 'name': name if name == 'openai' else None,
             'section': 'codex-api-key' if name == 'codex' else 'openai-compatibility',
             'expose': ['gpt-6.1-sol'], 'model_settings': {'gpt-6.1-sol': {
                 'levels': ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'], 'default': 'high'}}}
            for name in ('codex', 'openai')]
    (root / 'routing-plan.json').write_text(json.dumps({'providers': rows, 'selected': {'gpt': ['codex', 'openai']}}))
    process = subprocess.Popen([str(ROOT / 'cli-proxy-api.exe'), '-config', str(root / 'config.yaml')],
                               cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    old = bridge.ROOT, bridge.rs.ROOT, bridge.GATEWAY_BASE, bridge.CATALOG_PATH
    old_identity, original_config = identity.gateway_identity, identity.GATEWAY_CONFIG
    bridge.ROOT = bridge.rs.ROOT = root; bridge.GATEWAY_BASE = 'http://127.0.0.1:%d' % port
    bridge.CATALOG_PATH = root / 'codex-model-catalog.json'; identity.GATEWAY_CONFIG = root / 'config.yaml'
    identity.gateway_identity = lambda: old_identity(port)
    service = None
    try:
        for _ in range(60):
            try:
                bridge.gateway_get('config'); break
            except bridge.RouteError:
                time.sleep(.1)
        original_text = (root / 'config.yaml').read_text()
        ordinary = {'model': 'codex/gpt-6.1-sol', 'messages': [{'role': 'user', 'content': 'ordinary'}],
                    'reasoning_effort': 'medium'}
        assert call(port, '/v1/chat/completions', ordinary, 'local-test')[0] == 200
        baseline = copy.deepcopy(seen[-1][1])
        cursor.install_rules()
        print('PASS payload 安装、当前网关读回')
        cursor.install_rules()
        assert len(list((root / 'backups').iterdir())) == 1
        state = cursor.save({'source': 'codex', 'model': 'gpt-6.1-sol', 'revision': 0})
        state.update(enabled=True); cursor._write('cursor-settings.json', state)
        service = cursor_inference.InferenceServer(
            ('127.0.0.1', 0), cursor_inference.Handler,
            profile=cursor._profile, read_secrets=lambda: cursor._read('.local-secrets.json'))
        threading.Thread(target=service.serve_forever, daemon=True).start()
        revision = 1
        for source in ('codex', 'openai'):
            if source != 'codex':
                saved = cursor.save({'source': source, 'model': 'gpt-6.1-sol', 'revision': revision}); revision = saved['revision']
            for level in cursor.LEVELS:
                saved = cursor.save({'source': source, 'model': 'gpt-6.1-sol', 'level': level, 'revision': revision})
                revision = saved['revision']
                for path in ('/v1/chat/completions', '/v1/responses'):
                    for stream in (False, True):
                        body = {'model': 'prism-cursor', 'stream': stream}
                        if path == '/v1/chat/completions': body['messages'] = [{'role': 'user', 'content': 'hello'}]
                        else: body['input'] = 'hello'
                        status, raw = call(service.server_port, path, body, 'cursor-test')
                        assert status == 200, (source, path, stream, status, raw[:200])
                        upstream_path, actual, key = seen[-1]
                        field = actual.get('reasoning', {}).get('effort') if source == 'codex' else actual.get('reasoning_effort')
                        assert actual['model'] == 'gpt-6.1-sol' and field == level, (source, path, stream, level, field)
                        assert key == 'Bearer upstream-' + source
                        assert b'prism-cursor' in raw, raw[:300]
                print('PASS', source, level, 'Chat/Responses JSON/SSE 最终模型与等级')
            for path in ('/v1/chat/completions', '/v1/responses'):
                for stream in (False, True):
                    body = {'model': 'prism-cursor', 'stream': stream}
                    if path.endswith('completions'):
                        body.update(messages=[{'role': 'user', 'content': 'test'}], tools=[{
                            'type': 'function', 'function': {'name': 'check', 'parameters': {'type': 'object'}}}])
                    else:
                        body.update(input='test', tools=[{'type': 'function', 'name': 'check', 'parameters': {'type': 'object'}}])
                    status, raw = call(service.server_port, path, body, 'cursor-test')
                    assert status == 200 and b'check' in raw and b'keep' in raw, (source, path, stream, status, raw[:300])
            print('PASS', source, '工具调用 JSON/SSE')
        # 普通请求的等级仍由原网关决定，不匹配 Cursor 规则。
        status, raw = call(port, '/v1/chat/completions', {'model': 'codex/gpt-6.1-sol',
                           'messages': [{'role': 'user', 'content': 'ordinary'}], 'reasoning_effort': 'medium'}, 'local-test')
        assert status == 200 and seen[-1][1] == baseline, seen[-1][1]
        from core import sources
        parsed = bridge.gateway_get('config')
        rebuilt = bridge.rs.build_config(parsed, {'providers': [dict(p, available=[{
            'name': 'gpt-6.1-sol', 'alias': 'gpt-6.1-sol'}]) for p in rows]}, {'gpt': ['codex', 'openai']})
        assert rebuilt['payload'] == parsed['payload']
        assert (root / 'config.yaml').read_text().startswith(original_text[:original_text.index('payload:')])
        assert bridge.gateway_get('config')['payload']['default'] == parsed['payload']['default']
        for section in bridge.rs.SECTIONS:
            bridge.gateway_put(section, rebuilt[section])
        assert cursor.payload_equal(bridge.gateway_get('config')['payload'], parsed['payload'])
        assert call(service.server_port, '/v1/chat/completions', {
            'model': 'prism-cursor', 'messages': [{'role': 'user', 'content': 'after-save'}]}, 'cursor-test')[0] == 200
        assert seen[-1][1]['reasoning_effort'] == 'ultra'
        print('PASS 普通请求隔离、首页 build_config 保留规则、其他配置字节不变')
    finally:
        if service: service.shutdown(); service.server_close()
        process.terminate(); process.wait(timeout=10)
        upstream.shutdown(); upstream.server_close()
        bridge.ROOT, bridge.rs.ROOT, bridge.GATEWAY_BASE, bridge.CATALOG_PATH = old
        identity.gateway_identity, identity.GATEWAY_CONFIG = old_identity, original_config
