r"""Prism 控制台 HTTP 服务：HTTP API + 静态文件。

契约在 app/INTERFACES.md 的「HTTP API 契约」一节。本文件只做 HTTP 层的事：
路由、`{"ok":...}` 包装、错误到状态码的映射、静态文件，外加两个"前端要、但没有
独立 core 模块承载"的聚合（用量的请求计数、日志尾部）。业务逻辑一律转调 core/*，
core 再转调 route_selector —— 不在这里重写第二份。

安全模型照搬 script/route_selector.py 的 Handler.check：
  * Host 必须精确等于 127.0.0.1:<console_port>。用 localhost 或局域网地址访问一律拒绝，
    因为控制台能改用户的网关配置，DNS rebinding 这类把戏必须挡在门外；
  * 带 Origin 时必须是同源。浏览器里的其他站点因此打不进本机接口。
拒绝用 409 而不是 403，是为了跟既有选择页的行为一致 —— 前端 app.js 对 404/409/5xx
各有一套中文文案，行为一致才不会让用户看错原因。

进程生命周期由调用方掌握：main.py 用 start_background(port) 起线程，
`python server.py --port 8395` 则自己阻塞跑。本文件从不终止任何进程。
"""

from __future__ import annotations

import argparse
import errno
import json
import logging
import logging.handlers
import os
import re
import socket
import sys
import threading
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core import agents, bridge, health, sampling, sources  # noqa: E402

ROOT = bridge.ROOT
RouteError = bridge.RouteError

log = logging.getLogger('prism.server')

DEFAULT_PORT = 8318
DEFAULT_LOG_LINES = 200
MAX_LOG_LINES = 5000
MAX_BODY = 1024 * 1024          # 来源表单带整张模型目录，4096 那点额度不够
ERROR_LOG_LIMIT = 2 * 1024 * 1024   # 单个错误日志实测 3.19MB，全读会把内存和响应时间都拉爆
# main.log 的尾部回溯上限。行数够不代表字节数够：一行可能是几 MB 的超长堆栈，
# limit=5000 时 64KB 块会一直往回读到文件头。当前 main.log 5.5MB 不致命，但日志
# 只涨不缩，没有上限就是个随时间的慢雷。撞上限就少读几行并标记截断。
MAX_TAIL_BYTES = 8 * 1024 * 1024
STATIC_DIR = bridge.BUNDLE_DIR / 'static'
SETTINGS_PATH = bridge.ROOT / 'settings.json'
PRISM_LOG = bridge.ROOT / 'prism.log'
LOG_MAX_BYTES = 512 * 1024      # 与 main.py 的 setup_logging 保持一致
LOG_BACKUPS = 3
LOGS_DIR = ROOT / 'logs'
MAIN_LOG = LOGS_DIR / 'main.log'


