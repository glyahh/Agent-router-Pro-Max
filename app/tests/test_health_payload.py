"""health.py 聚合层回归：假 payload 注入，零网络、零磁盘（审查 HI-07）。

跑法（在项目根目录）：
    python app\\tests\\test_health_payload.py

**不碰生产数据**（DEV-RULES E4）：本文件只调纯解析函数，payload 全部是本文件
造出来的假数据 —— 不发任何网络请求（quota_state 传 dict、_routing 传 config 或
None、_sources 全注入），不读生产日志（gateway_version 的缓存行为已有
test_hardening §7 覆盖，这里不重复），不碰 usage-history.db。

为什么要有这个文件：监控页与用量页的上半区全是"把网关载荷翻译成界面数据"的
聚合函数，此前零测试。这类函数的典型死法是载荷形状怪一点就 500（审查里的
N3-05、Q-1 都是这么来的）——这里用形状怪一点的数据把它们钉住。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT / 'script'))

from core import health   # noqa: E402
import route_selector as rs  # noqa: E402

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


print('== 1. 版本号解析 ==')
check('三段版本', health._parse_version('7.3.9'), (7, 3, 9))
check('v 前缀', health._parse_version('v8.0'), (8, 0))
check('坏输入 None', health._parse_version('not-a-version'), None)
check('空输入 None', health._parse_version(''), None)
check('None 输入 None', health._parse_version(None), None)

print()
print('== 2. 配额窗口摊平（_norm_windows）==')
rows = health._norm_windows({'primary': {'used_percent': 75, 'window_minutes': 300,
                                         'reset_at': 1_800_000_000_000}})
check('used_percent + 分钟 + 毫秒时间戳', rows, [
    {'label': '主窗口', 'signal': 'primary', 'used_percent': 75.0,
     'window_minutes': 300, 'reset_at': 1_800_000_000}])
rows = health._norm_windows([{'remaining_percent': 30, 'window_seconds': 7200}])
check('remaining_percent 换算 + 秒换算分钟',
      (rows[0]['used_percent'], rows[0]['window_minutes']), (70.0, 120))
rows = health._norm_windows([{'used_percent': 150, 'window_minutes': 60}])
check('超界 used 夹到 100', rows[0]['used_percent'], 100.0)
rows = health._norm_windows({'primary': {'used_percent': 10, 'window_minutes': 300},
                             'secondary': {'used_percent': 20, 'window_minutes': 10080}})
check('多窗口按时长升序排列', [w['window_minutes'] for w in rows], [300, 10080])
check('周窗口标签', rows[1]['label'], '周窗口')
rows = health._norm_windows({'nested': {'primary': {'used_percent': 5, 'window_minutes': 300}}})
check('嵌套一层摊平（depth 守卫）', len(rows), 1)
check_true('非 dict/list 输入回空表', health._norm_windows('junk') == []
           and health._norm_windows([None, 42]) == [])

print()
print('== 3. quota_state（注入 auth_files，零网络）==')
payload = {'observed_at': '2026-10-01T12:00:00', 'files': [
    {'account': 'someone@example.com', 'id_token': {'plan_type': 'pro'},
     'quota': {'signals': {'primary': {'used_percent': 100, 'window_minutes': 300}}}}]}
state = health.quota_state(payload)
check('满额账号：available、账号、档位', (state['available'], state['account'],
                                          state['plan_type']),
      (True, 'someone@example.com', 'pro'))
check('窗口行数', len(state['windows']), 1)
check('observed_at 透传', state['observed_at'], '2026-10-01T12:00:00')
state = health.quota_state(None)
check('网关不可达分支', (state['available'], state['note']),
      (False, '网关不可达，配额读不到'))
state = health.quota_state({})
check('空载荷是"尚无观测"不是 0%', state['available'], False)
check_true('空载荷的 note 说清原因',
           '尚无配额观测' in (state['note'] or ''), str(state['note']))
state = health.quota_state({'files': ['junk', 42]})
check('坏行跳过不炸', state['available'], False)
state = health.quota_state({'files': [{'account': 'a@b.c'}]})
check('有账号但无信号：note 口径', (state['account'], state['available']),
      ('a@b.c', False))

print()
print('== 4. plan 行过滤（_plan_rows）==')
check('plan 非对象回空', health._plan_rows('junk'), [])
check('providers 非列表回空', health._plan_rows({'providers': 'junk'}), [])
check('混入坏行只留对象', health._plan_rows(
    {'providers': [{'id': 'ok'}, 'junk', None, 42]}), [{'id': 'ok'}])

print()
print('== 5. 路由聚合（_routing）==')
plan = {'providers': [
    {'id': 'alpha', 'group': 'gpt', 'expose': ['gpt-5.6-sol']},
    {'id': 'beta', 'group': 'glm', 'expose': ['glm-5.3-flash']},
    {'id': '', 'group': 'gpt'},            # 缺 id 的坏行：跳过，不 KeyError
    'junk',
]}
out = health._routing(plan, None)
check('config=None：selected 全空', out['selected'],
      {'gpt': [], 'deepseek': [], 'glm': []})
check('candidates 按分组归堆（坏行不进）', out['candidates'],
      {'gpt': ['alpha'], 'glm': ['beta']})
check('config=None 的 note', out['note'], '网关不可达，无法判定当前路由')
# 启用判定转调 route_selector（它有自己的测试），这里给一条真 apikey 行证明
# 管线通：find_entry + active 都吃标准 config 形状。
config = {'codex-api-key': [{'api-key': 'sk-test', 'base-url': 'https://relay.example/v1'}]}
provider = {'id': 'alpha', 'group': 'gpt', 'section': 'codex-api-key',
            'api_key': 'sk-test', 'base_url': 'https://relay.example/v1'}
try:
    entry = rs.find_entry(config, provider)
    live = rs.active(entry, provider['section'])
    check_true('route_selector 判定管线接通（返回值是 bool）', isinstance(live, bool), repr(live))
except Exception as exc:                            # noqa: BLE001 - 这里抛了就是判据漂了
    check_true('route_selector 判定管线接通', False, repr(exc))

print()
print('== 6. 冷却解析（_cooldown_of）==')
check('list[dict] 形状', health._cooldown_of(
    {'cooldowns': [{'until': '2026-10-01T00:00:00', 'reason': '配额'}]}),
    {'until': '2026-10-01T00:00:00', 'reason': '配额'})
check('list[str] 形状', health._cooldown_of({'cooldowns': ['paused']}),
      {'until': None, 'reason': 'paused'})
check('dict 形状', health._cooldown_of({'cooldowns': {'until': 'T1', 'reason': 'R1'}}),
      {'until': 'T1', 'reason': 'R1'})
check('无冷却回 None', health._cooldown_of({}), None)
check('空列表回 None', health._cooldown_of({'cooldowns': []}), None)

print()
print('== 7. config 健康度三态（_config_ok）==')
check('None 是"不知道"', health._config_ok(None), None)
check('非对象是 False', health._config_ok(['junk']), False)
check('空对象是 False（一个来源段都没有）', health._config_ok({}), False)
check('两个段都空但形状对是 True', health._config_ok(
    {section: [] for section in rs.SECTIONS}), True)

print()
print('== 8. 闸门缓存（_cached_monitor）==')
cached = {'gateway': {'running': True, 'error': '旧错误'}, 'version': '8.0.4'}
out = health._cached_monitor(cached, {'blocked': True, 'message': '已封禁'})
check('running/version 原样保留', (out['gateway']['running'], out['version']), (True, '8.0.4'))
check_true('封禁说明拼上旧错误', out['gateway']['error'] == '已封禁；上次的说明：旧错误',
           out['gateway']['error'])
check('cached 标记', out['cached'], True)
check_true('入参没被原地改（deepcopy 生效）', 'cached' not in cached)

print()
print('RESULT: %s (%d failed)' % ('ALL PASS' if not fails else 'FAILURES', len(fails)))
for f in fails:
    print('  - ' + f)
sys.exit(1 if fails else 0)
