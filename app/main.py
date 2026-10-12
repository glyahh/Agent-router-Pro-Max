r"""Prism 桌面入口：启动本地服务、托盘和 WebView 窗口。

业务在 core/，HTTP 在 server.py，Win32 与 WebView2 适配在 windowing.py。
退出 Prism 不停止网关；只在配置地址空闲且受 Prism 管理时启动它。
"""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import sys
import threading
import time
import traceback

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core import bridge, gateway as gateway_proc, identity  # noqa: E402
import server  # noqa: E402
import windowing  # noqa: E402
from diagnostics import (  # noqa: E402
    _console_visible, acquire_single_instance, log, notify_already_running,
    notify_error, setup_logging,
)
from shell import Shell, sync_autostart  # noqa: E402

ROOT = bridge.PROJECT_DIR
PROJECT_DIR = bridge.PROJECT_DIR
DEFAULT_CONSOLE_PORT = server.DEFAULT_PORT


def make_window_api(shell):
    return windowing.make_window_api(shell, hwnd_lookup=lambda retries=5: windowing._own_hwnd(retries),
                                     read_settings=server.read_settings,
                                     write_settings=server.write_settings)


def _hide_native_titlebar(window):
    windowing.hide_native_titlebar(window, dark=server.effective_theme() == 'dark')


def _busy_page(port: int, message: str) -> str:
    """端口被占时窗口里显示的东西。不能白屏，也不能只让用户去猜。"""
    # 脚本路径从 ROOT 拼。以前这里写死 D:\MY_DESIGN\Agent-router-Pro-Max\...，
    # 换个安装目录，那行提示就变成"照着敲，PowerShell 说找不到路径"——比不写还坏。
    import html
    stop_selector = html.escape(str(PROJECT_DIR / 'script' / 'Stop-Selector.ps1'))
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<title>Prism 启动失败</title>'
            '<style>body{background:#FFFFFF;color:#0D0D0D;font:15px/1.7 '
            '"Segoe UI",system-ui,sans-serif;padding:48px 56px;max-width:820px}'
            'h1{font-size:20px;font-weight:600;color:#C4342B}code{font-family:ui-monospace,Consolas,monospace;'
            'color:#0D0D0D}pre{background:#F9F9F9;border:1px solid #ECECEC;border-radius:10px;padding:14px 16px;'
            'white-space:pre-wrap}</style>'
            '<h1>控制台端口 %(port)d 被占用</h1><p>%(message)s</p>'
            '<p>确认是谁占着（PowerShell）：</p>'
            '<pre>Get-NetTCPConnection -LocalPort %(port)d -State Listen | Select-Object OwningProcess\n'
            'Get-CimInstance Win32_Process -Filter "ProcessId=&lt;上面那个PID&gt;" | Select-Object CommandLine</pre>'
            '<p>如果命令发出的是 <code>...\\script\\route_selector.py</code>，跑：</p>'
            '<pre>&amp; \'%(stop_selector)s\'</pre>'
            '<p>然后重新打开 Prism。</p></html>'
            % {'port': int(port), 'message': html.escape(str(message)),
           'stop_selector': stop_selector})


def _bootstrap_gateway(shell: Shell) -> None:
    if gateway_proc.port_open(identity.GATEWAY_HOST, identity.GATEWAY_PORT, 0.15):
        who = identity.gateway_identity()
        log('网关身份：' + who['note'])
        if who['state'] == identity.IDENTITY_FOREIGN:
            shell.notify('foreign:' + who['note'])
    else:
        result = gateway_proc.ensure_gateway(wait=True)
        log('网关异步启动完成：' + result)
        if result.startswith(('failed', 'foreign')):
            shell.notify(result)
    shell.refresh_tray()


def _warmup_http(url: str) -> None:
    try:
        import urllib.request
        request = urllib.request.Request(url, headers={'User-Agent': 'Prism-Warmup'})
        with urllib.request.urlopen(request, timeout=0.8):
            pass
    except Exception:
        pass


def _initialize_tray(shell: Shell, settings: dict, warning: str | None) -> None:
    try:
        sync_autostart(settings)
    except Exception:
        pass
    shell.start_tray()
    if warning:
        shell.notify(warning)
    threading.Thread(target=_status_loop, args=(shell,), name='prism-status', daemon=True).start()


