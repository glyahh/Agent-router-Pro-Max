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
from core import bridge, cursor, cursor_inference, identity, request_logs

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
              'usage-statistics-enabled': True,
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
    for section in ('codex-api-key', 'openai-compatibility'):
        config[section][0]['models'].append({'name': 'gpt-6-astra',
                                            'alias': ('codex' if section == 'codex-api-key' else 'openai') + '/gpt-6-astra'})
    (root / 'config.yaml').write_text(''.join(k + ': ' + json.dumps(v) + '\n' for k, v in config.items()), encoding='utf-8')
    for row in rows:
        row['expose'].append('gpt-6-astra')
        row['model_settings']['gpt-6-astra'] = {'levels': ['medium', 'high'], 'default': 'high'}
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
        # CLIProxyAPI 的 codex 请求管线要经过一次管理接口写入 config.yaml 之后，才会把
        # 客户端的 reasoning_effort 翻成 reasoning；写什么无所谓，原样 PUT 同一份也会触发。
        # 生产里配置早被写过无数次，所以先把这一步做掉再取基线，否则网关的重载时序会被
        # 误判成 Cursor 规则外泄。等真正翻过来为止，避免取到中间的半加载状态。
        warmup = {'model': 'codex/gpt-6.1-sol', 'messages': [{'role': 'user', 'content': 'warmup'}],
                  'reasoning_effort': 'medium', 'prompt_cache_key': 'warmup-key'}
        bridge.gateway_put_yaml((root / 'config.yaml').read_bytes())
        for _ in range(40):
            assert call(port, '/v1/chat/completions', warmup, 'local-test')[0] == 200
            if seen[-1][1].get('reasoning'):
                break
            time.sleep(.25)
        original_text = (root / 'config.yaml').read_text()
        ordinary = {'model': 'codex/gpt-6.1-sol', 'messages': [{'role': 'user', 'content': 'ordinary'}],
                    'reasoning_effort': 'medium', 'prompt_cache_key': 'ordinary-cache-key'}
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
            profile=cursor._profile, read_secrets=lambda: cursor._read('.local-secrets.json'),
            model_name=lambda: cursor._state()['model_name'])
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
        # 每次控制台保存返回后立即请求；用旧的暴露名及冲突等级模拟 Cursor 缓存。
        for source, model, level in [('codex', 'gpt-6-astra', 'high'),
                                     ('codex', 'gpt-6-astra', 'medium'),
                                     ('openai', 'gpt-6.1-sol', 'low')]:
            saved = cursor.save({'source': source, 'model': model, 'level': level,
                                 'model_name': 'cursor-renamed', 'revision': revision})
            revision = saved['revision']
            for path in ('/v1/chat/completions', '/v1/responses'):
                for stream in (False, True):
                    body = {'model': 'prism-cursor', 'stream': stream,
                            'reasoning_effort': 'ultra', 'reasoning': {'effort': 'ultra'}}
                    body.update({'messages': [{'role': 'user', 'content': 'hello'}]} if path.endswith('completions') else {'input': 'hello'})
                    status, raw = call(service.server_port, path, body, 'cursor-test')
                    assert status == 200, (source, model, level, path, status, raw[:200])
                    actual, key = seen[-1][1:]
                    effort = actual.get('reasoning', {}).get('effort') if source == 'codex' else actual.get('reasoning_effort')
                    assert actual['model'] == model and effort == level and key == 'Bearer upstream-' + source, (actual, key)
            print('PASS 下一请求最终上游', source, model, level, 'Chat/Responses JSON/SSE')
        saved = cursor.save({'source': 'openai', 'model': 'gpt-6.1-sol', 'level': 'ultra', 'revision': revision})
        revision = saved['revision']
        # 普通请求的等级仍由原网关决定，不匹配 Cursor 规则。
        status, raw = call(port, '/v1/chat/completions', ordinary, 'local-test')
        assert status == 200 and seen[-1][1] == baseline, seen[-1][1]
        # Cursor 的等级规则按请求头命中；不带该头的普通请求，等级只能来自它自己发来的
        # reasoning_effort。用两个只差这一字段的请求互证：规则一旦外泄，两者会被钉成同一个值。
        levels = {}
        for asked in ('low', 'xhigh'):
            probe = dict(ordinary, reasoning_effort=asked)
            assert call(port, '/v1/chat/completions', probe, 'local-test')[0] == 200
            levels[asked] = seen[-1][1].get('reasoning', {}).get('effort')
        assert levels == {'low': 'low', 'xhigh': 'xhigh'}, levels
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
        assert call(port, '/v1/messages', {
            'model': 'openai/gpt-6.1-sol', 'max_tokens': 16, 'stream': True,
            'messages': [{'role': 'user', 'content': 'hello'}]}, 'local-test')[0] == 200
        # 真实网关统计队列：既包含直连网关，也包含 Cursor 转发。
        for _ in range(30):
            request_logs.collect_once()
            metrics = request_logs.snapshot(200)['requests']
            if len(metrics) >= 60: break
            time.sleep(.1)
        assert len(metrics) >= 60, (len(metrics), request_logs.snapshot()['request_error'])
        assert {'codex/gpt-6.1-sol', 'openai/gpt-6.1-sol'} <= {r['requested_model'] for r in metrics}, metrics[:1]
        assert {'gpt-6.1-sol', 'gpt-6-astra'} <= {r['upstream_model'] for r in metrics}, metrics[:1]
        assert any(r['ttft_ms'] is not None and r['tokens_per_second'] is not None for r in metrics), metrics[:1]
        assert all(r['output_tokens'] in (None, 2) for r in metrics)
        assert not any('upstream-' in json.dumps(r) or 'ordinary' in json.dumps(r) for r in metrics)
        print('PASS 全代理端真实统计队列、模型映射、流式计量与隐私白名单')
    finally:
        if service: service.shutdown(); service.server_close()
        process.terminate(); process.wait(timeout=10)
        upstream.shutdown(); upstream.server_close()
        bridge.ROOT, bridge.rs.ROOT, bridge.GATEWAY_BASE, bridge.CATALOG_PATH = old
        identity.gateway_identity, identity.GATEWAY_CONFIG = old_identity, original_config
