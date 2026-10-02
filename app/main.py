r"""Prism 桌面壳：单实例 + 拉起后端 + WebView2 窗口 + 托盘 + 开机自启。

契约在 app/INTERFACES.md 的 `main.py` 一节。这里只做"进程与窗口"，一条业务逻辑
都不碰——控制台服务在 server.py，业务在 core/*，源头在 script/route_selector.py。

**本文件绝不终止任何进程。** 网关不是 Prism 的私产：用户可能正是用
script/Start-Proxy.ps1 起的它，也可能挂着别的客户端在用。所以：

  * 网关端口已在监听 → 一个字节都不动它；
  * 网关端口没监听 → 由 Prism 拉起来（隐藏窗口），但退出时**不关它**；
  * 托盘上的"重启网关"= 再探一次，没在跑才拉，不会先杀后起。

（网关端口默认 127.0.0.1:8317，被 PRISM_GATEWAY_BASE 指到别处时只探不拉——端口、
网关程序与配置路径三样都取自 core\identity.py 的模块常量。）

计划里原本写的是"崩溃检测 → 一键重启"，照字面实现要 taskkill 再 Popen。实测
`logs\main.log` 里用户的网关一直在跑（PID 27600），误杀一次就是一次生产中断，
所以这里改成上面的保守语义。

三个已知的坑按 INTERFACES.md 处理：
  * 端口互斥靠 server.py 的 SO_EXCLUSIVEADDRUSE，这里只负责把它抛的 PortBusy
    翻译成用户能看懂的话；
  * pystray 的 Icon.run() 内部线程**不是 daemon**（pystray\_win32.py:131），
    必须自己包一层 daemon Thread，并且退出路径一定有 icon.stop()；
  * exe 与 config.yaml 都不进包，路径一律用 ROOT 绝对定位。
"""

from __future__ import annotations

import ctypes
import atexit
import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
from ctypes import wintypes
from pathlib import Path

# WebView2 启动参数极致加速：开启 GPU 光栅化与零拷贝，禁用冗余非核心特性与外围检查，绕过回环代理检测
WEBVIEW2_ACCEL_ARGS = (
    '--disable-features=Translate,OptimizationHints,MediaRouter,DialMediaRouteProvider,'
    'CalculateNativeWinOcclusion,InterestFeedContentSuggestions,ElasticOverscroll '
    '--enable-gpu-rasterization --enable-zero-copy '
    '--disable-background-timer-throttling --disable-renderer-backgrounding '
    '--disable-component-update --disable-extensions --disable-default-apps '
    '--disable-sync --no-first-run --disable-breakpad --disable-domain-reliability '
    '--renderer-process-limit=2 --enable-fast-unload --disable-hang-monitor '
    '--proxy-bypass-list=<-loopback>'
)
os.environ['WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS'] = WEBVIEW2_ACCEL_ARGS

import webview          # pywebview 6.2.1（app\.venv 已装）

def _apply_edgechromium_patch() -> None:
    # 延迟按需补丁 pywebview edgechromium，防止其赋值覆盖加速参数；剥离顶层 CLR 加载耗时
    try:
        from webview.platforms import edgechromium as _ec
        if getattr(_ec, '_prism_patched', False):
            return
        _orig_ec_init = _ec.EdgeChrome.__init__
        def _patched_ec_init(self, form, window, cache_dir):
            _orig_ec_init(self, form, window, cache_dir)
            if hasattr(self, 'webview') and hasattr(self.webview, 'CreationProperties') and self.webview.CreationProperties:
                curr = self.webview.CreationProperties.AdditionalBrowserArguments or ''
                self.webview.CreationProperties.AdditionalBrowserArguments = (curr + ' ' + WEBVIEW2_ACCEL_ARGS).strip()
        _ec.EdgeChrome.__init__ = _patched_ec_init
        _ec._prism_patched = True
    except Exception:
        pass

# pystray、PIL、sqlite3/sampling 属于 heavy 模块，改为后台/按需导入，首屏主线程零开销

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core import bridge                     # noqa: E402  （顺带把 sys.path 理顺）
from core import identity                   # noqa: E402  （网关身份核对，实现见 core\identity.py）
import server                               # noqa: E402

ROOT = bridge.ROOT                          # D:\MY_DESIGN\Agent-router-Pro-Max
ICON_DIR = bridge.BUNDLE_DIR / 'design' / 'icons'

# ── Win32：把原生标题栏摘掉（Electron 的 titleBarStyle:'hidden' 等价物）──────────
#
# 为什么不是 pywebview 的 frameless=True：那个等价于 Electron 的 frame:false，
# 会把 WS_THICKFRAME 一起清掉 —— 边缘拖拽缩放、Snap 贴边、双击标题栏最大化全没了。
# 实测本机 Electron 应用（DSH Desktop，resources\app.asar 的 lib\main.js:7788 与
# :7797）用的是 titleBarStyle:'hidden' + titleBarOverlay：**保留窗口框架**，
# 只藏标题栏。所以这里也只摘 WS_CAPTION 这一位，其余样式原样留着。
GWL_STYLE = -16
WS_CAPTION = 0x00C00000
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
# DWMWA_WINDOW_CORNER_PREFERENCE / DWMWCP_ROUND：摘掉标题栏之后 Win11 不再自动
# 给圆角（系统只在有原生框架时才画），实测 CORNER_PREFERENCE 保持 0=默认、窗口是直角。
# 不补这一下，无边框窗口看起来比原生窗口更"旧"。
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2


