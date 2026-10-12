"""桌面窗口生命周期、托盘和开机自启。"""
from __future__ import annotations

import os
import sys
import threading
import traceback
from pathlib import Path

from core import bridge, gateway as gateway_proc, identity
import server
from diagnostics import log
from windowing import TITLE

APP_NAME = 'Prism'
ICON_DIR = bridge.BUNDLE_DIR / 'design' / 'icons'
STATE_COLORS = {
    'ok': (34, 197, 94),
    'down': (239, 68, 68),
    'unknown': (234, 179, 8),
    'foreign': (249, 115, 22),
}

# --------------------------------------------------------------------------- 托盘图标

_icon_cache: dict = {}


def tray_image(state: str):
    """基准图来自 design/icons/prism-64.png（已交付），状态只改右下角的圆点。"""
    from PIL import Image, ImageDraw

    key = state if state in STATE_COLORS else 'unknown'
    if key in _icon_cache:
        return _icon_cache[key]
    base = ICON_DIR / 'prism-64.png'
    try:
        image = Image.open(base).convert('RGBA')
    except (OSError, ValueError):
        # 图标文件缺失不该让程序起不来，画个纯色方块顶着
        image = Image.new('RGBA', (64, 64), (8, 8, 8, 255))
    size = image.size[0]
    dot = Image.new('RGBA', image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(dot)
    r = max(6, size // 5)
    pad = max(2, size // 16)
    draw.ellipse([size - 2 * r - pad, size - 2 * r - pad, size - pad, size - pad],
                 fill=STATE_COLORS[key] + (255,))
    image = Image.alpha_composite(image, dot)
    _icon_cache[key] = image
    return image


# --------------------------------------------------------------------------- 开机自启

def _launch_command() -> str:
    """写进注册表的那条命令。

    打包后 sys.executable 就是 Prism.exe；开发态是 python.exe，换成同目录的
    pythonw.exe——否则每次开机都会弹一个控制台黑框。
    """
    if getattr(sys, 'frozen', False):
        return '"%s"' % sys.executable
    exe = Path(sys.executable)
    pythonw = exe.with_name('pythonw.exe')
    if pythonw.is_file():
        exe = pythonw
    return '"%s" "%s"' % (exe, Path(__file__).with_name('main.py').resolve())


def set_autostart(enabled: bool) -> bool:
    """写 HKCU\\...\\Run。返回是否写成功。失败不抛错——自启失败不该拦住启动。"""
    try:
        import winreg
    except ImportError:
        log('当前平台没有 winreg，跳过自启设置')
        return False
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, server.RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, server.AUTOSTART_NAME, 0, winreg.REG_SZ, _launch_command())
            else:
                try:
                    winreg.DeleteValue(key, server.AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
        log('开机自启已%s' % ('开启' if enabled else '关闭'))
        return True
    except OSError as exc:
        log('写注册表失败（自启未生效）：%s' % exc)
        return False


def autostart_enabled() -> bool | None:
    """只读：注册表里现在有没有这条。读不到返回 None。"""
    return server._autostart_from_registry()


def sync_autostart(settings: dict) -> None:
    """把设置文件里的意愿落到注册表。设置页保存后由 server 回调进来。"""
    want = bool((settings.get('app') or {}).get('autostart'))
    if autostart_enabled() != want:
        set_autostart(want)


# --------------------------------------------------------------------------- 托盘菜单

class Shell:
    """窗口 / 托盘 / 控制台服务三者的共享状态。"""

    def __init__(self, console_port: int):
        self.console_port = console_port
        self.url = 'http://127.0.0.1:%d/' % console_port
        self.console_token = None     # run() 里生成后挂上来；open_in_browser 拼 ?t= 用
        self.window = None
        self.icon = None
        self.httpd = None
        self.quitting = threading.Event()
        self._tray_thread = None
        self._tray_lock = threading.Lock()  # refresh_tray 会被状态循环与托盘回调两个线程调
        self._last_state = None

    # -- 窗口 --------------------------------------------------------------

    def set_window(self, window) -> None:
        self.window = window
        window.events.closing += self._on_closing

    def request_close(self) -> bool:
        """关窗口的**唯一**入口：原生 X、自绘关闭按钮、托盘退出都走这里。

        返回 False = 取消关闭（默认收到托盘，README 的承诺）。抽出来是因为
        自绘了关闭按钮之后有两个调用方，逻辑复制两份必然漂移。
        """
        try:
            close_to_tray = bool(server.read_settings()['app'].get('close_to_tray', True))
        except Exception:
            close_to_tray = True
        if close_to_tray and not self.quitting.is_set():
            self.hide_window()
            return False
        return True

    def _on_closing(self):
        return self.request_close()

    def destroy_window(self) -> None:
        """真关窗口。destroy() 不触发 closing 事件（winforms 后端只 set closed），
        所以不会和 _on_closing 形成递归。"""
        window = self.window
        if window is None:
            return
        try:
            window.destroy()
        except Exception:
            log('关闭窗口失败：\n' + traceback.format_exc())

    def show_window(self) -> None:
        window = self.window
        if window is None:
            return
        try:
            window.show()
            window.restore()
        except Exception:
            log('显示窗口失败：\n' + traceback.format_exc())

    def hide_window(self) -> None:
        window = self.window
        if window is None:
            return
        try:
            window.hide()
        except Exception:
            log('隐藏窗口失败：\n' + traceback.format_exc())

    def open_in_browser(self, *_a) -> None:
        """托盘菜单里的"在浏览器中打开"：WebView2 万一挂了，还有条路看数据。

        桌面版服务启用了控制台令牌（ME-12），不带 ?t= 的 /api/* 全是 401 ——
        所以这里必须把令牌拼上。令牌只进这个 URL 参数、不进日志。
        """
        url = self.url + ('?t=' + self.console_token if self.console_token else '')
        try:
            os.startfile(url)           # noqa: S606 - 打开自己的本机地址
        except OSError:
            log('打不开浏览器')

    def quit(self, *_a) -> None:
        """托盘退出：先让窗口别再拦关闭，再停托盘，最后关窗口让 webview.start() 返回。"""
        self.quitting.set()
        icon = self.icon
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                log('停止托盘失败：\n' + traceback.format_exc())
        window = self.window
        if window is not None:
            try:
                window.destroy()
            except Exception:
                log('关闭窗口失败：\n' + traceback.format_exc())

    # -- 状态 --------------------------------------------------------------

    def gateway_state(self) -> str:
        """托盘状态。端口在听**并且**坐的是本目录那套，才叫 ok。

        以前这里是“端口在听即在线”；现在以网关身份核验结果为准。
        8317 上坐着别的部署时，托盘亮着绿灯说"在线"，tip 里显示的却是那边网关的路由，
        页面上的来源也全是那边的。绿灯必须真的代表本目录这套。
        identity.gateway_identity() 的四个状态名和 STATE_COLORS 的键是同一套，直接透传。
        """
        return identity.gateway_identity()['state']

    def notify(self, message: str) -> None:
        """托盘气泡。窗口没起或图标还没就绪时安静跳过。"""
        icon = self.icon
        if icon is None:
            return
        try:
            icon.notify(message, TITLE)
        except Exception:
            pass

    def refresh_tray(self, *_a) -> None:
        state = self.gateway_state()
        icon = self.icon
        if icon is None:
            return
        try:
            with self._tray_lock:
                if state != self._last_state:
                    self._last_state = state
                    icon.icon = tray_image(state)
                if icon.title != APP_NAME:
                    icon.title = APP_NAME
        except Exception:
            log('更新托盘图标失败：\n' + traceback.format_exc())

    def restart_gateway(self, *_a) -> None:
        """README 里的托盘"重启网关"。**先探后拉**，不杀进程——见文件头的说明。"""
        if gateway_proc.port_open(identity.GATEWAY_HOST, identity.GATEWAY_PORT):
            who = identity.gateway_identity()
            if who['state'] == identity.IDENTITY_FOREIGN:
                self.notify('%s。没有动它——要换成本目录的网关，得自己先把那个进程停掉。'
                            % who['note'])
            else:
                self.notify('网关正在运行（%s:%d），没有重启它。'
                            % (identity.GATEWAY_HOST, identity.GATEWAY_PORT))
            return
        self.notify('网关没在监听，正在拉起…')
        result = gateway_proc.ensure_gateway()
        log('托盘重启网关：' + result)
        self.notify('网关已拉起。' if result in ('running', 'started') else result)
        self.refresh_tray()

    # -- 托盘 --------------------------------------------------------------

    def build_menu(self):
        import pystray
        return pystray.Menu(
            pystray.MenuItem('打开 Prism', self.show_window, default=True),
            pystray.MenuItem('在浏览器中打开', self.open_in_browser),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('重启网关', self.restart_gateway),
            pystray.MenuItem('重新检查状态', self.refresh_tray),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('退出', self.quit),
        )

    def start_tray(self) -> None:
        """起托盘。

        pystray 的 Icon.run() 在 Windows 后端里是 `threading.Thread(target=...)
        .start()`，**没有 daemon=True**（pystray\\_win32.py:131）。直接把 run() 放在
        主线程会阻塞窗口消息循环，放在普通线程会让进程退不掉。所以自己包一层
        daemon 线程，并且退出路径一定走 icon.stop()（见 quit()）。
        """
        import pystray
        state = self.gateway_state()        # 只核一次：探两遍没意义
        icon = pystray.Icon(APP_NAME, tray_image(state), APP_NAME, self.build_menu())
        self.icon = icon
        thread = threading.Thread(target=self._run_tray, args=(icon,),
                                  name='prism-tray', daemon=True)
        self._tray_thread = thread
        thread.start()

    def _run_tray(self, icon) -> None:
        try:
            icon.run()
        except Exception:
            log('托盘线程异常退出：\n' + traceback.format_exc())


