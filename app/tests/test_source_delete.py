"""删除来源（含内置来源）的端到端回归测试。

跑法（在项目根目录）：
    python app\\tests\\test_source_delete.py

**不碰生产文件、不碰真网关**：rs.ROOT / bridge.ROOT 指到临时目录，网关读写（bridge 的
gateway_get/put/delete 与 rs.api）全部打桩。跑的是真实的 sources.delete_source →
_remove_entry → _restore_after_delete → rs.regen_catalog。

为什么要有这个文件：delete_source 以前只给自定义来源开放，现在内置来源也能删。三件
最容易错的事都没有别的用例看着：
  · 共享凭据（Goat 服务 DEEPSEEK 与 GLM）删一行不能把条目一起删掉，否则另一组变孤儿；
  · openai-compatibility 段的删除走整段 PUT 剔除，不能碰别的条目；
  · auth-file 行必须拒删（删了没有界面入口重建）。
"""
import copy
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT / 'script'))

import route_selector as rs                       # noqa: E402
from core import bridge, sources as S              # noqa: E402

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


def calls_of(stub, kind, path=None):
    return [c for c in stub.calls if c[0] == kind and (path is None or c[1] == path)]


# ─────────────────────────────────────────────────────────── 假网关

class StubGateway:
    """bridge 侧走相对路径（config / codex-api-key），rs.api 走完整路径（/v0/management/…）。"""

    def __init__(self, config):
        self.config = copy.deepcopy(config)
        self.calls = []
        self.models = []
        self.rebuild()

    def rebuild(self):
        out = []
        for section in ('codex-api-key', 'openai-compatibility'):
            for e in self.config.get(section) or []:
                if section == 'codex-api-key' and '*' in (e.get('excluded-models') or []):
                    continue
                if section == 'openai-compatibility' and e.get('disabled'):
                    continue
                out += [m.get('alias') for m in (e.get('models') or [])]
        self.models = sorted({x for x in out if x})

    def gw_get(self, path):
        if path == 'config':
            return copy.deepcopy(self.config)
        return copy.deepcopy(self.config.get(path))

    def gw_put(self, path, data):
        self.calls.append(('PUT', path, copy.deepcopy(data)))
        self.config[path] = copy.deepcopy(data)
        self.rebuild()
        return {'ok': True}

    def gw_delete(self, path, params):
        self.calls.append(('DELETE', path, dict(params or {})))
        key = (params or {}).get('api-key')
        base = ((params or {}).get('base-url') or '').rstrip('/')
        kept = [e for e in (self.config.get(path) or [])
                if not ((e.get('api-key') or '') == key
                        and str(e.get('base-url') or '').rstrip('/') == base)]
        self.config[path] = kept
        self.rebuild()
        return {'ok': True}

    def api(self, path, method='GET', data=None):
        if path == '/v0/management/config' and method == 'GET':
            return copy.deepcopy(self.config)
        if path == '/v1/models':
            return {'data': [{'id': m} for m in self.models]}
        if path.startswith('/v0/management/') and method == 'PUT':
            name = path.rsplit('/', 1)[-1]
            self.calls.append(('rs-PUT', name, copy.deepcopy(data)))
            self.config[name] = copy.deepcopy(data)
            self.rebuild()
            return {'ok': True}
        raise AssertionError('没桩的接口：%s %s' % (method, path))


# ─────────────────────────────────────────────────────────── 沙箱

def base_plan():
    return {'note': 'stub',
            'selected': {'gpt': ['oai', 'srapi'], 'deepseek': ['goat', 'deepseek-official'],
                         'glm': ['goat-glm']},
            'providers': [
                {'id': 'oai', 'label': 'OpenAI 官方', 'group': 'gpt', 'section': 'auth-file',
                 'base_url': 'auth://codex-official.json', 'models': [], 'expose': []},
                {'id': 'srapi', 'label': 'SRAPI', 'group': 'gpt', 'section': 'codex-api-key',
                 'base_url': 'https://b.example/v1', 'tag': 't-b', 'head': 'srapi',
                 'models': [], 'expose': ['gpt-5.6-sol']},
                {'id': 'goat', 'label': 'Command Code Goat', 'group': 'deepseek',
                 'section': 'openai-compatibility', 'base_url': 'https://c.example/v1',
                 'head': '', 'models': [], 'expose': ['deepseek-v4-pro']},
                {'id': 'goat-glm', 'label': 'Command Code Goat', 'group': 'glm',
                 'section': 'openai-compatibility', 'base_url': 'https://c.example/v1',
                 'head': '', 'models': [], 'expose': ['glm-5.3-flash']},
                {'id': 'deepseek-official', 'label': 'DeepSeek 官方', 'group': 'deepseek',
                 'section': 'openai-compatibility', 'base_url': 'https://d.example/v1',
                 'head': '', 'models': [], 'expose': []},
            ]}