class _RECT(ctypes.Structure):
    _fields_ = [
        ('left', ctypes.c_long),
        ('top', ctypes.c_long),
        ('right', ctypes.c_long),
        ('bottom', ctypes.c_long),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [
        ('x', ctypes.c_long),
        ('y', ctypes.c_long),
    ]


_CACHED_HWND = None


def _own_hwnd(retries: int = 5):
    """找本进程的顶层窗口句柄。

    pywebview 不把 HWND 暴露给 Python（只给 pywebview.Window），所以只能这样找。
    两条路：先按标题找（快）；找不到就枚举本进程的可见顶层窗口——网页里
    document.title 是会被路由改的（app.js 的 route()），万一哪天 pywebview 把
    它同步到窗口标题上，按标题找就会落空，所以留这条回退。

    窗口是异步创建的，start() 的回调里未必已经存在，所以重试。
    """
    global _CACHED_HWND
    user32 = ctypes.windll.user32
    if _CACHED_HWND:
        try:
            if user32.IsWindow(wintypes.HWND(_CACHED_HWND)):
                return _CACHED_HWND
        except Exception:
            _CACHED_HWND = None

    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL

    pid = os.getpid()
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    for _ in range(max(1, retries)):
        hwnd = user32.FindWindowW(None, TITLE)
        if hwnd:
            _CACHED_HWND = hwnd
            return hwnd
        found = []

        def _cb(h, _lparam):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(h, ctypes.byref(owner))
            if owner.value == pid and user32.IsWindowVisible(h):
                found.append(h)
            return True

        try:
            user32.EnumWindows(WNDENUMPROC(_cb), 0)
        except Exception:
            found = []
        if found:
            _CACHED_HWND = found[0]
            return found[0]
        time.sleep(0.01)
    return None


_SUBCLASS_WNDPROC = None
_OLD_WNDPROC = None


def _window_subclass_proc(hwnd, msg, wparam, lparam):
    global _OLD_WNDPROC
    # ponytail: 拦截 WM_NCCALCSIZE (0x0083)。
    # 当窗口加回 WS_THICKFRAME 时，系统默认计算会在顶部留出非客户区边框，
    # 在系统深色模式下 DWM 将其渲染为 4px 深色横条（"黑条"）。
    # 当 wparam 为 1 (TRUE) 时返回 0，强制令客户区覆盖整个窗口物理区域。
    if msg == 0x0083 and wparam == 1:
        return 0
    return ctypes.windll.user32.CallWindowProcW(_OLD_WNDPROC, hwnd, msg, wparam, lparam)


def hide_native_titlebar(window=None) -> None:
    """frameless 窗口上恢复系统边缘缩放与 DWM 圆角，彻底消除顶部黑条。

    create_window 传 frameless=True（FormBorderStyle=None），初始无非客户区。
    在此通过 Win32 SetWindowLongPtrW 子类化窗口过程，拦截 WM_NCCALCSIZE 返回 0。
    然后再加回 WS_THICKFRAME/WS_SYSMENU/WS_MINIMIZEBOX/WS_MAXIMIZEBOX。
    这样窗口既拥有完整的系统边缘拖拽缩放与 Win+方向键贴边能力，
    客户区又 100% 满铺窗口物理矩形，绝不产生顶部深色/黑色非客户区横条。
    """
    global _SUBCLASS_WNDPROC, _OLD_WNDPROC, _CACHED_HWND
    if window is not None and hasattr(window, 'native') and hasattr(window.native, 'Handle'):
        try:
            val = window.native.Handle.ToInt64()
            if isinstance(val, int) and val > 0:
                _CACHED_HWND = val
        except Exception:
            pass
    if not _CACHED_HWND and window is not None and hasattr(window, 'events') and hasattr(window.events, 'shown'):
        try:
            window.events.shown.wait(timeout=0.005)
        except Exception:
            pass
        if hasattr(window, 'native') and hasattr(window.native, 'Handle'):
            try:
                val = window.native.Handle.ToInt64()
                if isinstance(val, int) and val > 0:
                    _CACHED_HWND = val
            except Exception:
                pass
    hwnd = _CACHED_HWND or _own_hwnd()
    if not hwnd:
        log('恢复边缘缩放：没找到窗口句柄，跳过')
        return
    _CACHED_HWND = hwnd
    try:
        user32 = ctypes.windll.user32
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = wintypes.LONG
        user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
        user32.SetWindowLongW.restype = wintypes.LONG
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        user32.SetWindowPos.restype = wintypes.BOOL

        # 1. 注册 Win32 子类化过程（持久保存在全局变量防止被 GC 回收引发崩溃）
        GWLP_WNDPROC = -4
        WNDPROC_TYPE = ctypes.WINFUNCTYPE(ctypes.c_int64, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM)
        _SUBCLASS_WNDPROC = WNDPROC_TYPE(_window_subclass_proc)
        
        SetWindowLongPtr = user32.SetWindowLongPtrW
        SetWindowLongPtr.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        SetWindowLongPtr.restype = ctypes.c_void_p
        
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallWindowProcW.restype = ctypes.c_int64
        
        _OLD_WNDPROC = SetWindowLongPtr(wintypes.HWND(hwnd), GWLP_WNDPROC, ctypes.cast(_SUBCLASS_WNDPROC, ctypes.c_void_p))

        # 2. 加回系统级窗口功能（frameless=True 时 WinForms 清理了这些样式）
        WS_THICKFRAME  = 0x00040000
        WS_SYSMENU     = 0x00080000
        WS_MINIMIZEBOX = 0x00020000
        WS_MAXIMIZEBOX = 0x00010000
        style = user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_STYLE)
        style |= WS_THICKFRAME | WS_SYSMENU | WS_MINIMIZEBOX | WS_MAXIMIZEBOX
        user32.SetWindowLongW(wintypes.HWND(hwnd), GWL_STYLE, style)

        # 3. SWP_FRAMECHANGED 触发系统重新计算窗口框架（WM_NCCALCSIZE 将被拦截并返回 0）
        user32.SetWindowPos(wintypes.HWND(hwnd), None, 0, 0, 0, 0,
                            SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER)

        # 显式显示并置顶窗口，杜绝无终端或独立打包拉起时窗口未置于可见层
        SW_SHOW = 5
        user32.ShowWindow(wintypes.HWND(hwnd), SW_SHOW)
        user32.SetForegroundWindow(wintypes.HWND(hwnd))

        # 4. DWM 圆角与主题
        try:
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                wintypes.HWND(hwnd), ctypes.c_uint(DWMWA_WINDOW_CORNER_PREFERENCE),
                ctypes.byref(ctypes.c_int(DWMWCP_ROUND)), ctypes.c_uint(4))
        except Exception:
            log('设圆角失败（不致命）：\n' + traceback.format_exc())

        try:
            is_dark = (server.effective_theme() == 'dark')
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                wintypes.HWND(hwnd), ctypes.c_uint(DWMWA_USE_IMMERSIVE_DARK_MODE),
                ctypes.byref(ctypes.c_int(1 if is_dark else 0)), ctypes.c_uint(4))
        except Exception:
            pass

        log('frameless 窗口已成功子类化并恢复 WS_THICKFRAME（边缘缩放/Snap，零非客户区）')
    except Exception:
        log('恢复边缘缩放失败：\n' + traceback.format_exc())


