"""加固回归：出站重定向、不变量预检的异常吞并、以及 auth 的失败补偿。

跑法（在项目根目录）：
    python app\\tests\\test_hardening.py

**不碰生产数据**：出站重定向那组打的是本进程自己起的 302 环回服务；
另外两组把 `bridge._sources` / `rs.auth_current` / `rs.patch_auth_models`
全部换成桩，所以不会写网关、不会改 `auth/codex-official.json`、不会建备份目录。

为什么要有这个文件（三条都来自 2026-09-30 的上线就绪度审查）：

1. **`Authorization` 会跟着 3xx 走到别的域。** CPython 的
   `HTTPRedirectHandler.redirect_request` 只剔除 `content-length`/`content-type`，
   凭据原样复制且不校验主机。管理接口这条路径上带的是**网关管理密钥**（拿到就能改网关
   配置），上游那条带的是 relay key 与 OAuth token。原先全仓只有 `recheck_blocked`
   装了 NoRedirect。这条测试守的是"别再退回去"。
2. **不变量预检曾经把判据本身的异常也一起吞掉。** 那是写盘前的闸门，静默跳过就等于没有。
   现在只允许"读 plan 失败"这一种情况跳过。
3. **`auth/codex-official.json` 是"一次保存的第五个写入位置"**，原先不在
   `_state_snapshot`/`_compensate` 的清单里 —— regen 抛错时它停在新值，而补偿文案
   仍然宣称"回滚完成"。
"""
import json
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT / 'script'))

from core import agents        # noqa: E402  第 11 组用（客户端配置形状）
from core import bridge        # noqa: E402
from core import health        # noqa: E402
from core import sampling      # noqa: E402
import route_selector as rs    # noqa: E402
import server                  # noqa: E402  只在第 6 组用到（设置订阅点）

fails = []


def check(name, got, want):
    ok = got == want
    print(('  PASS  ' if ok else '  FAIL  ') + name)
    if not ok:
        print('        got : %r' % (got,))
        print('        want: %r' % (want,))
        fails.append(name)


def check_true(name, cond, detail=''):
    check(name + (('  -> ' + detail) if (detail and not cond) else ''), bool(cond), True)


SECRET = 'Bearer SECRET-SHOULD-NOT-TRAVEL'


class _ThreeOhTwo(BaseHTTPRequestHandler):
    """对任何请求都回 302 到别处。用来证明 opener 没有跟随重定向。"""

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(302)
        self.send_header('Location', 'http://198.51.100.7:9999/leak')
        self.send_header('Content-Length', '0')
        self.end_headers()


def probe_opener(opener, port, path):
    """返回 (状态码, 是否发生了重定向)。跟过去就是泄漏。"""
    req = urllib.request.Request('http://127.0.0.1:%d%s' % (port, path),
                                 headers={'Authorization': SECRET})
    try:
        with opener.open(req, timeout=5) as resp:
            return resp.status, True          # 跟到了最终响应 = 跟随了重定向
    except urllib.error.HTTPError as exc:
        return exc.code, False                # 停在 3xx 本身 = 没跟随


print('== 1. 出站请求绝不跟随重定向（凭据不外流）==')
srv = ThreadingHTTPServer(('127.0.0.1', 0), _ThreeOhTwo)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
try:
    for label, opener, path in (
            ('bridge.MGMT_OPENER（管理密钥）', bridge.MGMT_OPENER, '/v0/management/config'),
            ('sampling._MGMT_OPENER（管理密钥）', sampling._MGMT_OPENER, '/v0/management/auth-files'),
            ('rs.LOCAL_OPENER（上游 sk- / OAuth）', rs.LOCAL_OPENER, '/models'),
            ('rs.get_opener()（上游 sk- / OAuth）', rs.get_opener({}), '/models')):
        code, followed = probe_opener(opener, port, path)
        check_true('%s 停在 302' % label, code == 302, 'got HTTP %r' % code)
        check_true('%s 未跟随' % label, not followed)
