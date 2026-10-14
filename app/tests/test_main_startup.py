"""启动路径回归：main.py 里不绑 GUI 的纯逻辑（审查 HI-07 / ME-03 前置）。

跑法（在项目根目录）：
    python app\\tests\\test_main_startup.py

**不碰生产数据**（AGENTS.md 的数据隔离要求）：
* import main 前先把 webview / pystray 桩掉（测试解释器没装它们，main 顶层也只
  import 不实例化）—— 这样本文件在干净解释器里也能跑；
* 端口探测用本进程自己起的 listener 与死端口，**不碰 8317**；
* request_close 只读 server.read_settings，由本测试桩掉；
* 单实例互斥（acquire_single_instance）**故意不测**：它会创建真实的全局命名
  mutex，避免在测试进程里留下全局锁。
"""
import io
import json
import shutil
import socket
import sys
import tempfile
import threading
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT / 'script'))

# main.py 顶层 import webview / pystray，但只在函数体里用。测试解释器没装这两个
# 包，塞空模块让 import 通过（属性级用法在函数里，不桩到那一层）。
sys.modules.setdefault('webview', types.ModuleType('webview'))
sys.modules.setdefault('pystray', types.ModuleType('pystray'))

from core import bridge, gateway, identity, sampling  # noqa: E402
import server                      # noqa: E402
import main                        # noqa: E402
import diagnostics                 # noqa: E402
import shell as desktop            # noqa: E402

fails = []
_DB_PATH = bridge.ROOT / 'usage-history.db'
_DB_BEFORE = (_DB_PATH.stat().st_mtime_ns, _DB_PATH.stat().st_size) if _DB_PATH.exists() else None
# E4 闸门基线：本文件测的 ensure_first_run_files 产出的就是这几类根文件——
# 它们若被误写（函数改去用绝对路径绕过 bridge.ROOT 的 F-1 同类事故），这里必须红
_ROOT_FILES = ('config.yaml', '.local-secrets.json', 'routing-plan.json')


