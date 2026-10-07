"""渠道头（head）的回归测试。

跑法（在项目根目录）：
    python app\\tests\\test_heads.py

只读 routing-plan.json，**不写任何东西**。改动 sources.py 的渠道头逻辑后必须跑一遍。

为什么要有这个文件：渠道头的判据分"静态"和"动态"两类，而动态那两条**必须按已启用
的来源判**。第一版把它们写成按全部行判，结果把本来就合法的存量配置（9 行 / 0 头 /
gpt 组 5 行全无头）判成 13 条冲突，而且以后每加一个来源都会被拦住。这类错误在界面上
只表现为"点保存被拒"，看不出判据错在哪，所以固化成测试。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT / 'script'))

from core import sources as S   # noqa: E402

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


def any_msg(problems, needle):
    return any(needle in p for p in problems)


print('== client_id / strip_head ==')
check('无头原样', S.client_id('', 'gpt-5.6-sol'), 'gpt-5.6-sol')
check('有头拼接', S.client_id('srapi', 'gpt-5.6-sol'), 'srapi/gpt-5.6-sol')
check('head 带空白也归一', S.client_id('  srapi  ', 'x'), 'srapi/x')
check('strip 有头', S.strip_head('srapi/gpt-5.6-sol', ['srapi']), ('srapi', 'gpt-5.6-sol'))
check('strip 无头', S.strip_head('gpt-5.6-sol', ['srapi']), ('', 'gpt-5.6-sol'))
check('strip 最长优先', S.strip_head('ab/x', ['a', 'ab']), ('ab', 'x'))
check('strip 不误伤 A/ 遗留', S.strip_head('A/gpt-5.6-sol', ['srapi']), ('', 'A/gpt-5.6-sol'))

print('== HEAD_RE ==')
for good in ('srapi', 'hub-main', 'goat', 'a', 'openai.official', 'x_1'):
    check_true('合法 head %r' % good, bool(S.HEAD_RE.match(good)))
# 禁大写让渠道头永远撞不上遗留 A/ 别名（'kk/x' != 'A/x'）；禁斜杠让有头 ID 与无头 ID
# 结构上不可能撞车。
for bad in ('SRAPI', 'sr api', 'sra/pi', '-lead', '.lead', '', 'x' * 25):
    check_true('非法 head %r' % bad, not S.HEAD_RE.match(bad))

print('== row_client_ids ==')
check('有头行产出',
      S.row_client_ids({'head': 'srapi', 'expose': ['gpt-5.6-sol', 'A/gpt-5.6-sol', 'gpt-5.5']}),
      ['srapi/gpt-5.6-sol', 'srapi/gpt-5.5'])
check('无头行产出', S.row_client_ids({'head': '', 'expose': ['gpt-5.6-sol']}), ['gpt-5.6-sol'])

print('== 静态检查（enabled_ids=None，只看头的格式与唯一性）==')
check('无头行不算问题',
      S.plan_head_conflicts([{'id': 'a', 'group': 'gpt', 'head': '', 'expose': ['m']}]), [])
dup = [{'id': 'a', 'group': 'gpt', 'head': 'same', 'expose': ['m1']},
       {'id': 'b', 'group': 'deepseek', 'head': 'same', 'expose': ['m2']}]
check_true('重复 head 被拦', any_msg(S.plan_head_conflicts(dup), '必须唯一'))
bad = [{'id': 'a', 'group': 'gpt', 'head': 'Bad Head', 'expose': []}]
check_true('非法 head 被拦', any_msg(S.plan_head_conflicts(bad), '不合法'))
# LO-05：HEAD_RE 首字符只许字母/数字，报错文案必须把这一点也说清（原来只说点和连字符）。
lead_bad = [{'id': 'a', 'group': 'gpt', 'head': '_lead', 'expose': []}]
check_true('_ 开头的报错文案点明首字符要求',
           any_msg(S.plan_head_conflicts(lead_bad), '字母或数字开头'))

print('== 动态检查（按"已启用"判）==')
two_bare = [{'id': 'oai', 'group': 'gpt', 'head': '', 'expose': ['gpt-5.6-sol']},
            {'id': 'srapi', 'group': 'gpt', 'head': '', 'expose': ['gpt-5.6-sol']}]
check('两个无头都未启用 = 通过', S.plan_head_conflicts(two_bare, {'x'}), [])
p = S.plan_head_conflicts(two_bare, {'oai', 'srapi'})
check_true('两个无头同时启用被拦', any_msg(p, '没有渠道头'))
print('        -> ' + (p[0] if p else '(none)'))
check('只启用其中一个 = 通过', S.plan_head_conflicts(two_bare, {'oai'}), [])

mixed = [{'id': 'oai', 'group': 'gpt', 'head': '', 'expose': ['gpt-5.6-sol']},
         {'id': 'srapi', 'group': 'gpt', 'head': 'srapi', 'expose': ['gpt-5.6-sol']}]
check('一主一附同时启用 = 通过（需求 5 的目标形态）',
      S.plan_head_conflicts(mixed, {'oai', 'srapi'}), [])

per_group = [{'id': 'oai', 'group': 'gpt', 'head': '', 'expose': ['m']},
             {'id': 'ds', 'group': 'deepseek', 'head': '', 'expose': ['m']}]
check('跨组同名但只启用一个 = 通过', S.plan_head_conflicts(per_group, {'oai'}), [])
p = S.plan_head_conflicts(per_group, {'oai', 'ds'})
check_true('跨组同名且都启用被拦（/v1/models 是全局合并列表）', any_msg(p, '同时暴露'))
print('        -> ' + (p[0] if p else '(none)'))

legacy = [
    {'id': 'proxyhub', 'group': 'gpt', 'head': 'ph', 'expose': ['gpt-5.6-sol'],
     'models': [{'name': 'A/gpt-5.6-sol', 'alias': 'A/gpt-5.6-sol'}]},
    {'id': 'kkapi', 'group': 'gpt', 'head': 'kk', 'expose': ['gpt-5.6-sol'],
     'models': [{'name': 'A/gpt-5.6-sol', 'alias': 'A/gpt-5.6-sol'}]},
]
# 生产真实形状：routing-plan.json 的 proxyhub 与 kkapi 都带 A/gpt-5.6-sol。
# 两个都启用时两边都会把它写进 config，网关的合并列表就歧义了。
p = S.plan_head_conflicts(legacy, {'proxyhub', 'kkapi'})
check_true('两个启用行带同一个遗留 A/ 别名被拦', any_msg(p, '遗留别名'))
print('        -> ' + (p[0] if p else '(none)'))
check('只启用其中一个 = 通过', S.plan_head_conflicts(legacy, {'kkapi'}), [])
check('有头 ID 撞不上遗留 A/ 别名',
      S.plan_head_conflicts([{'id': 'a', 'group': 'gpt', 'head': 'kk', 'expose': ['gpt-6-astra']},
                             {'id': 'b', 'group': 'gpt', 'head': 'ph', 'expose': [],
                              'models': [{'name': 'A/gpt-6-astra', 'alias': 'A/gpt-6-astra'}]}],
                            {'a', 'b'}), [])

print('== _clean_spec ==')
clean = S._clean_spec({'label': 'T', 'group': 'gpt', 'base_url': 'https://e.com/v1',
                       'api_key': 'k', 'head': ' srapi ',
                       'models': [{'name': 'gpt-5.6-sol', 'alias': 'gpt-5.6-sol'}]}, True)
check('head 去空白', clean['head'], 'srapi')
clean2 = S._clean_spec({'label': 'T', 'group': 'gpt', 'base_url': 'https://e.com/v1',
                        'api_key': 'k', 'models': [{'name': 'm', 'alias': 'm'}]}, True)
check('缺省 head = 空串', clean2['head'], '')
try:
    S._clean_spec({'label': 'T', 'group': 'gpt', 'base_url': 'https://e.com/v1',
                   'api_key': 'k', 'head': 'sr/pi',
                   'models': [{'name': 'm', 'alias': 'm'}]}, True)
    check_true('非法 head 抛错', False)
except S.RouteError as e:
    check_true('非法 head 抛错', '不合法' in str(e))
    print('        -> ' + str(e))

print('== _view / resolve_client_settings ==')
v = S._view({'id': 'srapi', 'label': 'SRAPI', 'group': 'gpt', 'head': 'srapi',
             'expose': ['gpt-5.6-sol']})
check('_view 带 head', v['head'], 'srapi')
check('_view 带 client_ids', v['client_ids'], ['srapi/gpt-5.6-sol'])
v2 = S._view({'id': 'old', 'label': 'x', 'group': 'gpt'})
check('_view 缺 head 时补空串', v2['head'], '')
check('_view 缺 head 时 client_ids 为空', v2['client_ids'], [])

plan = {'providers': [
    {'id': 'openai-official', 'label': 'OpenAI 官方', 'head': '',
     'model_settings': {'gpt-5.6-sol': {'levels': ['high'], 'default': 'high'}}},
    {'id': 'srapi', 'label': 'SRAPI', 'head': 'srapi',
     'model_settings': {'gpt-5.6-sol': {'levels': ['max'], 'default': 'max'}}},
]}
old = S.resolve_model_settings(plan)
new = S.resolve_client_settings(plan)
check('旧映射键 = 干净别名', sorted(old.keys()), ['gpt-5.6-sol'])
check('旧映射归属 = 先声明者', old['gpt-5.6-sol']['id'], 'openai-official')
check('新映射键 = 客户端 ID', sorted(new.keys()), ['gpt-5.6-sol', 'srapi/gpt-5.6-sol'])
check('新映射 srapi 行归 srapi', new['srapi/gpt-5.6-sol']['id'], 'srapi')
check('新映射档位正确', new['srapi/gpt-5.6-sol']['settings']['default'], 'max')

print('== 存量 routing-plan.json（只读）==')
live = json.loads((ROOT / 'routing-plan.json').read_text(encoding='utf-8-sig'))
provs = live['providers']
print('  来源数: %d，其中有 head 字段的: %d'
      % (len(provs), sum(1 for x in provs if 'head' in x)))
# 这条是回归闸门：判据写错（按全部行判）时这里会冒出十几条冲突。
check('存量配置静态检查通过', S.plan_head_conflicts(provs), [])
print('  模拟"gpt 组两个无头同时启用"能拦下来: %r'
      % (any_msg(S.plan_head_conflicts(provs, {'openai-official', 'proxyhub'}), '没有渠道头'),))

print('== 按代理端分开的渠道头 ==')
import copy
import route_selector as rs

row = {'id': 'srapi', 'label': 'SRAPI', 'group': 'gpt', 'head': 'old',
       'expose': ['gpt-5.6-sol']}
split = {'providers': [row], 'agent_heads': {'codex': {'srapi': 'gly'}}}
check('Codex 用自己设的头', S.effective_head(split, 'codex', row), 'gly')
check('没设过的 Claude Code 回落行上的头', S.effective_head(split, 'claude-code', row), 'old')
shared = {'providers': [row], 'agent_heads': {'claude-code': {'srapi': ''}}}
check('桌面端和 Claude Code 共用一份', S.effective_head(shared, 'claude-code-desktop', row), '')
check('空字符串是主，不再回落行上的头', S.effective_head(shared, 'claude-code', row), '')
try:
    S.head_owner('copilot-agent')
    check_true('Copilot Agent 不存渠道头', False)
except S.RouteError as e:
    check_true('Copilot Agent 不存渠道头', '不使用渠道头' in str(e))

same = [{'id': 'srapi', 'group': 'gpt', 'head': 'old', 'expose': ['gpt-5.6-sol']}]
ok_heads = {'codex': {'srapi': 'gly'}, 'claude-code': {'srapi': ''}}
check('同一来源两个代理端不同头可以通过',
      S.plan_head_conflicts(same, {'srapi'}, ok_heads), [])
both_gly = {'codex': {'srapi': 'gly'}, 'opencode': {'srapi': 'gly'}}
check('同一来源在多个代理端用同一个头可以通过',
      S.plan_head_conflicts(same, {'srapi'}, both_gly), [])
two_src = [
    {'id': 'a', 'group': 'gpt', 'head': '', 'expose': ['m']},
    {'id': 'b', 'group': 'deepseek', 'head': '', 'expose': ['n']},
]
check_true('两个来源抢同一个头被拦',
           any_msg(S.plan_head_conflicts(two_src, None, {'codex': {'a': 'gly'}, 'claude-code': {'b': 'gly'}}),
                   '必须唯一'))

mains = [
    {'id': 'openai-official', 'group': 'gpt', 'head': '', 'expose': ['gpt-5.6-sol']},
    {'id': 'srapi', 'group': 'gpt', 'head': 'srapi', 'expose': ['gpt-5.6-sol']},
]
user_rows = [
    {'id': 'openai-official', 'group': 'gpt', 'head': '', 'expose': ['gpt-6.1-sol']},
    {'id': 'srapi', 'group': 'gpt', 'head': '', 'expose': ['gpt-6.1-sol']},
]
user_heads = {'codex': {'srapi': 'gly'}}
check('只算已接入的 Codex：主和 gly 不冲突',
      S.plan_head_conflicts(user_rows, {'openai-official', 'srapi'}, user_heads, {'codex'}), [])
check_true('Claude Code 也接入时仍拦两个无头',
           any_msg(S.plan_head_conflicts(user_rows, {'openai-official', 'srapi'}, user_heads,
                                         {'codex', 'claude-code'}),
                   'Claude Code'))
check_true('没传接入名单时仍查全部代理端',
           any_msg(S.plan_head_conflicts(user_rows, {'openai-official', 'srapi'}, user_heads),
                   'Claude Code'))
check('只算已接入时网关只写 Codex 的头',
      rs._gateway_heads({'agent_heads': user_heads}, user_rows[1], {'codex'}), ['gly'])
check('没传名单时没设过的代理端回落成无头',
      rs._gateway_heads({'agent_heads': user_heads}, user_rows[1]), ['gly', ''])

clash = S.plan_head_conflicts(mains, {'openai-official', 'srapi'}, {'claude-code': {'srapi': ''}})
clash_text = '；'.join(clash)
check_true('两个主撞同一个模型 ID 被拦', '同时暴露' in clash_text)
check_true('撞车文案点明两个代理端', 'Codex' in clash_text and 'Claude Code' in clash_text)
check_true('撞车文案点明两家来源', 'openai-official' in clash_text and 'srapi' in clash_text)
print('        -> ' + (clash[0] if clash else '(none)'))

alias = 'gpt-5.6-sol'
src = {'id': 'srapi', 'label': 'SRAPI', 'group': 'gpt', 'section': 'openai-compatibility',
       'base_url': 'https://srapi.example/v1', 'tag': 'srapi', 'head': 'old',
       'expose': [alias], 'available': [{'name': alias, 'alias': alias}], 'models': []}
cfg = {'codex-api-key': [], 'openai-compatibility': [{
    'base-url': src['base_url'], 'headers': {'X-Route-Tag': 'srapi'},
    'models': [], 'api-key': 'k'}]}
sel = {'gpt': ['srapi'], 'deepseek': [], 'glm': []}
alone = rs.build_config(cfg, {'providers': [src]}, sel)
check('没有 agent_heads 时仍只写行上的一个头',
      [m['alias'] for m in alone['openai-compatibility'][0]['models']], ['old/' + alias])
both = rs.build_config(cfg, {'providers': [src], 'agent_heads': ok_heads}, sel)
written = both['openai-compatibility'][0]['models']
check('各代理端的头都写成 alias，没设过的仍用行上的头',
      sorted(m['alias'] for m in written), sorted([alias, 'gly/' + alias, 'old/' + alias]))
check('这些 alias 的上游名相同', len(written) > 1 and len({m['name'] for m in written}) == 1, True)

box = {'plan': {'providers': [dict(src, head='old')]}}
orig_io = (S._read_plan, S._write_plan, S._gw_get, S._enabled_ids)
S._read_plan = lambda: copy.deepcopy(box['plan'])
S._write_plan = lambda p: box.__setitem__('plan', p)
S._gw_get = lambda *a, **k: {}
S._enabled_ids = lambda config, plan: {'srapi'}
try:
    saved = S.set_head('srapi', 'gly', 'codex')
    check('set_head 记下的是代理端', saved['agent'], 'codex')
    check('行上的 head 不被改掉', box['plan']['providers'][0]['head'], 'old')
    check('只写入 Codex', box['plan']['agent_heads']['codex']['srapi'], 'gly')
    check('Claude Code 的有效头不变',
          S.effective_head(box['plan'], 'claude-code', box['plan']['providers'][0]), 'old')
    S.set_head('srapi', '', 'claude-code-desktop')
    check('桌面端写到 claude-code', box['plan']['agent_heads']['claude-code']['srapi'], '')
    check('桌面端那一笔没有盖掉 Codex', box['plan']['agent_heads']['codex']['srapi'], 'gly')
    before = copy.deepcopy(box['plan'])
    try:
        S.set_head('srapi', 'x', 'copilot-agent')
        check_true('Copilot Agent 的 set_head 被拒', False)
    except S.RouteError as e:
        check_true('Copilot Agent 的 set_head 被拒', '不使用渠道头' in str(e))
    check('拒绝之后 plan 不变', box['plan'], before)
finally:
    S._read_plan, S._write_plan, S._gw_get, S._enabled_ids = orig_io

clash_box = {'plan': {'providers': [
    {'id': 'openai-official', 'group': 'gpt', 'head': '', 'expose': ['gpt-5.6-sol']},
    {'id': 'srapi', 'group': 'gpt', 'head': 'srapi', 'expose': ['gpt-5.6-sol']},
]}}
S._read_plan = lambda: copy.deepcopy(clash_box['plan'])
S._write_plan = lambda p: clash_box.__setitem__('plan', p)
S._gw_get = lambda *a, **k: {}
S._enabled_ids = lambda config, plan: {'openai-official', 'srapi'}
# 钉住「已接入」，不读本机 Claude 配不配置。这个用例要的就是 Claude Code 在接入名单里。
orig_live = S._live_head_agents
S._live_head_agents = lambda: {'claude-code', 'codex'}
try:
    frozen = copy.deepcopy(clash_box['plan'])
    try:
        S.set_head('srapi', '', 'claude-code')
        check_true('两个主撞车时 set_head 拒绝', False)
    except S.RouteError as e:
        check_true('两个主撞车时 set_head 拒绝', '同时暴露' in str(e))
        check_true('拒绝文案点明代理端和来源',
                   'Claude Code' in str(e) and 'openai-official' in str(e))
    check('撞车拒绝后 plan 不改', clash_box['plan'], frozen)
    # Claude Code 没接入时，给它写成主不影响已接入的 Codex（行上头仍是 srapi）。
    S._live_head_agents = lambda: {'codex'}
    saved_off = S.set_head('srapi', '', 'claude-code')
    check('没接入的代理端改成主可以记下', saved_off['head'], '')
    check('记下的是 claude-code', clash_box['plan']['agent_heads']['claude-code']['srapi'], '')
finally:
    S._live_head_agents = orig_live
    S._read_plan, S._write_plan, S._gw_get, S._enabled_ids = orig_io

print()
print('RESULT: %s (%d failed)' % ('ALL PASS' if not fails else 'FAILURES', len(fails)))
for f in fails:
    print('  - ' + f)
sys.exit(1 if fails else 0)