finally:
    srv.shutdown()

print()
print('== 2. 不变量预检只允许"读 plan 失败"跳过 ==')
_real_sources = bridge._sources


class _Boom:
    def __init__(self, where):
        self.where = where

    def _read_plan(self):
        if self.where == 'read':
            raise RuntimeError('读 plan 炸了')
        return {'providers': []}

    def _assert_heads_ok(self, *a, **kw):
        if self.where == 'check':
            raise RuntimeError('判据自己炸了')

    def _heads_arg(self, plan):
        return None


payload = {'selected': {'gpt': ['some-id'], 'deepseek': [], 'glm': []}}
for where, label, need in (('read', '读 plan 失败', False),
                           ('check', '判据内部异常', True)):
    bridge._sources = lambda w=where: _Boom(w)
    try:
        bridge._assert_selection_heads(payload)
        raised = False
    except RuntimeError:
        raised = True
    except Exception as exc:                      # noqa: BLE001
        raised = 'other:' + type(exc).__name__
    if need:
        check_true('%s 必须上抛（不许静默跳过）' % label, raised is True, 'got %r' % raised)
    else:
        check_true('%s 允许跳过（rs 会报得更准）' % label, raised is False, 'got %r' % raised)
bridge._sources = _real_sources

print()
print('== 3. auth 是"一次保存的第五个写入位置"：快照要收、补偿要回 ==')
snap = None
_real_gw = bridge.gateway_get


class _FakeSources:
    def _backup(self, label, files):
        return Path('（桩：不建备份目录）')


bridge.gateway_get = lambda path: {'codex-api-key': [], 'openai-compatibility': []}
bridge._sources = lambda: _FakeSources()
try:
    snap = bridge._state_snapshot()
finally:
    bridge.gateway_get = _real_gw
    bridge._sources = _real_sources
check_true('_state_snapshot 收了 auth_excluded',
           'auth_excluded' in snap and snap['auth_excluded'] == rs.auth_current(),
           'got %r' % (snap or {}).get('auth_excluded'))
check_true('_state_snapshot 收了 auth_error 位', 'auth_error' in snap)

print()
print('== 4. _restore_auth_line 的四个分支 ==')
print('  ' + bridge._restore_auth_line(None, 'OSError: 读不到'))
check_true('保存前读不到 -> 跳过比对（不误报回滚成功）',
           '跳过比对' in bridge._restore_auth_line(None, 'OSError: 读不到'))
check_true('值未推进 -> 无需回滚',
           '无需回滚' in bridge._restore_auth_line(rs.auth_current(), None))

_state = {'v': ['new']}
_calls = []
_orig_cur, _orig_patch = rs.auth_current, rs.patch_auth_models
rs.auth_current = lambda: _state['v']


def _ok_patch(v):
    _calls.append(v)
    _state['v'] = v


rs.patch_auth_models = _ok_patch
msg = bridge._restore_auth_line(['old'], None)
check('推进过 -> 用保存前的值 patch 回去', _calls, [['old']])
check_true('推进过 -> 文案说已回滚', '已回滚' in msg, msg)

_calls.clear()
_state['v'] = ['new']


def _bad_patch(v):
    _calls.append(v)
    raise RuntimeError('网关拒绝')


rs.patch_auth_models = _bad_patch
msg = bridge._restore_auth_line(['old'], None)
check_true('patch 抛错 -> 如实说回滚失败（不谎报）', '回滚失败' in msg, msg)
rs.auth_current, rs.patch_auth_models = _orig_cur, _orig_patch
check_true('生产 auth 未被本测试改动',
           rs.auth_current() == json.loads(
               (ROOT / 'auth' / 'codex-official.json').read_text(encoding='utf-8-sig')
           ).get('excluded_models'))