def _ensure_logging() -> None:
    """独立跑（`python server.py --port ...`）时没人装日志出口，这里补一个文件 handler。

    桌面版由 main.py 的 setup_logging 负责（那里还额外接了两个 excepthook）。两边
    都只认"root 上有没有 RotatingFileHandler"，谁先装谁算，不会出现两个写者抢同一
    个文件——轮转要 rename，两个句柄会写进两个不同的文件里。
    """
    root = logging.getLogger()
    if any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        return
    root.setLevel(logging.INFO)
    try:
        handler = logging.handlers.RotatingFileHandler(
            PRISM_LOG, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding='utf-8')
        handler.setFormatter(logging.Formatter(
            '[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    except OSError:
        return                      # 日志写不进去不是致命问题，绝不能因此让控制台起不来
    root.addHandler(handler)


# 500 的中文说明要能落到具体环境问题上。原来那句"这是 Prism 的 bug，请把控制台窗口的
# stderr 日志贴给开发者"在桌面版里指向一个不存在的东西：Prism 由 pythonw 拉起，没有
# 控制台窗口，也没有那份 stderr。用户手里只有 app\prism.log。
_WIN_SHARING_VIOLATION = 32     # ERROR_SHARING_VIOLATION：别的进程占着这个文件
_WIN_DISK_FULL = 112            # ERROR_DISK_FULL


def _internal_error_hint(exc: BaseException) -> str:
    """把异常归到一个用户能动手的方向；归不了就直说是内部缺陷，别甩给"环境"。"""
    if isinstance(exc, OSError):
        winerror = getattr(exc, 'winerror', None)
        code = getattr(exc, 'errno', None)
        if code == errno.ENOSPC or winerror == _WIN_DISK_FULL:
            return '磁盘可能已满'
        if code in (errno.EACCES, errno.EPERM, errno.EBUSY) or winerror == _WIN_SHARING_VIOLATION:
            return '文件正被别的程序占用，或没有写入权限'
        if code in (errno.ECONNREFUSED, errno.ECONNRESET, errno.ETIMEDOUT, errno.EHOSTUNREACH):
            return '连不上网关'
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return '连不上网关或它的响应超时'
    return '没归到已知的环境原因，是 Prism 自己的缺陷'

# style-src 必须带 'unsafe-inline'：config.js 与 settings.js 各自用
# document.createElement('style') 注入本页样式表（共约 20KB），而 CSP 规定
# <style> 元素与 style 属性都算内联样式。实测不带这个关键字时，浏览器把注入的
# 样式表整个丢弃（document.styleSheets 里只剩 app.css），两页的页内规则全部失效，
# 控制台刷 10 条 "Applying inline style violates ...". 控制台本身只监听环回口、
# 且上面 _check_client 已校验 Host 与 Origin，这里放宽样式内联不影响脚本执行面
# （script-src 仍只有 'self'）。若以后要收紧，正确的做法是给 index.html 发 nonce
# 并由 app.js 透传给各页，而不是各页自己注入。
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")

# 静态资源后缀 → MIME。用白名单而不是 mimetypes 猜，免得把 .py 之类也端出去。
MIME = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.webp': 'image/webp',
    '.ico': 'image/x-icon',
    '.woff2': 'font/woff2',
    '.map': 'application/json; charset=utf-8',
    '.txt': 'text/plain; charset=utf-8',
}

# 设置页的网关四项。只碰这四个键：整份 PUT /v0/management/config 会丢掉
# host/port/auth-dir/remote-management（实测 PUT 返回 48 个键、文件只有 26 个顶层键）。
GATEWAY_KEYS = (
    ('debug', 'debug', 'bool'),
    ('proxy_url', 'proxy-url', 'str'),
    ('request_retry', 'request-retry', 'int'),
    ('request_log', 'request-log', 'bool'),
)
APP_DEFAULTS = {'autostart': False, 'close_to_tray': True,
                'sample_interval_sec': 600, 'retention_days': 90,
                'theme_mode': 'system'}
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
AUTOSTART_NAME = 'Prism'     # 注册表里的值名，与 main.py 约定

STATIC_ALIAS = {'/': 'index.html'}


# --------------------------------------------------------------------------- 异常

class HttpError(Exception):
    """带 HTTP 状态码的中文错误。状态码只取 400/404/405/409/413/500。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = int(status)
        self.message = message


class PortBusy(RouteError):
    """控制台端口被占用。单独一个类型，好让 main.py 能用不同文案提示。"""

    def __init__(self, port: int, cause: OSError):
        self.port = int(port)
        self.cause = cause
        hint = ('很可能是 route_selector.py 或另一个 Prism 实例在跑' if port == 8318
                else '换一个端口，或先停掉占用它的程序')
        super().__init__('端口 %d 已被占用（%s），请先停止它：%s。'
                         % (port, hint, _oserror_text(cause)))


def _oserror_text(exc: OSError) -> str:
    code = getattr(exc, 'winerror', None) or exc.errno
    return ('WinError %s' % code) if code else type(exc).__name__


# --------------------------------------------------------------------------- 服务器

class ExclusiveServer(ThreadingHTTPServer):
    """端口互斥的 HTTP 服务。

    ThreadingHTTPServer（HTTPServer）默认 allow_reuse_address = True，在 Windows 上
    这意味着 8318 已被真实选择页占用时，新建 server 照样 bind 成功、不抛 OSError。
    后果是窗口指向一个由**老进程**应答的端口，Prism 后端一个请求都收不到且全程无报错
    （plan 修正 B8）。所以这里关掉地址复用，并显式设置 SO_EXCLUSIVEADDRUSE——
    后者才是 Windows 上真正让第二个 bind 报 WinError 10048 的那个开关。
    """

    allow_reuse_address = False
    daemon_threads = True
    request_queue_size = 32

    def server_bind(self):
        # SO_EXCLUSIVEADDRUSE 要在 bind 之前设；允许被继承的 socket 不算（默认不继承）
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        # 浏览器/WebView 提前关连接是常态（切页、刷新），不该在 stderr 刷栈
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        log.exception('处理请求出错：%s', client_address)


def create_server(port: int = DEFAULT_PORT) -> ExclusiveServer:
    """建服务但不启动。端口被占用时抛 PortBusy（中文说明）。"""
    port = int(port)
    if not 1 <= port <= 65535:
        raise RouteError('端口 %s 不在 1~65535 范围内' % port)
    try:
        httpd = ExclusiveServer(('127.0.0.1', port), Handler)
    except OSError as exc:
        raise PortBusy(port, exc) from None
    httpd.console_port = port
    return httpd


def stop_server(httpd: ExclusiveServer) -> None:
    """停服务。只作用于本进程内 create_server 出来的实例。"""
    try:
        httpd.shutdown()
    finally:
        httpd.server_close()


def _prune_backups() -> None:
    """启动时清一次旧的自动备份。

    catalog-regen 的备份是 script/route_selector.py 建的（那份文件被 route_selector.lock
    锁着，改不了），它自己不带保留策略。能改的只有这里和 core/sources.py 的
    _backup()；两边调同一个 prune，这样"只做了 regen、没动来源"也清得到。
    """
    try:
        removed = sources.prune_backups()
    except Exception:                      # noqa: BLE001 - 清理失败不该拦住控制台
        log.exception('清理旧备份失败')
        return
    if removed:
        log.info('清理旧备份 %d 个：%s', len(removed), '、'.join(removed))


def start_background(port: int = DEFAULT_PORT) -> ExclusiveServer:
    """后台线程里跑服务，立刻返回。main.py 用这个。"""
    _ensure_logging()
    _prune_backups()
    httpd = create_server(port)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.3},
                              name='prism-console', daemon=True)
    thread.start()
    httpd.console_thread = thread
    _start_sampling()
    return httpd


def serve_forever(port: int = DEFAULT_PORT) -> None:
    _ensure_logging()
    _prune_backups()
    httpd = create_server(port)
    _start_sampling()
    log.info('Prism 控制台已监听 http://127.0.0.1:%d/', httpd.console_port)
    httpd.serve_forever(poll_interval=0.3)


def _apply_runtime_settings(settings: dict) -> None:
    """设置页保存后的**运行期**生效点：采样间隔 + 历史保留天数。

    这两个原先要重启 Prism 才生效 —— 界面把它们列为可调项，实际静默失效
    （上线就绪度审查 HI-06：采样循环用启动时的固定间隔、prune() 用默认保留天数）。
    main.py 的 sync_autostart 是另一个订阅者，两者互不影响。
    """
    app = (settings or {}).get('app') or {}
    try:
        sampling.configure(app.get('sample_interval_sec'), app.get('retention_days'))
    except Exception:
        log.exception('应用采样设置失败（设置本身已保存）')


def _start_sampling() -> None:
    """起用量采样线程。间隔与保留天数都读设置；起不来也不能拖垮控制台。"""
    settings = {}
    try:
        settings = read_settings()['app']
    except Exception:      # 设置文件坏了不该阻止控制台启动
        pass
    try:
        interval = int(settings.get('sample_interval_sec') or 600)
    except (TypeError, ValueError):
        interval = 600
    try:
        retention = int(settings.get('retention_days') or 90)
    except (TypeError, ValueError):
        retention = 90
    try:
        sampling.start_scheduler(interval, retention)
    except Exception:
        log.exception('用量采样线程启动失败，history 会留空')
    # 之后每次保存设置都从这里生效，不必重启
    settings_observer.add(_apply_runtime_settings)
    # 业务逻辑文件被改动过时这里会抛错。只记日志、不退出：让用户还能打开页面看原因，
    # 每个调用网关的接口会各自返回同一条中文说明，不会静默降级成"看起来正常"。
    try:
        bridge.ensure_route_selector_intact()
    except RouteError as exc:
        log.error('route_selector 校验未通过：%s', exc)


# --------------------------------------------------------------------------- 设置

class _SettingsObserver:
    """设置保存后的回调点。main.py 注册它去同步注册表自启，server 不碰 winreg 写。

    **一串回调，不是一个**：原先只有一个槽位（`set`），被 main.py 的 `sync_autostart`
    占着，于是 server 自己再想订阅"采样间隔 / 保留天数"就没地方挂 —— 结果那两个设置
    改了要重启 Prism 才生效（上线就绪度审查 HI-06）。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._fns = []

    def add(self, fn):
        with self._lock:
            if fn not in self._fns:
                self._fns.append(fn)

    def notify(self, settings: dict) -> None:
        with self._lock:
            fns = list(self._fns)
        for fn in fns:
            try:
                fn(settings)
            except Exception:
                # 一个订阅者失败不能拖累其余订阅者；设置本身已经保存了。
                log.exception('设置回调失败：%s', getattr(fn, '__name__', fn))


settings_observer = _SettingsObserver()


def read_settings() -> dict:
    """当前设置。文件缺失/损坏一律回落到默认值，不让设置页白屏。"""
    stored = {}
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding='utf-8'))
        if isinstance(raw, dict):
            stored = raw
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        log.warning('%s 读取失败，用默认设置', SETTINGS_PATH)
    app = dict(APP_DEFAULTS)
    saved_app = stored.get('app') if isinstance(stored.get('app'), dict) else {}
    for key, default in APP_DEFAULTS.items():
        value = saved_app.get(key, default)
        if isinstance(default, bool):
            app[key] = bool(value)
        elif isinstance(default, str):
            app[key] = str(value)
        else:
            try:
                app[key] = max(0, int(value))
            except (TypeError, ValueError):
                app[key] = default
    gateway = stored.get('gateway') if isinstance(stored.get('gateway'), dict) else {}
    return {'gateway': gateway, 'app': app}


