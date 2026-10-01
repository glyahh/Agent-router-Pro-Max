"""HTTP 层回归：server.py 的安全闸、body 纪律、错误映射与静态文件（审查 HI-07）。

跑法（在项目根目录）：
    python app\\tests\\test_server_http.py

**不碰生产数据**（DEV-RULES E4）：
* 全管线组用 `Handler.__new__` 造实例、rfile/wfile 都是 BytesIO，请求到不了任何端口；
* 端到端组用 `create_server(0)` 起在随机回环端口，打的是本测试进程自己；
* 分发路径会碰到的 `bridge.snapshot / apply_selection / gateway_get / gateway_put`
  与 `sampling.prune` 全部换成桩，设置写路径把 `server.SETTINGS_PATH` 重定向到
  临时目录 —— 不会写生产 settings.json、不会碰 8317 网关、不会动 usage-history.db；
* 末组闸门核对生产 usage-history.db 的 mtime+size 在跑完前后零变化。
* 全程零出站网络（不出本机回环），不触发 `bridge.auth_gate()`。
"""
import io
import json
import shutil
import sys
import tempfile
import threading
import types
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT / 'script'))

from core import bridge, sampling  # noqa: E402
import server                      # noqa: E402

fails = []
# E4 闸门基线：进入本文件前生产库的指纹，退出前必须原样
_DB_PATH = ROOT / 'usage-history.db'
_DB_BEFORE = (_DB_PATH.stat().st_mtime_ns, _DB_PATH.stat().st_size) if _DB_PATH.exists() else None


def check(name, got, want):
    ok = got == want
    print(('  PASS  ' if ok else '  FAIL  ') + name)
    if not ok:
        print('        got : %r' % (got,))
        print('        want: %r' % (want,))
        fails.append(name)


def check_true(name, cond, detail=''):
    check(name + (('  -> ' + detail) if (detail and not cond) else ''), bool(cond), True)


FAKE_PORT = 8318


def _response(wfile):
    """把 BytesIO 里攒出的 HTTP 响应解析成 (status, headers, body_bytes)。"""
    raw = wfile.getvalue()
    head, _, body = raw.partition(b'\r\n\r\n')
    lines = head.split(b'\r\n')
    status = int(lines[0].split()[1])
    headers = Message()
    for line in lines[1:]:
        name, _, value = line.decode('iso-8859-1').partition(':')
        headers[name.strip()] = value.strip()
    return status, headers, body


def call(method, path, headers=None, body=b'', **handler_attrs):
    """造一个绕过 socket 的 Handler 实例，驱动完整管线（_handle）。

    覆盖面与真请求一致：读 body → 校验 Host/Origin → 分发 → 错误映射 → 写响应。
    """
    h = server.Handler.__new__(server.Handler)
    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()
    h.headers = Message()
    for key, value in (headers or {}).items():
        h.headers[key] = value
    h.path = path
    h.command = method
    h.request_version = 'HTTP/1.1'
    h.requestline = '%s %s HTTP/1.1' % (method, path)
    h.client_address = ('127.0.0.1', 51000)
    h.close_connection = False
    h.server = types.SimpleNamespace(console_port=FAKE_PORT,
                                     server_address=('127.0.0.1', FAKE_PORT))
    for key, value in handler_attrs.items():
        setattr(h, key, value)
    h._handle(method)
    return _response(h.wfile) + (h,)


def api(method, path, headers=None, body=b'', **handler_attrs):
    """Host 合法的请求快捷方式。"""
    merged = {'Host': '127.0.0.1:%d' % FAKE_PORT}
    merged.update(headers or {})
    return call(method, path, merged, body, **handler_attrs)


def jbody(obj):
    raw = json.dumps(obj).encode('utf-8')
    return {'Content-Length': str(len(raw))}, raw


print('== 1. Host/Origin 双闸 ==')
st, _, body_bytes, _ = call('GET', '/api/state')
check('缺 Host 一律 409', st, 409)
check('409 是 JSON 的 ok:false', json.loads(body_bytes),
      {'ok': False, 'error': '仅允许本机地址访问（Host 必须是 127.0.0.1:8318，收到 \'空\'）'})