print()
print('== 5. rs.snapshot() 对坏行容错（坏一行不该让首页整页 409）==')
# 对照：health._routing / _sources 早就逐行 try 并降级成"不知道"，snapshot 曾是唯一的例外。
# 坏行 = config 里那条来源缺失或重复 → find_entry 抛 RouteError → 旧写法整个 /api/state 409。
_tmp_dir = tempfile.mkdtemp(prefix='prism-snapshot-')
_orig = (rs.ROOT, rs.api, rs.fetch_all)
try:
    bad_plan = {'providers': [
        {'id': 'good', 'label': '好的来源', 'group': 'gpt', 'section': 'codex-api-key',
         'base_url': 'https://good.example/v1', 'tag': 't-good', 'models': [], 'expose': []},
        {'id': 'broken', 'label': '坏行', 'group': 'gpt', 'section': 'codex-api-key',
         'base_url': 'https://nowhere.example/v1', 'tag': 't-missing', 'models': [], 'expose': []},
    ]}
    Path(_tmp_dir, 'routing-plan.json').write_text(
        json.dumps(bad_plan, ensure_ascii=False), encoding='utf-8')
    rs.ROOT = Path(_tmp_dir)
    # config 里只有 good，没有 broken → find_entry('broken') 必抛。
    # good 故意**不带** excluded-models：那才算是"已启用"（active() 判据）。
    _cfg = {'codex-api-key': [{'api-key': 'k', 'base-url': 'https://good.example/v1',
                               'headers': {'X-Route-Tag': 't-good'}}],
            'openai-compatibility': []}

    def _fake_api(path, method='GET', data=None):
        if path == '/v0/management/config':
            return _cfg
        if path == '/v1/models':
            return {'data': []}
        raise AssertionError('没桩的接口：' + path)

    rs.api = _fake_api
    rs.fetch_all = lambda p, c: {x['id']: {'available': [], 'fetch_error': None, 'expose': []}
                                 for x in p['providers']}
    try:
        snap = rs.snapshot()
        raised = None
    except Exception as exc:                       # noqa: BLE001
        snap, raised = None, exc
    check_true('坏行不再让 snapshot 抛错（首页不再整页 409）', raised is None,
               'raised %r' % (raised,))
    if snap is not None:
        check('坏行被记进 unreadable（不静默显示成未启用）',
              [x['id'] for x in snap.get('unreadable', [])], ['broken'])
        check('好行照常算出（坏行不影响它）', 'good' in snap['selected']['gpt'], True)
        check('坏行没有被算成已启用', 'broken' in snap['selected']['gpt'], False)

    # 畸形的 plan 形状也不该让首页整页 409（X-01 的边界，审查 NB-03）
    Path(_tmp_dir, 'routing-plan.json').write_text(
        json.dumps({'providers': [{'label': '连 group 都没有的行'}, 'not-an-object',
                                  {'id': 'good', 'group': 'gpt', 'section': 'codex-api-key',
                                   'base_url': 'https://good.example/v1', 'tag': 't-good'}]},
                   ensure_ascii=False), encoding='utf-8')
    try:
        snap2 = rs.snapshot()
        raised2 = None
    except Exception as exc:                       # noqa: BLE001
        snap2, raised2 = None, exc
    check_true('畸形行（缺 group / 不是对象）不再让 snapshot 抛错', raised2 is None,
               'raised %r' % (raised2,))
    if snap2 is not None:
        labels = ' '.join(str(x.get('label')) for x in snap2.get('unreadable', []))
        check_true('缺 group 的行被点名', '连 group 都没有' in labels, labels)
        check_true('不是对象的行被点名', '不是对象' in labels, labels)
        check('好行仍被算出', 'good' in snap2['selected']['gpt'], True)

    # providers 根本不是列表
    Path(_tmp_dir, 'routing-plan.json').write_text('{"providers": "oops"}', encoding='utf-8')
    try:
        snap3 = rs.snapshot()
        raised3 = None
    except Exception as exc:                       # noqa: BLE001
        snap3, raised3 = None, exc
    check_true('providers 不是列表时也不抛错', raised3 is None, 'raised %r' % (raised3,))
    if snap3 is not None:
        check('providers 不是列表被点名',
              [x['label'] for x in snap3.get('unreadable', [])],
              ['routing-plan.json 的 providers 不是列表'])