def bring_existing_to_front(hwnd) -> bool:
    """唤醒并前置已有窗口。"""
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    try:
        SW_RESTORE = 9
        SW_SHOW = 5
        if user32.IsIconic(wintypes.HWND(hwnd)):
            user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)
        else:
            user32.ShowWindow(wintypes.HWND(hwnd), SW_SHOW)
        user32.SetForegroundWindow(wintypes.HWND(hwnd))
        return True
    except Exception:
        return False


def make_window_api(shell):
    """构造暴露给前端的窗口控制对象（create_window 的 js_api）。

    ⚠ **不要把 shell 存成实例属性**（别写 self.shell = shell）。
    实测：那样 pywebview 注册 js_api 时会沿着
        Shell -> window -> native(WinForms 窗体) -> browser.webview -> ...
    一路递归遍历，日志被
        "Error while processing shell.window.native.AccessibilityObject.Bounds.Empty.Empty..."
        "shell.window.native.browser.webview.Anchor.Bottom.Bottom.Bottom..."
        "maximum recursion depth exceeded"
    刷屏，紧接着 WebView2 整片报
        "CoreWebView2Controller members can only be accessed from the UI thread"（E_NOINTERFACE）。
    窗口表面上还开着，界面已经不可用。

    改成闭包捕获：方法体里引用的 shell 是**外层函数的局部变量**，不是实例属性，
    遍历实例时看不到它。这个写法看着绕，但它是这里唯一安全的形式。
    """

    def _get_hwnd():
        if shell.window is not None:
            native = getattr(shell.window, 'native', None)
            if native is not None and hasattr(native, 'Handle'):
                handle = getattr(native, 'Handle', None)
                if hasattr(handle, 'ToInt64'):
                    try:
                        val = handle.ToInt64()
                        if isinstance(val, int) and val > 0:
                            return val
                    except Exception:
                        pass
                elif isinstance(handle, int) and handle > 0:
                    return handle
        return _own_hwnd(retries=1)

    class _WindowApi:
        def __init__(self):
            self._drag_start_cursor = (0, 0)
            self._drag_start_pos = (0, 0)
            self._is_dragging = False

            self._resize_start_cursor = (0, 0)
            self._resize_start_rect = (0, 0, 0, 0)
            self._resize_direction = ''
            self._is_resizing = False

        def minimize(self) -> None:
            """最小化。"""
            try:
                shell.window.minimize()
            except Exception:
                log('最小化失败：\n' + traceback.format_exc())

        def is_maximized(self) -> bool:
            """用 IsZoomed 问系统，不自己记状态。

            双击顶栏、拖到屏幕边缘贴边、Win+↑ 都能改变最大化状态，自己记必然漂移。
            """
            hwnd = _get_hwnd()
            try:
                return bool(hwnd and ctypes.windll.user32.IsZoomed(wintypes.HWND(hwnd)))
            except Exception:
                return False

        def toggle_maximize(self) -> None:
            """最大化 / 还原。"""
            try:
                if self.is_maximized():
                    shell.window.restore()
                else:
                    shell.window.maximize()
            except Exception:
                log('最大化/还原失败：\n' + traceback.format_exc())

        def drag(self) -> None:
            """自绘标题栏原生系统拖动兼容入口。"""
            self.start_resize(2)

        def start_resize(self, edge: int = 2) -> None:
            """自绘无边框窗口边缘缩放与拖拽原生尝试。

            edge 对应 Win32 Hit-Test 代码：
              2:  HTCAPTION (标题栏拖拽)
              10: HTLEFT (左边缘)
              11: HTRIGHT (右边缘)
              12: HTTOP (上边缘)
              15: HTBOTTOM (下边缘)
              13: HTTOPLEFT (左上角)
              14: HTTOPRIGHT (右上角)
              16: HTBOTTOMLEFT (左下角)
              17: HTBOTTOMRIGHT (右下角)
            """
            hwnd = _get_hwnd()
            if hwnd:
                try:
                    user32 = ctypes.windll.user32
                    user32.ReleaseCapture()
                    pt = _POINT()
                    user32.GetCursorPos(ctypes.byref(pt))
                    lparam = (pt.y << 16) | (pt.x & 0xFFFF)
                    user32.SendMessageW(wintypes.HWND(hwnd), 0x00A1, int(edge), lparam)
                except Exception:
                    pass

        def drag_start(self) -> bool:
            """记录拖拽初始光标物理坐标与窗口物理位置。"""
            hwnd = _get_hwnd()
            if not hwnd:
                return False
            try:
                user32 = ctypes.windll.user32
                r = _RECT()
                user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r))
                pt = _POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                self._drag_start_pos = (r.left, r.top)
                self._drag_start_cursor = (pt.x, pt.y)
                self._is_dragging = True
                return True
            except Exception:
                return False

        def drag_move(self) -> None:
            """基于当前系统物理光标的绝对位移更新窗口位置（零累积误差、零漂移）。"""
            if not self._is_dragging:
                return
            hwnd = _get_hwnd()
            if not hwnd:
                return
            try:
                user32 = ctypes.windll.user32
                pt = _POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                dx = pt.x - self._drag_start_cursor[0]
                dy = pt.y - self._drag_start_cursor[1]
                new_x = self._drag_start_pos[0] + dx
                new_y = self._drag_start_pos[1] + dy
                user32.SetWindowPos(wintypes.HWND(hwnd), None,
                                    new_x, new_y, 0, 0,
                                    SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            except Exception:
                pass

        def drag_end(self) -> None:
            """释放拖拽状态。"""
            self._is_dragging = False

        def resize_start(self, direction: str) -> bool:
            """记录缩放初始光标物理坐标与窗口物理矩形。"""
            hwnd = _get_hwnd()
            if not hwnd:
                return False
            try:
                user32 = ctypes.windll.user32
                r = _RECT()
                user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r))
                pt = _POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                self._resize_start_rect = (r.left, r.top, r.right - r.left, r.bottom - r.top)
                self._resize_start_cursor = (pt.x, pt.y)
                self._resize_direction = str(direction).lower()
                self._is_resizing = True
                return True
            except Exception:
                return False

        def resize_move(self) -> None:
            """基于当前系统物理光标绝对位移的八方向平滑缩放，内建最小尺寸保护。"""
            if not self._is_resizing:
                return
            hwnd = _get_hwnd()
            if not hwnd:
                return
            try:
                user32 = ctypes.windll.user32
                pt = _POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                dx = pt.x - self._resize_start_cursor[0]
                dy = pt.y - self._resize_start_cursor[1]

                left, top, w, h = self._resize_start_rect
                min_w, min_h = 900, 620
                d = self._resize_direction
                has_left = ('left' in d) or (d in ('l', 'tl', 'bl'))
                has_right = ('right' in d) or (d in ('r', 'tr', 'br'))
                has_top = ('top' in d) or (d in ('t', 'tl', 'tr'))
                has_bottom = ('bottom' in d) or (d in ('b', 'bl', 'br'))

                if has_left:
                    nw = max(min_w, w - dx)
                    left += (w - nw)
                    w = nw
                elif has_right:
                    w = max(min_w, w + dx)

                if has_top:
                    nh = max(min_h, h - dy)
                    top += (h - nh)
                    h = nh
                elif has_bottom:
                    h = max(min_h, h + dy)

                user32.SetWindowPos(wintypes.HWND(hwnd), None,
                                    left, top, w, h,
                                    SWP_NOZORDER | SWP_NOACTIVATE)
            except Exception:
                pass

        def resize_end(self) -> None:
            """释放缩放状态。"""
            self._is_resizing = False

        def move_by(self, dx: int, dy: int) -> None:
            """平滑移动窗口（用于 WebView2 阻断原生消息时的精准拖拽跟随兼容）。"""
            hwnd = _get_hwnd()
            if not hwnd:
                return
            try:
                user32 = ctypes.windll.user32
                r = _RECT()
                user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r))
                user32.SetWindowPos(wintypes.HWND(hwnd), None,
                                    r.left + int(dx), r.top + int(dy), 0, 0,
                                    SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            except Exception:
                pass

        def resize_by(self, direction: str, dx: int, dy: int) -> None:
            """八方向平滑边缘缩放，内建最小尺寸保护（兼容）。"""
            hwnd = _get_hwnd()
            if not hwnd:
                return
            try:
                user32 = ctypes.windll.user32
                r = _RECT()
                user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r))
                left = r.left
                top = r.top
                w = r.right - r.left
                h = r.bottom - r.top

                dx = int(dx)
                dy = int(dy)
                min_w = 900
                min_h = 620

                d = str(direction).lower().strip()
                has_left = ('left' in d) or (d in ('l', 'tl', 'bl'))
                has_right = ('right' in d) or (d in ('r', 'tr', 'br'))
                has_top = ('top' in d) or (d in ('t', 'tl', 'tr'))
                has_bottom = ('bottom' in d) or (d in ('b', 'bl', 'br'))

                if has_left:
                    nw = max(min_w, w - dx)
                    left += (w - nw)
                    w = nw
                elif has_right:
                    w = max(min_w, w + dx)

                if has_top:
                    nh = max(min_h, h - dy)
                    top += (h - nh)
                    h = nh
                elif has_bottom:
                    h = max(min_h, h + dy)

                user32.SetWindowPos(wintypes.HWND(hwnd), None,
                                    left, top, w, h,
                                    SWP_NOZORDER | SWP_NOACTIVATE)
            except Exception:
                pass

        def close(self) -> None:
            """走和点原生 X 完全同一条路径（close_to_tray 时收到托盘）。"""
            hwnd = _get_hwnd()
            if hwnd:
                try:
                    r = _RECT()
                    ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r))
                    w, h = r.right - r.left, r.bottom - r.top
                    if w >= 900 and h >= 620:
                        cur_st = server.read_settings()
                        app_st = cur_st.get('app') or {}
                        app_st['window_width'] = w
                        app_st['window_height'] = h
                        server.write_settings({'gateway': cur_st.get('gateway') or {}, 'app': app_st})
                except Exception:
                    pass
            if shell.request_close():
                shell.destroy_window()

    return _WindowApi()