st, _, _, _ = call('GET', '/api/state', {'Host': 'localhost:8318'})
check('localhost 被 409 拒绝（DNS rebinding 闸）', st, 409)
st, _, _, _ = api('GET', '/api/state', {'Origin': 'http://evil.example'})
check('异源 Origin 被 409 拒绝', st, 409)
st, _, _, _ = api('GET', '/api/theme', {'Origin': 'http://127.0.0.1:8318'})
check('同源 Origin 放行', st, 200)

print()
print('== 2. body 纪律（先读完 body 再校验；判 None 不判真假）==')
st, _, body_bytes, h = call('POST', '/api/recheck',
                           {'Host': '127.0.0.1:8318', 'Transfer-Encoding': 'chunked'})
check('chunked 无 Content-Length 是 411', st, 411)
check_true('411 主动断开连接', h.close_connection)
st, _, _, h = api('POST', '/api/recheck', {'Content-Length': str(server.MAX_BODY + 1)})
check('超 MAX_BODY 是 413', st, 413)
check_true('413 主动断开连接', h.close_connection)
st, _, _, _ = api('POST', '/api/recheck', {'Content-Length': 'abc'})
check('Content-Length 非整数是 400', st, 400)
st, _, _, _ = api('POST', '/api/settings', {'Content-Length': '4'}, b'no json')
check('body 非法 JSON 是 400', st, 400)
st, _, _, _ = api('POST', '/api/settings', {'Content-Length': '2'}, b'[]')
check('body 是 JSON 数组是 400（必须是对象）', st, 400)

# {} 是合法 body：分发层的 apply_selection 桩成 RouteError，撞上 409 就证明
# 请求没被 400"请求体是空的"拦掉（前端 post() 空 body 发的正是 {}）。
_real_apply = bridge.apply_selection
try:
    bridge.apply_selection = lambda body: (_ for _ in ()).throw(
        bridge.RouteError('假桩：请求已进分发'))
    hdrs, raw = jbody({})
    st, _, body_bytes, _ = api('POST', '/api/select', hdrs, raw)
    check('{} 是合法 body（判 None 不判真假）', (st, json.loads(body_bytes)['error']),
          (409, '假桩：请求已进分发'))
finally:
    bridge.apply_selection = _real_apply
st, _, _, _ = api('POST', '/api/select')
check('POST /api/select 缺 body 是 400', st, 400)
st, _, _, _ = api('PUT', '/api/sources/x')
check('PUT 缺 body 是 400', st, 400)

print()
print('== 3. 错误映射与未知路径 ==')
_real_snapshot = bridge.snapshot
try:
    bridge.snapshot = lambda: (_ for _ in ()).throw(
        bridge.RouteError('网关不可达（假桩）'))
    st, _, body_bytes, _ = api('GET', '/api/state')
    check('RouteError 映射 409，消息透传给用户', (st, json.loads(body_bytes)['error']),
          (409, '网关不可达（假桩）'))
    bridge.snapshot = lambda: (_ for _ in ()).throw(RuntimeError('boom'))
    st, _, body_bytes, _ = api('GET', '/api/state')
    payload = json.loads(body_bytes)
    check('未预期异常兜底 500', st, 500)
    check_true('500 文案带异常类型与日志路径',
               'RuntimeError' in payload['error'] and 'prism.log' in payload['error'],
               payload['error'][:120])
finally:
    bridge.snapshot = _real_snapshot
st, _, _, _ = api('GET', '/api/definitely-not-here')
check('未知 API 是 404', st, 404)
st, _, body_bytes, _ = api('PATCH', '/api/state')
check('PATCH 是 405 且仍是 JSON 包装（不是 501 HTML）',
      (st, json.loads(body_bytes)['ok']), (405, False))

print()
print('== 4. 响应公共头 ==')
st, hd, _, _ = api('GET', '/api/theme')
check_true('CSP 的 script-src 不含 unsafe-inline', "script-src 'self'" in hd['Content-Security-Policy'])
check('Cache-Control no-store', hd['Cache-Control'], 'no-store')
check('nosniff', hd['X-Content-Type-Options'], 'nosniff')