finally:
    # 只恢复真的改过的：`rs.ROOT` 重定向后 `rs.snapshot()` 自己会读 <ROOT>/routing-plan.json，
    # 不需要动 bridge.PLAN_PATH（原先那对赋值是 no-op，复查轮 10 的 O-1）
    rs.ROOT, rs.api, rs.fetch_all = _orig
    shutil.rmtree(_tmp_dir, ignore_errors=True)

print()
print('== 6. 设置页改了立刻生效（采样间隔 / 历史保留天数）==')
# 这两项原先要重启 Prism 才生效：_loop 的间隔是启动时传入的固定值，prune() 又用默认
# 保留天数（HI-06）。现在配置存在内存里、由 configure() 唤醒循环。
_orig_start, _orig_sample, _orig_prune = (sampling.start_scheduler, sampling.sample_once,
                                         sampling.prune)
try:
    sampling.configure(120, 30)
    st = sampling.sample_state()
    check('configure 改间隔', st['interval_sec'], 120)
    check('configure 改保留天数', st['retention_days'], 30)

    # 走 server 那条订阅点（设置页保存时调的就是它）
    server._apply_runtime_settings({'app': {'sample_interval_sec': 900, 'retention_days': 7}})
    st = sampling.sample_state()
    check('server._apply_runtime_settings 生效（间隔）', st['interval_sec'], 900)
    check('server._apply_runtime_settings 生效（保留天数）', st['retention_days'], 7)

    # 重复 start_scheduler 必须应用新参数，而不是"已经在跑就直接返回"什么都不做
    sampling.sample_once = lambda usage=None, now=None: {'sampled_at': 'stub'}
    sampling.prune = lambda days=0: 0
    t1 = sampling.start_scheduler(600, 90)
    t2 = sampling.start_scheduler(60, 30)
    check_true('重复 start_scheduler 不新起线程', t1 is t2)
    st = sampling.sample_state()
    check('重复 start_scheduler 会应用新间隔', st['interval_sec'], 60)
    check('重复 start_scheduler 会应用新保留天数', st['retention_days'], 30)

    # 唤醒机制：正等着的那一轮要能被 configure 提前打断
    wake_t0 = time.monotonic()
    sampling.configure(600, 30)                 # 先把间隔设长，睡下去
    th = threading.Thread(target=sampling._wait_iteration, daemon=True)
    th.start()
    time.sleep(0.2)
    sampling.configure(30, 30)                  # 改设置应当立刻唤醒
    th.join(timeout=3)
    waited = time.monotonic() - wake_t0
    check_true('configure 能提前唤醒等待中的循环（不等满旧间隔）',
               not th.is_alive() and waited < 3, 'waited %.2fs' % waited)
finally:
    # **顺序是关键。** 第 6 组真的起了采样守护线程（`_loop` 是 while True），而
    # `configure()` 会 `_wake.set()` **唤醒**它 —— 唤醒后它立刻调 `sample_once()` 与
    # `prune()`。若先恢复真实函数再唤醒，那一轮就会真打网关、并真跑 `prune(90)` 去改
    # **生产 `usage-history.db`**（复查轮 10 的 F-3，与 F-1 同类，都违反 DEV-RULES E4）。
    # 所以：**先在桩还在的时候唤醒**，再恢复那些不改数据的桩（只恢复 start_scheduler，
    # 不恢复 sample_once/prune —— 进程随后就退出，恢复它们只会给守护线程重新装上真函数）。
    sampling.configure(600, 90)
    sampling.start_scheduler = _orig_start

