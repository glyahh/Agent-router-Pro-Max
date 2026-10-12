"""Windows 窗口适配：WebView2 参数、原生窗框与前端窗口 API。"""
from __future__ import annotations

import ctypes
import logging
import os
import time
import traceback
import urllib.parse
import webbrowser
from ctypes import wintypes

TITLE = 'Prism · 本地模型网关控制台'
_logger = logging.getLogger('prism')
MIN_WINDOW_SIZE = (900, 620)

# open_url 允许打开的域名。它从前端拿到什么就打开什么，不校验等于把「打开任意链接」
# 的能力交给页面内容；目前只有 Cursor 页那一步需要跳出应用去 ngrok。
OPEN_URL_HOSTS = ('ngrok.com',)

def log(message: str) -> None:
    _logger.info(message)


# WebView2 -----------------------------------------------------------------

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

# ── Win32：把原生标题栏摘掉（Electron 的 titleBarStyle:'hidden' 等价物）──────────
#
# 为什么不是 pywebview 的 frameless=True：那个等价于 Electron 的 frame:false，
# 会把 WS_THICKFRAME 一起清掉 —— 边缘拖拽缩放、Snap 贴边、双击标题栏最大化全没了。
# 实测本机 Electron 应用（DSH Desktop，resources\app.asar 的 lib\main.js:7788 与
# :7797）用的是 titleBarStyle:'hidden' + titleBarOverlay：**保留窗口框架**，
# 只藏标题栏。所以这里也只摘 WS_CAPTION 这一位，其余样式原样留着。
GWL_STYLE = -16
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
# DWMWA_WINDOW_CORNER_PREFERENCE / DWMWCP_ROUND：摘掉标题栏之后 Win11 不再自动
# 给圆角（系统只在有原生框架时才画），实测 CORNER_PREFERENCE 保持 0=默认、窗口是直角。
# 不补这一下，无边框窗口看起来比原生窗口更"旧"。
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
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


def hide_native_titlebar(window=None, dark=False) -> None:
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
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                wintypes.HWND(hwnd), ctypes.c_uint(DWMWA_USE_IMMERSIVE_DARK_MODE),
                ctypes.byref(ctypes.c_int(1 if dark else 0)), ctypes.c_uint(4))
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


def find_window_by_title(title: str = TITLE):
    user32 = ctypes.windll.user32
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    return user32.FindWindowW(None, title)


def _resized_rect(rect, direction: str, dx: int, dy: int):
    left, top, width, height = rect
    min_width, min_height = MIN_WINDOW_SIZE
    direction = direction.lower().strip()
    left_edge = 'left' in direction or direction in ('l', 'tl', 'bl')
    right_edge = 'right' in direction or direction in ('r', 'tr', 'br')
    top_edge = 'top' in direction or direction in ('t', 'tl', 'tr')
    bottom_edge = 'bottom' in direction or direction in ('b', 'bl', 'br')

    if left_edge:
        new_width = max(min_width, width - dx)
        left += width - new_width
        width = new_width
    elif right_edge:
        width = max(min_width, width + dx)

    if top_edge:
        new_height = max(min_height, height - dy)
        top += height - new_height
        height = new_height
    elif bottom_edge:
        height = max(min_height, height + dy)
    return left, top, width, height


# JavaScript window API ------------------------------------------------------

def make_window_api(shell, *, hwnd_lookup, read_settings, write_settings):
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
        return hwnd_lookup(retries=1)

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

        def read_clipboard(self) -> str:
            """读系统剪贴板里的文本，读不到返回空串。

            WebView2 里原生 Ctrl+V 在这台机器上不生效——JS 层那个弹窗按键拦截去掉之后
            仍然粘不进来，说明卡在更底下的剪贴板栈。这里直接问 Win32 要，绕开浏览器。
            """
            CF_UNICODETEXT = 13
            u32 = ctypes.windll.user32
            k32 = ctypes.windll.kernel32
            u32.GetClipboardData.restype = ctypes.c_void_p
            k32.GlobalLock.restype = ctypes.c_void_p
            k32.GlobalLock.argtypes = [ctypes.c_void_p]
            k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
            if not u32.OpenClipboard(None):
                return ''
            try:
                handle = u32.GetClipboardData(CF_UNICODETEXT)
                if not handle:
                    return ''
                ptr = k32.GlobalLock(handle)
                if not ptr:
                    return ''
                try:
                    return ctypes.wstring_at(ptr)
                finally:
                    k32.GlobalUnlock(handle)
            except Exception:
                log('读剪贴板失败：\n' + traceback.format_exc())
                return ''
            finally:
                u32.CloseClipboard()

        def open_url(self, url: str) -> bool:
            """用系统默认浏览器打开外部链接，返回是否真的打开了。

            WebView2 里 <a target="_blank"> 唤不起系统浏览器，页面要跳出去只能走这里。
            只放行 https 且域名落在 OPEN_URL_HOSTS 里的地址。
            """
            try:
                parsed = urllib.parse.urlparse(str(url or ''))
            except Exception:
                return False
            host = (parsed.hostname or '').lower()
            allowed = any(host == h or host.endswith('.' + h) for h in OPEN_URL_HOSTS)
            if parsed.scheme != 'https' or not allowed:
                log('拒绝打开非白名单外链：%r' % (str(url)[:120],))
                return False
            try:
                webbrowser.open(parsed.geturl())
                return True
            except Exception:
                log('打开外链失败：\n' + traceback.format_exc())
                return False

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

                left, top, width, height = _resized_rect(
                    self._resize_start_rect, self._resize_direction, dx, dy)
                user32.SetWindowPos(wintypes.HWND(hwnd), None,
                                    left, top, width, height,
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
                left, top, width, height = _resized_rect(
                    (r.left, r.top, r.right - r.left, r.bottom - r.top), direction, int(dx), int(dy))
                user32.SetWindowPos(wintypes.HWND(hwnd), None,
                                    left, top, width, height,
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
                        cur_st = read_settings()
                        app_st = cur_st.get('app') or {}
                        app_st['window_width'] = w
                        app_st['window_height'] = h
                        write_settings({'gateway': cur_st.get('gateway') or {}, 'app': app_st})
                except Exception:
                    pass
            if shell.request_close():
                shell.destroy_window()

    return _WindowApi()


