"""启动日志、原生错误提示与单实例互斥。"""
from __future__ import annotations

import atexit
import ctypes
import logging
import logging.handlers
import sys
import threading

from core import bridge

MUTEX_NAME_TEMPLATE = 'Global\\PrismConsole-%d'
ERROR_ALREADY_EXISTS = 183

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
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
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