print()
print('== 7. 版本号缓存不再被自身轮询打失效（HI-04）==')
# 网关把每个请求都写进 main.log，而 Prism 自己每 5 秒就发管理请求 —— 旧缓存键用了
# size/mtime，因此几乎永远失效，每次轮询都把整个 main.log（上限 32 MB）读一遍。
_vtmp = tempfile.mkdtemp(prefix='prism-ver-')
_orig_log, _orig_vin = health.LOG_PATH, health._version_in
_orig_cache = dict(health._version_cache)
try:
    logf = Path(_vtmp) / 'main.log'
    logf.write_text('2026-10-01 00:00:00 CLIProxyAPI Version: 7.3.9\n', encoding='utf-8')
    health.LOG_PATH = logf
    reads = []

    def _spy(path):
        reads.append(str(path))
        return _orig_vin(path)

    health._version_in = _spy
    health._version_cache.update(stamp=None, version=None)
    v1 = health.gateway_version()
    n1 = len(reads)
    with logf.open('a', encoding='utf-8') as fh:            # 模拟网关持续追加日志
        fh.write('127.0.0.1 GET /v0/management/config 200\n' * 500)
    v2 = health.gateway_version()
    n2 = len(reads)
    check('读到版本号', v1, '7.3.9')
    check_true('追加日志后不再重读（缓存键不看 size/mtime）', n2 == n1,
               '读取次数 %d -> %d' % (n1, n2))
    check('两次结果一致', v2, v1)
finally:
    health.LOG_PATH, health._version_in = _orig_log, _orig_vin
    health._version_cache.update(_orig_cache)
    shutil.rmtree(_vtmp, ignore_errors=True)

print()
print('== 8. 托盘读路由不再发 live 上游请求（HI-05）==')
_orig_fetch, _orig_snap, _orig_gwget = rs.fetch_all, rs.snapshot, bridge.gateway_get
try:
    def _boom(*a, **k):
        raise AssertionError('enabled_groups() 不该走 live fetch')

    rs.fetch_all = _boom
    rs.snapshot = _boom
    bridge.gateway_get = lambda path: {'codex-api-key': [], 'openai-compatibility': []}
    groups = bridge.enabled_groups()
    _plan = rs.read_json(rs.ROOT / 'routing-plan.json')
    check('分组与计划一致', sorted(groups.keys()), sorted(rs.group_ids(_plan)))
    check('每个分组的取值都是列表', sorted({type(v).__name__ for v in groups.values()}), ['list'])
    # 注意：这里**故意不写** `check_true(..., True)` 那种恒真断言（它永远 PASS，是假绿）。
    # "没调用 live fetch"这个属性由上面两个 _boom 桩保证：enabled_groups() 真去调
    # fetch_all/snapshot 就会抛 AssertionError，整个用例直接红。
finally:
    rs.fetch_all, rs.snapshot, bridge.gateway_get = _orig_fetch, _orig_snap, _orig_gwget

print()
print('== 9. 预检判的对象 == 本次要落盘的对象（ME-01）==')
# D2 读每行的 expose，而 rs.apply_selection 会先用 picks 覆盖它 —— 预检若只看盘上的旧值，
# 判的就是上一次的勾选。这里直接断言"交给判据的那份 providers 已经合并了 picks"。
_seen = {}


class _FakeSources:
    def _read_plan(self):
        return {'providers': [
            {'id': 'p1', 'group': 'gpt', 'section': 'codex-api-key', 'head': '',
             'expose': ['disk-only']},
            {'id': 'p2', 'group': 'gpt', 'section': 'codex-api-key', 'head': 'h',
             'expose': []},
        ]}

    def _assert_heads_ok(self, providers, enabled, agent_heads=None):
        _seen['providers'] = providers
        _seen['enabled'] = enabled

    def _heads_arg(self, plan):
        return None


_orig_src = bridge._sources
bridge._sources = lambda: _FakeSources()
try:
    bridge._assert_selection_heads({'selected': {'gpt': ['p1', 'p2']},
                                    'picks': {'p2': ['submitted', 'A/x']}})
finally:
    bridge._sources = _orig_src
_rows = {r['id']: r for r in _seen['providers']}
check('被 picks 提到的来源：用本次要写的 expose', _rows['p2']['expose'], ['submitted', 'A/x'])
check('没被 picks 提到的来源：保持盘上原值', _rows['p1']['expose'], ['disk-only'])
check('enabled 集合正确', _seen['enabled'], {'p1', 'p2'})

