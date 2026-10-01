"""多来源「保存路由」的端到端测试（走真实的 apply_selection 链路）。

跑法（在项目根目录）：
    python app\\tests\\test_multi_source_save.py

**不碰生产文件、不碰真网关、不碰上游**：把 rs.ROOT / bridge.ROOT 指到临时目录，并在
两个网络边界上打桩：

  * rs.api        —— 网关管理接口（原来的实现把 http://127.0.0.1:8317 写死了，
                     所以 PRISM_GATEWAY_BASE 这个"沙箱开关"其实盖不住 route_selector；
                     见本文件末尾的 finding，桩函数绕开它）
  * rs.fetch_all  —— 各上游的 /models 拉取

打桩只发生在测试进程里，不改任何源码。跑的是真实的 rs.apply_selection →
build_config → regen_catalog，以及 bridge._apply_selection_locked 的
渠道头预检 / _state_snapshot / _compensate / sync_catalog_reasoning。
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
from core import bridge, sources as S             # noqa: E402

fails = []
findings = []


def check(name, got, want):
    ok = got == want
    print(('  PASS  ' if ok else '  FAIL  ') + name)
    if not ok:
        print('        got : %r' % (got,))
        print('        want: %r' % (want,))
        fails.append(name)


def check_true(name, cond, detail=''):
    check(name + (('  -> ' + detail) if (detail and not cond) else ''), bool(cond), True)


# ─────────────────────────────────────────────────────────── 假网关

class StubGateway:
    """够用的管理接口桩：GET config / PUT <section> / GET /v1/models。

    PUT 之后会重建模型列表 —— 真实网关也是热重载后立刻反映到 /v1/models，
    wait_models 就靠这个。只列**启用**条目的 alias，这是断言的关键。
    """

    def __init__(self, config):
        self.config = copy.deepcopy(config)
        self.calls = []
        self.models = []
        self.rebuild()

    def enabled(self, section, entry):
        if section == 'codex-api-key':
            return '*' not in (entry.get('excluded-models') or [])
        if section == 'openai-compatibility':
            return not entry.get('disabled', False)
        return True

    def rebuild(self):
        ids = []
        for section in rs.SECTIONS:
            for entry in self.config.get(section, []) or []:
                if not self.enabled(section, entry):
                    continue
                for m in entry.get('models', []) or []:
                    a = m.get('alias')
                    if a and a not in ids:
                        ids.append(a)
        self.models = ids

    def api(self, path, method='GET', data=None):
        self.calls.append((method, path))
        if path == '/v0/management/config' and method == 'GET':
            return copy.deepcopy(self.config)
        if path == '/v1/models':
            return {'data': [{'id': i} for i in self.models]}
        if method == 'PUT' and path.startswith('/v0/management/'):
            section = path[len('/v0/management/'):]
            self.config[section] = copy.deepcopy(data)
            self.rebuild()
            return {'ok': True}
        if path == '/v0/management/auth-files/fields' and method == 'PATCH':
            return {'ok': True}
        raise AssertionError('桩没实现 %s %s' % (method, path))


# ─────────────────────────────────────────────────────────── 场景

TMP = Path(tempfile.mkdtemp(prefix='prism-save-'))
real_api, real_fetch_all, real_roots = rs.api, rs.fetch_all, (rs.ROOT, bridge.ROOT)
real_paths = (bridge.PLAN_PATH, bridge.CATALOG_PATH, bridge.TEMPLATES_PATH)
stub = None

try:
    (TMP / 'backups').mkdir()
    (TMP / 'config.yaml').write_text('port: 1\n', encoding='utf-8')
    (TMP / '.local-secrets.json').write_text(
        json.dumps({'management_key': 'stub', 'api_key': 'stub-client-key'}), encoding='utf-8')
    (TMP / 'codex-model-catalog-templates.json').write_text(json.dumps({'models': [
        {'slug': 'gpt-5.5', 'display_name': 'GPT-5.5', 'description': 'd',
         'base_instructions': 'bi', 'visibility': 'list',
         'supported_reasoning_levels': [{'effort': 'low', 'description': 'l'},
                                        {'effort': 'high', 'description': 'h'}]},
        {'slug': 'deepseek-v4-pro', 'display_name': 'DS', 'description': 'd',
         'base_instructions': 'bi', 'visibility': 'list'},
        {'slug': 'glm-5.3-flash', 'display_name': 'GLM', 'description': 'd',
         'base_instructions': 'bi', 'visibility': 'list'},
    ]}, ensure_ascii=False), encoding='utf-8')

    A = 'gpt-5.6-sol'
    plan = {'note': 'stub', 'providers': [
        {'id': 'oai', 'label': 'OpenAI 官方', 'group': 'gpt', 'section': 'codex-api-key',
         'base_url': 'https://a.example/v1', 'tag': 't-a', 'head': '',
         'models': [{'name': A, 'alias': A}], 'blocked': None, 'expose': []},
        {'id': 'srapi', 'label': 'SRAPI', 'group': 'gpt', 'section': 'codex-api-key',
         'base_url': 'https://b.example/v1', 'tag': 't-b', 'head': 'srapi',
         'models': [{'name': A, 'alias': A}], 'blocked': None, 'expose': []},
    ]}
    (TMP / 'routing-plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2),
                                           encoding='utf-8')

    config = {'codex-api-key': [
        {'api-key': 'k-a', 'base-url': 'https://a.example/v1',
         'headers': {'X-Route-Tag': 't-a'}, 'excluded-models': ['*'], 'models': []},
        {'api-key': 'k-b', 'base-url': 'https://b.example/v1',
         'headers': {'X-Route-Tag': 't-b'}, 'excluded-models': ['*'], 'models': []},
    ], 'openai-compatibility': []}
    stub = StubGateway(config)

    avail = {'oai': [{'name': A, 'alias': A, 'context_length': None}],
             'srapi': [{'name': A, 'alias': A, 'context_length': None}]}

    # 打桩：重定向根、网关、上游
    rs.ROOT = TMP
    bridge.ROOT = TMP
    bridge.PLAN_PATH = TMP / 'routing-plan.json'
    bridge.CATALOG_PATH = TMP / 'codex-model-catalog.json'
    bridge.TEMPLATES_PATH = TMP / 'codex-model-catalog-templates.json'
    rs.api = stub.api
    # 让本用例**完全不碰真网关**。_state_snapshot / _compensate 走的是 bridge.gateway_get，
    # 而它原先打真实 8317、带着沙箱里那个假管理密钥 → 每跑一次本文件就对真网关留下 **2 次 401**，
    # 凑满 AUTH_FAIL_FAST_AT=2 还会触发 bridge 的"暂停 30 秒"快闸，把后面的用例一起带坏
    # （实测：新加的"候选为空"用例就是被这个闸门挡掉的）。这正是 HI-02 说的"测试绑真实服务"。
    # 钉成常量后，补偿看到的是"config 未推进"，既不误判也不会误写。
    real_gw_get = bridge.gateway_get
    bridge.gateway_get = lambda path: {'codex-api-key': [], 'openai-compatibility': []}
    rs.fetch_all = lambda p, c: {x['id']: {'available': copy.deepcopy(avail.get(x['id'], [])),
                                           'fetch_error': None,
                                           'expose': list(x.get('expose') or [])}
                                 for x in p['providers']}
    rs._OFFICIAL_CACHE[:] = [time.time(), {
        A: {'slug': A, 'display_name': 'GPT-5.6 Sol', 'description': 'official desc',
            'supported_reasoning_levels': [{'effort': 'low', 'description': 'l'},
                                           {'effort': 'high', 'description': 'h'}]}}]

    print('== 保存：gpt 组同时启用「无头官方」+「有头 srapi」==')
    payload = {
        'revision': rs.revision(stub.config),
        'selected': {'gpt': ['oai', 'srapi'], 'deepseek': [], 'glm': []},
        'picks': {'oai': [A], 'srapi': [A]},
    }
    result = bridge._apply_selection_locked(payload)

    check_true('返回体是 snapshot', isinstance(result, dict) and 'selected' in result)
    check('selected.gpt 是两家', result['selected']['gpt'], ['oai', 'srapi'])
    check('selected.deepseek 是空列表', result['selected']['deepseek'], [])

    put_sections = [p for (m, p) in stub.calls if m == 'PUT']
    check('PUT 了 codex-api-key', put_sections, ['/v0/management/codex-api-key'])

    ent = {e['headers']['X-Route-Tag']: e for e in stub.config['codex-api-key']}
    check('官方条目保留干净 ID', [m['alias'] for m in ent['t-a']['models']], [A])
    check('srapi 条目加了渠道头', [m['alias'] for m in ent['t-b']['models']], ['srapi/' + A])
    check('上游模型名不带头', [m['name'] for m in ent['t-b']['models']], [A])
    check('官方条目已启用', 'excluded-models' in ent['t-a'], False)
    check('srapi 条目已启用', 'excluded-models' in ent['t-b'], False)
    check('两个条目端点未被写错', [ent['t-a']['base-url'], ent['t-b']['base-url']],
          ['https://a.example/v1', 'https://b.example/v1'])

    print('== 网关 /v1/models 与 wait_models ==')
    check('网关列出两个不同 ID', sorted(stub.models), sorted([A, 'srapi/' + A]))

    print('== 落盘的 routing-plan.json ==')
    disk = json.loads((TMP / 'routing-plan.json').read_text(encoding='utf-8'))
    check('plan 里写进了 expose', [p['expose'] for p in disk['providers']], [[A], [A]])
    check('plan 里没有 available（live 态不落盘）',
          any('available' in p for p in disk['providers']), False)
    check('plan 保留 head', [p.get('head') for p in disk['providers']], ['', 'srapi'])

    print('== 目录（codex-model-catalog.json）==')
    cat = json.loads((TMP / 'codex-model-catalog.json').read_text(encoding='utf-8'))
    by = {m['slug']: m for m in cat['models']}
    check('目录有带头的 slug', 'srapi/' + A in by, True)
    check('目录有无头的 slug', A in by, True)
    check('带头条目 display_name', by['srapi/' + A]['display_name'], 'SRAPI · gpt-5.6-sol')
    check('无头条目 display_name', by[A]['display_name'], 'GPT-5.6 Sol')
    check('带头条目可见', by['srapi/' + A]['visibility'], 'list')
    check('reasoning_sync 报成功', result.get('reasoning_sync', {}).get('ok'), True)

    print('== 备份目录已建 ==')
    bks = sorted(p.name for p in (TMP / 'backups').iterdir() if p.is_dir())
    check_true('route-switch 备份存在', any(n.startswith('route-switch-') for n in bks),
               repr(bks))

    print('== 渠道头预检：两个无头同时启用 -> 拒绝且不写盘 ==')
    calls_before = len(stub.calls)
    bad_plan = copy.deepcopy(plan)
    bad_plan['providers'][1]['head'] = ''
    (TMP / 'routing-plan.json').write_text(json.dumps(bad_plan, ensure_ascii=False, indent=2),
                                           encoding='utf-8')
    stub.config = copy.deepcopy(config)
    stub.rebuild()
    try:
        bridge._apply_selection_locked({
            'revision': rs.revision(stub.config),
            'selected': {'gpt': ['oai', 'srapi'], 'deepseek': [], 'glm': []},
            'picks': {'oai': [A], 'srapi': [A]},
        })
        check_true('两个无头同时启用被拒', False)
    except bridge.RouteError as e:
        check_true('两个无头同时启用被拒', '没有渠道头' in str(e))
        print('        -> ' + str(e))
    check('被拒时没有对网关发过任何写请求',
          [c for c in stub.calls[calls_before:] if c[0] == 'PUT'], [])
    check('被拒时网关配置未变', stub.config['codex-api-key'][0].get('excluded-models'), ['*'])

    print('== 老前端形状（selected 用字符串）仍能保存 ==')
    (TMP / 'routing-plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2),
                                           encoding='utf-8')
    stub.config = copy.deepcopy(config)
    stub.rebuild()
    res2 = bridge._apply_selection_locked({
        'revision': rs.revision(stub.config),
        'selected': {'gpt': 'srapi', 'deepseek': None, 'glm': None},
        'picks': {'srapi': [A]},
    })
    ent2 = {e['headers']['X-Route-Tag']: e for e in stub.config['codex-api-key']}
    check('老形状：srapi 启用且带头', [m['alias'] for m in ent2['t-b']['models']], ['srapi/' + A])
    check('老形状：官方被停用', ent2['t-a'].get('excluded-models'), ['*'])
    check('老形状：selected 仍是列表', res2['selected']['gpt'], ['srapi'])
    check('老形状：未选的分组是空列表', res2['selected']['glm'], [])

    print('== 上游 HTTP 200 但候选为空：必须拒绝，且不得清空用户勾选 ==')
    # 这是 ADR-0008 §4 点名"最需要守住"的那条的反面：旧写法在这里
    # picks ∩ available 求交得空 → p['expose']=[] 落盘，还报保存成功。
    # 闸门有两条：① 抛出可读的拒绝；② **plan 里原有勾选一个都没少**。
    plan_kept = copy.deepcopy(plan)
    for row in plan_kept['providers']:
        row['expose'] = [A]                      # 用户此前已经勾好了
    (TMP / 'routing-plan.json').write_text(json.dumps(plan_kept, ensure_ascii=False, indent=2),
                                           encoding='utf-8')
    stub.config = copy.deepcopy(config)
    stub.rebuild()
    calls_before2 = len(stub.calls)
    # 上游"成功"（fetch_error 为 None）但一个候选都没有
    rs.fetch_all = lambda p, c: {x['id']: {'available': [], 'fetch_error': None,
                                          'expose': list(x.get('expose') or [])}
                                 for x in p['providers']}
    # 这一步会走 _compensate（它要经 bridge.gateway_get 读网关）。该接口已在场景开头
    # 被钉成常量，所以这里不必再桩一次，也不会碰真实 8317。
    try:
        bridge._apply_selection_locked({
            'revision': rs.revision(stub.config),
            'selected': {'gpt': ['oai', 'srapi'], 'deepseek': [], 'glm': []},
            'picks': {'oai': [A], 'srapi': [A]},
        })
        check_true('候选为空时拒绝保存（不再静默清空勾选）', False)
    except bridge.RouteError as e:
        check_true('候选为空时拒绝保存（不再静默清空勾选）', '空的模型列表' in str(e))
        print('        -> ' + str(e))
    check('候选为空：被拒时没有对网关发过任何写请求',
          [c for c in stub.calls[calls_before2:] if c[0] == 'PUT'], [])
    kept = json.loads((TMP / 'routing-plan.json').read_text(encoding='utf-8'))
    check('候选为空：plan 里用户原有勾选一个都没少',
          [x['expose'] for x in kept['providers']], [[A], [A]])

    print('== 上游候选非空、但勾选**全军覆没**：同样拒绝（NB-02①）==')
    # 只差一点点：候选里有别的模型，用户勾的一个都不在 —— 多半是上游改名/退役，
    # 旧写法会把 expose 清空落盘还报成功。**部分**消失仍按原设计放行，只拦全军覆没。
    plan_x = copy.deepcopy(plan)
    for row in plan_x['providers']:
        row['expose'] = [A]
    (TMP / 'routing-plan.json').write_text(json.dumps(plan_x, ensure_ascii=False, indent=2),
                                          encoding='utf-8')
    stub.config = copy.deepcopy(config)
    stub.rebuild()
    calls_before3 = len(stub.calls)
    rs.fetch_all = lambda p, c: {x['id']: {'available': [{'name': 'other', 'alias': 'other'}],
                                          'fetch_error': None,
                                          'expose': list(x.get('expose') or [])}
                                 for x in p['providers']}
    try:
        bridge._apply_selection_locked({
            'revision': rs.revision(stub.config),
            'selected': {'gpt': ['oai', 'srapi'], 'deepseek': [], 'glm': []},
            'picks': {'oai': [A]},
        })
        check_true('勾选全军覆没时拒绝保存', False)
    except bridge.RouteError as e:
        check_true('勾选全军覆没时拒绝保存', '一个都不在其中' in str(e))
        print('        -> ' + str(e))
    check('全军覆没：被拒时没有写请求',
          [c for c in stub.calls[calls_before3:] if c[0] == 'PUT'], [])
    kept2 = json.loads((TMP / 'routing-plan.json').read_text(encoding='utf-8'))
    check('全军覆没：勾选没被清空', [x['expose'] for x in kept2['providers']], [[A], [A]])

    print('== 已启用来源候选为空、而它本来有勾选：不得把它静默清空（NB-02②）==')
    # 这一条**不经过 picks**（用户没动过它），所以前面那道 picks 循环拦不住：
    # build_config 会把这个凭据的 models 写成 []（网关于是不再注册它），界面报保存成功。
    (TMP / 'routing-plan.json').write_text(json.dumps(plan_x, ensure_ascii=False, indent=2),
                                          encoding='utf-8')
    stub.config = copy.deepcopy(config)
    stub.rebuild()
    calls_before4 = len(stub.calls)
    rs.fetch_all = lambda p, c: {x['id']: {
        'available': ([{'name': 'other', 'alias': 'other'}] if x['id'] == 'oai' else []),
        'fetch_error': None, 'expose': list(x.get('expose') or [])}
        for x in p['providers']}
    try:
        bridge._apply_selection_locked({
            'revision': rs.revision(stub.config),
            'selected': {'gpt': ['oai', 'srapi'], 'deepseek': [], 'glm': []},
            'picks': {'oai': ['other']},          # srapi 没被 picks 提到，但它是启用的
        })
        check_true('候选为空但有勾选 → 拒绝（不静默停用该来源）', False)
    except bridge.RouteError as e:
        check_true('候选为空但有勾选 → 拒绝（不静默停用该来源）', '清空' in str(e))
        print('        -> ' + str(e))
    check('NB-02②：被拒时没有写请求',
          [c for c in stub.calls[calls_before4:] if c[0] == 'PUT'], [])

    print('== 只勾了图片/过期别名（不是 codex 可用）：不得被误拦（NC-1）==')
    # 这种配置本来就只能写出 models: []，守卫只该看"真正会被写进去的别名"。
    plan_z = copy.deepcopy(plan)
    for row in plan_z['providers']:
        row['expose'] = ['gpt-image-2']
    (TMP / 'routing-plan.json').write_text(json.dumps(plan_z, ensure_ascii=False, indent=2),
                                          encoding='utf-8')
    stub.config = copy.deepcopy(config)
    stub.rebuild()
    rs.fetch_all = lambda p, c: {x['id']: {'available': [], 'fetch_error': None,
                                          'expose': list(x.get('expose') or [])}
                                 for x in p['providers']}
    _err = None
    try:
        bridge._apply_selection_locked({
            'revision': rs.revision(stub.config),
            'selected': {'gpt': ['oai', 'srapi'], 'deepseek': [], 'glm': []},
            'picks': {},
        })
    except bridge.RouteError as e:
        _err = e
    check_true('图片别名不触发"清空"守卫（不误拦）', _err is None, repr(_err))

finally:
    rs.api, rs.fetch_all = real_api, real_fetch_all
    rs.ROOT, bridge.ROOT = real_roots
    bridge.PLAN_PATH, bridge.CATALOG_PATH, bridge.TEMPLATES_PATH = real_paths
    bridge.gateway_get = real_gw_get
    shutil.rmtree(TMP, ignore_errors=True)
    print('  沙箱已删除: %s' % TMP)

# ─────────────────────────────────────────────────────────── 记录一个既有问题

print()
print('== FINDING（既有问题，不是本次改动引入）==')
findings.append(
    "rs.api() 把网关地址写死成 'http://127.0.0.1:8317'（script/route_selector.py:40），"
    "而 bridge.GATEWAY_BASE 认 PRISM_GATEWAY_BASE 环境变量。也就是说文档里那个"
    "\"测试用开关\"（bridge.py:31-32）只盖住了 bridge 自己的管理接口调用，"
    "rs.apply_selection / rs.reconnect 这些走 rs.api 的路径仍然打真实 8317。"
    "本次不动它（冻结文件 + 不属于本需求），但要知道这个沙箱开关是不完整的。")
for f in findings:
    print('  * ' + f)

print()
print('RESULT: %s (%d failed)' % ('ALL PASS' if not fails else 'FAILURES', len(fails)))
for f in fails:
    print('  - ' + f)
sys.exit(1 if fails else 0)