def run(console_port: int = DEFAULT_CONSOLE_PORT, open_window: bool = True) -> int:
    """完整启动流程。返回进程退出码。"""
    if not acquire_single_instance(console_port):
        log('已经有一个 Prism 在运行（控制台端口 %d）' % console_port)
        if open_window:
            existing = windowing.find_window_by_title()
            if existing and windowing.bring_existing_to_front(existing):
                log('已成功激活并置顶已有 Prism 窗口（HWND=%d）' % existing)
                return 0
            # 找不到可见窗口时（例如收在托盘）再通知用户
            if not _console_visible():
                notify_already_running(console_port)
        return 0

    shell = Shell(console_port)

    # 首启引导（HI-03）：缺 .local-secrets.json / config.yaml 就生成最小可用集。
    # 必须在体检之前——生成完文件才齐，体检的"缺失不算半写"也就只兜真正的首启前。
    try:
        created = bridge.ensure_first_run_files()
        if created:
            log('首启已生成：' + '、'.join(created))
    except Exception:                       # noqa: BLE001 - 引导失败不该拦住能起的启动
        log('首启引导失败：\n' + traceback.format_exc())

    # 启动体检（ME-03）：一次「保存路由」改四份文件，进程在途中被强杀会留下互相
    # 矛盾的半写状态。矛盾状态下拉起网关只会把问题放大（网关读坏 config、页面读
    # 坏 plan），所以这里 fail-loud：弹窗说明问题并指向最近一次 route-switch 备份。
    problems = bridge.verify_startup_files()
    if problems:
        backup = bridge.latest_switch_backup()
        message = ('启动体检发现配置文件状态矛盾（上次切换可能被中断）：\n\n'
                   + '\n'.join('· ' + p for p in problems))
        if backup is not None:
            message += ('\n\n最近的自动备份（可人工比对恢复）：\n' + str(backup))
        else:
            message += '\n\n没有找到 route-switch 自动备份，需要人工核对这四份文件。'
        notify_error(message)
        return 2

    if open_window:
        threading.Thread(target=_bootstrap_gateway, args=(shell,),
                         name='prism-gw-bootstrap', daemon=True).start()
        startup_warning = None
    else:
        state = gateway_proc.ensure_gateway(wait=True)
        log('网关状态：' + state)
        startup_warning = state if (state.startswith('failed')
                                    or state.startswith('foreign')) else None

    # 设置里的自启意愿落到注册表；server 保存设置时会回调同一个函数
    settings = server.read_settings()
    server.settings_observer.add(sync_autostart)

    try:
        # 控制台令牌（ME-12）：每次启动随机生成，只经窗口 URL 一次性交给前端，
        # 之后前端每个 /api/* 请求带头。多用户机器上第二个本地账户伪造到回环口的
        # 请求没有这个令牌，改不了网关配置。独立调试形态（python server.py）不启用。
        console_token = secrets.token_urlsafe(24)
        shell.console_token = console_token    # open_in_browser 拼 ?t= 用
        httpd = server.start_background(console_port, token=console_token)
    except server.PortBusy as exc:
        log(str(exc))
        if not open_window:
            return 2
        # 这一支**故意不调 shell.set_window()**，不是漏了。
        windowing.webview.create_window(windowing.TITLE, html=_busy_page(console_port, str(exc)),
                              width=760, height=560)
        windowing.webview.start()
        return 2
    shell.httpd = httpd
    log('控制台已监听 ' + shell.url)

    # 异步预热本地控制台 HTTP，消除 WebView2 首次握手延迟
    threading.Thread(target=_warmup_http, args=(shell.url,), name='prism-warmup', daemon=True).start()

    if open_window:
        threading.Thread(target=_initialize_tray, args=(shell, settings, startup_warning),
                         name='prism-tray-init', daemon=True).start()
    else:
        _initialize_tray(shell, settings, startup_warning)

    if not open_window:
        # 没有窗口时 windowing.webview.start() 不会跑，主线程必须自己等着，否则进程直接退出、
        # 服务跟着线程一起消失。Ctrl-C 或托盘退出都能离开这里。
        log('--no-window：控制台在跑 %s，没有窗口。按 Ctrl-C 退出。' % shell.url)
        try:
            while not shell.quitting.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            try:
                server.stop_server(httpd)
            except Exception:
                log('停止控制台服务失败：\n' + traceback.format_exc())
        return 0

    # 拖拽全部走 app.js 绑定的原生 Win32 WM_NCLBUTTONDOWN (HTCAPTION)，
    # 禁用 pywebview 内置的 JS 轮询坐标伪拖拽，手感顺滑且完美支持 Windows Snap。
    windowing.webview.settings['DRAG_REGION_SELECTOR'] = '.none-pywebview-drag'

    # 恢复记忆的窗口尺寸
    app_cfg = settings.get('app') or {}
    min_width, min_height = windowing.MIN_WINDOW_SIZE
    win_w = max(min_width, min(3840, int(app_cfg.get('window_width') or 1180)))
    win_h = max(min_height, min(2160, int(app_cfg.get('window_height') or 800)))

    # 令牌拼在窗口 URL 上（?t=）：app.js 首次读到就存 sessionStorage，请求全程带头。
    # 回环口上的 URL 不出本机；令牌只走这一条路，不打日志、不进环境变量。
    # 深色模式底色严格对齐 CSS --bg (#212121)，消除首帧色差抖动
    bg_color = '#212121' if server.effective_theme() == 'dark' else '#FFFFFF'
    window = windowing.webview.create_window(windowing.TITLE, shell.url + '?t=' + console_token,
                                   width=win_w, height=win_h,
                                   min_size=windowing.MIN_WINDOW_SIZE, text_select=True,
                                   background_color=bg_color,
                                   js_api=make_window_api(shell),
                                   frameless=True)
    shell.set_window(window)
    cache_dir = os.path.join(os.environ.get('LOCALAPPDATA') or str(ROOT), 'Prism', 'webview2_cache')
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except Exception:
        cache_dir = None
    try:
        # func 在 GUI 循环起来之后调用，那时窗口才真的存在
        windowing._apply_edgechromium_patch()
        if cache_dir:
            windowing.webview.start(_hide_native_titlebar, (window,), storage_path=cache_dir)
        else:
            windowing.webview.start(_hide_native_titlebar, (window,))
    finally:
        icon = shell.icon
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
        if httpd is not None:
            try:
                server.stop_server(httpd)
            except Exception:
                log('停止控制台服务失败：\n' + traceback.format_exc())
    return 0