def _fingerprint(p):
    return (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None


_ROOT_BEFORE = {n: _fingerprint(bridge.ROOT / n) for n in _ROOT_FILES}


def check(name, got, want):
    ok = got == want
    print(('  PASS  ' if ok else '  FAIL  ') + name)
    if not ok:
        print('        got : %r' % (got,))
        print('        want: %r' % (want,))
        fails.append(name)


def check_true(name, cond, detail=''):
    check(name + (('  -> ' + detail) if (detail and not cond) else ''), bool(cond), True)


print('== 1. 端口被占兜底页（_busy_page）==')
page = main._busy_page(8318, '测试占用原因')
check_true('是完整 HTML 且标注端口',
           page.startswith('<!doctype html>') and '8318' in page, page[:80])
check_true('故障排查命令指向本目录的 Stop-Selector.ps1',
           'Stop-Selector.ps1' in page and str(main.PROJECT_DIR / 'script') in page)
page2 = main._busy_page(8390, '<script>alert(1)</script>')
check_true('message 做过 html 转义', '&lt;script&gt;' in page2 and '<script>alert' not in page2)

print()
print('== 2. 自启命令（_launch_command）==')
cmd = desktop._launch_command()
check_true('开发态用 pythonw（不弹黑框）', 'pythonw.exe' in cmd and 'main.py' in cmd, cmd)
_frozen_exe, _frozen_flag = sys.executable, getattr(sys, 'frozen', False)
try:
    sys.executable = r'C:\Program Files\Prism\Prism.exe'
    sys.frozen = True
    cmd = desktop._launch_command()
    check('打包态就是 exe 本身加引号', cmd, '"%s"' % sys.executable)
finally:
    sys.executable, sys.frozen = _frozen_exe, _frozen_flag

print()
print('== 3. 网关探测（不碰 8317）==')
listener = socket.socket()
listener.bind(('127.0.0.1', 0))
listener.listen(1)
free_port = listener.getsockname()[1]
try:
    check_true('本进程 listener 是"在监听"', gateway.port_open('127.0.0.1', free_port, timeout=0.5))
    check_true('死端口探不到（连接被拒）', not gateway.port_open('127.0.0.1', 1, timeout=0.5))
    check_true('沙箱地址不代拉网关', not main.gateway_proc.is_prisms_address('127.0.0.1', 8390))
    check_true('默认地址由 Prism 管理', main.gateway_proc.is_prisms_address(
        identity.DEFAULT_GATEWAY_HOST, identity.DEFAULT_GATEWAY_PORT))
finally:
    listener.close()

print()
print('== 4. request_close：关窗口唯一入口 ==')
_orig_read = server.read_settings
try:
    shell = desktop.Shell(8318)
    hidden, notified = [], []
    shell.hide_window = lambda: hidden.append(1)
    shell.notify = lambda msg: notified.append(msg)

    server.read_settings = lambda: {'app': {'close_to_tray': True}}
    check('托盘常驻开（默认）：关闭被取消', shell.request_close(), False)
    check('取消时隐藏了窗口且保持静默（不弹通知气泡）', (len(hidden), len(notified)), (1, 0))
    server.read_settings = lambda: {'app': {'close_to_tray': False}}
    check('托盘常驻关：放行真关闭', shell.request_close(), True)
    shell.quitting.set()
    server.read_settings = lambda: {'app': {'close_to_tray': True}}
    check('退出流程中不再拦（避免托盘退出死不掉）', shell.request_close(), True)
    fresh = desktop.Shell(8318)       # 新实例：上面已把 quitting 置位，别让状态泄漏
    fresh.hide_window = lambda: hidden.append(1)
    fresh.notify = lambda msg: notified.append(msg)
    server.read_settings = lambda: (_ for _ in ()).throw(RuntimeError('读不到'))
    check_true('设置读炸时按默认（托盘常驻）处理', fresh.request_close() is False)
finally:
    server.read_settings = _orig_read

print()
print('== 5. 托盘状态与提示 ==')
shell = desktop.Shell(8318)
_orig_identity = identity.gateway_identity
try:
    identity.gateway_identity = lambda: {'state': 'foreign'}
    check('gateway_state 透传 identity', shell.gateway_state(), 'foreign')
finally:
    identity.gateway_identity = _orig_identity
check('托盘名称极简', desktop.APP_NAME, 'Prism')
icon = types.SimpleNamespace(icon=None, title='旧的冗长提示')
shell.icon = icon
shell.gateway_state = lambda: 'ok'
_orig_image = desktop.tray_image
try:
    desktop.tray_image = lambda _state: None
    shell.refresh_tray()
    check('托盘提示只显示 Prism', icon.title, 'Prism')
finally:
    desktop.tray_image = _orig_image

check_true('notify_already_running 是模块级函数（历史上曾被缩进进 notify_error 体内，'
           '第二次启动分支一走就 NameError 静默退出）',
           callable(getattr(diagnostics, 'notify_already_running', None)))

print()
print('== 7. 启动体检（ME-03：bridge.verify_startup_files，全在临时目录）==')
_vroot = tempfile.mkdtemp(prefix='prism-medit-')
_orig_root = bridge.ROOT
try:
    bridge.ROOT = Path(_vroot)
    check('全缺失（首次启动）算健康', bridge.verify_startup_files(), [])

    def _write(name, content):
        p = Path(_vroot, name)
        p.parent.mkdir(exist_ok=True)
        p.write_text(content, encoding='utf-8')

    _write('config.yaml', 'codex-api-key: []\n')
    _write('routing-plan.json', json.dumps(
        {'providers': [{'id': 'alpha', 'group': 'gpt'}],
         'selected': {'gpt': ['alpha']}, 'version': 2}))
    _write('codex-model-catalog.json', '{"models": {}}')
    _write('codex-model-catalog-templates.json', '{}')
    check('健康四件套零问题', bridge.verify_startup_files(), [])
    check_true('找到的备份目录为空（还没人备份过）',
               bridge.latest_switch_backup() is None)

    _write('routing-plan.json', '{"providers": [{"id": "al')
    check_true('plan 截断被点名',
               any('routing-plan.json 解析失败' in p for p in bridge.verify_startup_files()),
               str(bridge.verify_startup_files()))

    _write('routing-plan.json', json.dumps(
        {'providers': [{'id': 'alpha', 'group': 'gpt'}],
         'selected': {'gpt': ['ghost-id']}}))
    check_true('selected 引用不存在的 id 被点名',
               any("selected.gpt 引用了不存在的来源 id" in p and 'ghost-id' in p
                   for p in bridge.verify_startup_files()),
               str(bridge.verify_startup_files()))

    _write('routing-plan.json', json.dumps(
        {'providers': [{'id': 'alpha', 'group': 'gpt'}], 'selected': {'gpt': 'alpha'}}))
    check('旧版单字符串形状照收', bridge.verify_startup_files(), [])

    _write('routing-plan.json', json.dumps(
        {'providers': [{'id': 'alpha', 'group': 'gpt'}], 'selected': {'gpt': None}}))
    check('组内 null = 未选（合法形状，route_selector._selected_ids 同口径）',
          bridge.verify_startup_files(), [])

    _write('routing-plan.json', json.dumps(
        {'providers': [{'id': 'alpha', 'group': 'gpt'}], 'selected': {'gpt': 42}}))
    check_true('selected 非数组非字符串被点名',
               any('selected.gpt' in p for p in bridge.verify_startup_files()),
               str(bridge.verify_startup_files()))

    _write('routing-plan.json', json.dumps({'providers': 'junk', 'selected': {}}))
    check_true('providers 非列表被点名',
               any('providers 不是列表' in p for p in bridge.verify_startup_files()),
               str(bridge.verify_startup_files()))

    _write('routing-plan.json', json.dumps({'providers': [], 'selected': {}}))
    _write('config.yaml', '')
    check_true('空 config.yaml 被点名',
               any('config.yaml 是空文件' in p for p in bridge.verify_startup_files()),
               str(bridge.verify_startup_files()))

    _write('config.yaml', 'ok: yes\n')
    _write('codex-model-catalog.json', '{oops}')
    check_true('catalog 截断被点名',
               any('codex-model-catalog.json 解析失败' in p
                   for p in bridge.verify_startup_files()),
               str(bridge.verify_startup_files()))

    Path(_vroot, 'backups', 'route-switch-20260901-000000-111111').mkdir(parents=True)
    Path(_vroot, 'backups', 'route-switch-20261001-000000-222222').mkdir(parents=True)
    latest = bridge.latest_switch_backup()
    check_true('备份按名字取最新（时间戳定宽，字典序=时间序）',
               latest is not None and '20261001' in latest.name, str(latest))
finally:
    bridge.ROOT = _orig_root
    shutil.rmtree(_vroot, ignore_errors=True)

print()
print('== 8. 首启引导（HI-03：bridge.ensure_first_run_files，全在临时目录）==')
check('项目源码目录独立于运行数据目录', bridge.ROOT, ROOT / '.runtime')
check('route_selector 共享同一运行目录', bridge.rs.ROOT, bridge.ROOT)
check('网关程序保留在项目根目录', identity.GATEWAY_EXE, ROOT / 'cli-proxy-api.exe')
check('网关配置位于 runtime/', identity.GATEWAY_CONFIG, bridge.ROOT / 'config.yaml')
_froot = tempfile.mkdtemp(prefix='prism-first-run-')
_orig_root2 = bridge.ROOT
try:
    bridge.ROOT = Path(_froot)
    check('全新目录：生成配置、计划与模型模板', sorted(bridge.ensure_first_run_files()),
          ['.local-secrets.json', 'codex-model-catalog-templates.json', 'config.yaml', 'routing-plan.json'])
    plan = json.loads(Path(_froot, 'routing-plan.json').read_text(encoding='utf-8'))
    check('空计划没有来源和分组', plan, {'providers': [], 'groups': [], 'selected': {}})
    saved = json.loads(Path(_froot, '.local-secrets.json').read_text(encoding='utf-8'))
    cfg = Path(_froot, 'config.yaml').read_text(encoding='utf-8')
    check_true('management_key 前缀 prism- 且两处一致',
               saved['management_key'].startswith('prism-')
               and ('secret-key: "%s"' % saved['management_key']) in cfg,
               saved['management_key'][:12] + '…')
    check_true('api_key 进了网关放行清单',
               saved['api_key'].startswith('sk-prism-')
               and ('"%s"' % saved['api_key']) in cfg, saved['api_key'][:12] + '…')
    check_true('首启会创建完整运行目录结构', Path(_froot, 'auth').is_dir()
               and Path(_froot, 'logs').is_dir()
               and Path(_froot, 'codex-model-catalog-templates.json').is_file())
    check_true('首启配置默认开启请求统计与文件日志',
               'usage-statistics-enabled: true' in cfg
               and 'logging-to-file: true' in cfg
               and 'logs-max-total-size-mb: 32' in cfg
               and 'error-logs-max-files: 5' in cfg)
    check_true('上游来源段是空列表（不编造凭据）',
               'codex-api-key: []' in cfg and 'openai-compatibility: []' in cfg)
    # 全新尚无 usage-history.db 时直接查 history() 应幂等建表并返回空数组，不抛 OperationalError
    _orig_db = sampling.DB_PATH
    try:
        sampling.DB_PATH = Path(_froot, 'usage-history.db')
        check('全新目录查采样历史：自动建表并返回空列表', sampling.history(7), [])
    finally:
        sampling.DB_PATH = _orig_db
    check('再跑一次幂等（一个字节不动）', bridge.ensure_first_run_files(), [])

    before_cfg = cfg
    check('已初始化的目录：什么都不生成', bridge.ensure_first_run_files(), [])
    check_true('既有 config.yaml 原样', Path(_froot, 'config.yaml').read_text(encoding='utf-8') == before_cfg)

    # 密钥随机性：清空重来，两轮的 key 必须不同
    first_key = saved['management_key']
    Path(_froot, '.local-secrets.json').unlink()
    Path(_froot, 'config.yaml').unlink()
    bridge.ensure_first_run_files()
    saved2 = json.loads(Path(_froot, '.local-secrets.json').read_text(encoding='utf-8'))
    check_true('两轮生成的 management_key 不同（secrets 随机）',
               saved2['management_key'] != first_key, saved2['management_key'][:12] + '…')

    # 半初始化 A：config 在而 secrets 缺 → 不自动补、不抛、原样保留
    Path(_froot, '.local-secrets.json').unlink()
    keep = Path(_froot, 'config.yaml').read_text(encoding='utf-8')
    check('半初始化 A：不生成、不抛', bridge.ensure_first_run_files(), [])
    check_true('半初始化 A：config.yaml 原样', Path(_froot, 'config.yaml').read_text(encoding='utf-8') == keep)

    # 半初始化 B：secrets 在而 config 缺 → 以磁盘上的 secrets 补写 config，两处一致
    Path(_froot, '.local-secrets.json').write_text(json.dumps(saved2), encoding='utf-8')
    Path(_froot, 'config.yaml').unlink()
    created_b = bridge.ensure_first_run_files()
    check('半初始化 B：补出 config.yaml', created_b, ['config.yaml'])
    cfg_b = Path(_froot, 'config.yaml').read_text(encoding='utf-8')
    check_true('半初始化 B：config 里的 key 与 secrets 一致',
               ('secret-key: "%s"' % saved2['management_key']) in cfg_b,
               cfg_b[:120])

    # 并发抢先（复查 M-2）：目标已存在时原子写必须放弃且绝不覆盖
    victim = Path(_froot, 'probe.json')
    victim.write_text('original', encoding='utf-8')
    check('并发抢先：_atomic_write_json 返回 False', bridge._atomic_write_json(victim, {'x': 1}), False)
    check('并发抢先：原内容未覆盖', victim.read_text(encoding='utf-8'), 'original')
    check_true('并发抢先：临时文件被撤掉', not list(Path(_froot).glob('*.tmp')))
finally:
    bridge.ROOT = _orig_root2
    shutil.rmtree(_froot, ignore_errors=True)

print()
print('== 9. 生产数据污染闸门（E4）==')
_db_after = (_DB_PATH.stat().st_mtime_ns, _DB_PATH.stat().st_size) if _DB_PATH.exists() else None
check('usage-history.db 跑完前后零变化', _db_after, _DB_BEFORE)
for _n in _ROOT_FILES:
    check('生产 %s 零变化（首启引导若绕过 bridge.ROOT 这里必须红）' % _n,
          _fingerprint(bridge.ROOT / _n), _ROOT_BEFORE[_n])
backups_dir = bridge.ROOT / 'backups'
leaked = sorted(p.name for p in backups_dir.glob('prism-*')) if backups_dir.is_dir() else []
check('生产 backups/ 没有漏进临时目录', leaked, [])

print()
print('RESULT: %s (%d failed)' % ('ALL PASS' if not fails else 'FAILURES', len(fails)))
for f in fails:
    print('  - ' + f)
sys.exit(1 if fails else 0)