print()
print('== 10. 官方不可达时，没动过勾选的保存不该被拒（HI-12）==')
# auth_excluded() 在启用时要 fetch_official_models()；旧写法对每个 auth 来源**无条件**重算，
# 于是官方一不可达，用户只改了一家无关的中转商也整次保存被拒。
_t10 = tempfile.mkdtemp(prefix='prism-auth-')
_o10 = (rs.ROOT, rs.api, rs.fetch_all, rs.fetch_official_models)
try:
    Path(_t10, 'auth').mkdir()
    Path(_t10, 'auth', rs.AUTH_FILE).write_text(
        json.dumps({'excluded_models': ['gpt-reserve']}), encoding='utf-8')
    Path(_t10, 'routing-plan.json').write_text(json.dumps({'providers': [
        {'id': 'oai', 'label': '官方', 'group': 'gpt', 'section': 'auth-file',
         'base_url': 'auth://codex-official.json', 'models': [], 'expose': ['m1']}
    ]}, ensure_ascii=False), encoding='utf-8')
    _cfg10 = {'codex-api-key': [], 'openai-compatibility': []}
    rs.ROOT = Path(_t10)
    rs.api = lambda path, method='GET', data=None: (
        _cfg10 if path == '/v0/management/config'
        else {'data': []} if path == '/v1/models' else {'ok': True})
    rs.fetch_all = lambda p, c: {x['id']: {'available': [{'name': 'm1', 'alias': 'm1'},
                                                        {'name': 'm2', 'alias': 'm2'}],
                                          'fetch_error': None,
                                          'expose': list(x.get('expose') or [])}
                                 for x in p['providers']}
    _calls = []

    def _official_down():
        _calls.append(1)
        return (['m1', 'gpt-reserve'], 'network down')

    rs.fetch_official_models = _official_down

    err_a = None
    try:
        rs.apply_selection({'revision': rs.revision(_cfg10),
                            'selected': {'gpt': ['oai'], 'deepseek': [], 'glm': []},
                            'picks': {'oai': ['m1']}})      # 与盘上完全一致
    except Exception as exc:                                # noqa: BLE001
        err_a = exc
    check('勾选没变 → 一次都没去连官方', _calls, [])
    check_true('勾选没变 → 保存没有被拒', err_a is None, repr(err_a))

    _calls.clear()
    err_b = None
    try:
        rs.apply_selection({'revision': rs.revision(_cfg10),
                            'selected': {'gpt': ['oai'], 'deepseek': [], 'glm': []},
                            'picks': {'oai': ['m1', 'm2']}})    # 勾选变了
    except rs.RouteError as exc:
        err_b = exc
    check_true('勾选变了 → 仍然会去连官方（连不上就按原样拒绝）',
               bool(_calls) and err_b is not None and '无法从官方' in str(err_b),
               'calls=%d err=%r' % (len(_calls), err_b))
finally:
    rs.ROOT, rs.api, rs.fetch_all, rs.fetch_official_models = _o10
    shutil.rmtree(_t10, ignore_errors=True)

print()
print('== 11. 畸形形状不再让只读接口 500（复查轮 4 的 #2/#3/#4）==')
# #2：health 的行缺 id —— 原先 provider["id"] 直取 → KeyError → /api/monitor 整页 500
try:
    health._routing({'providers': [{}, {'group': 'gpt'}, 'not-an-object']}, None)
    _e2 = None
except Exception as exc:                            # noqa: BLE001
    _e2 = exc
check_true('health._routing 容忍缺 id / 非对象的行', _e2 is None, repr(_e2))

# #4：config 的段不是列表 / 列表里混坏条目
try:
    # source_map 只收 config（plan 它自己读，只读操作）
    sampling.source_map({'codex-api-key': {'a': 1}, 'openai-compatibility': ['oops', None, {}]})
    _e4 = None