# 网关程序、配置、地址三样都归 core\identity.py 管，这里只把名字接过来。
#
# 为什么要收敛：不认同一个开关就是"各说各话"——监控页走的 core\health.py 认
# PRISM_GATEWAY_BASE，托盘却死盯 8317，沙箱里跑控制台时页面上写着网关在线、托盘
# 标题写着未运行，同一台机器两个说法。现在两边从同一个模块取，算不出两样。
#
# 身份那四个状态名同理：它们要和 STATE_COLORS 的键、以及 identity.gateway_identity()
# 的 state 对得上，散在两处早晚会漂。
GATEWAY_EXE = identity.GATEWAY_EXE
GATEWAY_CONFIG = identity.GATEWAY_CONFIG
DEFAULT_GATEWAY_HOST = identity.DEFAULT_GATEWAY_HOST
DEFAULT_GATEWAY_PORT = identity.DEFAULT_GATEWAY_PORT
GATEWAY_HOST = identity.GATEWAY_HOST
GATEWAY_PORT = identity.GATEWAY_PORT
IDENTITY_OK = identity.IDENTITY_OK
IDENTITY_FOREIGN = identity.IDENTITY_FOREIGN
IDENTITY_UNKNOWN = identity.IDENTITY_UNKNOWN
IDENTITY_DOWN = identity.IDENTITY_DOWN