def write_settings(data: dict) -> None:
    """原子写。临时名带 pid：两个 Prism 同时保存时不互相盖半成品。"""
    # 临时名要唯一到**线程**：控制台是 ThreadingHTTPServer，两个并发 POST /api/settings
    # 会写同一个临时文件互踩（与 NC-4 给 agents.py 的修法保持一致，复查轮 7 待确认项）。
    temp = SETTINGS_PATH.with_suffix('.json.%d-%d.tmp' % (os.getpid(), threading.get_ident()))
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, SETTINGS_PATH)


def _autostart_from_registry():
    """注册表是自启的权威来源。非 Windows 或读不到时返回 None（调用方回落设置文件）。

    这里只读不写：写自启要拼可执行文件路径，那是 main.py 的事（它知道自己是
    pythonw main.py 还是打包后的 Prism.exe）。
    """
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, AUTOSTART_NAME)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return None


# 主题检测：读 Windows 的「应用」主题（AppsUseLightTheme），不是「系统」主题 ——
# 系统主题管的是任务栏/开始菜单，应用主题才管窗口底色，两者可以不一致。
# 0 = 深色。机制与理由见 docs/adr/0010-dark-theme-follows-system.md。
THEME_KEY = r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize'
THEME_VALUE = 'AppsUseLightTheme'
# index.html 里的占位属性，服务它时按系统主题替换成 dark。定长字节串，找不到就原样端出去。
THEME_PLACEHOLDER = b'data-theme="light"'


