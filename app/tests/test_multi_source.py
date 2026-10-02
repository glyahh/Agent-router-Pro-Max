"""多来源 + 渠道头的路由核心回归测试（build_config / regen_catalog）。

跑法（在项目根目录）：
    python app\\tests\\test_multi_source.py

**不碰生产文件**：build_config 是纯函数；regen_catalog 会写盘，所以先把 rs.ROOT
改指到一个临时目录（里面放假的 config.yaml / 模板池 / backups），跑完删掉。
生产 config.yaml 与 routing-plan.json 全程只读。

为什么要有这个文件：这两处改动都"不报错但结果不对"——别名没加头、遗留 A/ 被加了头、
带头模型的官方元数据丢失、模板池被客户端 ID 污染后越滚越大。这几种都只能靠断言内容发现。
"""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT / 'script'))

import route_selector as rs   # noqa: E402

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


def models_of(cfg, tag):
    for e in cfg['codex-api-key']:
        if e.get('headers', {}).get('X-Route-Tag') == tag:
            return e.get('models')
    return None


def aliases_of(cfg, tag):
    return [m['alias'] for m in (models_of(cfg, tag) or [])]


def entry_of(cfg, tag):
    for e in cfg['codex-api-key']:
        if e.get('headers', {}).get('X-Route-Tag') == tag:
            return e
    return None


# ─────────────────────────────────────────────────── build_config

print('== build_config：一个分组两家来源 ==')
A = 'gpt-5.6-sol'
plan = {'providers': [
    {'id': 'oai', 'label': 'OpenAI 官方', 'group': 'gpt', 'section': 'codex-api-key',
     'base_url': 'https://a.example/v1', 'tag': 't-a', 'head': '',
     'models': [{'name': A, 'alias': A}],
     'available': [{'name': A, 'alias': A, 'context_length': None}],
     'expose': [A]},
    {'id': 'srapi', 'label': 'SRAPI', 'group': 'gpt', 'section': 'codex-api-key',
     'base_url': 'https://b.example/v1', 'tag': 't-b', 'head': 'srapi',
     'models': [{'name': A, 'alias': A},
                {'name': 'A/gpt-5.6-sol', 'alias': 'A/gpt-5.6-sol'}],
     'available': [{'name': A, 'alias': A, 'context_length': None}],
     'expose': [A]},
]}
config = {'codex-api-key': [
    {'api-key': 'k-a', 'base-url': 'https://a.example/v1',
     'headers': {'X-Route-Tag': 't-a'}, 'excluded-models': ['*'], 'models': []},
    {'api-key': 'k-b', 'base-url': 'https://b.example/v1',
     'headers': {'X-Route-Tag': 't-b'}, 'excluded-models': ['*'], 'models': []},
], 'openai-compatibility': []}

new = rs.build_config(config, plan, {'gpt': ['oai', 'srapi'], 'deepseek': [], 'glm': []})
check('无头那家保留干净 ID', aliases_of(new, 't-a'), [A])
check('有头那家加前缀', aliases_of(new, 't-b'), ['srapi/' + A, 'A/gpt-5.6-sol'])
check('遗留 A/ 别名不加头',
      [m for m in models_of(new, 't-b') if m['name'] == 'A/gpt-5.6-sol'],
      [{'name': 'A/gpt-5.6-sol', 'alias': 'A/gpt-5.6-sol'}])
check('name 保持上游模型名', models_of(new, 't-b')[0]['name'], A)
check('启用的条目去掉 excluded-models', 'excluded-models' in entry_of(new, 't-a'), False)
check('原始 config 未被就地修改', config['codex-api-key'][0].get('excluded-models'), ['*'])

print('== build_config：老形状（单个 id 字符串）仍可用 ==')
old_shape = rs.build_config(config, plan, {'gpt': 'srapi', 'deepseek': '', 'glm': None})
check('老形状字符串被接受', aliases_of(old_shape, 't-b'), ['srapi/' + A, 'A/gpt-5.6-sol'])
check('老形状下另一家被停用', entry_of(old_shape, 't-a')['excluded-models'], ['*'])

print('== build_config：非法 id 被拒 ==')
try:
    rs.build_config(config, plan, {'gpt': ['nope'], 'deepseek': [], 'glm': []})
    check_true('未知 id 抛错', False)
