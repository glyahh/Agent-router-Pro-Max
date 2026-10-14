"""官方登录来源（auth-file）的编辑回归。

跑法（在项目根目录）：
    python app\\tests\\test_official_edit.py

不碰生产文件、不碰真网关。确认能改名称、分组、渠道头、模型和推理档位，
同时 section、端点、登录文件和网关配置保持原样。其它内置来源仍不可编辑。
"""
import copy
import json
import shutil
import sys
import tempfile
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


class StubGateway:
    """只应答读配置。官方编辑不应该发出任何写请求。"""

    def __init__(self, config):
        self.config = copy.deepcopy(config)
        self.calls = []

    def gw_get(self, path):
        self.calls.append(('GET', path))
        if path == 'config':
            return copy.deepcopy(self.config)
        return copy.deepcopy(self.config.get(path))

    def gw_put(self, path, data):
        self.calls.append(('PUT', path))
        raise AssertionError('官方编辑不该写网关：' + path)

    def gw_delete(self, path, params):
        self.calls.append(('DELETE', path))
        raise AssertionError('官方编辑不该删网关：' + path)


def base_plan():
    return {
        'note': 'stub',
        'selected': {'gpt': ['oai'], 'deepseek': []},
        'providers': [
            {'id': 'oai', 'label': 'OpenAI 官方', 'group': 'gpt', 'section': 'auth-file',
             'base_url': 'auth://codex-official.json', 'blocked': 'keep-me',
             'models': [{'name': 'gpt-5.6-sol', 'alias': 'gpt-5.6-sol'},
                        {'name': 'gpt-6-astra', 'alias': 'gpt-6-astra'}],
             'expose': ['gpt-5.6-sol', 'gpt-6-astra']},
            {'id': 'srapi', 'label': 'SRAPI', 'group': 'gpt', 'section': 'codex-api-key',
             'base_url': 'https://b.example/v1', 'tag': 't-b', 'head': 'srapi',
             'models': [{'name': 'gpt-5.6-sol', 'alias': 'gpt-5.6-sol'}], 'expose': []},
            {'id': 'deepseek-official', 'label': 'DeepSeek 官方', 'group': 'deepseek',
             'section': 'openai-compatibility', 'base_url': 'https://d.example/v1',
             'name': 'DeepSeek Official', 'models': [], 'expose': []},
        ],
    }


def base_config():
    return {
        'codex-api-key': [
            {'api-key': 'k-b', 'base-url': 'https://b.example/v1',
             'headers': {'X-Route-Tag': 't-b'}, 'excluded-models': ['*'], 'models': []},
        ],
        'openai-compatibility': [
            {'name': 'DeepSeek Official', 'base-url': 'https://d.example/v1', 'disabled': True,
             'api-key-entries': [{'api-key': 'k-d'}], 'models': []},
        ],
    }


AUTH_TEXT = json.dumps({'access_token': 'keep-token', 'excluded_models': ['*']})

TMP = Path(tempfile.mkdtemp(prefix='prism-official-edit-'))
real = (rs.ROOT, bridge.ROOT, bridge.gateway_get, bridge.gateway_put, bridge.gateway_delete,
        bridge.PLAN_PATH, bridge.CATALOG_PATH)