def system_theme() -> str:
    """当前系统主题：'dark' 或 'light'。读不到一律回落 'light'。

    每次调用现读注册表（一次 OpenKey + QueryValueEx，微秒级），**不缓存** ——
    用户在 Windows 设置里换了主题，最迟一个轮询周期（5 秒）此处就跟上了。
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, THEME_KEY) as key:
            value, _ = winreg.QueryValueEx(key, THEME_VALUE)
        return 'light' if int(value) else 'dark'
    except Exception:                       # noqa: BLE001 - 非 Windows / 键不存在 / 值类型怪
        return 'light'


def effective_theme() -> str:
    """有效外观主题：用户若显式设定 light / dark 则固定生效，否则跟随系统。"""
    try:
        stored = read_settings()
        mode = str(stored.get('app', {}).get('theme_mode') or 'system').strip().lower()
        if mode in ('light', 'dark'):
            return mode
    except Exception:
        pass
    return system_theme()


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(value)


def _as_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        # OverflowError：json.loads 默认接受 Infinity，int(float('inf')) 会抛这个，
        # 不接住就是 500 而不是"设置值不合法"（复查轮 7 的 #3）。
        return default


# --------------------------------------------------------------------------- 网关设置项


def _unwrap(payload, endpoint):
    """网关 GET 回来的值可能在 {"debug": true} 里，也可能直接是裸值。

    返回值与"该用什么字段名 PUT 回去"——两者都靠这一次 GET 的形状定，不猜。
    """
    if isinstance(payload, dict):
        if endpoint in payload:
            return payload[endpoint], endpoint
        if len(payload) == 1:
            return next(iter(payload.values())), next(iter(payload))
        for key in ('value', 'data', 'result'):
            if key in payload:
                return payload[key], key
    return payload, endpoint


def _gateway_read(endpoint: str, kind: str):
    raw, field = _unwrap(bridge.gateway_get(endpoint), endpoint)
    if kind == 'bool':
        return _as_bool(raw), field
    if kind == 'int':
        return _as_int(raw), field
    return ('' if raw is None else str(raw)), field


def _gateway_write(endpoint: str, field: str, value) -> None:
    bridge.gateway_put(endpoint, {field: value})


def settings_payload(include_gateway: bool = True) -> dict:
    """设置页数据。网关四项读不到就退回上次保存的值，并带 note 说明是旧值。"""
    stored = read_settings()
    notes = []
    app = dict(stored['app'])
    registry = _autostart_from_registry()
    if registry is not None:
        # 注册表说什么就是什么：用户可能在系统设置里关掉了自启，设置页不该还说"已开启"
        app['autostart'] = registry
        if registry != stored['app'].get('autostart'):
            notes.append('自启状态以系统注册表为准（现在是%s）。开关会弹回，说明注册表没写成功：'
                         '确认 main.py 读设置后调用了自己的自启同步，或检查 HKCU\\%s\\%s。'
                         % ('开启' if registry else '关闭', RUN_KEY, AUTOSTART_NAME))
            # **GET 一律只读，不写盘。** 原先这里会 write_settings 把文件对齐注册表，
            # 于是"读设置"这个接口带了写副作用：settings.json 顶层若还有别的键，
            # 会被这次写回连同 read_settings 的裁剪一起丢掉（审查 NEW-06）。
            # 对齐留给显式动作：保存设置（apply_settings）或下次启动。
    cached = stored['gateway']
    if not include_gateway:
        return {'gateway': cached, 'app': app, 'notes': notes or None}

    gateway = {}
    for key, endpoint, kind in GATEWAY_KEYS:
        try:
            value, _field = _gateway_read(endpoint, kind)
            gateway[key] = value
        except (RouteError, sampling.SamplingError) as exc:
            gateway[key] = cached.get(key)      # 网关不可达时显示上次的值，别显示假 false
            notes.append('%s 读不到（%s），下面是上次保存的值' % (key, str(exc)[:120]))
    return {'gateway': gateway, 'app': app, 'notes': notes or None}


def apply_settings(body: dict) -> dict:
    """保存设置。应用项先落盘（本地写永远能成），网关项逐条 PUT 并回读校验。"""
    stored = read_settings()
    incoming_app = body.get('app') if isinstance(body.get('app'), dict) else {}
    app = dict(stored['app'])
    for key, default in APP_DEFAULTS.items():
        if key not in incoming_app:
            continue
        value = incoming_app[key]
        if isinstance(default, bool):
            app[key] = _as_bool(value)
        elif isinstance(default, str):
            val_str = str(value).strip().lower()
            app[key] = val_str if val_str in ('system', 'light', 'dark') else default
        else:
            app[key] = max(0, _as_int(value, default))
    if not 30 <= app['sample_interval_sec'] <= 86400:
        raise HttpError(400, '采样间隔要在 30~86400 秒之间（现在填的是 %d）'
                        % app['sample_interval_sec'])
    if not 1 <= app['retention_days'] <= 3650:
        raise HttpError(400, '历史保留天数要在 1~3650 之间（现在填的是 %d）' % app['retention_days'])

    incoming_gw = body.get('gateway') if isinstance(body.get('gateway'), dict) else {}
    gateway = dict(stored['gateway'])
    failures = []
    for key, endpoint, kind in GATEWAY_KEYS:
        if key not in incoming_gw:
            continue
        want = _as_bool(incoming_gw[key]) if kind == 'bool' else (
            _as_int(incoming_gw[key]) if kind == 'int' else str(incoming_gw[key] or ''))
        try:
            _current, field = _gateway_read(endpoint, kind)
            _gateway_write(endpoint, field, want)
            actual, _field = _gateway_read(endpoint, kind)
            if actual != want:
                # 网关收下了请求但值没变，说明字段名或类型不对。宁可报错也不假装保存成功
                failures.append('%s 没生效（写回后读到的还是 %r）' % (key, actual))
                continue
            gateway[key] = actual
        except (RouteError, sampling.SamplingError) as exc:
            failures.append('%s：%s' % (key, exc))

    try:
        write_settings({'gateway': gateway, 'app': app})
    except OSError as exc:
        raise HttpError(500, '设置写不进 %s：%s' % (SETTINGS_PATH, exc)) from None
    settings_observer.notify({'gateway': gateway, 'app': app})
    if app.get('retention_days'):
        try:
            sampling.prune(int(app['retention_days']))
        except Exception:
            log.exception('清理过期历史失败（不影响设置本身）')
    if failures:
        raise HttpError(409, '应用设置已保存；网关项未能写入：' + '；'.join(failures))
    return settings_payload()


# --------------------------------------------------------------------------- 用量聚合


def _usage_counts(payload: dict, mapping: dict | None) -> list[dict]:
    """api-key-usage → 契约里的 counts 数组。

    桶标签（"14:10-14:20"）不含日期，这里只在内存里按来源聚合，落库交给 sampling。
    标签/厂商靠 sampling 的映射（拿 `|` 之后的 api-key 去比对 config 条目），
    不按分组名——分组名是 codex / command code (goat)，跟来源名对不上。
    """
    grouped: dict[str, dict] = {}
    for row in sampling.parse_usage(payload):
        item = grouped.setdefault(row['source_key'], {
            'source_key': row['source_key'], 'success': 0, 'failed': 0, '_buckets': {},
        })
        item['success'] += row['success']
        item['failed'] += row['failed']
        bucket = item['_buckets'].setdefault(row['bucket'], [row['bucket'], 0, 0])
        bucket[1] += row['success']
        bucket[2] += row['failed']
    out = []
    for item in grouped.values():
        named = sampling.source_key_label(item['source_key'], mapping)
        out.append({
            # 出网前换成短哈希：counts 是响应体，完整 api-key 不该进浏览器
            'source_key': sampling.public_source_key(item['source_key']),
            'label': named['label'],
            'vendor': named['vendor'],
            'success': item['success'],
            'failed': item['failed'],
            # recent_requests 本身就是时间升序，dict 保序，直接照原样给出
            'buckets': [{'time': b[0], 'success': b[1], 'failed': b[2]} for b in item['_buckets'].values()],
        })
    out.sort(key=lambda x: (x['label'] or '', x['source_key']))
    return out


def usage_payload(days: int) -> dict:
    """用量页数据。任何一块拿不到就空着并附 note，不编数字、不整页 500。"""
    notes = []
    quota = None
    try:
        quota = health.quota_state()
    except Exception as exc:
        notes.append('配额读不到：' + str(exc)[:160])
        quota = {'available': False, 'account': None, 'plan_type': None, 'windows': [],
                 'note': '配额读不到：' + str(exc)[:160]}

    counts: list[dict] = []
    try:
        usage = sampling.mgmt_get('/v0/management/api-key-usage')
        mapping = None
        try:
            mapping = sampling.source_map()
        except Exception:
            # 标签映射失败只影响名字，计数照样要有
            log.warning('来源标签映射失败，计数会退化成主机名')
        counts = _usage_counts(usage, mapping)
    except sampling.SamplingError as exc:
        notes.append('请求计数读不到：' + str(exc)[:160])

    try:
        # history 的每一行也带 source_key（库里存的是原始值），同样不能原样出网
        history = [dict(row, source_key=sampling.public_source_key(row.get('source_key') or ''))
                   for row in sampling.history(days)]
    except Exception as exc:
        history = []
        notes.append('历史读不到：' + str(exc)[:160])

    return {'quota': quota, 'counts': counts, 'history': history,
            'days': days, 'notes': notes or None}


# --------------------------------------------------------------------------- 日志


def tail_lines(path: Path, limit: int, max_bytes: int = MAX_TAIL_BYTES):
    """从文件尾按块往回读最后 limit 行，返回 (lines, truncated)。

    日志会长到几十 MB，每次 3 秒轮询都全量读不划算；但也不能像版本行那样只读固定尾部
    ——200 行可能比一个 64KB 块还长。所以从尾往前累积到够行为止。

    两个上限同时管着：行数（limit）和字节数（max_bytes）。只数换行的话，一行几 MB 的
    堆栈会把整个文件读进内存；撞到字节上限就停手，少给几行并用 truncated 标出来，
    由调用方转成 X-Prism-Truncated 告诉前端"这不是全部"。

    注意 truncated 只表示字节上限生效。回溯到的第一行往往是半截，照旧丢掉（见下），
    那种截断几乎每次大日志读取都会发生，标出来等于常亮，没有信息量。
    """
    if not path.is_file():
        return [], False
    block = 64 * 1024
    truncated = False
    with path.open('rb') as handle:
        handle.seek(0, os.SEEK_END)
        pos = handle.tell()
        data = b''
        while pos > 0 and data.count(b'\n') <= limit:
            room = max_bytes - len(data)
            if room <= 0:
                truncated = True
                break
            step = min(block, pos, room)    # room 收口，data 绝不会超过 max_bytes
            pos -= step
            handle.seek(pos)
            data = handle.read(step) + data
    text = data.decode('utf-8', 'replace')
    lines = text.splitlines()
    if pos > 0 and not data.startswith(b'\n') and lines:
        # 回溯到的第一行很可能只有半截，丢掉它，免得界面出现断头行
        lines = lines[1:]
    return lines[-limit:], truncated


def read_logs(limit: int) -> dict:
    lines, truncated = tail_lines(MAIN_LOG, limit)
    return {'lines': lines, 'line_count': len(lines), 'truncated': truncated}


def clear_logs() -> dict:
    """清空网关日志。走管理接口而不是直接删文件——网关自己持有文件句柄。"""
    bridge.gateway_delete('logs')
    return {'cleared': True}


def list_error_logs() -> dict:
    files = []
    if LOGS_DIR.is_dir():
        for path in LOGS_DIR.glob('error-*.log'):
            try:
                stat = path.stat()
            except OSError:
                continue
            files.append({'name': path.name, 'size': stat.st_size,
                          'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(timespec='seconds')})
    files.sort(key=lambda f: f['modified'], reverse=True)
    return {'files': files, 'dir': str(LOGS_DIR)}


def read_error_log(name: str):
    """返回 (原始字节, 是否被截断)。名字必须先在 logs 目录里落地过一次。"""
    if not re.match(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$', name or ''):
        raise HttpError(400, '日志文件名不合法：只允许字母数字与 . _ -')
    path = (LOGS_DIR / name)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HttpError(404, '日志文件不存在：' + name) from None
    if resolved.parent != LOGS_DIR.resolve() or not resolved.is_file():
        raise HttpError(404, '日志文件不存在：' + name)
    truncated = False
    with resolved.open('rb') as handle:
        raw = handle.read(ERROR_LOG_LIMIT + 1)
    if len(raw) > ERROR_LOG_LIMIT:
        raw = raw[:ERROR_LOG_LIMIT]
        truncated = True
    return raw, truncated


# --------------------------------------------------------------------------- 来源预览


def preview_models(body: dict) -> dict:
    """用表单里**还没保存**的端点与密钥去拉上游 /models。

    实现只有一份：core/sources.preview_source（这里以前自己另写了一份，两份行为
    并不一样：sources 那份会按 leaf 别名去重、缺密钥时回"先填 API 密钥再拉取模型"，
    这份直接透传上游文案，所以线上看到的是"缺少密钥"）。前端 config.js 的注释和
    错误分支从一开始就是照 sources 那份写的，留着两份只会让下次改哪一份变成运气。

    这里刻意不再自己从 config 里找同端点的已存密钥：config.js 在编辑模式下没重填
    密钥时压根不发这个请求（它退回去读 /api/state 的快照），而新建模式下的新端点在
    config 里本来就没有条目，那段兜底唯一的效果就是把错误文案换成"缺少密钥"。
    body 里的 group 也一样：唯一的调用方从来不传，留着就是第二条分支。
    """
    return sources.preview_source(body.get('base_url'), body.get('api_key'))


# --------------------------------------------------------------------------- 请求处理


def source_write_result(view, fallback_id: str | None = None) -> dict:
    """POST/PUT /api/sources 的 data 体。字段形状（前端照这个对接）：

        {
          "id": "s-xxxx",                  # 来源 id；PUT 时等于路径里的那个
          "reasoning_sync": {
            "ok": true,                    # catalog 写回成功了没有
            "updated": ["gpt-5-codex"],    # 这次真的改动的 slug；空数组=没有行需要改
            "reason": "catalog 里的推理档位已经和 plan 一致，无需改动"
                                           # 没改动时的原因，改动过或出错时为 null
            "error": null,                 # ok=false 时的失败原因（读/解析/写），成功为 null
            "path": "...\\codex-model-catalog.json"   # 目标文件的绝对路径
          }
        }

    reasoning_sync 是 core/sources._sync_reasoning 挂上来的，以前这里只回 id 就把它丢了：
    catalog 写失败时响应照样是 {"ok": true}，界面照着 "来源已创建" 显示，用户以为档位
    生效了，其实没有。来源本身是建成的（config/plan 都落盘了），所以 ok 仍然是 true，
    但把失败一起交出去，前端才能在 reason/error 非空时提示一句。

    core 没挂（说明这次写入压根没走到同步那一步，不是"同步成功"）时补一条 ok=false，
    免得又变成静默。字段一定存在，前端不用写 data.reasoning_sync && ... 的兜底。
    """
    data = {'id': (view or {}).get('id') or fallback_id}
    report = (view or {}).get('reasoning_sync')
    data['reasoning_sync'] = report if isinstance(report, dict) else {
        'ok': False, 'updated': [], 'reason': None, 'path': None,
        'error': 'core/sources 没有返回推理档位同步结果，这次写入可能没走到同步那一步',
    }
    return data


class Handler(BaseHTTPRequestHandler):
    server_version = 'Prism'
    sys_version = ''
    protocol_version = 'HTTP/1.1'   # 前端每 5 秒轮询一次，连接复用能省一堆 TIME_WAIT
    timeout = 60                    # 半开连接别一直占着线程

    # -- 基础设施 ----------------------------------------------------------

    def log_message(self, fmt, *args):
        log.debug('%s - %s', self.address_string(), fmt % args)

    def log_error(self, fmt, *args):
        # 基类会把这些直接喷到 stderr（桌面应用没控制台，只会污染日志），统一走 logging
        log.debug('%s - %s', self.address_string(), fmt % args)

    def _port(self) -> int:
        return int(getattr(self.server, 'console_port', self.server.server_address[1]))

    def _send(self, status: int, raw: bytes, content_type: str, extra: dict | None = None,
              body: bool = True):
        """body=False 只发头。HEAD 必须走这条路：HTTP 规定 HEAD 的响应不能带正文，
        真写了这堆字节，Content-Length 声明的那些就会留在连接里，HTTP/1.1 keep-alive
        的下一个请求被解析成垃圾（同 _read_body 里超限时关连接的理由）。要不要发正文
        由调用方明说，不再靠在这里偷看 self.command —— 那种写法在基类没实现 do_HEAD
        时永远是死代码，看着像在处理 HEAD，其实一次都没跑过。"""
        try:
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', CSP)
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if body:
                self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def _json(self, status: int, payload: dict, extra: dict | None = None, body: bool = True):
        self._send(status, json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                   'application/json; charset=utf-8', extra, body=body)

    def _ok(self, data, extra: dict | None = None):
        self._json(200, {'ok': True, 'data': data}, extra)

    def _fail(self, status: int, message: str):
        self._json(status, {'ok': False, 'error': message})

    def _check_client(self) -> None:
        """Host 与 Origin 两道闸。照 script/route_selector.py 的 check() 抄，不是形式主义：
        控制台能改用户的网关配置，DNS rebinding 和跨站表单都能把请求送到本机端口上。"""
        port = self._port()
        host = (self.headers.get('Host') or '').strip()
        if host != '127.0.0.1:%d' % port:
            raise HttpError(409, '仅允许本机地址访问（Host 必须是 127.0.0.1:%d，收到 %r）'
                            % (port, host[:60] or '空'))
        origin = (self.headers.get('Origin') or '').strip()
        if origin and origin != 'http://127.0.0.1:%d' % port:
            raise HttpError(409, '拒绝跨站请求（Origin %r 与本机控制台不同源）' % origin[:80])

    def _read_body(self, required: bool):
        raw_len = self.headers.get('Content-Length')
        if raw_len is None:
            if self.headers.get('Transfer-Encoding'):
                self.close_connection = True
                raise HttpError(411, '不支持分块传输，请带上 Content-Length')
            if required:
                raise HttpError(400, '请求体是空的')
            return None
        try:
            length = int(raw_len)
        except (TypeError, ValueError):
            raise HttpError(400, 'Content-Length 不是整数') from None
        if length <= 0:
            if required:
                raise HttpError(400, '请求体是空的')
            return None
        if length > MAX_BODY:
            # 超限时连接里还剩没读完的字节，keep-alive 会错位，直接关掉连接
            self.close_connection = True
            raise HttpError(413, '请求体过大（%d 字节，上限 %d）。来源表单里的模型目录不该有这么大。'
                            % (length, MAX_BODY))
        raw = self.rfile.read(length)
        if not raw:
            if required:
                raise HttpError(400, '请求体是空的')
            return None
        try:
            payload = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            raise HttpError(400, '请求体不是合法 JSON') from None
        if not isinstance(payload, dict):
            raise HttpError(400, '请求体必须是 JSON 对象')
        return payload

    @staticmethod
    def _int_arg(query: dict, name: str, default: int, low: int, high: int) -> int:
        raw = (query.get(name) or [None])[0]
        if raw is None or raw == '':
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise HttpError(400, '参数 %s 必须是整数，收到 %r' % (name, str(raw)[:40])) from None
        if not low <= value <= high:
            raise HttpError(400, '参数 %s 超出范围（%d~%d）：%d' % (name, low, high, value))
        return value

    # -- 方法与分发 --------------------------------------------------------

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')

    def do_PUT(self):
        self._handle('PUT')

    def do_DELETE(self):
        self._handle('DELETE')

    def do_PATCH(self):
        self._unsupported_method('PATCH')

    def do_HEAD(self):
        self._unsupported_method('HEAD')

    def _unsupported_method(self, method: str):
        """没实现的方法也要回 {"ok": false, ...} 的 JSON。

        基类只在收到自己没定义的方法时 send_error(501)，回的是英文 HTML。前端 app.js
        解析响应时先看有没有 ok 字段，HTML 会被判成"响应格式不符合约定"，用户看不到
        "不支持的方法：PATCH"这句真正的原因。PATCH 以前就撞在这个 501 上。

        HEAD 另有一条 HTTP 硬规矩：响应不能带正文，所以这里只发头（body=False）。这个
        判断留在调用点上、由方法名决定；不再让 _send 去偷看 self.command —— 那种写法在
        do_HEAD 不存在时永远是死代码，看着像在处理 HEAD，实际一次都没跑到过。
        """
        head = method == 'HEAD'
        try:
            try:
                self._read_body(False)      # 请求体先读干净，连接里别留残字节
                self._check_client()
                self._json(405, {'ok': False, 'error': '不支持的方法：' + method}, body=not head)
            except HttpError as exc:
                self._json(exc.status, {'ok': False, 'error': exc.message}, body=not head)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception as exc:                      # noqa: BLE001 - HTTP 边界，什么都不能漏出去
            log.error('处理 %s %s 时异常：\n%s', method, self.path, traceback.format_exc())
            self._json(500, {'ok': False, 'error': '控制台内部错误（%s）：%s。完整 traceback 在 %s。'
                             % (type(exc).__name__, _internal_error_hint(exc), PRISM_LOG)},
                       body=not head)

    def do_OPTIONS(self):
        try:
            # 与 _unsupported_method 一样：**先把请求体读干净**再校验，否则被拒的请求会把
            # 没读完的字节留在 keep-alive 连接里，下一个请求被解析成垃圾（同 _read_body
            # 超限时关连接的理由）。浏览器预检不带 body，但别的客户端会带。
            self._read_body(False)
            self._check_client()
        except HttpError as exc:
            return self._fail(exc.status, exc.message)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
            return
        # 同源请求不会真的发预检，这里只是别让某些客户端撞上 501
        self._send(200, b'{}', 'application/json; charset=utf-8',
                   {'Allow': 'GET, POST, PUT, DELETE, OPTIONS'})

    def _handle(self, method: str):
        try:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if len(path) > 1:
                path = path.rstrip('/') or '/'
            query = parse_qs(parsed.query)
            # 先把请求体读干净，再校验 Host/Origin。反过来的话，被拒绝的请求会在连接里
            # 留下没读完的字节，HTTP/1.1 keep-alive 的下一个请求就被解析成垃圾。
            body = self._read_body(False)   # 有的 POST（/api/recheck）本来就不带 body
            self._check_client()
            if not path.startswith('/api/'):
                if method != 'GET':
                    raise HttpError(405, '静态文件只支持 GET')
                return self._serve_static(path)
            # 判 None 而不是判真假：{} 是合法请求体，但它 falsy。前端 app.js 的 post()
            # 在 body 为 undefined 时发的正是 `{}`（`body === undefined ? {} : body`），
            # 用 `not body` 会把这类请求打成 400"请求体是空的"——用户点一下按钮就报错，
            # 而错的原因跟界面上什么都没填没关系。"有没有带 body"是 None 与否的问题。
            if method == 'POST' and body is None and path in ('/api/select', '/api/connect',
                                                              '/api/sources', '/api/settings'):
                raise HttpError(400, '请求体是空的')
            if method == 'PUT' and body is None:
                raise HttpError(400, '请求体是空的')
            self._dispatch(method, path, query, body)
        except HttpError as exc:
            self._fail(exc.status, exc.message)
        except (RouteError, sampling.SamplingError) as exc:
            # 网关不可达/被封、配置冲突、业务校验失败——这些都有明确的中文原因
            self._fail(409, str(exc))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception as exc:                      # noqa: BLE001 - HTTP 边界，什么都不能漏出去
            log.error('处理 %s %s 时异常：\n%s', method, self.path, traceback.format_exc())
            self._fail(500, '控制台内部错误（%s）：%s。完整 traceback 在 %s。'
                       % (type(exc).__name__, _internal_error_hint(exc), PRISM_LOG))

    def _dispatch(self, method: str, path: str, query: dict, body):
        if method == 'GET':
            if path == '/api/state':
                return self._ok(bridge.snapshot())
            if path == '/api/theme':
                # 前端每 5 秒跟一次，系统换主题或手动切主题时不必重启 Prism。
                return self._ok({'theme': effective_theme(), 'system': system_theme()})
            if path == '/api/sources':
                return self._ok(sources.list_sources())
            if path == '/api/usage':
                days = self._int_arg(query, 'days', 7, 1, 365)
                return self._ok(usage_payload(days))
            if path == '/api/monitor':
                return self._ok(health.monitor_state())
            if path == '/api/logs':
                limit = self._int_arg(query, 'limit', DEFAULT_LOG_LINES, 1, MAX_LOG_LINES)
                payload = read_logs(limit)
                # 撞了 MAX_TAIL_BYTES 就少给了行，必须说出来：前端照旧渲染，但会拿这个头
                # 提示"日志太长只显示了一部分"。data.truncated 是同一件事的 JSON 副本，
                # 给不看响应头的调用方用。
                extra = {'X-Prism-Truncated': '1'} if payload['truncated'] else None
                return self._ok(payload, extra)
            if path == '/api/error-logs':
                return self._ok(list_error_logs())
            if path == '/api/settings':
                return self._ok(settings_payload())
            if path == '/api/agents':
                # 5 个客户端的注册信息 + 实测状态（配置文件在不在、base_url 指向哪、
                # 有没有指向本网关）。**纯只读**，不写任何客户端配置。
                return self._ok(agents.list_agents())
            hit = re.match(r'^/api/error-logs/(.+)$', path)
            if hit:
                raw, truncated = read_error_log(hit.group(1))
                extra = {'X-Prism-Truncated': '1'} if truncated else None
                return self._send(200, raw, 'text/plain; charset=utf-8', extra)
            raise HttpError(404, '没有这个接口：GET ' + path)

        if method == 'POST':
            if path == '/api/select':
                return self._ok(bridge.apply_selection(body or {}))
            if path == '/api/connect':
                # body 里的 agent 决定改哪个客户端，缺省 codex（老前端不带这个字段）。
                # codex 仍然走 rs.connect_client，行为一字未改。
                return self._ok(agents.connect_agent(
                    str((body or {}).get('agent') or 'codex'), body or {}))
            if path == '/api/connect/preview':
                # 只算"会改哪几行"，不落盘。界面必须先展示它，用户确认后才发 /api/connect。
                return self._ok(agents.preview_agent(
                    str((body or {}).get('agent') or 'codex')))
            if path == '/api/recheck':
                return self._ok(bridge.recheck_blocked())
            if path == '/api/sources':
                created = sources.create_source(body or {})
                return self._ok(source_write_result(created))
            if path == '/api/sources/preview':
                return self._ok(preview_models(body or {}))
            if path == '/api/settings':
                return self._ok(apply_settings(body or {}))
            hit = re.match(r'^/api/sources/([^/]+)/test$', path)
            if hit:
                return self._ok(sources.test_source(hit.group(1)))
            raise HttpError(404, '没有这个接口：POST ' + path)

        if method == 'PUT':
            # 渠道头单独一条：它是纯 plan 字段，内置来源也要能改（update_source 只让改
            # custom 的行）。必须排在下面那条通配之前——虽然 [^/]+$ 本来就匹配不到
            # 带 /head 的路径，但顺序写清楚，以后加子资源不会踩。
            hit = re.match(r'^/api/sources/([^/]+)/head$', path)
            if hit:
                return self._ok(sources.set_head(hit.group(1),
                                                 (body or {}).get('head')))
            hit = re.match(r'^/api/sources/([^/]+)$', path)
            if hit:
                updated = sources.update_source(hit.group(1), body or {})
                return self._ok(source_write_result(updated, hit.group(1)))
            raise HttpError(404, '没有这个接口：PUT ' + path)

        if method == 'DELETE':
            if path == '/api/logs':
                return self._ok(clear_logs())
            hit = re.match(r'^/api/sources/([^/]+)$', path)
            if hit:
                sources.delete_source(hit.group(1))
                return self._ok({'deleted': hit.group(1)})
            raise HttpError(404, '没有这个接口：DELETE ' + path)

        raise HttpError(405, '不支持的方法：' + method)

    # -- 静态文件 ----------------------------------------------------------

    def _serve_static(self, path: str):
        rel = STATIC_ALIAS.get(path, path.lstrip('/'))
        if rel.startswith('static/'):
            rel = rel[len('static/'):]          # 前端若写成绝对 /static/xxx 也能命中
        if not rel:
            rel = 'index.html'
        candidate = (STATIC_DIR / rel)
        try:
            target = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            raise HttpError(404, '静态文件不存在：%s（找的是 %s）。确认 app/static 目录完整。'
                            % (path, candidate)) from None
        # 目录穿越（../）必须挡住：静态目录之外的文件一个都不许端出去
        if not target.is_file() or not target.is_relative_to(STATIC_DIR.resolve()):
            raise HttpError(404, '静态文件不存在：' + path)
        ctype = MIME.get(target.suffix.lower(), 'application/octet-stream')
        raw = target.read_bytes()
        if target.name == 'index.html':
            # 首屏就把主题定下来，避免"先白后黑"闪一下。定长模式替换，找不到占位就
            # 原样端出去（老 index.html 也不会因此 500）。
            raw = raw.replace(THEME_PLACEHOLDER,
                              b'data-theme="' + effective_theme().encode('ascii') + b'"')
        self._send(200, raw, ctype)


# --------------------------------------------------------------------------- 入口


def _resolve_port(argv) -> int:
    parser = argparse.ArgumentParser(
        prog='server.py',
        description='Prism 控制台 HTTP 服务（默认端口 8318，可用 --port 改）')
    parser.add_argument('--port', type=int,
                        default=int(os.environ.get('PRISM_CONSOLE_PORT') or DEFAULT_PORT),
                        help='监听端口，默认 8318 或环境变量 PRISM_CONSOLE_PORT')
    parser.add_argument('--verbose', action='store_true', help='把每个请求也打到 stderr')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s %(message)s')
    return args.port


def main(argv=None) -> int:
    port = _resolve_port(argv)
    try:
        serve_forever(port)
    except PortBusy as exc:
        # 这条中文提示是给用户看的：端口被占时窗口会静默显示老页面（plan 修正 B8），
        # 所以宁可启动失败也要把原因喊出来
        print('[Prism] ' + str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