except rs.RouteError as e:
    check_true('未知 id 抛错', '供应商选择无效' in str(e))
    print('        -> ' + str(e))

print('== build_config：一个凭据被两个头共用 -> 拒绝 ==')
shared = {'providers': [
    {'id': 'goat-ds', 'label': 'Goat', 'group': 'deepseek', 'section': 'codex-api-key',
     'base_url': 'https://c.example/v1', 'tag': 't-c', 'head': 'goat',
     'models': [], 'available': [], 'expose': []},
    {'id': 'goat-glm', 'label': 'Goat', 'group': 'glm', 'section': 'codex-api-key',
     'base_url': 'https://c.example/v1', 'tag': 't-c', 'head': 'goatglm',
     'models': [], 'available': [], 'expose': []},
]}
shared_cfg = {'codex-api-key': [
    {'api-key': 'k-c', 'base-url': 'https://c.example/v1',
     'headers': {'X-Route-Tag': 't-c'}, 'excluded-models': ['*'], 'models': []},
], 'openai-compatibility': []}
try:
    rs.build_config(shared_cfg, shared, {'gpt': [], 'deepseek': ['goat-ds'], 'glm': ['goat-glm']})
    check_true('同凭据两个头被拒', False)
except rs.RouteError as e:
    check_true('同凭据两个头被拒', '渠道头' in str(e))
    print('        -> ' + str(e))
# 同一个头则是允许的（Goat 服务两个分组的正常形状）
same_head = [dict(shared['providers'][0]), dict(shared['providers'][1], head='goat')]
ok = rs.build_config(shared_cfg, {'providers': same_head},
                     {'gpt': [], 'deepseek': ['goat-ds'], 'glm': ['goat-glm']})
check_true('同凭据同一个头允许', ok['codex-api-key'][0].get('excluded-models') is None)

# ─────────────────────────────────────────────────── _selected_ids

print('== _selected_ids ==')
check('None -> []', rs._selected_ids({'gpt': None}, 'gpt'), [])
check('字符串 -> 单元素', rs._selected_ids({'gpt': 'x'}, 'gpt'), ['x'])
check('空串 -> []', rs._selected_ids({'gpt': ''}, 'gpt'), [])
check('列表原样', rs._selected_ids({'gpt': ['a', 'b']}, 'gpt'), ['a', 'b'])
check('缺 key -> []', rs._selected_ids({}, 'gpt'), [])
try:
    rs._selected_ids({'gpt': 5}, 'gpt')
    check_true('非法形状抛错', False)
except rs.RouteError:
    check_true('非法形状抛错', True)

# ─────────────────────────────────────────────────── regen_catalog（沙箱）

