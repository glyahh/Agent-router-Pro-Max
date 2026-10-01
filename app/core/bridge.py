r"""Prism 与既有 route_selector 之间唯一的桥接层。

为什么不把 route_selector.py 搬进 app/core：它有 38 个顶层函数、11 个模块级常量，
以 snapshot/apply_selection 为根的闭包有 33 个函数；搬一层就会让文件里的
ROOT = Path(__file__).resolve().parent.parent 指到 app/，config.yaml、
routing-plan.json、.local-secrets.json、backups 全部找不到。所以这里只做
import + 转调，业务逻辑保持单份。

网关会封本机 IP（实测 D:/My_Agent_Proxy/logs/main.log 2026-09-26 18:00:29-30）：
    18:00:29  401  DELETE /v0/management/logs
    18:00:29  401  POST   /v0/management/logs
    18:00:29  401  DELETE /v0/management/codex-api-key
    18:00:30  401  GET    /v0/management/auth-files
    18:00:30  401  GET    /v0/management/request-error-logs     <- 第 5 次
    18:00:30  403  GET    /v0/management/request-log            <- 0s，开始封禁
    18:00:37  403  GET    /v0/management/nonexistent-xyz        <- 不存在的路径也是 403
封禁期间所有 /v0/management/* 一律 403、0s 返回，连不存在的路径也是 403 而不是
404（17:59:10 未封禁时 /v0/management/quota 还是 404）。也就是说 403 是封禁闸门、
跑在路由匹配之前，不是权限判定。二进制里的文案是
    "IP banned due to too many failed attempts. Try again in %s"

本模块据此定两条铁律：
  1. 403 一律按"封禁或疑似封禁"处理，绝不重试；
  2. 401 是密钥错，抛错即止，并且短时间内不再打网关切身（防止连续 5 次把用户封了）。

这两条以前只有本模块内部知道。用量采样（core/sampling.py 的 mgmt_get）也在打同一个
管理接口，却没有这道闸门：监控页开着时那条轮询路径约 70 秒就能把 5 次失败凑满，把用户
的本机 IP 送进 30 分钟封禁。所以闸门的状态查询对外公开，见 auth_gate()——**任何打
/v0/management/* 的代码路径，发请求前都该先问它一次**。

测试用开关：环境变量 PRISM_GATEWAY_BASE 可把管理接口指向沙箱桩（如
http://127.0.0.1:8390），默认仍是 http://127.0.0.1:8317。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

if getattr(sys, 'frozen', False):
    # 打包后 exe 与 config.yaml / script / auth 同级，数据目录就是 exe 所在目录。
    # 代码进了包，__file__ 会指向 PyInstaller 的解压目录，不能再靠它推算——这是
    # 打包后最容易出事的一处：算错了 config.yaml / auth 一个都找不到，且报错信息
    # 会把人引向临时目录。
    ROOT = Path(sys.executable).resolve().parent
    APP_DIR = ROOT
else:
    APP_DIR = Path(__file__).resolve().parent.parent      # <项目>\app
    ROOT = APP_DIR.parent                                 # <项目>

# 只读资源（static、design/icons）：打包时用 --add-data 放进 _MEIPASS，源码态就在 app\。
# 与 APP_DIR 分开是因为打包后可写数据在 exe 旁边、只读资源在包内，两者不是同一个目录。
BUNDLE_DIR = Path(getattr(sys, '_MEIPASS', APP_DIR))
SCRIPT_DIR = ROOT / 'script'

# route_selector 的启动代码在 if __name__ == '__main__' 保护里，模块级只有常量和一个
# 空的 build_opener，import 无副作用（已逐行核对 script/route_selector.py）。
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import route_selector as rs  # noqa: E402

RouteError = rs.RouteError

if ROOT.resolve() != Path(getattr(rs, 'ROOT', ROOT)).resolve():
    # 两个模块对"配置文件在哪"的判断不一致，后面所有读写都会跑偏，这里直接炸掉。
    raise RouteError(
        'bridge 与 route_selector 的 ROOT 不一致：'
        + str(ROOT) + ' != ' + str(getattr(rs, 'ROOT', None))
    )

GATEWAY_BASE = os.environ.get('PRISM_GATEWAY_BASE', 'http://127.0.0.1:8317').rstrip('/')
GATEWAY_PREFIX = '/v0/management/'
TIMEOUT = 15
DEFAULT_BAN_SECONDS = 1800        # 网关没给时长时的经验值：实测封禁约 30 分钟
AUTH_FAIL_FAST_SECONDS = 30       # 连续 401 后暂停打网关的窗口
AUTH_FAIL_FAST_AT = 2             # 第几次 401 开始暂停

LOCK_PATH = APP_DIR / 'route_selector.lock'
HEX64 = re.compile(r'^[0-9a-f]{64}$')
HTTP_CODE = re.compile(r'HTTP (\d{3})')
# Go 的 time.Duration 文案：1h2m3.5s / 29m59.99s / 45s
GO_DURATION = re.compile(r'(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)')
BAN_MARKERS = ('ip banned', 'too many failed attempts')

PLAN_PATH = ROOT / 'routing-plan.json'
CATALOG_PATH = ROOT / 'codex-model-catalog.json'
# regen_catalog 在遇到模板池里没有的别名时，会把这些新条目追加进模板池
# （route_selector.py:552 `if added: write_json(...codex-model-catalog-templates.json...)`），
# 所以"保存路由"实际会改四份文件。漏掉这一份，补偿时回滚了 catalog 却把被追加过的
# 模板池留在新状态，下一次 regen 就会按"池里本来就有"处理，来源和档位都可能串味。
TEMPLATES_PATH = ROOT / 'codex-model-catalog-templates.json'
# 一次"保存路由"会同时改这四份**文件**，补偿时要一起备、一起核
SWITCH_FILES = ('config.yaml', 'routing-plan.json', 'codex-model-catalog.json',
                'codex-model-catalog-templates.json')
# ─────────────────────────────────────────────────────────────────────────────
# **不变量：一次「保存路由」的写入位置清单。** 改这条链的人先读这里 ——
# 历史上漏过一次（auth 不在补偿清单里，regen 抛错时它停在新值而文案宣称"回滚完成"），
# 而每次"再加一个写入位置"都会重演一次同样的漏。
#
#   1. 网关 config 的两个 section   ← rs.apply_selection 经管理接口 PUT
#   2. routing-plan.json            ← rs.apply_selection 末尾 write_json
#   3. codex-model-catalog.json     ← rs.regen_catalog
#   4. codex-model-catalog-templates.json ← regen 遇到池里没有的别名时追加
#   5. auth/codex-official.json 的 excluded_models ← rs.patch_auth_models
#
# 前四份在 SWITCH_FILES 里（人肉备份 + 逐文件核对）；**第五份只记值、不拷文件**
# （补偿只需要那个值，多拷一份就是把 OAuth 令牌在磁盘上多留一份）。
# 加新的写入位置时：既要在 _state_snapshot/_compensate 里挂上，也要回来改这份清单。
AUTH_NAME = 'auth/codex-official.json'

log = logging.getLogger('prism.bridge')

# 前端 api 封装的 AbortController 默认 15s（static/app.js:157 DEFAULT_TIMEOUT）。互斥体的
# 等待上限必须明显短于它：两者相等时浏览器先掐断，界面显示的是"请求超时…网关可能卡住"，
# 真正的原因（另一个控制端正在写入）根本传不到用户眼前。10s 留出 5s 给后面的读写。
MUTEX_TIMEOUT_MS = 10000


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """绝不跟随重定向。

    为什么必须显式关掉：CPython 的 `HTTPRedirectHandler.redirect_request` 只剔除
    `content-length` / `content-type`，**`Authorization` 会原样复制到新 URL，而且不校验
    主机变化**。管理接口这条路径上带的是**网关管理密钥** —— 拿到它就能改网关配置；
    环回口 8317 上坐着的若不是本项目的网关，一个 302 就能把密钥送到任意主机。

    原先这里复用 `rs.LOCAL_OPENER`（那个 opener 没有 NoRedirect）。冻结文件里的
    `LOCAL_OPENER` / `get_opener()` 要改得走 DEV-RULES D5，所以这里自建一个，
    先把管理密钥这条最高价值的路径堵上。
    """

    def redirect_request(self, *args, **kwargs):
        return None


# 免代理（环回口不能被 Clash 劫持，理由同原有注释）+ 免重定向
MGMT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


# --------------------------------------------------------------------------- 异常

class GatewayError(RouteError):
    """网关客户端自己的错误，仍然是 RouteError，调用方 catch RouteError 一样能接住。"""


class GatewayBanned(GatewayError):
    """本机 IP 被网关封了。remaining_seconds 永远有值（网关没报时长时按经验值 30 分钟）。"""

    def __init__(self, message: str, remaining_seconds: int, estimated: bool = False):
        super().__init__(message)
        self.remaining_seconds = int(remaining_seconds)
        self.estimated = bool(estimated)

    @property
    def retry_after(self) -> int:
        return self.remaining_seconds


class GatewayAuthError(GatewayError):
    """管理密钥被网关拒绝（HTTP 401）。绝不自动重试。"""


# --------------------------------------------------------------------------- 状态

_STATE_LOCK = threading.Lock()
_ban_until = 0.0
_ban_estimated = False
_auth_fail_streak = 0
_auth_fail_until = 0.0
_INTACT_CHECKED = False
# 串起指纹文件的"读-判-写"。见 ensure_route_selector_intact 的注释。
_LOCK_FILE_LOCK = threading.Lock()


def _human(seconds: int) -> str:
    m, s = divmod(max(0, int(seconds)), 60)
    return (str(m) + ' 分 ' + str(s) + ' 秒') if m else (str(s) + ' 秒')


def _ban_message(seconds: int, estimated: bool) -> str:
    tail = '（网关未报出剩余时长，按实测经验 30 分钟计）' if estimated else ''
    return ('本机 IP 已被网关封禁' + tail + '，剩余约 ' + _human(seconds)
            + '。封禁期间所有管理接口一律 403，请等待自动解除，不要重启网关。')


def _arm_ban(seconds: int, estimated: bool) -> None:
    global _ban_until, _ban_estimated
    with _STATE_LOCK:
        remaining = max(1, int(seconds))
        # 只在更晚的时候延长，避免短窗口把已知的长封禁覆盖掉
        if time.time() + remaining > _ban_until:
            _ban_until = time.time() + remaining
            _ban_estimated = estimated


def _auth_paused_message(seconds: int) -> str:
    return ('管理密钥无效（HTTP 401）。为免连续失败触发网关封禁，已暂停 '
            + str(seconds) + ' 秒再试；请核对 '
            + str(ROOT / '.local-secrets.json') + ' 里的 management_key。')


def auth_gate() -> dict:
    """当前的"禁打网关"窗口。**任何打 8317 管理接口的调用，发请求前都要先看它。**

    网关会把连续 5 次失败（401，且不分是谁打来的）当成攻击，直接封本机 IP 30 分钟。
    这条闸门以前只有 bridge 自己知道：用量采样（sampling.mgmt_get）那条轮询路径完全
    不查，监控页开着时约 70 秒就能把闸门凑满。所以状态和查询都在这里，谁都能问。

    签名与返回（本轮对外的正式接口，sampling.mgmt_get 照这个用）::

        auth_gate() -> {
            'blocked': bool,                # True = 现在不要打网关
            'kind': 'ban' | 'auth' | None,  # ban=IP 封禁窗口；auth=401 失败暂停窗口
            'remaining_seconds': int,       # 还剩多少秒；blocked 为 False 时是 0
            'estimated': bool,              # 剩余秒数是不是经验估算（网关没报时长）
            'message': str,                 # 可直接给用户看的中文说明；未 blocked 时是 ''
        }

    典型用法::

        gate = bridge.auth_gate()
        if gate['blocked']:
            raise SamplingError(gate['message'])   # 不要改成"我先试一次再说"

    只读本地计数，不打网络，可以随便调。返回的是快照，不保证下一秒还成立；真正的
    权威判断仍在 bridge 内部每次发包前自己做一遍。
    """
    with _STATE_LOCK:
        ban_left = _ban_until - time.time()
        ban_estimated = _ban_estimated
        # 暂停窗口只在失败次数到阈值后才算数，与 _note_auth_failure 的口径一致
        auth_left = (_auth_fail_until - time.time()
                     if _auth_fail_streak >= AUTH_FAIL_FAST_AT else 0.0)
    if ban_left > 0:
        seconds = max(1, int(math.ceil(ban_left)))
        return {'blocked': True, 'kind': 'ban', 'remaining_seconds': seconds,
                'estimated': ban_estimated, 'message': _ban_message(seconds, ban_estimated)}
    if auth_left > 0:
        seconds = max(1, int(math.ceil(auth_left)))
        return {'blocked': True, 'kind': 'auth', 'remaining_seconds': seconds,
                'estimated': False, 'message': _auth_paused_message(seconds)}
    return {'blocked': False, 'kind': None, 'remaining_seconds': 0,
            'estimated': False, 'message': ''}


def note_gate_success() -> None:
    """bridge 之外的调用者（如 sampling）报告"刚才那次网关调用成功了"。

    只清 401 暂停窗口。封禁窗口不在这里解除——它是等出来的，不是试出来的。
    """
    _note_success()


def note_gate_auth_failure() -> None:
    """bridge 之外的调用者报告"刚才被 401 拒了"。累计到阈值就开暂停窗口。"""
    _note_auth_failure()


def note_gate_banned(seconds: int | None = None) -> int:
    """bridge 之外的调用者报告"刚才吃到 403 封禁"。返回封禁窗口剩余秒数。

    seconds 为 None 时按经验值 DEFAULT_BAN_SECONDS（实测约 30 分钟）计，并标记为估算。
    """
    if seconds is None:
        _arm_ban(DEFAULT_BAN_SECONDS, True)
    else:
        _arm_ban(seconds, False)
    with _STATE_LOCK:
        left = _ban_until - time.time()
    return max(1, int(math.ceil(left)))


def _raise_if_banned() -> None:
    gate = auth_gate()
    if not gate['blocked']:
        return
    if gate['kind'] == 'ban':
        raise GatewayBanned(gate['message'], gate['remaining_seconds'], gate['estimated'])
    raise GatewayAuthError(gate['message'])


def _note_success() -> None:
    global _auth_fail_streak, _auth_fail_until
    with _STATE_LOCK:
        _auth_fail_streak = 0
        _auth_fail_until = 0.0


def _note_auth_failure() -> None:
    global _auth_fail_streak, _auth_fail_until
    with _STATE_LOCK:
        _auth_fail_streak += 1
        if _auth_fail_streak >= AUTH_FAIL_FAST_AT:
            _auth_fail_until = time.time() + AUTH_FAIL_FAST_SECONDS


# --------------------------------------------------------------- route_selector 指纹

def route_selector_path() -> Path:
    return SCRIPT_DIR / 'route_selector.py'


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _read_lock() -> dict | None:
    if not LOCK_PATH.exists():
        return None
    try:
        data = json.loads(LOCK_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise RouteError(
            '指纹文件损坏：' + str(LOCK_PATH) + '。请确认 route_selector.py 未被改动后删除它重建。'
        ) from None
    if not isinstance(data, dict) or not HEX64.match(str(data.get('sha256', ''))):
        raise RouteError('指纹文件内容无效：' + str(LOCK_PATH) + '。删除它可重建。')
    # path 字段以前只写不验：打包后 APP_DIR 与 ROOT 都是 exe 目录，但源码态跑过之后
    # 又会留下另一份写在 app\ 的指纹。两份指纹并存时，用旧部署那一份去比对当前
    # route_selector.py 的哈希，结论没有任何意义（可能假通过，也可能假报"被改动"）。
    recorded_path = str(data.get('path') or '')
    current_path = str(route_selector_path())
    if recorded_path != current_path:
        raise RouteError(
            '指纹文件记录的 route_selector.py 路径是 ' + (recorded_path or '（空）')
            + '，当前是 ' + current_path + '。这份指纹来自另一次部署（例如打包版 vs 源码态），'
            '拿它比对哈希没有意义。请删除 ' + str(LOCK_PATH) + ' 后重新启动，'
            '让指纹按当前部署重建。'
        )
    return data


def _write_lock(digest: str, size: int) -> None:
    payload = {
        'sha256': digest,
        'bytes': size,
        'path': str(route_selector_path()),
        'recorded_at': datetime.now().isoformat(timespec='seconds'),
        'note': '首次运行记录。改动 script/route_selector.py 后需删掉本文件重建，或调用 record_route_selector_fingerprint() 显式接受新版本。',
    }
    # 临时名带 pid + 线程号：多个进程同时首启时不会互相盖掉对方的半成品，
    # 同进程的多个线程也不会抢同一个临时文件（原先只有 pid，同进程内是同一个名字）。
    temp = LOCK_PATH.with_suffix(
        LOCK_PATH.suffix + '.' + str(os.getpid()) + '.' + str(threading.get_ident()) + '.tmp')
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, LOCK_PATH)
    except OSError as exc:
        # 盘满、目录只读、杀软锁住文件都会走到这里。裸 OSError 会以 500 冒到界面上，
        # 用户看到的是一串英文，看不出该去查什么。
        try:
            temp.unlink()
        except OSError:
            pass
        raise RouteError(
            '写入指纹文件失败：' + str(LOCK_PATH) + '（' + type(exc).__name__ + ': '
            + str(exc) + '）。请检查该目录是否可写、磁盘是否已满。'
        ) from None


def ensure_route_selector_intact() -> None:
    """校验 route_selector.py 存在且哈希与首次运行的记录一致；不一致直接抛错，不静默降级。"""
    # "查看文件在不在 → 算哈希 → 读指纹 → 没有就写"这一串不是原子的。同进程两个线程
    # 同时首启时，两边都看到文件不存在、都去写临时文件，先完成的那次 os.replace 之后，
    # 后一次的 os.replace 找不到临时文件，抛的是裸 FileNotFoundError（实测 30 轮 × 4 线程
    # 里异常 29 次）。模块级锁把整串串起来；跨进程由临时名的 pid 段兜住。
    with _LOCK_FILE_LOCK:
        _ensure_route_selector_intact_locked()


def _ensure_route_selector_intact_locked() -> None:
    path = route_selector_path()
    if not path.is_file():
        raise RouteError('缺少业务逻辑文件：' + str(path) + '，Prism 无法工作。')
    try:
        digest = _sha256_file(path)
        size = path.stat().st_size
    except OSError as exc:
        raise RouteError(
            '读取业务逻辑文件失败：' + str(path) + '（' + type(exc).__name__ + ': '
            + str(exc) + '）。'
        ) from None
    lock = _read_lock()
    if lock is None:
        _write_lock(digest, size)
        return
    if lock['sha256'] != digest:
        raise RouteError(
            'script/route_selector.py 与首次运行时的版本不一致（可能被改动或升级）。'
            'Prism 依赖它的既有行为，请恢复原文件；确认新版本可用就删除 '
            + str(LOCK_PATH) + ' 后重新启动，或显式调用 record_route_selector_fingerprint()。'
        )


def record_route_selector_fingerprint() -> dict:
    """显式接受当前版本：重写指纹文件。只应由用户确认后调用。"""
    with _LOCK_FILE_LOCK:
        path = route_selector_path()
        if not path.is_file():
            raise RouteError('缺少业务逻辑文件：' + str(path))
        try:
            digest = _sha256_file(path)
            size = path.stat().st_size
        except OSError as exc:
            raise RouteError(
                '读取业务逻辑文件失败：' + str(path) + '（' + type(exc).__name__ + ': '
                + str(exc) + '）。'
            ) from None
        _write_lock(digest, size)
        return {'sha256': digest, 'bytes': size, 'path': str(path)}


# --------------------------------------------------------------------------- 启动体检

_FIRST_RUN_CONFIG = """\
# Prism 首启生成的最小网关配置。上游来源为空：在控制台里加来源后这里会长出来。
host: "127.0.0.1"
port: 8317
debug: false
auth-dir: '%(auth_dir)s'
remote-management:
  allow-remote: false
  # 明文写入是网关的官方约定（config.example.yaml：plaintext will be hashed on
  # startup），网关启动时自己哈希——不必也不该在 Prism 里引入 bcrypt 依赖。
  secret-key: "%(management_key)s"
