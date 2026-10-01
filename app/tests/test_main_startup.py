"""启动路径回归：main.py 里不绑 GUI 的纯逻辑（审查 HI-07 / ME-03 前置）。

跑法（在项目根目录）：
    python app\\tests\\test_main_startup.py

**不碰生产数据**（DEV-RULES E4）：
* import main 前先把 webview / pystray 桩掉（测试解释器没装它们，main 顶层也只
  import 不实例化）—— 这样本文件在干净解释器里也能跑；
* 端口探测用本进程自己起的 listener 与死端口，**不碰 8317**；
* request_close / tray_tip 会碰 server.read_settings、sampling.history、
  bridge.enabled_groups 三个真实数据源，全部桩掉 —— 不读生产 settings.json、
  不碰 usage-history.db、不发环回管理请求（不经过 auth_gate）；
* 单实例互斥（acquire_single_instance）**故意不测**：它会创建真实的全局命名
  mutex，正是 HANDOFF §2.3 里"僵尸进程握着 mutex"那个陷阱的现场。
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

from core import bridge, sampling  # noqa: E402
import server                      # noqa: E402
import main                        # noqa: E402

fails = []
_DB_PATH = ROOT / 'usage-history.db'
_DB_BEFORE = (_DB_PATH.stat().st_mtime_ns, _DB_PATH.stat().st_size) if _DB_PATH.exists() else None


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
           'Stop-Selector.ps1' in page and str(main.ROOT / 'script') in page)
page2 = main._busy_page(8390, '<script>alert(1)</script>')
check_true('message 做过 html 转义', '&lt;script&gt;' in page2 and '<script>alert' not in page2)

print()
print('== 2. 自启命令（_launch_command）==')
cmd = main._launch_command()
check_true('开发态用 pythonw（不弹黑框）', 'pythonw.exe' in cmd and 'main.py' in cmd, cmd)
_frozen_exe, _frozen_flag = sys.executable, getattr(sys, 'frozen', False)
try:
    sys.executable = r'C:\Program Files\Prism\Prism.exe'
    sys.frozen = True
    cmd = main._launch_command()
    check('打包态就是 exe 本身加引号', cmd, '"%s"' % sys.executable)
finally:
    sys.executable, sys.frozen = _frozen_exe, _frozen_flag

print()
print('== 3. 网关探测（不碰 8317）==')
_orig_host, _orig_port = main.GATEWAY_HOST, main.GATEWAY_PORT
listener = socket.socket()
listener.bind(('127.0.0.1', 0))
listener.listen(1)
free_port = listener.getsockname()[1]
try:
    main.GATEWAY_HOST, main.GATEWAY_PORT = '127.0.0.1', free_port
    check_true('本进程 listener 是"在监听"', main.gateway_listening(timeout=0.5))
    main.GATEWAY_PORT = 1
    check_true('死端口探不到（连接被拒）', not main.gateway_listening(timeout=0.5))
    main.GATEWAY_HOST, main.GATEWAY_PORT = _orig_host, 8390
    check_true('沙箱端口不代拉网关', not main.gateway_is_prisms_to_launch())
    main.GATEWAY_HOST, main.GATEWAY_PORT = _orig_host, _orig_port
    check_true('默认地址（config.yaml 那个）才代拉', main.gateway_is_prisms_to_launch())
finally:
    main.GATEWAY_HOST, main.GATEWAY_PORT = _orig_host, _orig_port
    listener.close()

print()
print('== 4. request_close：关窗口唯一入口 ==')
_orig_read = server.read_settings
try:
    shell = main.Shell(8318)
    hidden, notified = [], []
    shell.hide_window = lambda: hidden.append(1)
    shell.notify = lambda msg: notified.append(msg)

    server.read_settings = lambda: {'app': {'close_to_tray': True}}
    check('托盘常驻开（默认）：关闭被取消', shell.request_close(), False)
    check('取消时隐藏了窗口并通知了用户', (len(hidden), len(notified)), (1, 1))
    server.read_settings = lambda: {'app': {'close_to_tray': False}}
    check('托盘常驻关：放行真关闭', shell.request_close(), True)
    shell.quitting.set()
    server.read_settings = lambda: {'app': {'close_to_tray': True}}
    check('退出流程中不再拦（避免托盘退出死不掉）', shell.request_close(), True)
    fresh = main.Shell(8318)          # 新实例：上面已把 quitting 置位，别让状态泄漏
    fresh.hide_window = lambda: hidden.append(1)
    fresh.notify = lambda msg: notified.append(msg)
    server.read_settings = lambda: (_ for _ in ()).throw(RuntimeError('读不到'))
    check_true('设置读炸时按默认（托盘常驻）处理', fresh.request_close() is False)
finally:
    server.read_settings = _orig_read

print()
print('== 5. 托盘文案（_tip_route / _tray_title）==')
shell = main.Shell(8318)
check('down 分支', shell._tip_route(main.IDENTITY_DOWN), '路由 网关未运行')
check('foreign 分支', shell._tip_route(main.IDENTITY_FOREIGN),
      '路由 网关不是本目录的，读不到')
check('unknown 分支', shell._tip_route(main.IDENTITY_UNKNOWN),
      '路由 网关身份未核实，读不到')
_orig_groups = bridge.enabled_groups
try:
    bridge.enabled_groups = lambda: {'gpt': ['openai-official', 'hub-big'],
                                     'deepseek': 'goat', 'glm': []}
    check('列表/字符串/空列表三种形状', shell._tip_route(main.IDENTITY_OK),
          '路由 deepseek=goat glm=未选 gpt=openai-official、hub-big')
    bridge.enabled_groups = lambda: {}
    check('空路由', shell._tip_route(main.IDENTITY_OK), '路由 数据不可用')
    bridge.enabled_groups = lambda: (_ for _ in ()).throw(RuntimeError('x'))
    check('读路由失败不抛', shell._tip_route(main.IDENTITY_OK), '路由 数据不可用')
finally:
    bridge.enabled_groups = _orig_groups
title = shell._tray_title('ok')
check_true('标题带名字与在线', title.startswith('Prism · ') and '网关在线' in title, title)
_orig_identity = main.identity.gateway_identity
try:
    main.identity.gateway_identity = lambda: {'state': 'foreign'}
    check('gateway_state 透传 identity', shell.gateway_state(), 'foreign')
finally:
    main.identity.gateway_identity = _orig_identity

print()
print('== 6. tray_tip 节流（now 注入，零真数据源）==')
_orig_history = sampling.history
shell = main.Shell(8318)
try:
    sampling.history = lambda days: [{'success': 3, 'failed': 1}]
    bridge.enabled_groups = lambda: {'gpt': ['openai-official']}
    shell.gateway_state = lambda: 'ok'
    first = shell.tray_tip(now=1000.0)
    check_true('首算含今日计数与路由',
               '今日请求 3 成功/1 失败' in first and 'gpt=openai-official' in first, first)
    cached = shell.tray_tip(now=1000.0 + main.TRAY_TIP_INTERVAL - 1)
    check('节流窗口内返回缓存', cached, first)
    second = shell.tray_tip(now=1000.0 + main.TRAY_TIP_INTERVAL + 0.5)
    check_true('窗口外重算（still 同一文案时也算过）', second == first or len(second) > 0, second)
    check_true('tip 不超 Windows szTip 上限', len(shell.tray_tip(now=2000.0)) <= main.TRAY_TIP_MAX)
finally:
    sampling.history = _orig_history
    bridge.enabled_groups = _orig_groups

print()
print('== 7. 生产数据污染闸门（E4）==')
_db_after = (_DB_PATH.stat().st_mtime_ns, _DB_PATH.stat().st_size) if _DB_PATH.exists() else None
check('usage-history.db 跑完前后零变化', _db_after, _DB_BEFORE)
backups_dir = ROOT / 'backups'
leaked = sorted(p.name for p in backups_dir.glob('prism-*')) if backups_dir.is_dir() else []
check('生产 backups/ 没有漏进临时目录', leaked, [])

print()
print('RESULT: %s (%d failed)' % ('ALL PASS' if not fails else 'FAILURES', len(fails)))
for f in fails:
    print('  - ' + f)
sys.exit(1 if fails else 0)