except Exception as exc:                            # noqa: BLE001
    _e4 = exc
check_true('sampling.source_map 容忍畸形段与坏条目', _e4 is None, repr(_e4))

# #3：客户端配置顶层不是对象 → 应当 RouteError（409），不是 AttributeError（500）
_tmp3 = tempfile.mkdtemp(prefix='prism-shape-')
_orig_oc = agents._opencode_path
try:
    _bad = Path(_tmp3, 'opencode.json')
    _bad.write_text('[1,2]', encoding='utf-8')
    agents._opencode_path = lambda: _bad
    try:
        agents._opencode_preview([])
        _e3 = None
    except agents.RouteError:
        _e3 = 'RouteError'
    except Exception as exc:                        # noqa: BLE001
        _e3 = type(exc).__name__
    check('配置顶层不是对象 → RouteError（而非 500）', _e3, 'RouteError')
finally:
    agents._opencode_path = _orig_oc
    shutil.rmtree(_tmp3, ignore_errors=True)

print()
print('== 12. 两条边界（复查轮 7 的 #2/#4）==')
# #4：providers 为空是**合法状态**（全新/空配置），不该 ValueError
try:
    _r = rs.fetch_all({'providers': []}, {})
    _e = None
except Exception as exc:                            # noqa: BLE001
    _r, _e = None, exc
check_true('空 providers 时 fetch_all 不抛错', _e is None, repr(_e))
check('空 providers 时返回空表', _r, {})

# #2：opencode.json 的 "provider": null —— apply 原先 data.setdefault 返回 None → TypeError
_tmp = tempfile.mkdtemp(prefix='prism-prov-none-')
_o = (agents._opencode_path, bridge.ROOT, agents._local_key)
# **必须把 bridge.ROOT 也指到临时目录**：`_opencode_apply` 在写盘前会调 `_backup`，而它按
# `bridge.ROOT/backups/` 落盘 —— 只桩 `_opencode_path` 的话，每跑一次用例就往**生产 backups/**
# 留一个 `agent-connect-opencode-*` 目录（复查轮 9 的 F-1，实测已留 3 个），违反 DEV-RULES E4。
# 顺带把 `_local_key` 也桩掉：不依赖真实 .local-secrets.json，换机/CI 也能跑（F-2）。
_before_pollution = set(p.name for p in (ROOT / 'backups').glob('agent-connect-opencode-*'))     if (ROOT / 'backups').is_dir() else set()
try:
    _f = Path(_tmp, 'opencode.json')
    _f.write_text('{"provider": null}', encoding='utf-8')
    agents._opencode_path = lambda: _f
    bridge.ROOT = Path(_tmp)
    agents._local_key = lambda: 'test-local-key'
    try:
        agents._opencode_apply([])
        _e2 = None
    except Exception as exc:                        # noqa: BLE001 - 任何异常都算不通过
        _e2 = exc
    check_true('provider 为 null 时 apply 不抛错', _e2 is None, repr(_e2))
    if _e2 is None:
        _written = json.loads(_f.read_text(encoding='utf-8'))
        check_true('provider 被补成对象并写入 prism',
                   isinstance(_written.get('provider'), dict)
                   and agents.OPENCODE_PROVIDER_ID in _written['provider'],
                   str(_written)[:120])
    # 闸门：本用例绝不许往生产 backups/ 写东西（F-1）
    _after_pollution = set(p.name for p in (ROOT / 'backups').glob('agent-connect-opencode-*'))         if (ROOT / 'backups').is_dir() else set()
    check('用例没有往生产 backups/ 写东西（F-1 闸门）',
          sorted(_after_pollution - _before_pollution), [])
finally:
    agents._opencode_path, bridge.ROOT, agents._local_key = _o
    shutil.rmtree(_tmp, ignore_errors=True)

print()
print('RESULT: %s (%d failed)' % ('ALL PASS' if not fails else 'FAILURES', len(fails)))
for f in fails:
    print('  - ' + f)
sys.exit(1 if fails else 0)