api-keys:
  - "%(api_key)s"
codex-api-key: []
openai-compatibility: []
"""


def ensure_first_run_files() -> list[str]:
    """首启引导（HI-03）：缺 .local-secrets.json / config.yaml 时生成最小可用集。

    只生成缺失的文件，**绝不覆盖已存在文件**——那里面是用户的凭据。两把密钥一次
    生成、两处落盘：management_key 同时进 .local-secrets.json（Prism 读它发 Bearer）
    和 config.yaml 的 remote-management.secret-key（网关启动时哈希），api_key 同时
    进 .local-secrets.json（agents 接代理端用）和 config.yaml 的 api-keys（网关放行
    清单）——两边天然一致，不存在「密钥对不上」的首启死局。

    半初始化（config.yaml 在而 .local-secrets.json 缺）**不自动补**：网关侧的
    management key 已经是 bcrypt，Prism 造不出配对的新 key，乱写只会得到一连串
    401 并触发网关的 IP 封禁。只记日志，让人工处理。
    返回新建文件的文件名清单（空 = 已初始化过，一个字节没动）。
    """
    created: list[str] = []
    secrets_path = ROOT / '.local-secrets.json'
    config_path = ROOT / 'config.yaml'
    # 两个文件各自独立判断（不是 if/elif 链——全新首启两件都要生成）
    if not secrets_path.exists():
        if config_path.exists():
            # 半初始化 A：config 在而 secrets 缺。网关侧的 management key 已经是
            # bcrypt，Prism 造不出配对的新 key——乱写只会得到一连串 401 并触发
            # 网关的 IP 封禁。只记日志，让人工处理。
            log.warning('半初始化：%s 在而 %s 缺，management key 无法配对生成，请人工恢复后者。',
                        config_path.name, secrets_path.name)
        else:
            payload = {'management_key': 'prism-' + secrets.token_urlsafe(24),
                       'api_key': 'sk-prism-' + secrets.token_urlsafe(24)}
            if _atomic_write_json(secrets_path, payload):
                created.append(secrets_path.name)
    if not config_path.exists():
        # 补 config 一律**读磁盘**上的 secrets（哪怕这份 secrets 是刚写或并发实例
        # 写的）——两处密钥天然一致，不存在「config 是 A 的 key、secrets 是 B 的
        # key」的死局（复查 M-2）。
        try:
            stored = json.loads(secrets_path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            stored = None
        if isinstance(stored, dict) and stored.get('management_key'):
            text = _FIRST_RUN_CONFIG % {'auth_dir': str(ROOT / 'auth'),
                                        'management_key': stored['management_key'],
                                        'api_key': stored.get('api_key') or ''}
            if _atomic_write_text(config_path, text):
                created.append(config_path.name)
        else:
            # 半初始化 B：secrets 在但里面没有可用的 management_key
            log.warning('半初始化：%s 缺，而 %s 里读不到 management_key——请人工处理，不自动生成。',
                        config_path.name, secrets_path.name)
    try:
        (ROOT / 'auth').mkdir(exist_ok=True)   # 网关的 auth-dir 指这里，缺失会让首次登录没地方落
    except OSError:
        pass
    return created


def _atomic_write_json(path: Path, payload: dict) -> bool:
    """原子写。replace 前复查目标是否存在：并发实例抢先落盘时放弃并撤掉临时文件，
    绝不覆盖（HI-03 的承诺；检查-写入之间的窗口收窄到一次系统调用内也无妨，
    os.replace 本身是原子的，复查挡的是「检查之后别人写了」的交错）。"""
    temp = path.with_suffix(path.suffix + '.%d.tmp' % os.getpid())
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    if path.exists():
        temp.unlink(missing_ok=True)
        return False
    os.replace(temp, path)
    return True


def _atomic_write_text(path: Path, text: str) -> bool:
    temp = path.with_suffix(path.suffix + '.%d.tmp' % os.getpid())
    temp.write_text(text, encoding='utf-8')
    if path.exists():
        temp.unlink(missing_ok=True)
        return False
    os.replace(temp, path)
    return True


def latest_switch_backup() -> Path | None:
    """backups\\ 下最近的 route-switch-* 目录（恢复时的第一站）。

    目录名是 route-switch-<时间戳>-<随机段>，时间戳定宽，字典序就是时间序。
    """
    backups = ROOT / 'backups'
    if not backups.is_dir():
        return None
    try:
        dirs = sorted(p for p in backups.iterdir()
                      if p.is_dir() and p.name.startswith('route-switch-'))
    except OSError:
        return None
    return dirs[-1] if dirs else None


def verify_startup_files() -> list[str]:
    """启动体检（ME-03）：检测「PUT 成功与落盘之间被强杀」留下的半写状态。

    一次「保存路由」会原子地改 SWITCH_FILES 四份文件；进程在途中被杀，它们可能停在
    互相矛盾的状态。这里在拉网关之前做一次**只读**核对，返回问题清单（空 = 健康）：

    * JSON 三份（plan / catalog / templates）必须能解析 —— 截断是半写的最强信号；
    * plan 顶层必须是对象、providers 必须是列表、selected 引用的 id 必须存在
      （矛盾说明四份文件停在了不同的时刻）；
    * config.yaml 只查非空 —— Prism 不解析 YAML（PyYAML 不在打包依赖里），它真正的
      读者是网关（Go）；解析错误网关自己起不来，ensure_gateway 会兜住。
    """
    problems: list[str] = []
    plan: dict | None = None
    for name in SWITCH_FILES:
        path = ROOT / name
        if not path.is_file():
            continue                      # 首次启动缺失不算半写（生成是 Init 的活）
        if name == 'config.yaml':
            try:
                if path.stat().st_size == 0:
                    problems.append('config.yaml 是空文件（可能半写）')
            except OSError as exc:
                problems.append('config.yaml 读不了：%s' % str(exc)[:80])
            continue
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            problems.append('%s 解析失败（JSON 截断？）：%s' % (name, str(exc)[:80]))
            continue
        if name == 'routing-plan.json':
            if isinstance(payload, dict):
                plan = payload
            else:
                problems.append('routing-plan.json 顶层不是 JSON 对象')
    if plan is not None:
        problems.extend(_plan_consistency(plan))
    return problems


def _plan_consistency(plan: dict) -> list[str]:
    """plan 内部自洽：selected（新旧两种形状）引用的 id 必须存在于 providers。"""
    problems: list[str] = []
    providers = plan.get('providers')
    if not isinstance(providers, list):
        return ['routing-plan.json 的 providers 不是列表']
    ids = {row.get('id') for row in providers
           if isinstance(row, dict) and isinstance(row.get('id'), str)}
    selected = plan.get('selected')
    if selected is None:
        return problems
    if not isinstance(selected, dict):
        return problems + ['routing-plan.json 的 selected 不是对象']
    for group, picks in selected.items():
        if picks is None:
            continue                      # 组内 null = 未选，合法形状（route_selector._selected_ids 同口径）
        if isinstance(picks, str):        # 旧版单字符串形状：/api/select 两种都吃，这里同样
            picks = [picks]
        if not isinstance(picks, list):
            problems.append('routing-plan.json 的 selected.%s 既不是数组也不是字符串' % group)
            continue
        for pid in picks:
            if pid not in ids:
                problems.append('routing-plan.json 的 selected.%s 引用了不存在的来源 id：%r'
                                % (group, pid))
    return problems


def _ensure_checked() -> None:
    """进程内首次碰到网关调用时校验一次，免得 server.py 忘了显式调用。"""
    global _INTACT_CHECKED
    with _STATE_LOCK:
        if _INTACT_CHECKED:
            return
    ensure_route_selector_intact()
    with _STATE_LOCK:
        _INTACT_CHECKED = True


# --------------------------------------------------------------------------- 网关客户端

def _management_key() -> str:
    path = ROOT / '.local-secrets.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        raise RouteError('缺少 ' + str(path) + '，读不到网关管理密钥。') from None
    except (OSError, ValueError):
        raise RouteError('.local-secrets.json 读取失败或格式无效，无法取得管理密钥。') from None
    key = data.get('management_key') if isinstance(data, dict) else None
    if not isinstance(key, str) or not key:
        raise RouteError('.local-secrets.json 里没有 management_key，无法访问网关管理接口。')
    return key


def _url(path: str) -> str:
    """只允许打本机管理接口。path 可以带或不带 /v0/management 前缀。"""
    p = str(path or '').strip()
    if p.lower().startswith(('http://', 'https://')):
        raise RouteError('gateway_* 只接受管理接口的相对路径，不接收完整 URL')
    p = p.lstrip('/')
    if p == 'v0/management':
        p = ''
    elif p.startswith('v0/management/'):
        p = p[len('v0/management/'):]
    return GATEWAY_BASE + GATEWAY_PREFIX + p


def _error_body(exc) -> str:
    try:
        raw = exc.read(64 * 1024)
    except Exception:
        return ''
    if not raw:
        return ''
    return ' '.join(raw.decode('utf-8', 'replace').split())


def _ban_evidence(text: str) -> tuple[bool, int | None]:
    """返回 (是否封禁, 剩余秒数)。秒数从 "Try again in 29m59.9s" 里抠。"""
    low = text.lower()
    hits = [low.rfind(marker) for marker in BAN_MARKERS]
    hits = [i for i in hits if i >= 0]
    if not hits:
        return False, None
    m = GO_DURATION.search(low[min(hits):])
    if not m:
        return True, None
    hours, minutes, seconds = m.groups()
    total = int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds or 0)
    return True, max(1, int(math.ceil(total)))


def _retry_after_header(exc) -> int | None:
    try:
        raw = exc.headers.get('Retry-After')
    except Exception:
        return None
    if not raw:
        return None
    try:
        return max(1, int(float(raw)))
    except (TypeError, ValueError):
        return None


def _raise_http_error(exc):
    code = getattr(exc, 'code', 0)
    text = _error_body(exc)
    if code == 401:
        _note_auth_failure()
        raise GatewayAuthError(
            '网关拒绝管理密钥（HTTP 401）：' + (text[:200] or '无响应体')
            + '。不会自动重试；请核对 ' + str(ROOT / '.local-secrets.json') + ' 里的 management_key。'
        ) from None
    if code == 403:
        # 403 在实测里只出现在封禁期间（未封禁时错误的管理请求是 401、未知路径是 404），
        # 所以这里一律按封禁处理，宁可少打网关也不冒再被封一次的风险。
        confirmed, remaining = _ban_evidence(text)
        if remaining is None:
            remaining = _retry_after_header(exc)
        estimated = remaining is None
        seconds = remaining if remaining is not None else DEFAULT_BAN_SECONDS
        _arm_ban(seconds, estimated)
        note = '' if confirmed else '（响应体 ' + (text[:120] or '为空') + '）'
        raise GatewayBanned(_ban_message(seconds, estimated) + note, seconds, estimated) from None
    detail = ('：' + text[:200]) if text else ''
    raise RouteError('网关管理接口返回 HTTP ' + str(code) + detail) from None


def _timeout_error(elapsed: float) -> RouteError:
    """"网关慢"和"网关没开"是两件事，文案也必须分开。

    socket.timeout 从 3.10 起是 OSError 的子类，原先那句 `except (URLError, OSError,
    ValueError)` 会把它一起吞掉，于是网关明明在跑、只是卡了 15 秒，用户看到的却是
    "无法连接本地网关…请先启动 CLIProxyAPI"。照着这句话去重启网关，恰好是 bridge 自己
    在封禁文案里反复叮嘱"不要重启"的那件事。
    """
    return RouteError(
        '本地网关 ' + GATEWAY_BASE + ' 对管理接口 ' + str(TIMEOUT) + ' 秒没有响应'
        '（实际等了 ' + str(round(elapsed, 1)) + ' 秒）：可能卡住，或正在处理大请求。'
        '网关进程本身是活着的，不要重启它（重启会清掉它自己的封禁计数和正在跑的请求）。'
        '稍后重试，或先看网关日志确认卡在哪个请求上。'
    )


def _request(method: str, path: str, data=None, params: dict | None = None) -> dict:
    _ensure_checked()
    _raise_if_banned()
    url = _url(path)
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += '?' + urllib.parse.urlencode(clean)
    body = None
    headers = {'Authorization': 'Bearer ' + _management_key(), 'Accept': 'application/json'}
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=body, method=method.upper(), headers=headers)
    started = time.monotonic()
    try:
        # 管理接口在环回口：免代理（免得 config.yaml 的 proxy-url 把它带走）+ 免重定向
        # （免得 3xx 把管理密钥复制到别的域；见 MGMT_OPENER 的说明）
        with MGMT_OPENER.open(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        _raise_http_error(e)
    except (TimeoutError, socket.timeout):
        # 读超时/http.client 的套接字超时走这条
        raise _timeout_error(time.monotonic() - started) from None
    except urllib.error.URLError as exc:
        # urllib 的 do_open 里 `except OSError as err: raise URLError(err)`，连接阶段的
        # 超时会被包成 URLError 而不是直接冒出来，所以这里要往 .reason 里再看一层。
        if isinstance(getattr(exc, 'reason', None), (TimeoutError, socket.timeout)):
            raise _timeout_error(time.monotonic() - started) from None
        raise RouteError(
            '无法连接本地网关 ' + GATEWAY_BASE + '，请先启动 CLIProxyAPI。'
        ) from None
    except (OSError, ValueError):
        raise RouteError(
            '无法连接本地网关 ' + GATEWAY_BASE + '，请先启动 CLIProxyAPI。'
        ) from None
    _note_success()
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise RouteError('网关管理接口返回了非 JSON 响应（HTTP 200）') from None
    return payload


def gateway_get(path: str) -> dict:
    """GET /v0/management/<path>。带 Bearer 鉴权；403 判定为封禁并抛 GatewayBanned；401 绝不重试。"""
    return _request('GET', path)


def gateway_put(path: str, data) -> dict:
    return _request('PUT', path, data=data)


def gateway_delete(path: str, params: dict | None = None) -> dict:
    return _request('DELETE', path, params=params)


# --------------------------------------------------------------- 跨进程写入互斥

def _sources():
    """延迟导入 sources。它在模块级 `from . import bridge`，这里顶层 import 会成环。"""
    from . import sources
    return sources


def acquire_rs_lock(timeout_ms=MUTEX_TIMEOUT_MS) -> None:
    """拿 route_selector 的进程内 LOCK，**带超时**。拿到了才有释放的责任。

    rs.LOCK 是裸 threading.Lock，`acquire()` 不带超时就是无限等。sources._Transaction
    原先就是这么拿的：与 10s（跨进程互斥体）/ 15s（前端 AbortController）两段预算完全
    脱节，实测等了 60.1 秒才拿到。更糟的是浏览器早在 15s 就掐断了，服务端线程还在等，
    等到了照样往下写 —— 用户以为失败、重点一次，就得到两条同端点的来源条目。

    等待上限与跨进程互斥体一致（MUTEX_TIMEOUT_MS），失败文案也一致，用户在不同步骤
    卡住时看到的是同一句话。
    """
    if rs.LOCK.acquire(timeout=max(0.0, timeout_ms / 1000.0)):
        return
    raise RouteError('另一个控制端正在写入路由配置（已等 %d 秒），请稍后重试'
                     % int(timeout_ms // 1000))


@contextmanager
def _named_mutex(timeout_ms=MUTEX_TIMEOUT_MS):
    """sources 那套命名互斥体（Global\\PrismRoutingPlan）的裸入口：只拿跨进程锁，
    不碰 rs.LOCK —— rs.apply_selection / rs.recheck_blocked 内部自己 `with LOCK`，
    这里再拿一次会自死锁（threading.Lock 不可重入）。

    为什么必须有它：rs.write_json 的临时名固定是 `<path>.tmp`，而 8318 的
    route_selector 进程写的是同一份 routing-plan.json。实测两个进程各写 300 次，
    无锁时 93% 报 WinError 32 / Errno 13（抢同一个临时文件）。"""
    src = _sources()
    handle = src._acquire(timeout_ms)
    try:
        yield handle
    finally:
        src._release(handle)


# --------------------------------------------------------------- route_selector 转调

def _translate(exc: RouteError) -> RouteError:
    """rs 内部只抛 '网关接口 HTTP 403'，看不出是不是封禁；把同一信号翻译回来。"""
    m = HTTP_CODE.search(str(exc))
    if not m:
        return exc
    code = int(m.group(1))
    if code == 401:
        _note_auth_failure()
        return GatewayAuthError(
            '网关拒绝管理密钥（HTTP 401）。不会自动重试；请核对 '
            + str(ROOT / '.local-secrets.json') + ' 里的 management_key。'
        )
    if code == 403:
        with _STATE_LOCK:
            left = _ban_until - time.time()
        if left > 0:
            seconds, estimated = max(1, int(math.ceil(left))), _ban_estimated
        else:
            # 只有 rs 那一侧报的 403，本地没有观测到时长，按经验值算并标记为估算
            seconds, estimated = DEFAULT_BAN_SECONDS, True
            _arm_ban(seconds, estimated)
        return GatewayBanned(_ban_message(seconds, estimated), seconds, estimated)
    return exc


def _with_tail(err: RouteError, tail: str) -> RouteError:
    """给翻译后的错误（含 GatewayBanned / GatewayAuthError）接一段补充说明。
    封禁类错误的文案是 _ban_message 现生成的，直接 raise 会把补偿结果丢掉。"""
    if not tail:
        return err
    message = str(err) + tail
    if isinstance(err, GatewayBanned):
        return GatewayBanned(message, err.remaining_seconds, err.estimated)
    return type(err)(message)


def _forward(fn, *args, **kwargs):
    """转调 route_selector 的入口函数。

    刻意不自己组装 fetch_all -> build_config -> regen_catalog 那条链：漏掉 fetch_all
    会让 build_config 从残缺的 plan 读出空 picked，静默清空用户勾选的模型且不抛异常。
    那条链只有 rs.apply_selection 里那一份是对的。
    """
    _ensure_checked()
    _raise_if_banned()
    try:
        return fn(*args, **kwargs)
    except GatewayError:
        raise
    except RouteError as e:
        raise _translate(e) from None


def snapshot() -> dict:
    """当前完整状态（selected / providers / models / revision），直连 rs.snapshot。"""
    return _forward(rs.snapshot)


def enabled_groups() -> dict:
    """`{分组: [来源 id]}` —— **只读本地，不发任何出站请求**。

    为什么单开一个：`snapshot()` → `rs.snapshot()` → `rs.fetch_all()` 会对**每一个**来源
    发一轮 live `/models`（8 并发、单请求 12 秒超时）。而托盘提示每 60 秒就要问一次"现在
    用的是哪几家"，于是窗口收进托盘之后仍在持续对全部上游（含第三方中转站）发出站请求，
    可能触碰上游限流 —— 审查里的 **HI-05**。

    判据与 `rs.snapshot()` 的 `selected` **同源**：用的是 `sources._enabled_ids`，它内部
    就是 `rs.active` / `rs.auth_active`（它自己的 docstring 也这么写着）。这里不重写一份，
    免得"界面说在用的"和"这里认为在用的"分叉。

    唯一的网络访问是 `gateway_get('config')`（一个环回管理接口请求）—— 那是权威状态，
    无法避免。它失败时按原样抛，调用方（托盘）已经有兜底文案。
    """
    plan = rs.read_json(rs.ROOT / 'routing-plan.json')
    config = gateway_get('config')
    enabled = _sources()._enabled_ids(config, plan)
    out = {group: [] for group in rs.GROUPS}
    for row in plan.get('providers') or []:
        if not isinstance(row, dict):
            continue
        group = row.get('group')
        if row.get('id') in enabled and group in out:
            out[group].append(row['id'])
    return out


def revision() -> str:
    """当前配置指纹，用于"保存前先比对"的并发守卫。"""
    return rs.revision(gateway_get('config'))


def apply_selection(payload: dict) -> dict:
    """保存路由。payload = {revision, selected:{gpt,deepseek,glm}, picks:{providerId:[alias]}}。

    整段包在跨进程互斥体里：rs.apply_selection 会写 routing-plan.json，而这台机器上
    8318 的 route_selector 进程写的是同一份文件（用户机器上实测 PID 20068 正在跑）。
    互斥体只在这里拿一次，rs 内部自己的 LOCK 由它自己拿。
    """
    with _named_mutex():
        return _apply_selection_locked(payload)


def _assert_selection_heads(payload: dict) -> None:
    """保存路由前先过渠道头的不变量（见 sources.plan_head_conflicts）。

    放在这里而不是 route_selector.build_config 里，有两个原因：
      1. 那是哈希锁定的存量文件，越少动越好；
      2. 完整判据（含与 rs.active 对齐的"哪些来源算生效"）已经在 sources.py 里了，
         在那里再抄一份必然漂移。

    时机是关键：这段跑在 rs.apply_selection 之前，所以 config.yaml / routing-plan.json
    一个字节都还没动，拒绝是干净的 —— 不需要 _compensate() 那一套回滚。

    "要同时启用的这家来源"直接由 payload 的 selected 给出，不去读网关的实时状态：
    用户点的就是这一次要生效的集合。
    """
    selected = payload.get('selected')
    if not isinstance(selected, dict):
        return                       # 形状不对不是这里的事，交给 rs.build_config 报
    enabled = set()
    for value in selected.values():
        if isinstance(value, str):
            enabled.add(value)
        elif isinstance(value, (list, tuple)):
            enabled.update(x for x in value if isinstance(x, str))
    src = _sources()
    try:
        plan = src._read_plan()
    except Exception as exc:                       # noqa: BLE001
        # **只有**"读 plan 都失败"这一种情况才跳过：rs.apply_selection 自己会报出真正的错，
        # 没必要在这里造一个新的失败面；但也别静默，留一条 warn。
        # （`_sources()` 故意留在 try 外面：它是导入本模块的邻居，抛异常说明代码本身坏了，
        #  那种情况必须上抛，不该被这条 warn 吞掉 —— 与上面这句注释保持一致。）
        log.warning('渠道头预检跳过：读 plan 失败（%s: %s）', type(exc).__name__, exc)
        return
    providers = plan.get('providers') or []
    # **判据对象必须 == 本次将要落盘的对象。** D2 读的是每一行的 `expose`（见
    # sources.row_client_ids），而 `rs.apply_selection` 会先用 payload 的 picks 覆盖它 ——
    # 不合并的话，这里判的是**盘上那一次**的勾选，不是这次要写下去的（审查 ME-01）。
    # 用"提交的勾选"整份合并，比真实写入的（submitted ∩ available）更保守：
    # 多判出来的 ID 只可能更严格，不会放过真实冲突。
    picks = payload.get('picks')
    if isinstance(picks, dict) and picks:
        merged = []
        for row in providers:
            if isinstance(row, dict) and row.get('id') in picks:
                names = picks[row['id']]
                if isinstance(names, (list, tuple)):
                    row = dict(row, expose=[str(n) for n in names])
            merged.append(row)
        providers = merged
    # 判据本身的任何异常都不许吞：这是一道写盘前的闸门，静默跳过就等于没有闸门。
    # （曾经 _assert_heads_ok 也被包在同一个 except 里 → 不变量可以静默失效继续写盘。）
    src._assert_heads_ok(providers, enabled)


def _apply_selection_locked(payload: dict) -> dict:
    _ensure_checked()
    _raise_if_banned()
    _assert_selection_heads(payload)
    snap = _state_snapshot()
    try:
        result = rs.apply_selection(payload)
    except Exception as exc:                       # noqa: BLE001 - StopIteration 也要兜住
        lines = _compensate(snap)
        _raise_failure(exc, lines)
    _sync_catalog_or_loud(result)
    return result


def connect_client(payload: dict) -> dict:
    """把本机 Codex 接到本地网关。payload = {revision}。"""
    return _forward(rs.connect_client, payload)


def recheck_blocked() -> dict:
    """重测被标记 blocked 的来源。它写 routing-plan.json，同样要走跨进程互斥。"""
    with _named_mutex():
        return _forward(rs.recheck_blocked)


# ------------------------------------------------- 保存失败的现场核对与补偿
#
# rs.apply_selection 把 regen_catalog 放在自己的 try/except **之后**（route_selector.py:375）：
# 一旦 regen 抛错（模板池被改坏 → StopIteration、磁盘满、os.replace 撞上另一个写手），
# config.yaml 已经是新值、routing-plan.json 还是旧值、catalog 没生成，而且这一步没有
# 任何回滚（rs.backup 只复制 config.yaml，plan/catalog 无备份）。下一次点"保存路由"就报
# "供应商配置缺失或重复"。冻结文件不能改，所以补偿放在这一层。


def _sections(config) -> dict:
    """按 rs.revision 的口径取两个 section，别的键不参与比较。"""
    if not isinstance(config, dict):
        return {s: [] for s in rs.SECTIONS}
    return {s: config.get(s, []) for s in rs.SECTIONS}


def _read_text_soft(path):
    """(文本, 错误)。文件不存在返回 (None, None)。"""
    try:
        return path.read_text(encoding='utf-8-sig'), None
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, type(exc).__name__ + ': ' + str(exc)


def _json_object(text):
    """(解析结果, 错误)。"""
    try:
        return json.loads(text), None
    except ValueError as exc:
        return None, type(exc).__name__ + ': ' + str(exc)


def _json_key(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def _plan_key(plan):
    """plan 的语义指纹：clean_plan 去掉 available / fetch_error 这些 live 字段。"""
    if not isinstance(plan, dict):
        return None
    try:
        return _json_key(rs.clean_plan(plan))
    except Exception:                              # noqa: BLE001 - 结构坏了也要能比对
        return None


def _write_text_atomic(path: Path, text: str) -> None:
    """临时名带 pid。rs.write_json 的临时名固定为 <path>.tmp，两个写手会互抢。"""
    temp = path.parent / (path.name + '.tmp-%d' % os.getpid())
    temp.write_text(text, encoding='utf-8')
    os.replace(temp, path)


def _write_plan_text(text: str) -> None:
    """回滚 plan 走 sources 的写入口（pid 临时名 + clean_plan），与别处保持一致。"""
    _sources()._write_plan(json.loads(text))


# 一次"保存路由"真正会改的盘上文件，以及各自怎么比对、怎么回补。
# 顺序就是 _state_snapshot / _compensate 里的处理顺序。
_TEXT_FILES = {
    'plan': PLAN_PATH,
    'catalog': CATALOG_PATH,
    'templates': TEMPLATES_PATH,
}
_FILE_KEYFN = {
    'plan': _plan_key,
    # catalog 与 templates 都是 {'models': [...]}，纯结构比较就够；格式差异（indent、
    # 键序）不该被当成"内容变了"而误回滚。
    'catalog': _json_key,
    'templates': _json_key,
}
_FILE_WRITER = {
    'plan': _write_plan_text,
    'catalog': lambda text: _write_text_atomic(CATALOG_PATH, text),
    'templates': lambda text: _write_text_atomic(TEMPLATES_PATH, text),
}


def _state_snapshot() -> dict:
    """保存前的现场：网关 config（JSON 是权威）、auth 的 excluded_models、盘上
    plan / catalog / templates，外加这几份文件的一份人肉备份（backups/route-switch-*）。

    auth 只记**值**不拷文件 —— 它也是本次保存的写入位置之一（见 AUTH_NAME 的说明），
    但补偿只需要那个值，多拷一份就是把 OAuth 令牌在磁盘上多留一份。"""
    snap = {'config': None, 'config_error': None,
            'auth_excluded': None, 'auth_error': None,
            'backup_dir': None, 'backup_error': None}
    for name in _TEXT_FILES:
        snap[name] = None
        snap[name + '_text'] = None
        snap[name + '_error'] = None
    try:
        snap['config'] = _sections(gateway_get('config'))
    except Exception as exc:                       # noqa: BLE001 - 拿不到就如实记下
        snap['config_error'] = type(exc).__name__ + ': ' + str(exc)
    try:
        snap['auth_excluded'] = rs.auth_current()
    except Exception as exc:                       # noqa: BLE001 - 同上
        snap['auth_error'] = type(exc).__name__ + ': ' + str(exc)
    for name, path in _TEXT_FILES.items():
        text, err = _read_text_soft(path)
        snap[name + '_text'] = text
        if err:
            snap[name + '_error'] = err
        elif text is not None:
            obj, jerr = _json_object(text)
            snap[name] = obj
            snap[name + '_error'] = jerr
    try:
        snap['backup_dir'] = _sources()._backup('route-switch', SWITCH_FILES)
    except Exception as exc:                       # noqa: BLE001 - 备份失败不拦住保存，但要记下
        snap['backup_error'] = type(exc).__name__ + ': ' + str(exc)
    return snap


def _restore_line(name, path, pre_text, pre_obj, pre_error, keyfn, writer) -> str:
    """一个文件的核对与回补，返回一句人话。"""
    if pre_error:
        return name + '：保存前就解析不了（' + str(pre_error) + '），无法比对'
    if pre_text is None:
        return name + '：保存前不存在，跳过（本次若新建了它，请人工确认是否该留）'
    cur_text, cur_error = _read_text_soft(path)
    if cur_error:
        return name + '：现在读不到（' + cur_error + '），无法比对'
    if cur_text is None:
        return name + '：现在文件不见了，需人工从备份目录恢复'
    if cur_text == pre_text:
        return name + '：未推进，无需回滚'
    cur_obj, _ = _json_object(cur_text)
    if cur_obj is not None and keyfn(cur_obj) == keyfn(pre_obj):
        return name + '：内容与保存前等价（只差格式），无需回滚'
    try:
        writer(pre_text)
    except Exception as exc:                       # noqa: BLE001
        return name + '：回滚失败（' + type(exc).__name__ + ': ' + str(exc) + '），仍是新值，需人工恢复'
    back_text, _ = _read_text_soft(path)
    if back_text == pre_text:
        return name + '：已回滚到保存前的值'
    return name + '：回滚后与保存前仍有差异，需人工核对'


def _compensate(snap: dict) -> list:
    """把失败留下的半成品收拾成一致的旧状态，返回逐项说明。"""
    lines = []
    if snap.get('backup_error'):
        lines.append('备份目录未建成（' + str(snap['backup_error']) + '）')
    elif snap.get('backup_dir'):
        lines.append('快照在 ' + str(snap['backup_dir']))

    if snap['config'] is None:
        lines.append('config：保存前没读到（' + str(snap['config_error']) + '），跳过比对')
    else:
        lines.append(_restore_config_line(snap['config']))

    lines.append(_restore_auth_line(snap.get('auth_excluded'), snap.get('auth_error')))

    for name, path in _TEXT_FILES.items():
        lines.append(_restore_line(name, path, snap[name + '_text'], snap[name],
                                   snap[name + '_error'], _FILE_KEYFN[name],
                                   _FILE_WRITER[name]))
    return [line for line in lines if line]


def _restore_auth_line(pre, pre_error) -> str:
    """auth 的 excluded_models 对着改：与 config 一样只能经网关读回、写回。

    它是"一次保存有五个写入位置"里原先被漏掉的那个（见 AUTH_NAME）。少了这一句，
    补偿文案在 auth 已经被改过的情况下仍然宣称"回滚完成"。"""
    if pre_error:
        return 'auth：保存前没读到（' + str(pre_error) + '），跳过比对'
    try:
        now = rs.auth_current()
    except Exception as exc:                       # noqa: BLE001
        return 'auth：现在读不到（' + type(exc).__name__ + ': ' + str(exc) + '），无法判断是否已推进'
    if now == pre:
        return 'auth：未推进，无需回滚'
    try:
        rs.patch_auth_models(pre)
    except Exception as exc:                       # noqa: BLE001
        return 'auth：回滚失败（' + type(exc).__name__ + ': ' + str(exc) + '），仍是新值，需人工恢复'
    try:
        if rs.auth_current() == pre:
            return 'auth：已回滚到保存前的值（excluded_models）'
    except Exception as exc:                       # noqa: BLE001
        return 'auth：写回去了但复查失败（' + str(exc) + '）'
    return 'auth：回滚后与保存前仍有差异，需人工核对'


def _restore_config_line(pre: dict) -> str:
    """config 存在网关里，只能靠管理接口读回、PUT 回。"""
    try:
        now = _sections(gateway_get('config'))
    except Exception as exc:                       # noqa: BLE001
        return 'config：现在读不到（' + type(exc).__name__ + ': ' + str(exc) + '），无法判断是否已推进'
    if now == pre:
        return 'config：未推进，无需回滚'
    drifted = [s for s in rs.SECTIONS if now.get(s, []) != pre.get(s, [])]
    failed = []
    for section in drifted:
        try:
            gateway_put(section, pre.get(section, []))
        except Exception as exc:                   # noqa: BLE001
            failed.append(section + '（' + type(exc).__name__ + ': ' + str(exc) + '）')
    if failed:
        return 'config：回滚失败，' + '、'.join(failed) + ' 段还是新值，需人工恢复'
    try:
        back = _sections(gateway_get('config'))
    except Exception as exc:                       # noqa: BLE001
        return 'config：PUT 回去了但复查失败（' + str(exc) + '）'
    if back == pre:
        return 'config：已回滚到保存前的值（' + '/'.join(drifted) + ' 段）'
    still = [s for s in rs.SECTIONS if back.get(s, []) != pre.get(s, [])]
    return 'config：回滚后仍与保存前不一致，差异还在 ' + '/'.join(still) + ' 段，需人工恢复'


def _raise_failure(exc: Exception, lines: list) -> None:
    """把裸异常（尤其 StopIteration）翻成 RouteError 再出 HTTP 层，别让用户看到 500。
    同时把现场核对结果拼进文案 —— 只说"回滚不完整"等于什么都没说。"""
    head = ('保存路由失败（' + type(exc).__name__ + ': ' + str(exc).strip() + '）。'
            if str(exc).strip() else '保存路由失败（' + type(exc).__name__ + '）。')
    tail = '；'.join(lines) if lines else 'config / plan / catalog / templates 都没查到差异'
    translated = _translate(RouteError(head))
    raise _with_tail(translated, '失败后的现场核对：' + tail + '。') from None


# ------------------------------------------------- 推理档位写回 catalog
#
# rs.regen_catalog 全文不读 model_settings（grep 零命中）：它只认官方 meta，拿不到就沿用
# 模板池里的 donor 阶梯（`donor_levels=max(...)`）。于是用户在自定义来源里配的 low/high/
# ultra 和默认档，在 codex-model-catalog.json 里一个字都不会出现 —— 连唯一声明它的那家
# 来源也不生效，而界面（config.js 的 tooltip、list_sources 的 warning）还在说设置生效。
# 补齐放在这一层：冻结文件不能改，但"生成完之后再覆盖一遍"是等价的。


def _overwrite_levels(entry: dict, levels: list, default: str) -> bool:
    """把 supported_reasoning_levels / default_reasoning_level 换成 plan 里声明的。
    description 尽量沿用原阶梯同一档位的文案，找不到就用档位名兜底。"""
    old = entry.get('supported_reasoning_levels')
    ladder = {}
    if isinstance(old, list):
        for item in old:
            if isinstance(item, dict) and isinstance(item.get('effort'), str):
                ladder.setdefault(item['effort'], item.get('description'))
    new = [{'effort': lv, 'description': ladder.get(lv) or lv} for lv in levels]
    if old == new and entry.get('default_reasoning_level') == default:
        return False
    entry['supported_reasoning_levels'] = new
    entry['default_reasoning_level'] = default
    return True


def sync_catalog_reasoning() -> dict:
    """把 plan 里声明的推理档位写回 codex-model-catalog.json。

    归属口径与 sources.resolve_client_settings 一致：同一别名被多家暴露时，取 plan 顺序里
    最先声明它的那家（regen_catalog 的 picked 是 slug 唯一键的字典，后者反而覆盖前者）。

    ⚠ 用**客户端可见 ID**（带渠道头）做键，不是干净别名。目录条目的 slug 是客户端 ID
    （srapi/gpt-5.6-sol），拿干净别名去查会全部落空 —— 表现是"来源里配了档位、目录里
    一个字都没有"，而没有任何报错。多来源功能上线时这一处最容易漏。

    返回 {'ok','updated','reason','error','path'}。读失败、解析失败、写失败都带 error，
    绝不静默；调用方决定是抛还是记日志。"""
    report = {'ok': True, 'updated': [], 'reason': None, 'error': None,
              'path': str(CATALOG_PATH)}
    try:
        owners = _sources().resolve_client_settings()
    except Exception as exc:                       # noqa: BLE001
        report.update(ok=False, error='读 routing-plan.json 失败：'
                                      + type(exc).__name__ + ': ' + str(exc))
        log.error('推理档位写回失败：%s', report['error'])
        return report
    text, read_error = _read_text_soft(CATALOG_PATH)
    if read_error:
        report.update(ok=False, error='读 codex-model-catalog.json 失败：' + read_error)
        log.error('推理档位写回失败：%s', report['error'])
        return report
    if text is None:
        report['reason'] = 'codex-model-catalog.json 不存在，没有可同步的行'
        return report
    catalog, parse_error = _json_object(text)
    if parse_error:
        report.update(ok=False, error='codex-model-catalog.json 不是合法 JSON：' + parse_error)
        log.error('推理档位写回失败：%s', report['error'])
        return report
    models = catalog.get('models') if isinstance(catalog, dict) else None
    if not isinstance(models, list):
        report.update(ok=False, error='codex-model-catalog.json 缺少 models 列表')
        log.error('推理档位写回失败：%s', report['error'])
        return report

    changed = []
    for entry in models:
        if not isinstance(entry, dict):
            continue
        slug = entry.get('slug')
        owner = owners.get(slug) if isinstance(slug, str) else None
        if not isinstance(owner, dict):
            continue
        settings = owner.get('settings')
        if not isinstance(settings, dict):
            continue
        levels = [lv for lv in (settings.get('levels') or []) if isinstance(lv, str)]
        if not levels:
            continue
        default = settings.get('default')
        if default not in levels:
            default = levels[0]
        if _overwrite_levels(entry, levels, default):
            changed.append(slug)
    if not changed:
        report['reason'] = 'catalog 里的推理档位已经和 plan 一致，无需改动'
        return report
    try:
        _write_text_atomic(CATALOG_PATH, json.dumps(catalog, ensure_ascii=False, indent=2))
    except OSError as exc:
        report.update(ok=False, updated=[], error='写 codex-model-catalog.json 失败：'
                                                 + type(exc).__name__ + ': ' + str(exc))
        log.error('推理档位写回失败：%s', report['error'])
        return report
    report['updated'] = changed
    log.info('推理档位已写回 catalog：%s', ', '.join(changed))
    return report


def _sync_catalog_or_loud(result) -> None:
    """保存路由成功后的收尾。写回失败要报出来 —— 路由确实存下了，但用户配的档位此刻
    不生效，静默过去就是这一版要修的那个 bug。"""
    report = sync_catalog_reasoning()
    if isinstance(result, dict):
        result['reasoning_sync'] = report
    if not report['ok']:
        raise RouteError(
            '路由已保存，但推理档位没能写进 codex-model-catalog.json：' + str(report['error'])
            + '。界面上的档位设置此刻不生效，请处理磁盘或权限问题后重新保存一次。'
        )