print()
print('== 5. 静态文件与目录穿越 ==')
st, hd, body_bytes, _ = api('GET', '/')
check('GET / 是 200 HTML', (st, hd['Content-Type'].split(';')[0]), (200, 'text/html'))
check_true('index.html 已做主题替换（data-theme 出现）', b'data-theme="' in body_bytes)
st, hd, _, _ = api('GET', '/app.js')
check('GET /app.js 的 MIME', hd['Content-Type'].split(';')[0], 'text/javascript')
st, _, _, _ = api('GET', '/../server.py')
check('目录穿越被 404 挡住（../server.py）', st, 404)
st, _, _, _ = api('GET', '/..%2Fserver.py')
check('URL 编码的目录穿越同样 404（unquote 后再判）', st, 404)

print()
print('== 6. /api/theme 与 /api/settings（桩掉网关读写与 prune）==')
st, _, body_bytes, _ = api('GET', '/api/theme')
payload = json.loads(body_bytes)
check_true('/api/theme 形状 {theme, system}',
           st == 200 and payload['ok'] and set(payload['data']) == {'theme', 'system'}
           and payload['data']['theme'] in ('dark', 'light')
           and payload['data']['system'] in ('dark', 'light'), body_bytes[:120].decode('utf-8', 'replace'))

_real_gget, _real_gput = bridge.gateway_get, bridge.gateway_put
_real_prune = sampling.prune
_real_spath = server.SETTINGS_PATH
_tmp = tempfile.mkdtemp(prefix='prism-settings-')
_fake_settings = Path(_tmp, 'settings.json')
try:
    bridge.gateway_get = lambda endpoint: {}
    bridge.gateway_put = lambda endpoint, payload: None
    sampling.prune = lambda days: None
    st, _, body_bytes, _ = api('GET', '/api/settings')
    payload = json.loads(body_bytes)
    check_true('/api/settings 200 且带 gateway/app/notes',
               st == 200 and set(payload['data']) == {'gateway', 'app', 'notes'}, body_bytes[:160].decode('utf-8', 'replace'))

    server.SETTINGS_PATH = _fake_settings
    hdrs, raw = jbody({'app': {'theme_mode': 'dark', 'sample_interval_sec': 120,
                               'retention_days': 90},
                       'gateway': {}})
    st, _, body_bytes, _ = api('POST', '/api/settings', hdrs, raw)
    check('合法设置保存是 200', st, 200)
    saved = json.loads(_fake_settings.read_text(encoding='utf-8'))
    check_true('设置写进了临时 settings.json（theme_mode=dark）',
               saved['app']['theme_mode'] == 'dark', str(saved)[:120])
    check_true('采样间隔落在合法范围', saved['app']['sample_interval_sec'] == 120)
    before_bad = _fake_settings.read_text(encoding='utf-8')
    bad_hdrs, bad = jbody({'app': {'sample_interval_sec': 1}})
    st, _, _, _ = api('POST', '/api/settings', bad_hdrs, bad)
    check('采样间隔越界是 400', st, 400)
    check('越界请求一个字节都不落盘', _fake_settings.read_text(encoding='utf-8'), before_bad)
finally:
    bridge.gateway_get, bridge.gateway_put = _real_gget, _real_gput
    sampling.prune = _real_prune
    server.SETTINGS_PATH = _real_spath
    shutil.rmtree(_tmp, ignore_errors=True)

print()
print('== 7. 端到端（真实 socket，随机回环端口）==')
import socket
_probe = socket.socket()
_probe.bind(('127.0.0.1', 0))
_free_port = _probe.getsockname()[1]
_probe.close()
httpd = server.create_server(_free_port)
thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.05},
                          daemon=True)