def _status_loop(shell: Shell) -> None:
    while not shell.quitting.is_set():
        try:
            shell.refresh_tray()
        except Exception:
            log('状态刷新失败：\n' + traceback.format_exc())
        # Event.wait 替代 sleep：退出置位后立即返回，不用等满 5 秒
        shell.quitting.wait(5)


def main(argv=None) -> int:
    setup_logging()          # 最早的一步：这之后的异常才有地方落
    if argv is None and len(sys.argv) == 1:
        # 默认双击无参极速直通路径：跳过 argparse 模块导入与构建开销
        port = int(os.environ.get('PRISM_CONSOLE_PORT') or DEFAULT_CONSOLE_PORT)
        try:
            return run(port, open_window=True)
        except Exception:
            detail = traceback.format_exc()
            log(detail)
            notify_error('Prism 启动时崩溃：\n\n' + detail.strip().splitlines()[-1])
            return 1

    import argparse
    parser = argparse.ArgumentParser(
        prog='main.py',
        description='Prism 桌面控制台（默认 8318；--port 只给排障用，改动前先读 README 的 FAQ）')
    parser.add_argument('--port', type=int,
                        default=int(os.environ.get('PRISM_CONSOLE_PORT') or DEFAULT_CONSOLE_PORT),
                        help='控制台端口，默认 8318。设置页不暴露这一项，端口一变 Host 校验就全 409')
    parser.add_argument('--no-window', action='store_true',
                        help='只起控制台服务与托盘，不开窗口（远程/排障用）')
    args = parser.parse_args(argv)
    try:
        return run(args.port, open_window=not args.no_window)
    except Exception:
        detail = traceback.format_exc()
        log(detail)
        notify_error('Prism 启动时崩溃：\n\n' + detail.strip().splitlines()[-1])
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