try:
    (TMP / '.local-secrets.json').write_text(
        json.dumps({'management_key': 'stub', 'api_key': 'stub-client-key'}), encoding='utf-8')
    (TMP / 'config.yaml').write_text('port: 1\n', encoding='utf-8')
    (TMP / 'auth').mkdir(exist_ok=True)
    auth_path = TMP / 'auth' / 'codex-official.json'
    auth_path.write_text(AUTH_TEXT, encoding='utf-8')

    rs.ROOT = TMP
    bridge.ROOT = TMP
    bridge.PLAN_PATH = TMP / 'routing-plan.json'
    bridge.CATALOG_PATH = TMP / 'codex-model-catalog.json'

    def write_plan(plan):
        (TMP / 'routing-plan.json').write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')

    def disk_plan():
        return json.loads((TMP / 'routing-plan.json').read_text(encoding='utf-8'))

    def reset(plan=None):
        write_plan(plan if plan is not None else base_plan())
        (TMP / 'codex-model-catalog.json').write_text(json.dumps({'models': [
            {'slug': 'oai/gpt-5.6-sol', 'display_name': 'Sol',
             'supported_reasoning_levels': [{'effort': 'low', 'description': 'low'}],
             'default_reasoning_level': 'low'},
        ]}, ensure_ascii=False), encoding='utf-8')
        auth_path.write_text(AUTH_TEXT, encoding='utf-8')
        stub = StubGateway(base_config())
        bridge.gateway_get = stub.gw_get
        bridge.gateway_put = stub.gw_put
        bridge.gateway_delete = stub.gw_delete
        return stub

    print('== 官方登录来源可以改显示项 ==')
    stub = reset()
    saved_config = copy.deepcopy(stub.config)
    res = S.update_source('oai', {
        'label': 'OpenAI 官方改',
        'group': 'deepseek',
        'head': 'oai',
        'base_url': 'https://evil.example/v1',
        'api_key': 'sk-should-ignore',
        'models': [
            {'name': 'Sol', 'alias': 'gpt-5.6-sol'},
            {'name': 'gpt-5.5', 'alias': 'gpt-5.5'},
        ],
        'model_settings': {'gpt-5.6-sol': {'levels': ['high', 'xhigh'], 'default': 'high'}},
        'context_window': 272000,
    })
    row = next(p for p in disk_plan()['providers'] if p['id'] == 'oai')
    check('返回的仍是官方来源', res.get('section'), 'auth-file')
    check('显示名称已改', row.get('label'), 'OpenAI 官方改')
    check('分组已改', row.get('group'), 'deepseek')
    check('渠道头已改', row.get('head'), 'oai')
    check('模型列表已改', row.get('models'), [
        {'name': 'Sol', 'alias': 'gpt-5.6-sol'},
        {'name': 'gpt-5.5', 'alias': 'gpt-5.5'},
    ])
    check('推理档位已改', row.get('model_settings'),
          {'gpt-5.6-sol': {'levels': ['high', 'xhigh'], 'default': 'high'}})
    check('上下文窗口已改', row.get('context_window'), 272000)
    check('删掉的模型不再暴露，新模型不自动暴露', row.get('expose'), ['gpt-5.6-sol'])
    check('section 仍是 auth-file', row.get('section'), 'auth-file')
    check('端点没被请求体换掉', row.get('base_url'), 'auth://codex-official.json')
    check('id 没变', row.get('id'), 'oai')
    check_true('没有被标成自定义来源', 'custom' not in row)
    check('blocked 保留', row.get('blocked'), 'keep-me')
    others = [p for p in disk_plan()['providers'] if p['id'] != 'oai']
    check('其它来源没被改写', others, [p for p in base_plan()['providers'] if p['id'] != 'oai'])
    check('选择项没动', disk_plan().get('selected'), base_plan()['selected'])
    check('登录文件没动', auth_path.read_text(encoding='utf-8'), AUTH_TEXT)
    check('网关配置没动', stub.config, saved_config)
    check_true('没有写网关', not any(c[0] in ('PUT', 'DELETE') for c in stub.calls))
    catalog = json.loads((TMP / 'codex-model-catalog.json').read_text(encoding='utf-8'))
    entry = catalog['models'][0]
    check('目录默认推理等级已涂上', entry.get('default_reasoning_level'), 'high')
    check('目录推理等级已涂上',
          [x.get('effort') for x in entry.get('supported_reasoning_levels') or []],
          ['high', 'xhigh'])

    print('== 重复渠道头拒绝，且不写盘 ==')
    stub = reset()
    before = (TMP / 'routing-plan.json').read_text(encoding='utf-8')
    try:
        S.update_source('oai', {
            'label': 'OpenAI 官方', 'group': 'gpt', 'head': 'srapi',
            'base_url': 'https://evil.example/v1', 'api_key': 'sk-nope',
            'models': [{'name': 'gpt-5.6-sol', 'alias': 'gpt-5.6-sol'}],
        })
        check_true('重复渠道头被拒', False)
    except S.RouteError as e:
        check_true('重复渠道头被拒', '渠道头' in str(e), str(e))
    check('拒绝后 plan 原样', (TMP / 'routing-plan.json').read_text(encoding='utf-8'), before)
    check('拒绝后登录文件原样', auth_path.read_text(encoding='utf-8'), AUTH_TEXT)

    print('== 空模型列表拒绝 ==')
    stub = reset()
    before = (TMP / 'routing-plan.json').read_text(encoding='utf-8')
    try:
        S.update_source('oai', {
            'label': 'OpenAI 官方', 'group': 'gpt', 'head': '',
            'models': [],
        })
        check_true('空模型被拒', False)
    except S.RouteError as e:
        check_true('空模型被拒', '至少填一个模型' in str(e), str(e))
    check('空模型拒绝后 plan 原样', (TMP / 'routing-plan.json').read_text(encoding='utf-8'), before)

    print('== 其它内置来源仍不可编辑 ==')
    stub = reset()
    before = (TMP / 'routing-plan.json').read_text(encoding='utf-8')
    try:
        S.update_source('deepseek-official', {
            'label': '改名', 'group': 'deepseek', 'head': '',
            'base_url': 'https://d.example/v1',
            'models': [{'name': 'deepseek-v4-pro', 'alias': 'deepseek-v4-pro'}],
        })
        check_true('非官方内置来源被拒', False)
    except S.RouteError as e:
        check_true('非官方内置来源被拒', '内置来源不可编辑' in str(e), str(e))
    check('拒绝后 plan 原样', (TMP / 'routing-plan.json').read_text(encoding='utf-8'), before)
    check_true('拒绝时也没写网关', not any(c[0] in ('PUT', 'DELETE') for c in stub.calls))

finally:
    (rs.ROOT, bridge.ROOT, bridge.gateway_get, bridge.gateway_put, bridge.gateway_delete,
     bridge.PLAN_PATH, bridge.CATALOG_PATH) = real
    shutil.rmtree(TMP, ignore_errors=True)
    print('  沙箱已删除: %s' % TMP)

print()
print('RESULT: %s (%d failed)' % ('ALL PASS' if not fails else 'FAILURES', len(fails)))
for item in fails:
    print('  - ' + item)
sys.exit(1 if fails else 0)