thread.start()
try:
    base = 'http://127.0.0.1:%d' % httpd.console_port
    with urllib.request.urlopen(base + '/', timeout=5) as resp:
        raw = resp.read()
        check('真端口 GET / 是 200', resp.status, 200)
        check_true('真端口的 index.html 带主题属性', b'data-theme="' in raw)
        check_true('真端口同样带 CSP 头',
                   resp.headers['Content-Security-Policy'].startswith("default-src 'self'"))
    try:
        urllib.request.urlopen(base + '/api/nope', timeout=5)
        check('真端口未知 API 是 404', False, '不应到达这里')
    except urllib.error.HTTPError as exc:
        check('真端口未知 API 是 404 JSON', (exc.code, json.loads(exc.read())['ok']), (404, False))
    # Host 闸在真 socket 上同样生效：把 URL 的主机名换成 localhost（urllib 会照抄进
    # Host 头），服务端必须拒绝 —— 这是 DNS rebinding 防线在真实栈上的那次复核。
    try:
        urllib.request.urlopen(base.replace('127.0.0.1', 'localhost') + '/api/theme', timeout=5)
        check('localhost Host 在真端口被 409 拒绝', False, '不应到达这里')
    except urllib.error.HTTPError as exc:
        check('localhost Host 在真端口被 409 拒绝', exc.code, 409)
finally:
    server.stop_server(httpd)

print()
print('== 8. 控制台令牌闸（ME-12：桌面版启用，调试形态不启用）==')
_real_snapshot = bridge.snapshot
bridge.snapshot = lambda: {'ok-marker': True}
_tport = None
_probe2 = socket.socket()
_probe2.bind(('127.0.0.1', 0))
_tport = _probe2.getsockname()[1]
_probe2.close()
_tokened = server.create_server(_tport, token='tok-abc-123')
_tthread = threading.Thread(target=_tokened.serve_forever, kwargs={'poll_interval': 0.05},
                            daemon=True)
_tthread.start()
try:
    base_t = 'http://127.0.0.1:%d' % _tokened.console_port
    # 端到端：令牌闸只在桌面版启用
    try:
        urllib.request.urlopen(base_t + '/api/theme', timeout=5)
        check('启用令牌：无头 /api/* 被 401 拒', False, '不应到达')
    except urllib.error.HTTPError as exc:
        check('启用令牌：无头 /api/* 被 401 拒', exc.code, 401)
        check_true('401 是 JSON 的 ok:false', json.loads(exc.read())['ok'] is False)
    req = urllib.request.Request(base_t + '/api/theme',
                                 headers={'X-Prism-Token': 'wrong'})
    try:
        urllib.request.urlopen(req, timeout=5)
        check('启用令牌：错头同样 401', False, '不应到达')
    except urllib.error.HTTPError as exc:
        check('启用令牌：错头同样 401', exc.code, 401)
    req = urllib.request.Request(base_t + '/api/theme',
                                 headers={'X-Prism-Token': 'tok-abc-123'})
    with urllib.request.urlopen(req, timeout=5) as resp:
        check('启用令牌：对头放行', resp.status, 200)
    with urllib.request.urlopen(base_t + '/', timeout=5) as resp:
        check('启用令牌：静态资源不拦（页面要先加载）', resp.status, 200)
finally:
    server.stop_server(_tokened)

# 纯分发：fake 实例的 server 没有 console_token 属性 → 不启用，行为与旧版完全一致
st, _, _, _ = api('GET', '/api/theme')
check('调试形态（无 token 属性）：/api/* 照常放行', st, 200)
bridge.snapshot = _real_snapshot

print()
print('== 9. 生产数据污染闸门（E4）==')
_db_after = (_DB_PATH.stat().st_mtime_ns, _DB_PATH.stat().st_size) if _DB_PATH.exists() else None
check('usage-history.db 跑完前后零变化', _db_after, _DB_BEFORE)
backups_dir = ROOT / 'backups'
auto_dirs = sorted(p.name for p in backups_dir.glob('prism-settings-*')) if backups_dir.is_dir() else []
check('临时目录没有漏进生产 backups/', auto_dirs, [])

print()
print('RESULT: %s (%d failed)' % ('ALL PASS' if not fails else 'FAILURES', len(fails)))
for f in fails:
    print('  - ' + f)
sys.exit(1 if fails else 0)