def base_config():
    return {
        'codex-api-key': [
            {'api-key': 'k-b', 'base-url': 'https://b.example/v1',
             'headers': {'X-Route-Tag': 't-b'}, 'excluded-models': ['*'], 'models': []},
        ],
        'openai-compatibility': [
            {'name': 'Command Code (Goat)', 'base-url': 'https://c.example/v1',
             'api-key-entries': [{'api-key': 'k-c'}], 'models': []},
            {'name': 'DeepSeek Official', 'base-url': 'https://d.example/v1', 'disabled': True,
             'api-key-entries': [{'api-key': 'k-d'}], 'models': []},
        ],
    }


TMP = Path(tempfile.mkdtemp(prefix='prism-del-'))
real = (rs.ROOT, bridge.ROOT, rs.api, rs.fetch_all,
        bridge.gateway_get, bridge.gateway_put, bridge.gateway_delete,
        bridge.PLAN_PATH, bridge.CATALOG_PATH, bridge.TEMPLATES_PATH)
try:
    (TMP / '.local-secrets.json').write_text(
        json.dumps({'management_key': 'stub', 'api_key': 'stub-client-key'}), encoding='utf-8')
    (TMP / 'config.yaml').write_text('port: 1\n', encoding='utf-8')
    (TMP / 'auth').mkdir(exist_ok=True)
    (TMP / 'auth' / 'codex-official.json').write_text(
        json.dumps({'excluded_models': ['*']}), encoding='utf-8')
    (TMP / 'codex-model-catalog-templates.json').write_text(json.dumps({'models': [
        {'slug': 'gpt-5.6-sol', 'display_name': 'GPT-5.6 Sol', 'description': 'd',
         'base_instructions': 'bi', 'visibility': 'list',
         'supported_reasoning_levels': [{'effort': 'low', 'description': 'l'}],
         'default_reasoning_level': 'low', 'service_tiers': []},
        {'slug': 'deepseek-v4-pro', 'display_name': 'DS', 'description': 'd',
         'base_instructions': 'bi', 'visibility': 'list'},
        {'slug': 'glm-5.3-flash', 'display_name': 'GLM', 'description': 'd',
         'base_instructions': 'bi', 'visibility': 'list'},
    ]}, ensure_ascii=False), encoding='utf-8')

    rs.ROOT = TMP
    bridge.ROOT = TMP
    bridge.PLAN_PATH = TMP / 'routing-plan.json'
    bridge.CATALOG_PATH = TMP / 'codex-model-catalog.json'
    bridge.TEMPLATES_PATH = TMP / 'codex-model-catalog-templates.json'
    rs.fetch_all = lambda p, c: {x['id']: {'available': [], 'fetch_error': None,
                                           'expose': list(x.get('expose') or [])}
                                 for x in p['providers']}
    rs._OFFICIAL_CACHE[:] = [time.time(), {}]

    def reset(plan=None, config=None):
        (TMP / 'routing-plan.json').write_text(
            json.dumps(plan if plan is not None else base_plan(), ensure_ascii=False, indent=2),
            encoding='utf-8')
        (TMP / 'codex-model-catalog.json').write_text(json.dumps({'models': []}), encoding='utf-8')
        stub = StubGateway(config if config is not None else base_config())
        rs.api = stub.api
        bridge.gateway_get = stub.gw_get
        bridge.gateway_put = stub.gw_put
        bridge.gateway_delete = stub.gw_delete
        return stub

    def disk_plan():
        return json.loads((TMP / 'routing-plan.json').read_text(encoding='utf-8'))

    # ─────────────────────────────────────────────── 1. 内置 codex-api-key 来源

    print('== 内置来源（codex-api-key，此前不可删）==')
    stub = reset()
    res = S.delete_source('srapi')
    check('返回体标记条目已删', res.get('config_entry'), 'removed')
    check('网关它那一段被清空', stub.config['codex-api-key'], [])
    check('DELETE 用了 api-key + base-url',
          calls_of(stub, 'DELETE', 'codex-api-key'),
          [('DELETE', 'codex-api-key',
            {'api-key': 'k-b', 'base-url': 'https://b.example/v1'})])
    check('plan 里那一行没了', [p['id'] for p in disk_plan()['providers']],
          ['oai', 'goat', 'goat-glm', 'deepseek-official'])
    check('selected 里也摘掉了', disk_plan()['selected']['gpt'], ['oai'])
    cat = json.loads((TMP / 'codex-model-catalog.json').read_text(encoding='utf-8'))
    slugs = [m['slug'] for m in cat['models']]
    check_true('目录里不再有它的模型（删除即重刷）', 'srapi/gpt-5.6-sol' not in slugs, repr(slugs))
    check_true('其余来源的模型仍在目录里',
               'deepseek-v4-pro' in slugs and 'glm-5.3-flash' in slugs, repr(slugs))

    # ─────────────────────────────────────────────── 2. 共享凭据

    print('== 共享凭据（Goat 服务 DEEPSEEK 与 GLM）：只删行，条目留着 ==')
    stub = reset()
    res = S.delete_source('goat')
    check('返回体标记为共用', res.get('config_entry'), 'shared')
    check('没有对网关发过任何写请求',
          [c for c in stub.calls if c[0] in ('PUT', 'DELETE', 'rs-PUT')], [])
    names = [e.get('name') for e in stub.config['openai-compatibility']]
    check('共用条目原样留着（GLM 还要用）', names,
          ['Command Code (Goat)', 'DeepSeek Official'])
    check('条目没有被停用',
          'disabled' in stub.config['openai-compatibility'][0], False)
    rows = disk_plan()['providers']
    check('plan 里只剩 GLM 那一行', [p['id'] for p in rows],
          ['oai', 'srapi', 'goat-glm', 'deepseek-official'])
    check('selected.deepseek 不再含 goat', disk_plan()['selected']['deepseek'],
          ['deepseek-official'])
    states, failures = rs.row_states(stub.config, rows, rs.selection_of(disk_plan()))
    check('glm 那行仍算启用', states.get('goat-glm'), True)
    check('没有读不出来的行', failures, {})

    # ─────────────────────────────────────────────── 3. openai-compatibility 非共享

    print('== openai-compatibility 段：整段 PUT 剔除，不碰别的条目 ==')
    stub = reset()
    other_before = copy.deepcopy(stub.config['openai-compatibility'][0])
    res = S.delete_source('deepseek-official')
    check('返回体标记条目已删', res.get('config_entry'), 'removed')
    check('没有用 DELETE 接口（那一段的参数形状没验证过）',
          calls_of(stub, 'DELETE'), [])
    check('整段 PUT 了两次（① 停用 + ③ 剔除）',
          len(calls_of(stub, 'PUT', 'openai-compatibility')), 2)
    check('该条目已不在网关配置里',
          [e.get('name') for e in stub.config['openai-compatibility']],
          ['Command Code (Goat)'])
    check('同段其它条目字节不变',
          stub.config['openai-compatibility'][0], other_before)
    check('plan 里那一行没了',
          [p['id'] for p in disk_plan()['providers']],
          ['oai', 'srapi', 'goat', 'goat-glm'])

    # ─────────────────────────────────────────────── 4. auth-file 拒删

    print('== 官方登录来源（auth-file）拒删 ==')
    stub = reset()
    try:
        S.delete_source('oai')
        check_true('拒删', False)
    except S.RouteError as e:
        check_true('拒删且文案可读', '官方登录来源不可删除' in str(e))
        print('        -> ' + str(e))
    check('一个字节都没动网关', stub.calls, [])
    check('plan 行还在', [p['id'] for p in disk_plan()['providers']],
          [p['id'] for p in base_plan()['providers']])

    # ─────────────────────────────────────────────── 5. 回滚

    print('== plan 写失败：网关条目复原，行写回 ==')
    stub = reset()
    (TMP / 'codex-model-catalog.json').write_text(json.dumps({'models': []}), encoding='utf-8')
    real_write = S._write_plan

    def boom(plan):
        raise OSError('磁盘写不动（测试桩）')

    S._write_plan = boom
    try:
        try:
            S.delete_source('srapi')
            check_true('写失败时上抛', False)
        except S.RouteError as e:
            check_true('写失败时上抛可读错误', '删除来源失败' in str(e))
            print('        -> ' + str(e)[:120])
    finally:
        S._write_plan = real_write
    check('网关条目复原（含原来的停用状态）',
          [e.get('api-key') for e in stub.config['codex-api-key']], ['k-b'])
    check('复原的条目仍带着 excluded-models:["*"]',
          stub.config['codex-api-key'][0].get('excluded-models'), ['*'])
    check('plan 行还在磁盘上', [p['id'] for p in disk_plan()['providers']],
          [p['id'] for p in base_plan()['providers']])
    check('selected 也复原', disk_plan()['selected'], base_plan()['selected'])

    # ─────────────────────────────────────────────── 6. 找不到来源

    print('== 找不到来源 / 缺条目 ==')
    stub = reset()
    try:
        S.delete_source('nope')
        check_true('未知 id 报错', False)
    except S.RouteError as e:
        check_true('未知 id 报错可读', '找不到来源' in str(e))
    plan_no_entry = base_plan()
    plan_no_entry['providers'] = [p for p in plan_no_entry['providers'] if p['id'] != 'goat-glm']
    stub = reset(plan_no_entry)
    res = S.delete_source('goat')          # 条目还在，但没有别的行用它了 → 真删
    check('没有别的行共用时按真删走', res.get('config_entry'), 'removed')
    check('共用条目此时被删掉',
          [e.get('name') for e in stub.config['openai-compatibility']],
          ['DeepSeek Official'])

finally:
    (rs.ROOT, bridge.ROOT, rs.api, rs.fetch_all,
     bridge.gateway_get, bridge.gateway_put, bridge.gateway_delete,
     bridge.PLAN_PATH, bridge.CATALOG_PATH, bridge.TEMPLATES_PATH) = real
    shutil.rmtree(TMP, ignore_errors=True)
    print('  沙箱已删除: %s' % TMP)

print()
print('RESULT: %s (%d failed)' % ('ALL PASS' if not fails else 'FAILURES', len(fails)))
for f in fails:
    print('  - ' + f)
sys.exit(1 if fails else 0)