DEFAULT_CONSOLE_PORT = 8318

# 互斥体名里带控制台端口。以前是光秃秃的 Global\PrismConsole，与旧部署同名——
# 迁移期两份 Prism（老目录那份 8318、新目录这份 8319）会互相把对方挡在门外，
# 被挡的那次还只写一行日志就退（见 notify_already_running 的说明）。
MUTEX_NAME_TEMPLATE = 'Global\\PrismConsole-%d'
ERROR_ALREADY_EXISTS = 183

TITLE = 'Prism · 本地模型网关控制台'

# 托盘 tip 的字符上限。pystray 把它塞进 NOTIFYICONDATAW.szTip = WCHAR[128]
# （pystray\_util\win32.py:160），实测超过 127 个字符时赋值当场 ValueError，
# 整个 tip 更新就断在那儿，所以这里自己先截断。
TRAY_TIP_MAX = 120
# tip 里的"今日计数"要查 SQLite、"当前路由"要向网关发一次管理接口 GET（bridge 的
# 超时是 15 秒），比 5 秒一次的状态灯贵得多，所以按这个间隔节流。
TRAY_TIP_INTERVAL = 60

# 托盘图标状态色。只用于状态，不参与配色体系（INTERFACES.md 的界面规范）。
STATE_COLORS = {
    'ok': (34, 197, 94),        # #22C55E
    'down': (239, 68, 68),      # #EF4444
    'unknown': (234, 179, 8),   # #EAB308
    'foreign': (249, 115, 22),  # #F97316 端口在听，但不是本目录的网关
}

# 状态在托盘抬头上的说法（%d 是网关端口）。键必须和 identity.gateway_identity()
# 的 state 对得上。
# foreign 那条是整句重写：用户看到的"在线"必须真的代表本目录这套网关，不是
# "8317 上有东西在应答"。
STATE_LABELS = {
    'ok': '网关在线 :%d',
    'down': '网关未运行 :%d',
    'unknown': '网关在线（身份未核实） :%d',
    'foreign': '%d 上跑的不是本目录的配置',
}

_log_lock = threading.Lock()
_RECENT: list[str] = []
LOG_PATH = bridge.ROOT / 'prism.log'
LOG_MAX_BYTES = 512 * 1024      # 超过就轮转，别让它无限长
LOG_BACKUPS = 3                 # prism.log + prism.log.1~.3

_LOG_FORMAT = logging.Formatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')


