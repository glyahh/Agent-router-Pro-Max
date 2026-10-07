"""分组名单：空计划首启、旧计划只读补全、改名不改 id、有来源时拒绝删除。

跑法（在项目根目录）：
    python app\\tests\\test_groups.py

不碰生产 routing-plan.json，也不打真网关。
"""
import json
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


print('== 旧计划没有 groups 键：读路径补显示名，不写盘 ==')
old = {'providers': [
    {'id': 'a', 'group': 'gpt'},
    {'id': 'b', 'group': 'glm'},
    {'id': 'c', 'group': 'gpt'},
]}
check('补出的分组', rs.groups_of(old),
      [{'id': 'gpt', 'name': 'GPT 中转站'}, {'id': 'glm', 'name': 'GLM'}])
check('空名单就是空', rs.groups_of({'providers': [], 'groups': [], 'selected': {}}), [])

print('== 首启写出空计划；已有文件不覆盖 ==')
tmp = Path(tempfile.mkdtemp(prefix='prism-groups-'))
real = (rs.ROOT, bridge.ROOT)
try:
    rs.ROOT = tmp
    bridge.ROOT = tmp
    (tmp / '.local-secrets.json').write_text('{"management_key":"prism-x","api_key":"sk-prism-x"}',
                                             encoding='utf-8')
    (tmp / 'config.yaml').write_text('port: 1\n', encoding='utf-8')
    created = bridge.ensure_first_run_files()
    check('只补 routing-plan.json', created, ['routing-plan.json'])
    plan_path = tmp / 'routing-plan.json'
    check('空计划', json.loads(plan_path.read_text(encoding='utf-8')),
          {'providers': [], 'groups': [], 'selected': {}})
    kept = plan_path.read_text(encoding='utf-8')
    # 旧文件：换成没有 groups 键的计划，再跑首启，字节不动
    legacy = {'providers': [{'id': 'a', 'group': 'deepseek', 'label': 'D'}], 'selected': {}}
    plan_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding='utf-8')
    before = plan_path.read_bytes()
    check('已有计划不再生成', bridge.ensure_first_run_files(), [])
    check_true('旧计划字节不动', plan_path.read_bytes() == before)
    check('读出来仍是 DEEPSEEK', rs.groups_of(json.loads(plan_path.read_text(encoding='utf-8'))),
          [{'id': 'deepseek', 'name': 'DEEPSEEK'}])

    print('== 建组、改名不改 id、有来源拒绝删除 ==')
    made = S.create_group({'name': 'Kimi'})
    check_true('新 id 以 g- 开头', isinstance(made.get('id'), str) and made['id'].startswith('g-'),
               str(made))
    check('显示名', made.get('name'), 'Kimi')
    disk = json.loads(plan_path.read_text(encoding='utf-8'))
    ids = [g['id'] for g in disk['groups']]
    check_true('旧分组还在', 'deepseek' in ids)
    check_true('新分组写进名单', made['id'] in ids)
    check('来源的 group 没被改名带走', disk['providers'][0]['group'], 'deepseek')
    renamed = S.rename_group(made['id'], {'name': '月之暗面'})
    check('改名后 id 不变', renamed['id'], made['id'])
    check('改名后的显示名', renamed['name'], '月之暗面')
    try:
        S.delete_group('deepseek')
        check_true('有来源时删除应拒绝', False)
    except S.RouteError as exc:
        check_true('有来源时删除拒绝', '还有来源' in str(exc), str(exc))
    S.delete_group(made['id'])
    left = json.loads(plan_path.read_text(encoding='utf-8'))
    check('空分组可以删', [g['id'] for g in left['groups']], ['deepseek'])

    print('== 添加来源拒绝未知分组 ==')
    try:
        S._clean_spec({'label': 'T', 'group': 'nope', 'base_url': 'https://e.com/v1',
                       'api_key': 'k', 'models': [{'name': 'm', 'alias': 'm'}]}, True,
                      groups=['deepseek'])
        check_true('未知分组应拒绝', False)
    except S.RouteError as exc:
        check_true('未知分组拒绝', '分组不存在' in str(exc), str(exc))
    try:
        S.create_group({'name': '  '})
        check_true('空名应拒绝', False)
    except S.RouteError as exc:
        check_true('空名拒绝', '不能为空' in str(exc), str(exc))
    try:
        S.create_group({'name': 'DEEPSEEK'})
        check_true('同名应拒绝', False)
    except S.RouteError as exc:
        check_true('同名拒绝', '同名' in str(exc), str(exc))
finally:
    rs.ROOT, bridge.ROOT = real
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

print()
if fails:
    print('FAILED %d' % len(fails))
    sys.exit(1)
print('ALL PASS')