print('== regen_catalog（沙箱 ROOT，不碰生产）==')
tmp = Path(tempfile.mkdtemp(prefix='prism-regen-'))
try:
    (tmp / 'backups').mkdir()
    (tmp / 'config.yaml').write_text('port: 1\n', encoding='utf-8')
    pool = {'models': [
        {'slug': 'gpt-5.5', 'display_name': 'GPT-5.5', 'description': 'd',
         'base_instructions': 'bi',
         'supported_reasoning_levels': [{'effort': 'low', 'description': 'l'},
                                        {'effort': 'high', 'description': 'h'},
                                        {'effort': 'max', 'description': 'm'}],
         'default_reasoning_level': 'high', 'service_tiers': [], 'visibility': 'list'},
        {'slug': 'deepseek-v4-pro', 'display_name': 'DS', 'description': 'd',
         'base_instructions': 'bi', 'visibility': 'list'},
        {'slug': 'glm-5.3-flash', 'display_name': 'GLM', 'description': 'd',
         'base_instructions': 'bi', 'visibility': 'list'},
    ]}
    (tmp / 'codex-model-catalog-templates.json').write_text(
        json.dumps(pool, ensure_ascii=False), encoding='utf-8')

    rs.ROOT = tmp
    # 预置官方 meta 缓存，避免真去 chatgpt.com 拉（缓存命中时不会读 auth 文件）
    rs._OFFICIAL_CACHE[:] = [time.time(), {
        'gpt-5.6-sol': {'slug': 'gpt-5.6-sol', 'display_name': 'GPT-5.6 Sol',
                        'description': 'official desc',
                        'supported_reasoning_levels': [{'effort': 'low', 'description': 'l'},
                                                       {'effort': 'high', 'description': 'h'}]},
    }]

    rplan = {'providers': [
        {'id': 'oai', 'label': 'OpenAI 官方', 'group': 'gpt', 'head': '',
         'expose': ['gpt-5.6-sol'],
         'available': [{'name': 'gpt-5.6-sol', 'alias': 'gpt-5.6-sol', 'context_length': None}],
         'models': []},
        {'id': 'srapi', 'label': 'SRAPI', 'group': 'gpt', 'head': 'srapi',
         'expose': ['gpt-5.6-sol', 'brand-new-model'],
         'available': [{'name': 'gpt-5.6-sol', 'alias': 'gpt-5.6-sol', 'context_length': None},
                       {'name': 'brand-new-model', 'alias': 'brand-new-model', 'context_length': 128000}],
         'models': [{'name': 'A/legacy-thing', 'alias': 'A/legacy-thing'}]},
    ]}
    rs.regen_catalog(rplan)
    cat = json.loads((tmp / 'codex-model-catalog.json').read_text(encoding='utf-8'))
    by_slug = {m['slug']: m for m in cat['models']}
    print('  产出 slug: %r' % (sorted(by_slug),))

    check_true('无头条目存在', 'gpt-5.6-sol' in by_slug)
    check_true('有头条目存在', 'srapi/gpt-5.6-sol' in by_slug)
    check_true('遗留 A/ 条目存在', 'A/legacy-thing' in by_slug)
    check('无头 display_name 用官方名', by_slug['gpt-5.6-sol']['display_name'], 'GPT-5.6 Sol')
    check('有头 display_name = 来源名 · 干净名',
          by_slug['srapi/gpt-5.6-sol']['display_name'], 'SRAPI · gpt-5.6-sol')
    check('有头条目继承官方描述', by_slug['srapi/gpt-5.6-sol']['description'], 'official desc')
    check('有头条目继承官方档位',
          [l['effort'] for l in by_slug['srapi/gpt-5.6-sol']['supported_reasoning_levels']],
          ['low', 'high'])
    check('有头条目可见', by_slug['srapi/gpt-5.6-sol']['visibility'], 'list')
    check('无头条目可见', by_slug['gpt-5.6-sol']['visibility'], 'list')
    check('遗留 A/ 条目隐藏', by_slug['A/legacy-thing']['visibility'], 'hide')
    check('未知模型走模板兜底', by_slug['srapi/brand-new-model']['slug'], 'srapi/brand-new-model')
    check('未知模型的 display_name 带来源头',
          by_slug['srapi/brand-new-model']['display_name'], 'SRAPI · brand-new-model')
    check('未知模型不得继承 Fast 档', by_slug['srapi/brand-new-model']['service_tiers'], [])

    tpl = json.loads((tmp / 'codex-model-catalog-templates.json').read_text(encoding='utf-8'))
    tpl_slugs = [m['slug'] for m in tpl['models']]
    check_true('池子里新增的是干净别名，不是客户端 ID',
               'brand-new-model' in tpl_slugs and 'srapi/brand-new-model' not in tpl_slugs)
    check('池子没有重复项', len(tpl_slugs), len(set(tpl_slugs)))

    # 幂等：再跑一次，池子不该再长
    n_before = len(tpl_slugs)
    rs.regen_catalog(rplan)
    tpl2 = json.loads((tmp / 'codex-model-catalog-templates.json').read_text(encoding='utf-8'))
    check('第二次 regen 池子不增长', len(tpl2['models']), n_before)
    cat2 = json.loads((tmp / 'codex-model-catalog.json').read_text(encoding='utf-8'))
    check('第二次 regen 产出条数不变', len(cat2['models']), len(cat['models']))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
    print('  沙箱已删除: %s' % tmp)

print()
print('RESULT: %s (%d failed)' % ('ALL PASS' if not fails else 'FAILURES', len(fails)))
for f in fails:
    print('  - ' + f)
sys.exit(1 if fails else 0)