class _RecentHandler(logging.Handler):
    """日志尾部留在内存里：致命错误弹窗要带几行上下文，用户截图给开发者。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:                       # noqa: BLE001 - 日志本身不许抛
            return
        with _log_lock:
            _RECENT.append(line)
            del _RECENT[:-200]


def _stderr_usable() -> bool:
    """pythonw 下 sys.stderr 是 None，往它上面写不是"没输出"，是抛异常。"""
    return getattr(sys, 'stderr', None) is not None


def _console_visible() -> bool:
    """这个进程有没有一个用户看得见的控制台。

    **别拿 `_stderr_usable()` 当这个判断。** pywebview 在 import 时会把
    `sys.stderr` 从 None 换成一个 TextIOWrapper（实测：import webview 之前
    is_none=True，之后 is_none=False），而 main.py 在模块顶部就 import 了它——
    等到 run() 里再问，答案恒为 True，弹窗就永远不弹了。这个坑是本轮自己踩的：
    写完 `not _stderr_usable()` 之后，pythonw + 无控制台那一路实测没弹窗。

    有没有控制台要看 GetConsoleWindow() 给不给句柄：终端里跑 python.exe 给，
    pythonw.exe 和打包后的 Prism.exe（--windowed）都不给。
    """
    try:
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:
        return False


def _log_uncaught(exc_type, exc_value, exc_tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        # Ctrl-C 是用户意图不是事故；开发态照旧打屏，别往 prism.log 里塞噪声
        if _stderr_usable():
            sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    logging.getLogger('prism').critical('未捕获异常：', exc_info=(exc_type, exc_value, exc_tb))


def _log_uncaught_thread(args) -> None:
    if issubclass(args.exc_type, SystemExit):
        return
    logging.getLogger('prism').critical(
        '线程 %s 未捕获异常：', getattr(args.thread, 'name', '?'),
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def setup_logging() -> None:
    """装日志出口。启动最早期调一次；重复调用是空操作。

    桌面版是 pythonw 拉起来的：没有控制台，`sys.stderr` 就是 None，而解释器打印
    未捕获异常的目标恰恰是它——traceback 一个字节都到不了任何地方（实测
    app\\prism.log 里 `Traceback` 出现 0 次）。所以文件 handler 和两个 excepthook
    都得自己装，否则 500 页让用户"把 stderr 日志贴给开发者"时，那份东西根本不存在。
    """
    root = logging.getLogger()
    if any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        return
    root.setLevel(logging.INFO)
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding='utf-8')
        file_handler.setFormatter(_LOG_FORMAT)
        root.addHandler(file_handler)
    except OSError:
        pass          # 日志写不进去不是致命问题，绝不能因此让启动失败
    if _stderr_usable():
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(_LOG_FORMAT)
        root.addHandler(stream_handler)
    root.addHandler(_RecentHandler())
    sys.excepthook = _log_uncaught
    threading.excepthook = _log_uncaught_thread


def log(message: str) -> None:
    """一条启动/运行日志。出口见 setup_logging：文件、stderr（有的话）、环形缓冲。

    消息里带 `%` 是安全的：logging 只在有 args 时才做 %-格式化，这里不传 args。
    """
    logging.getLogger('prism').info(message)


def recent_log(tail: int = 6) -> list[str]:
    with _log_lock:
        return list(_RECENT[-tail:])


def _message_box(body: str, title: str, flags: int) -> None:
    """弹一个原生对话框。没有控制台时这是唯一能把话说到用户眼前的路子。

    对话框是模态的，会不会卡住调用方由调用方自己判断——所以这里只负责弹，
    不做"要不要弹"的决策。
    """
    try:
        ctypes.windll.user32.MessageBoxW(None, body, title, flags)
    except Exception:
        pass          # 弹不出来也不能让启动挂掉


MB_ICONERROR = 0x10
MB_ICONINFO = 0x40


def notify_error(message: str) -> None:
    """启动期的致命错误：打包成 exe 后没有控制台，必须弹窗，否则用户只看到"没反应"。

    弹窗里带上环形缓冲里最近几行——用户截图给开发者时就有上下文了。
    """
    context = recent_log()
    log('致命错误：' + message)
    body = message
    if context:
        body += '\n\n最近日志（完整日志见 %s）：\n%s' % (LOG_PATH, '\n'.join(context))
    _message_box(body, 'Prism 启动失败', MB_ICONERROR)


def notify_already_running(console_port: int) -> None:
    """第二次启动：告诉用户"已经在跑了"，别让他对着一个没反应的图标反复双击。

    以前这里只写一行日志就 return 0。打包成 exe 之后没有控制台，那行日志用户
    看不到——双击、什么都没发生、再双击、还是没有，最后以为程序坏了。
    窗口可能正收在托盘里，也可能压根没起（就是那个点 X 不退出的僵尸）。
    以前还指过"用浏览器打开 8318"这条路；控制台令牌（ME-12）启用后，第二个
    实例拿不到令牌、指过去只会看到 401，所以这条指路收掉，只留托盘。

    ⚠ 这个函数必须保持**模块级**：它被 run() 的"已有实例"分支直接调用。
    历史上它曾被缩进进 notify_error 的函数体里变成嵌套函数，run() 一走到
    这条分支就 NameError 静默退出（第二次双击永远"没反应"的那个场景）。
    """
    _message_box(
        'Prism 已经在运行了（控制台端口 %d）。\n\n'
        '它可能收在托盘里：右下角找一下 Prism 图标，右键 → 打开 Prism。'
        % console_port,
        'Prism 已经在运行', MB_ICONINFO)


# --------------------------------------------------------------------------- 单实例

_mutex_handle = None


def acquire_single_instance(console_port: int) -> bool:
    """拿到单实例锁返回 True；已经有实例在跑返回 False。

    CreateMutexW 的返回值是句柄（64 位指针），默认 restype=c_int 会被截断，
    必须显式声明，否则 GetLastError 读到的可能不是 183。

    锁的名字跟着控制台端口走（MUTEX_NAME_TEMPLATE）：不同端口的两份 Prism 是
    两个独立实例，本来就该各开各的窗口。
    """
    global _mutex_handle
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    handle = k32.CreateMutexW(None, False, MUTEX_NAME_TEMPLATE % int(console_port))
    if not handle:
        # 建不出互斥体（权限异常等）不该把用户挡在门外，放行。
        log('单实例互斥体创建失败（WinError %d），跳过检查' % ctypes.get_last_error())
        return True
    _mutex_handle = handle
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        return False
    return True


def _release_single_instance() -> None:
    global _mutex_handle
    if _mutex_handle:
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CloseHandle.argtypes = (ctypes.c_void_p,)
        k32.CloseHandle(_mutex_handle)
        _mutex_handle = None


atexit.register(_release_single_instance)


# --------------------------------------------------------------------------- 网关

def gateway_listening(timeout: float = 0.5) -> bool:
    """探网关端口。网关没有 /health（404），只能用 socket。"""
    try:
        with socket.create_connection((GATEWAY_HOST, GATEWAY_PORT), timeout=timeout):
            return True
    except OSError:
        return False


# ----------------------------------------------------------------------- 网关身份
#
# 实现整个搬到了 core\identity.py：监控页（core\health.py → static\pages\monitor.js）
# 也要核这同一件事，留两份迟早会漂。为什么必须核、四个状态各是什么意思，见那个文件
# 的模块注释。本文件只负责把结论翻译成托盘的颜色和文案。


def gateway_is_prisms_to_launch() -> bool:
    """探的地址就是 config.yaml 里那一个（127.0.0.1:8317）吗？

    PRISM_GATEWAY_BASE 是 core/* 共用的测试开关。指到 8390 这类沙箱端口时，
    Prism 不该去拉 cli-proxy-api.exe：那个 exe 读的还是 config.yaml，起来也只会
    去听 8317，既解决不了探测失败，还凭空起了个用户没要的生产服务。
    """
    return (GATEWAY_HOST, GATEWAY_PORT) == (DEFAULT_GATEWAY_HOST, DEFAULT_GATEWAY_PORT)


def ensure_gateway(wait: bool = True) -> str:
    """确保网关端口在监听。返回 'running' / 'started' / 'starting' / 'foreign:说明' / 'failed:原因'。

    **不在监听才拉**：已经在跑就一个字节都不动（README 里承诺"你自己的网关不会被顶掉"）。

    但"在跑"不等于"是本目录这套"。端口是公共资源，8317 上完全可能坐着另一个部署的
    网关（实测就是这样）。那种情况下返回 'foreign:...' 而不是 'running'，让托盘和
    启动日志如实说出来——监控页/日志页拿着别人的数据当真，比"网关未运行"更糟。
    """
    if gateway_listening(timeout=0.15):
        who = identity.gateway_identity()
        log('网关身份：' + who['note'])
        if who['state'] == IDENTITY_FOREIGN:
            return 'foreign:' + who['note']
        return 'running'
    if not gateway_is_prisms_to_launch():
        return ('failed:PRISM_GATEWAY_BASE 指着 %s:%d，Prism 不代管这个地址'
                '（只有 config.yaml 里的 %s:%d 才由 Prism 拉）'
                % (GATEWAY_HOST, GATEWAY_PORT, DEFAULT_GATEWAY_HOST, DEFAULT_GATEWAY_PORT))
    if not GATEWAY_EXE.is_file():
        return 'failed:找不到网关程序 ' + str(GATEWAY_EXE)
    if not GATEWAY_CONFIG.is_file():
        return 'failed:找不到网关配置 ' + str(GATEWAY_CONFIG)
    try:
        subprocess.Popen(
            [str(GATEWAY_EXE), '-config', str(GATEWAY_CONFIG)],
            cwd=str(ROOT),
            # 隐藏窗口：桌面应用弹一个黑框很难看，而且用户会手动去关它
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        return 'failed:拉起网关失败 ' + str(exc)
    if not wait:
        return 'starting'
    # 网关是 Go 程序，冷启动通常 1~3 秒；给 20 秒余量，期间界面照样能开
    deadline = time.time() + 20
    while time.time() < deadline:
        if gateway_listening(timeout=0.2):
            return 'started'
        time.sleep(0.15)
    return 'failed:网关已拉起但 %d 秒内没有监听 %d 端口，去看 %s' % (
        20, GATEWAY_PORT, ROOT / 'logs' / 'main.log')


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
    return '"%s" "%s"' % (exe, Path(__file__).resolve())


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
        self._tip_text = None       # 上一次算出的悬浮提示，配合 _tip_at 做节流
        self._tip_at = 0.0

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
            self.notify('正在后台运行')
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

        以前这里是 `'ok' if gateway_listening() else 'down'`——端口有人听就亮绿灯。
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
                # tip 里的今日计数与当前路由比颜色贵（一次 SQLite + 一次网关 GET），
                # tray_tip() 自己按 TRAY_TIP_INTERVAL 节流，这里每次调都行
                tip = self.tray_tip()
                if tip != icon.title:
                    icon.title = tip
        except Exception:
            log('更新托盘图标失败：\n' + traceback.format_exc())

    # -- 悬浮提示 ----------------------------------------------------------
    # 用户点名要的：悬浮提示里要有今日计数和当前路由。这两样都不在这个进程里
    # ——今日计数在 app\usage-history.db（sampling.history），当前路由要问网关
    # （bridge.snapshot）。它们都会失败（网关没起、库还没建、业务文件被改过），
    # 失败就写"数据不可用"，异常一个都不许冒到托盘线程：托盘线程一死，
    # 整个图标连菜单一起没了。

    def _tip_today(self) -> str:
        try:
            from core import sampling
            rows = sampling.history(1)          # days=1 → 只有今天
            ok = sum(int(r.get('success') or 0) for r in rows)
            bad = sum(int(r.get('failed') or 0) for r in rows)
        except Exception as exc:                # noqa: BLE001 - 托盘线程不许抛
            log('托盘读今日计数失败：%s: %s' % (type(exc).__name__, str(exc)[:120]))
            return '今日请求 数据不可用'
        return '今日请求 %d 成功/%d 失败' % (ok, bad)

    def _tip_route(self, state: str) -> str:
        # 先探端口再问管理接口：网关没在跑时 bridge 要等满 15 秒才报错，托盘
        # 刷新不该有这种停顿。端口探测只要 0.5 秒。
        if state == IDENTITY_DOWN:
            return '路由 网关未运行'
        if state == IDENTITY_FOREIGN:
            # 端口上坐着别人的网关时，它的路由是那个部署的事实，摆到本目录的
            # 提示里只会误导人。
            return '路由 网关不是本目录的，读不到'
        if state != IDENTITY_OK:
            return '路由 网关身份未核实，读不到'
        try:
            # **不走 bridge.snapshot()**：那条路会 rs.fetch_all()，对每一个来源发 live
            # /models。托盘每 60 秒问一次，等于收进托盘也持续打上游（审查 HI-05）。
            # enabled_groups() 只读本地 + 一个环回管理请求。
            selected = bridge.enabled_groups() or {}
        except Exception as exc:                # noqa: BLE001 - 同上
            log('托盘读当前路由失败：%s: %s' % (type(exc).__name__, str(exc)[:120]))
            return '路由 数据不可用'
        if not selected:
            return '路由 数据不可用'
        # selected[group] 自多来源功能起是**列表**（一个分组可以同时启用多家来源）。
        # 直接 '%s' % ['a','b'] 会渲染成 "['a', 'b']"，托盘提示里很难看；这里自己拼。
        # 字符串形状也照收——托盘可能在旧版 route_selector 旁边跑。
        def _names(v):
            if isinstance(v, str):
                return v or '未选'
            if isinstance(v, (list, tuple)):
                live = [x for x in v if isinstance(x, str) and x]
                return '、'.join(live) if live else '未选'
            return '未选'
        return '路由 ' + ' '.join('%s=%s' % (g, _names(selected.get(g)))
                                  for g in sorted(selected))

    def _tray_title(self, state: str) -> str:
        """只有网关状态那一版。start_tray() 用——查计数和路由会卡住主线程。"""
        label = STATE_LABELS.get(state, '网关状态未知 :%d') % GATEWAY_PORT
        return '%s · %s' % (TITLE, label)

    def tray_tip(self, now: float | None = None) -> str:
        """托盘悬浮提示：网关状态 + 今日计数 + 当前路由。

        TRAY_TIP_INTERVAL 秒内重复调用直接返回上次的结果。返回值一定不超过
        TRAY_TIP_MAX 个字符（Windows 的 szTip 只有 128 个 WCHAR）。
        """
        now = time.time() if now is None else now
        if self._tip_text is not None and now - self._tip_at < TRAY_TIP_INTERVAL:
            return self._tip_text
        # 一次身份核对喂三处：标题、颜色、以及 _tip_route 要不要去问网关
        state = self.gateway_state()
        text = ' · '.join([self._tray_title(state),
                           self._tip_today(), self._tip_route(state)])
        self._tip_text = text if len(text) <= TRAY_TIP_MAX else text[:TRAY_TIP_MAX - 1] + '…'
        self._tip_at = now
        return self._tip_text

    def restart_gateway(self, *_a) -> None:
        """README 里的托盘"重启网关"。**先探后拉**，不杀进程——见文件头的说明。"""
        if gateway_listening():
            who = identity.gateway_identity()
            if who['state'] == IDENTITY_FOREIGN:
                self.notify('%s。没有动它——要换成本目录的网关，得自己先把那个进程停掉。'
                            % who['note'])
            else:
                self.notify('网关正在运行（%s:%d），没有重启它。'
                            % (GATEWAY_HOST, GATEWAY_PORT))
            return
        self.notify('网关没在监听，正在拉起…')
        result = ensure_gateway()
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
        icon = pystray.Icon('Prism', tray_image(state), self._tray_title(state),
                            self.build_menu())
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


# --------------------------------------------------------------------------- 启动

def _busy_page(port: int, message: str) -> str:
    """端口被占时窗口里显示的东西。不能白屏，也不能只让用户去猜。"""
    # 脚本路径从 ROOT 拼。以前这里写死 D:\MY_DESIGN\Agent-router-Pro-Max\...，
    # 换个安装目录，那行提示就变成"照着敲，PowerShell 说找不到路径"——比不写还坏。
    import html
    stop_selector = html.escape(str(ROOT / 'script' / 'Stop-Selector.ps1'))
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


def run(console_port: int = DEFAULT_CONSOLE_PORT, open_window: bool = True) -> int:
    """完整启动流程。返回进程退出码。"""
    if not acquire_single_instance(console_port):
        log('已经有一个 Prism 在运行（控制台端口 %d）' % console_port)
        if open_window:
            user32 = ctypes.windll.user32
            user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
            user32.FindWindowW.restype = wintypes.HWND
            existing = user32.FindWindowW(None, TITLE)
            if existing and bring_existing_to_front(existing):
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

    def _async_bootstrap_gateway(sh: Shell):
        # 异步线程拉起网关并在就绪后刷新托盘，主线程零等待
        if gateway_listening(timeout=0.15):
            who = identity.gateway_identity()
            log('网关身份：' + who['note'])
            if who['state'] == IDENTITY_FOREIGN:
                sh.notify('foreign:' + who['note'])
            sh.refresh_tray()
            return
        gw_res = ensure_gateway(wait=True)
        log('网关异步启动完成：' + gw_res)
        if gw_res.startswith('failed') or gw_res.startswith('foreign'):
            sh.notify(gw_res)
        sh.refresh_tray()

    if open_window:
        threading.Thread(target=_async_bootstrap_gateway, args=(shell,),
                         name='prism-gw-bootstrap', daemon=True).start()
        startup_warning = None
    else:
        state = ensure_gateway(wait=True)
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
        webview.create_window(TITLE, html=_busy_page(console_port, str(exc)),
                              width=760, height=560)
        webview.start()
        return 2
    shell.httpd = httpd
    log('控制台已监听 ' + shell.url)

    # 异步预热本地控制台 HTTP，消除 WebView2 首次握手延迟
    def _warmup_http(target_url: str):
        try:
            import urllib.request
            req = urllib.request.Request(target_url, headers={'User-Agent': 'Prism-Warmup'})
            with urllib.request.urlopen(req, timeout=0.8):
                pass
        except Exception:
            pass
    threading.Thread(target=_warmup_http, args=(shell.url,), name='prism-warmup', daemon=True).start()

    def _init_tray_worker(sh: Shell, warn: str | None):
        try:
            sync_autostart(settings)
        except Exception:
            pass
        sh.start_tray()
        if warn:
            sh.notify(warn)
        threading.Thread(target=_status_loop, args=(sh,), name='prism-status', daemon=True).start()

    if open_window:
        threading.Thread(target=_init_tray_worker, args=(shell, startup_warning),
                         name='prism-tray-init', daemon=True).start()
    else:
        try:
            sync_autostart(settings)
        except Exception:
            pass
        shell.start_tray()
        if startup_warning:
            shell.notify(startup_warning)
        threading.Thread(target=_status_loop, args=(shell,), name='prism-status',
                         daemon=True).start()

    if not open_window:
        # 没有窗口时 webview.start() 不会跑，主线程必须自己等着，否则进程直接退出、
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
    webview.settings['DRAG_REGION_SELECTOR'] = '.none-pywebview-drag'

    # 恢复记忆的窗口尺寸
    app_cfg = settings.get('app') or {}
    win_w = max(900, min(3840, int(app_cfg.get('window_width') or 1180)))
    win_h = max(620, min(2160, int(app_cfg.get('window_height') or 800)))

    # 令牌拼在窗口 URL 上（?t=）：app.js 首次读到就存 sessionStorage，请求全程带头。
    # 回环口上的 URL 不出本机；令牌只走这一条路，不打日志、不进环境变量。
    # 深色模式底色严格对齐 CSS --bg (#212121)，消除首帧色差抖动
    bg_color = '#212121' if server.effective_theme() == 'dark' else '#FFFFFF'
    window = webview.create_window(TITLE, shell.url + '?t=' + console_token,
                                   width=win_w, height=win_h,
                                   min_size=(900, 620), text_select=True,
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
        _apply_edgechromium_patch()
        if cache_dir:
            webview.start(hide_native_titlebar, (window,), storage_path=cache_dir)
        else:
            webview.start(hide_native_titlebar, (window,))
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
