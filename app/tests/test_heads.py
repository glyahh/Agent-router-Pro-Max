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

print()
print('RESULT: %s (%d failed)' % ('ALL PASS' if not fails else 'FAILURES', len(fails)))
for f in fails:
    print('  - ' + f)
sys.exit(1 if fails else 0)
