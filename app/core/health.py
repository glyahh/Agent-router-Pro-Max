r"""监控数据聚合。

四件事只有在网关里才问得到，别处都拿不到：

- **版本**：没有版本端点，exe 文件属性也是空的。唯一来源是 logs\\main.log 里的
  `CLIProxyAPI Version: 7.3.9` 行。**不能只搜日志尾部**——实测 8444 行里该行在
  第 8246 行（98% 处），`?limit=500` 那个接口只覆盖约 14 分钟，搜不到。所以按
  文件尾巴 32MB 读一次（当前日志 1.1MB，等于全量）。
  **轮转**：网关按大小轮转，旧文件叫 `main-<时间戳>.log`（lumberjack 的命名，
  exe 里带着它的 `2006-01-02T15-04-05.000` 格式串）。刚轮转完 main.log 是空的，
  版本行留在刚被换下去的那个文件里，所以 main.log 找不到时要回看轮转文件。
- **存活**：探 8317 端口。/health 是 404，别用。
- **身份**：端口有人听**不等于**听的是本目录这套网关。实测本机 8317 上坐着的是
  另一个部署（`D:\\My_Agent_Proxy` 的 config.yaml），它的来源、auth-files、日志页
  全是那边的。所以每轮都核一次身份，实现与成本见 core\identity.py；读不到就给
  unknown，**绝不当成 ok**——那正是这个页面以前骗人的地方。
- **配额**：auth-files 的 quota.signals。**实测可能是空 `{}`**——空是"还没观测到"，
  不是 0%。空的时候一律 available:false，让 UI 去显示"尚无配额观测"。
"""
from __future__ import annotations

import copy
import json
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
from core import identity  # noqa: E402  网关身份核对（只依赖标准库，见 identity.py）
from core import sampling  # noqa: E402  复用它的网关 GET、来源映射与 rs 引入
from core import sources  # noqa: E402  客户端可见 ID 的唯一实现（client_id），health 只消费不复算

rs = sampling.rs
ROOT = sampling.ROOT

# 与 bridge/sampling 对齐：同上，允许环境变量把监控与用量也指向沙箱，
# 否则"网关不可达"只能在浏览器层 mock，没法在真后端上端到端复现。
#
# 这份解析 2026-09-28 起归 core\identity.py 管（托盘要用同一对 host:port 去核身份，
# 各算各的早晚会漂）。这里只把名字接过来，仍可被测试直接改（port_open 是调用时
# 取模块全局，不是默认参数）。
GATEWAY_HOST = identity.GATEWAY_HOST
GATEWAY_PORT = identity.GATEWAY_PORT
LOG_PATH = ROOT / "logs" / "main.log"
# 网关轮转出来的旧日志：lumberjack 把 main.log 改名成 main-<时间戳>.log（本地时间，
# 格式 2006-01-02T15-04-05.000）。名字按时间可排序，但我们按 mtime 排——更耐改名。
LOG_GLOB = "main-*.log"
# lumberjack 的轮转时间戳，固定宽度，字典序就是时间序。认得出就用它排序。
ROTATED_TS_RE = re.compile(r"-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}\.\d{3})\.log$")
VERSION_SCAN_FILES = 5
VERSION_RE = re.compile(r"CLIProxyAPI Version:\s*([0-9][0-9A-Za-z.\-]*)")
VERSION_TAIL_BYTES = 32 * 1024 * 1024

# exe 里就带着这个地址（Go 字符串 "https://api.github.com/repos/router-for-me/CLIProxyAPI/releases/latest"），
# 我们跟它查同一个源，免得自己猜"最新版是多少"。
LATEST_URL = "https://api.github.com/repos/router-for-me/CLIProxyAPI/releases/latest"
UPDATE_TTL_SEC = 6 * 3600
# 查失败（含线程里意外抛异常）后多久允许再查。原来一律等满 6 小时——网络抖一下，
# 版本号就要错 6 小时不更新。成功时仍按 UPDATE_TTL_SEC。
UPDATE_ERROR_TTL_SEC = 5 * 60

_version_cache: dict = {"stamp": None, "version": None}
_update_lock = threading.Lock()
_update: dict = {"latest": None, "checked_at": None, "error": None, "_by": None, "_at": 0.0}

# 上一次监控页的结论。闸门关着时直接回这一份，一个网关请求都不发：monitor_state 每几秒
# 被轮询一次，每次要发 3 个管理接口请求，封禁窗口里那 3 个请求 100% 是 403——白等 3 个
# 6 秒超时，而且每一次 403 都在给封禁时钟续命。
_monitor_lock = threading.Lock()
_monitor_cache: dict = {"payload": None}


# ---------------------------------------------------------------- 网关状态

def _version_in(path: Path) -> str | None:
    """读一个日志文件的尾巴，返回里面**最后**一条版本行。读不了返回 None，不抛错。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > VERSION_TAIL_BYTES:
                handle.seek(size - VERSION_TAIL_BYTES)
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    found = None
    for hit in VERSION_RE.finditer(text):
        found = hit.group(1)
    return found


def _file_stamp(path: Path) -> tuple:
    """缓存键用：文件没了也要有个确定的形状，不要抛。

    用于**轮转文件**（已封口，size/mtime 不再变），所以带上 size 是对的。
    main.log 不能用它 —— 见 _head_stamp()。
    """
    try:
        stat = path.stat()
        return (path.name, stat.st_size, stat.st_mtime_ns)
    except OSError:
        return (path.name, None, None)


# 版本行缓存的最长新鲜度。见 gateway_version() 的说明：TTL 是**兜底**，
# 主判据是"main.log 还是不是同一个文件"。
VERSION_CACHE_TTL = 300.0


def _head_stamp() -> tuple | None:
    """main.log 的"这是不是同一个文件"指纹：**inode + 创建时间，不用 size/mtime**。

    为什么不能用 size/mtime：网关把**每一个**请求都追加进 main.log，而 Prism 自己每 5 秒
    就发 3 个管理接口请求 —— 那个键几乎永远在变，缓存等于没有，于是每次轮询都要把整个
    main.log（上限 VERSION_TAIL_BYTES = 32 MB）读一遍并正则全扫。这正是审查里的 **HI-04**
    （"版本号缓存被自身轮询持续打失效"）。

    版本行只在网关启动时写一次，同一个文件里它不会变 —— 所以"同一个文件"就够做键。
    换文件（轮转）时 inode/ctime 变 → 失效并回看轮转文件，正是需要的语义。
    """
    try:
        stat = LOG_PATH.stat()
        return (stat.st_ino, stat.st_ctime_ns)
    except OSError:
        return None


def _rotation_key(path: Path) -> tuple:
    """排序键：优先用名字里的轮转时间戳，认不出名字才退回 mtime。

    轮转文件是"死"文件，但它的 mtime 会被备份工具、杀软、网盘同步改掉；mtime 一乱就
    可能把更早的那份当成"刚换下去的那份"，于是显示一个早就重启掉的旧版本号。名字不会
    被改（lumberjack 用固定宽度格式，字典序就是时间序）。mtime 只当兜底。
    """
    hit = ROTATED_TS_RE.search(path.name)
    if hit:
        return (1, hit.group(1), "")
    return (0, "", f"{_file_stamp(path)[2] or 0:020d}")


def _rotated_logs() -> list[Path]:
    """`logs\\main-*.log`，新的在前。"""
    try:
        files = [p for p in LOG_PATH.parent.glob(LOG_GLOB) if p.is_file()]
    except OSError:
        return []
    return sorted(files, key=_rotation_key, reverse=True)


def gateway_version() -> str | None:
    """日志里最后一条版本行。读失败返回 None，不抛错——版本读不到不该拖垮监控页。

    main.log 里找不到就问轮转文件。轮转刚发生时新 main.log 是空的，版本行（只在启动
    时写一次）还在被换下去的那个文件里；不回看的话 UI 会一直显示"—"直到网关重启。

    缓存命中判据 = "还是同一个 main.log（inode + ctime）" **且** 轮转文件名单没变
    **且** 还在同一个 TTL 窗口内。**先比键再读内容**：一次未命中要读的字节数是 MB 级的。

    为什么要 TTL 兜底：网关重启若沿用同一个 main.log（inode/ctime 不变），"同一个文件"
    这条判据看不出变化，版本会停在旧值。TTL 保证 VERSION_CACHE_TTL 秒内必然刷新一次，
    代价是每 5 分钟最多一次 MB 级读取 —— 相对原先"每次轮询都读"是数量级的下降。
    """
    head_stamp = _head_stamp()
    rotated = _rotated_logs()[:VERSION_SCAN_FILES]
    bucket = int(time.time() // VERSION_CACHE_TTL)
    stamp = (head_stamp, tuple(_file_stamp(path) for path in rotated), bucket)
    if _version_cache["stamp"] == stamp:
        return _version_cache["version"]

    found = _version_in(LOG_PATH) if head_stamp is not None else None
    if found is None:
        for path in rotated:
            found = _version_in(path)
            if found is not None:
                break
    _version_cache.update(stamp=stamp, version=found)
    return found


def port_open(port: int | None = None, timeout: float = 0.5) -> bool:
    """socket 连一下就知道网关活着没有。用 /health 会拿到 404。

    端口默认值在**调用时**取模块常量，不要写成默认参数——默认参数在 def 时就固定了，
    改 GATEWAY_PORT 会不生效（这个坑在自测里真踩到了）。
    """
    try:
        with socket.create_connection((GATEWAY_HOST, port or GATEWAY_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def _parse_version(text: str | None) -> tuple | None:
    if not text:
        return None
    parts = text.strip().lstrip("vV").split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def _fetch_latest(timeout: float = 6.0) -> tuple[str | None, str | None]:
    """查最新版本，返回 (tag, error)。直连不通再走 config.yaml 里的代理。

    这里**不抛**可预期的异常：每种失败都翻成一句能直接显示的 error 文案。except 原来只
    收 OSError/ValueError，别的异常（比如 rs.get_proxy_url() 炸了）会一路穿进检查线程，
    把"检查中"永久卡住——见 _refresh_update 的注释。
    """
    req = urllib.request.Request(LATEST_URL, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "Prism-Control-Console",
    })
    last = "未知错误"
    for proxy in (None, rs.get_proxy_url()):
        handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else [urllib.request.ProxyHandler({})]
        try:
            with urllib.request.build_opener(*handlers).open(req, timeout=timeout) as resp:
                payload = json.load(resp)
            tag = payload.get("tag_name")
            return (tag, None) if isinstance(tag, str) and tag else (None, "发布信息里没有 tag_name")
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
        except TimeoutError:
            # 超时和"连不上"是两回事（同 group 3 口径），文案分开，别让用户去查网络。
            last = f"查询超时（{timeout:g} 秒无响应）"
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, TimeoutError):
                last = f"查询超时（{timeout:g} 秒无响应）"
            else:
                last = f"{type(exc).__name__}: {reason}" if reason else type(exc).__name__
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
    return None, last


def _refresh_update() -> None:
    """后台线程的入口。**任何情况下都要把 _update['_by'] 清掉。**

    _by 是 update_info 判断"还要不要再起一个检查线程"的唯一依据。原来这里没有兜底：
    _fetch_latest 里冒出一次意外异常（线程就死在这儿），_by 永远留着那个死掉的 Thread，
    于是 checking 一直是 True、stale 再真也不会重新查——只有重启 Prism 才能恢复。
    try/except/finally 三件事各就各位：捕获意外、翻成 error 文案、finally 里清状态位。
    """
    tag = None
    error = None
    try:
        tag, error = _fetch_latest()
    except Exception as exc:      # 兜底：_fetch_latest 自己已经收了可预期的那些
        error = f"{type(exc).__name__}: {exc}"
    finally:
        # 放在 finally：即使抛上来的是 BaseException（线程被强杀之类），状态位也必须清掉。
        with _update_lock:
            _update.update(latest=tag, error=error,
                           checked_at=datetime.now().isoformat(timespec="seconds"),
                           _at=time.time(), _by=None)


def update_info() -> dict:
    """最新版本。查一次缓存 6 小时，而且在**后台线程**里查——监控页每几秒轮询一次，
    不能为了一个版本号把请求卡在网络上。查不到就是 None，UI 显示"—"，不猜。
    """
    with _update_lock:
        ttl = UPDATE_TTL_SEC if _update["error"] is None else UPDATE_ERROR_TTL_SEC
        stale = time.time() - _update["_at"] > ttl
        if stale and _update["_by"] is None:
            thread = threading.Thread(target=_refresh_update, daemon=True, name="prism-version-check")
            _update["_by"] = thread
            thread.start()
        return {"latest": _update["latest"], "checked_at": _update["checked_at"], "error": _update["error"],
                "checking": _update["_by"] is not None}


# ---------------------------------------------------------------- 配额

_PERCENT_USED = ("used_percent", "usedPercent", "used_percent_float", "used", "usage_percent", "percent")
_PERCENT_LEFT = ("remaining_percent", "remainingPercent", "remaining")
_RESET_AT = ("reset_at", "resets_at", "resetAt", "reset_time", "window_reset_at", "reset")
_WINDOW_MIN = ("window_minutes", "windowMinutes", "period_minutes", "window_minutes_int")


def _first_number(source: dict, names) -> float | None:
    for name in names:
        value = source.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return None


def _window_label(key: str, minutes: float | None) -> str:
    """窗口标签按**时长**给：契约里 300 分钟叫"主窗口"、10080 分钟叫"周窗口"，UI 照这个写。

    真实 signals 的键名没能观测到（实测 quota.signals 是空 {}，而它要上游真的回一次
    带配额头的响应才会有值），所以原始键名不丢，另放在 windows[].signal 里。
    """
    if minutes:
        if minutes >= 10080:
            return "周窗口"
        if minutes >= 1440:
            return "日窗口"
        return "主窗口"
    return key or "未知窗口"


def _norm_windows(raw, depth: int = 0) -> list[dict]:
    """把 signals / model_quotas 里的东西摊平成 [{label, used_percent, window_minutes, reset_at}]。

    字段名按 exe 里出现过的几种写法都试一遍（used_percent / remaining_percent /
    window_minutes / reset_at）。**观测不到真实结构**，所以宁可宽容解析也不做断言。
    """
    windows: list[dict] = []
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, list):
        items = [(None, item) for item in raw]
    else:
        return windows

    for key, value in items:
        if not isinstance(value, dict):
            continue
        used = _first_number(value, _PERCENT_USED)
        if used is None:
            left = _first_number(value, _PERCENT_LEFT)
            used = None if left is None else 100.0 - left
        minutes = _first_number(value, _WINDOW_MIN)
        if minutes is None:
            seconds = _first_number(value, ("window_seconds",))
            minutes = seconds / 60 if seconds else None
        reset_at = _first_number(value, _RESET_AT)
        if reset_at is not None and reset_at > 1e11:  # 毫秒时间戳
            reset_at = reset_at / 1000
        if used is None and minutes is None:
            # 可能是 {"primary": {...}, "secondary": {...}} 这种再包一层。
            if depth < 1:
                nested = _norm_windows(value, depth + 1)
                for row in nested:
                    row["label"] = _window_label(str(key or ""), row["window_minutes"])
                windows += nested
            continue
        name = value.get("label") or value.get("name") or value.get("title") or (str(key) if key else None)
        windows.append({
            "label": _window_label(str(name or ""), minutes),
            "signal": str(key) if key is not None else None,
            "used_percent": None if used is None else round(max(0.0, min(100.0, used)), 2),
            "window_minutes": None if minutes is None else int(minutes),
            "reset_at": None if reset_at is None else int(reset_at),
        })
    windows.sort(key=lambda w: (w["window_minutes"] if w["window_minutes"] is not None else 10 ** 9))
    return windows


_FETCH = object()


def quota_state(auth_files=_FETCH) -> dict:
    """配额区块。/api/usage 的上半区直接用这个结构。

    signals 为空时 available:false 且 windows:[]，**不是 0%**——"没观测到"和"用了 0%"
    对用户是两件事，前者要显示"尚无配额观测"。

    auth_files 不传 = 自己去网关拉；显式传 None = 调用方已经知道网关不可达，
    别再撞一次网络（监控页是轮询的，每次多等一个超时很难看）。
    """
    if auth_files is _FETCH:
        try:
            auth_files = sampling.mgmt_get("/v0/management/auth-files", timeout=6.0)
        except sampling.SamplingError as exc:
            return {"available": False, "account": None, "plan_type": None, "windows": [],
                    "note": "读不到网关数据：" + str(exc), "observed_at": None}
    unreachable = auth_files is None

    files = auth_files.get("files") if isinstance(auth_files, dict) else None
    account = plan_type = None
    windows: list[dict] = []
    for entry in files or []:
        if not isinstance(entry, dict):
            continue
        account = account or entry.get("account") or entry.get("email")
        token = entry.get("id_token") if isinstance(entry.get("id_token"), dict) else {}
        plan_type = plan_type or token.get("plan_type")
        quota = entry.get("quota") if isinstance(entry.get("quota"), dict) else {}
        windows += _norm_windows(quota.get("signals"))
        windows += _norm_windows(entry.get("model_quotas") or quota.get("model_quotas"))
        if windows:
            break  # 目前只有一个 OAuth 账号；多个账号要分账号展示时再改成按账号分组

    note = None
    if unreachable:
        note = "网关不可达，配额读不到"
    elif not windows:
        note = "尚无配额观测：网关还没有从上游收到过配额信号（quota.signals 为空）"
    return {
        "available": bool(windows),
        "account": account,
        "plan_type": plan_type,
        "windows": windows,
        "note": note,
        "observed_at": (auth_files or {}).get("observed_at") if isinstance(auth_files, dict) else None,
    }


# ---------------------------------------------------------------- 路由与来源


def _plan_rows(plan) -> list:
    """routing-plan.json 里的 provider 行，**只保留对象行**。

    `plan` 未必是对象、`providers` 未必是列表、列表里未必都是对象 —— 三种形状都会让
    `plan.get(...)` / `provider.get(...)` 直接 AttributeError，把 `/api/monitor` 打成 500
    （审查 N3-05）。同一批形状在 `route_selector.snapshot()` 那边已经被当作"必须容忍"
    （见它的注释），这里保持同口径：合法 JSON 但形状不对时，跳过坏行比整页崩掉好。

    与首页的区别：首页会把坏行**点名**进 `unreadable`；监控页这边只做降级（它是只读的
    汇总视图，报错通道是 `gateway.error`/`routing.note`）。
    """
    if not isinstance(plan, dict):
        return []
    raw = plan.get("providers")
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def _routing(plan: dict, config: dict | None) -> dict:
    selected: dict = {}
    grouped: dict = {}
    # 行级生效判定转调 rs.row_states —— 那是"行级是否生效"的唯一实现（同时吃 plan 顶层的
    # 行级选择），与 rs.snapshot() 的口径**同源**：两处不一致时监控页和首页会说两套话。
    # 行级失败（凭据缺失/重复、auth 文件读不出来）在里面被收进 failures 降级 —— 不算
    # "选中"，也不让一行坏数据把 /api/monitor 打成 500（auth 文件的两种坏法：
    # FileNotFoundError 与"合法 JSON 但不是对象"导致的 AttributeError，都在那里兜住）。
    states, _failures = ({}, {})
    if config is not None:
        states, _failures = rs.row_states(config, _plan_rows(plan), rs.selection_of(plan))
    for group in rs.group_ids(plan):
        if config is None:
            selected[group] = []
            continue
        # 列表，不是标量：一个分组可以同时启用多家来源。旧版的 "conflict" 哨兵随之下线。
        selected[group] = [p["id"] for p in _plan_rows(plan)
                           if p.get("group") == group and states.get(p.get("id")) is True]
    for provider in _plan_rows(plan):
        # 缺 id 的行要**跳过**，不能直取：_plan_rows 只保证"是对象"，一行 `{}` 就会让
        # `provider["id"]` KeyError → /api/monitor 整页 500。同一份盘上数据
        # route_selector.snapshot() 是容忍的（收进 unreadable），两处口径必须一致
        # （复查轮 4 的 #2）。
        pid = provider.get("id")
        if not isinstance(pid, str) or not pid:
            continue
        grouped.setdefault(provider.get("group"), []).append(pid)

    exposed = []
    for provider in _plan_rows(plan):
        if provider.get("id") not in (selected.get(provider.get("group")) or []):
            continue
        # 这里列的是**客户端可见 ID**（带渠道头），与 config.yaml 里写进 models 的
        # alias、以及目录条目的 slug 是同一个东西。用干净别名会显示成"有头的那家
        # 没暴露任何模型"。
        head = (provider.get("head") or "").strip()
        for alias in provider.get("expose", []) or []:
            # 图片模型与过期别名进不了当前 Codex 的 /v1/responses，别列进来当"已暴露"。
            if isinstance(alias, str) and rs.codex_usable(alias):
                # A/ 遗留别名永不加头；其余的 head+SEP 组合只信 sources.client_id 这一份实现。
                cid = alias if alias.startswith("A/") else sources.client_id(head, alias)
                if cid not in exposed:
                    exposed.append(cid)
    return {
        "selected": selected,
        "groups": rs.groups_of(plan),
        "exposed_models": sorted(exposed),
        "candidates": grouped,
        "note": None if config is not None else "网关不可达，无法判定当前路由",
    }


def _auth_file_of(provider: dict) -> str:
    return (provider.get("base_url") or "").split("://", 1)[-1]


def _route_error_note(exc: Exception) -> str:
    """把行级失败翻成一句能给用户看的话。

    凭据文件缺失是最常见的一种（重新登录 ChatGPT、auth-dir 变动），默认的
    `[Errno 2] No such file or directory: '...'` 对用户没信息量。同一个文件的另一种
    坏法是"是合法 JSON 但不是对象"，表现为 AttributeError，也翻译一下。
    """
    if isinstance(exc, FileNotFoundError):
        return "读不到凭据文件：" + (exc.filename or "auth 目录下的文件")
    if isinstance(exc, AttributeError):
        return "凭据文件的内容不是对象，重新登录可能写坏了"
    return f"{type(exc).__name__}: {exc}"


def _cooldown_of(entry: dict):
    """冷却。API key 来源没有这个状态，调用方直接给 None。"""
    raw = entry.get("cooldowns")
    if isinstance(raw, list) and raw:
        first = raw[0]
        if isinstance(first, dict):
            return {"until": first.get("until") or first.get("expires_at") or first.get("reset_at"),
                    "reason": first.get("reason") or first.get("message")}
        return {"until": None, "reason": str(first)}
    if isinstance(raw, dict) and raw:
        return {"until": raw.get("until") or raw.get("expires_at"), "reason": raw.get("reason") or raw.get("message")}
    return None


def _sources(plan: dict, config: dict | None, auth_files: dict | None, usage: dict | None,
             mapping: dict | None, quota: dict | None = None) -> list[dict]:
    files = {}
    for entry in (auth_files or {}).get("files") or []:
        if isinstance(entry, dict):
            files[entry.get("name") or entry.get("id")] = entry
    usage_by_key = {}
    for group in (usage or {}).values():
        if isinstance(group, dict):
            for source_key, stat in group.items():
                if isinstance(stat, dict):
                    usage_by_key[source_key] = stat

    # 行级生效判定与 rs.snapshot() 同源（rs.row_states 是唯一实现，同时吃 plan 顶层的
    # 行级选择）。失败的行降级成"不知道"，不是 False。
    states, failures = ({}, {})
    if config is not None:
        states, failures = rs.row_states(config, _plan_rows(plan), rs.selection_of(plan))
    rows = []
    for provider in _plan_rows(plan):
        oauth = provider.get("section") == "auth-file"
        row = {
            "id": provider.get("id"),
            "label": provider.get("label") or provider.get("id"),
            "group": provider.get("group"),
            "kind": "oauth" if oauth else "apikey",
            "enabled": None,
            "success": None,
            "failed": None,
            # API key 来源在网关里没有冷却状态，这里给 null，UI 不要编一个出来。
            "cooldown": None,
            "counters_since": "网关启动以来",
            "unavailable": True,
        }
        if config is not None:
            pid = provider.get("id")
            if pid in states:
                row["enabled"] = states[pid]
            else:
                exc = failures.get(pid)
                # 行级失败降级成"不知道"（enabled 保持 null），不是 False。读不到凭据
                # 文件和"这一行被停用了"是两回事，别把不知道画成一个确定的否定。
                if exc is not None:
                    row["error"] = _route_error_note(exc)
        if oauth:
            # oauth 行一定有 quota_hint 这个键（未知就是 null），前端可以无条件读。
            row["quota_hint"] = None
            entry = files.get(_auth_file_of(provider))
            if entry is None:
                entry = next((e for e in files.values() if e.get("type") == "codex"), None)
            if entry is not None:
                row.update(success=entry.get("success"), failed=entry.get("failed"),
                           cooldown=_cooldown_of(entry), unavailable=False,
                           account=entry.get("account") or entry.get("email"),
                           expires_at=(entry.get("id_token") or {}).get("chatgpt_subscription_active_until")
                           if isinstance(entry.get("id_token"), dict) else None)
                windows = (quota or quota_state(auth_files)).get("windows") or []
                # 契约里 quota_hint 形如 "主窗口 75%"；没有配额信号就是 None，不编。
                if windows:
                    # `_norm_windows` 明确会产出 `used_percent=None` 的行（"有窗口时长、
                    # 没有百分比"），原来的 `f"{None:g}%"` 会 TypeError → /api/monitor 与
                    # /api/usage 双双 500（审查 Q-1）。取不到百分比就**不给 quota_hint**
                    # （保持"没有配额信号就是 None，不编"的既有口径），标签也缺就用"窗口"。
                    head = windows[0] if isinstance(windows[0], dict) else {}
                    percent = head.get("used_percent")
                    if isinstance(percent, (int, float)) and not isinstance(percent, bool):
                        row["quota_hint"] = f"{head.get('label') or '窗口'} {percent:g}%"
        else:
            slot = ((mapping or {}).get("by_provider") or {}).get(provider.get("id")) or {}
            stat = usage_by_key.get(slot.get("source_key") or "")
            if stat is not None:
                row.update(success=stat.get("success"), failed=stat.get("failed"), unavailable=False)
            # 出网前换成短哈希：这个字段是 base_url|api_key，前端只需要一个稳定的行
            # 标识，不需要认出原文（见 sampling.public_source_key）
            raw_key = slot.get("source_key")
            row["source_key"] = sampling.public_source_key(raw_key) if raw_key else None
        rows.append(row)
    return rows


# ---------------------------------------------------------------- 汇总

def _config_ok(config) -> bool | None:
    """config.yaml 的健康度，**三态**：None=不知道，True=读到且能用，False=读到了但形状不对。

    以前这里是 `config is not None`，于是"网关不可达、config 根本没读到"和"读到了但
    有问题"都被压成 False，前端那个专门的"未知"分支永远走不到，把"不知道"渲染成红色
    的"异常"。不知道和异常对用户是两件事。

    False 只给结构性异常：网关回了不是对象的载荷（数组、字符串），或者一个来源段都没有
    ——route_selector 的 find_entry 全靠 `config.get(section)`，这两种形状下路由判定
    根本做不了。空配置但两个段都在（都是 `[]`）是合法的，算 True。
    """
    if config is None:
        return None
    if not isinstance(config, dict):
        return False
    if not any(isinstance(config.get(section), list) for section in rs.SECTIONS):
        return False
    return True


def _cached_monitor(cached: dict, gate: dict) -> dict:
    """闸门关着时回的"上次结论"：原样带回来，只换掉错误说明并标注 cached。

    不动 running / version 这些字段——它们就是"上次看到的"那个值，闸门不是网关死了，
    编一个 False 反而是新的假数字。真正的解释在 gateway.error 里。

    identity 同理留着不动：闸门拦的是打向网关的 HTTP，身份核对读的是本机进程、不走
    网络，单独刷新它技术上做得到——但那样这一格里"端口在线 + 身份 ok"就可能来自两个
    时刻，反而不如整份快照一致。cached:true 已经说明了它是上一轮的数字。
    """
    payload = copy.deepcopy(cached)
    payload["gateway"] = dict(payload.get("gateway") or {})
    prior = payload["gateway"].get("error")
    payload["gateway"]["error"] = gate["message"] + ("；上次的说明：" + prior if prior else "")
    payload["cached"] = True
    payload["checked_at"] = datetime.now().isoformat(timespec="seconds")
    return payload


def _gateway_identity() -> dict:
    """核一次网关身份，**永不抛异常**。

    两条失败路径都必须落到 unknown，绝不能落到 ok：

      * identity 自己抛了（跨位数进程、受保护进程、以后 Windows 改了 API 形状）；
      * state 不是四个已知值之一（上游改了名）——名字不认识就不许当成"是本目录的"。

    "读不到就说读不到"，这个页面前面已经因为反过来的做法骗过人一次了。
    """
    try:
        who = identity.gateway_identity_cached(GATEWAY_PORT)
        state = who.get('state')
        if state not in (identity.IDENTITY_OK, identity.IDENTITY_FOREIGN,
                         identity.IDENTITY_UNKNOWN, identity.IDENTITY_DOWN):
            raise ValueError('认不出的 state: %r' % (state,))
        return who
    except Exception as exc:                # noqa: BLE001 - 展示层要的是"没有结论"
        return {'state': identity.IDENTITY_UNKNOWN, 'pid': None, 'config': None,
                'image': None,
                'note': '网关身份核对失败（%s: %s），这一格没有结论'
                        % (type(exc).__name__, str(exc)[:120])}


def monitor_state() -> dict:
    """监控页的全部数据。任何一块拿不到都给 null/空并附错误说明，绝不编数字。

    闸门关着（本机 IP 被封、或管理密钥连续失败进了暂停窗口）时**一个网关请求都不发**：
    有上一份结论就回那份（cached:true + 闸门说明），没有就退化成空数据 + 说明。以前这里
    完全不看闸门，监控页开着时约 70 秒就能把网关的 5 次失败闸门凑满，本机 IP 被封 30 分钟。
    """
    gate = sampling.auth_gate()
    with _monitor_lock:
        cached = _monitor_cache["payload"]
    if gate["blocked"] and cached is not None:
        return _cached_monitor(cached, gate)

    running = port_open()
    # 端口在听只说明"有人坐着"，坐的是谁要另外核——8317 上真坐着另一个部署时，
    # 下面 sources/quota/config_ok 全是那边的数据。核对成本 0.4 毫秒（实测），
    # 缓存策略与理由写在 identity.gateway_identity_cached 的注释里。
    who = _gateway_identity()

    config = None
    auth_files = None
    usage = None
    errors = []
    if gate["blocked"]:
        # 没有上一份结论可回（第一轮就被封）：一个请求都不发，直接说清楚为什么。
        errors.append("网关请求已暂停：" + gate["message"])
    elif running:
        for name, path, target in (("config", "/v0/management/config", "config"),
                                   ("auth-files", "/v0/management/auth-files", "auth_files"),
                                   ("api-key-usage", "/v0/management/api-key-usage", "usage")):
            try:
                data = sampling.mgmt_get(path, timeout=6.0)
            except sampling.SamplingError as exc:
                # 三个接口是同一个网关：第一个就不通，后面两个必然也不通。这里踩过坑——
                # 本机对一个没人听的端口，urllib 要 ~2 秒才报"积极拒绝"，不 break 就是白等 6 秒。
                errors.append(f"{name}: {exc}")
                break
            if target == "config":
                config = data
            elif target == "auth_files":
                auth_files = data
            else:
                usage = data
    else:
        errors.append(f"网关未在 {GATEWAY_HOST}:{GATEWAY_PORT} 监听")

    version = gateway_version()
    update = update_info()
    current = _parse_version(version)
    latest = _parse_version(update.get("latest"))
    stale = None if (current is None or latest is None) else latest > current

    try:
        plan = rs.read_json(ROOT / "routing-plan.json")
    except (OSError, ValueError) as exc:
        plan = {"providers": []}
        errors.append("routing-plan.json 读不了：" + type(exc).__name__)

    # 读到了但不是对象（数组、字符串）时，下游的 `config.get` 会 AttributeError。
    # 统一成 None 往下传；原始形状交给 _config_ok 判成 False（结构性异常），
    # 并在 gateway.error 里说清楚，不然 routing.note 会把锅甩给"网关不可达"。
    config_usable = config if isinstance(config, dict) else None
    if config is not None and config_usable is None:
        errors.append("config: 响应不是对象（" + type(config).__name__ + "）")

    mapping = None
    if config_usable is not None:
        try:
            mapping = sampling.source_map(config_usable)
        except (sampling.SamplingError, OSError, ValueError):
            mapping = None

    quota = quota_state(auth_files)
    payload = {
        "gateway": {
            "running": running,
            "port": GATEWAY_PORT,
            "version": version,
            "latest": update.get("latest"),
            "version_stale": stale,
            "config_ok": _config_ok(config),
            "latest_checked_at": update.get("checked_at"),
            "latest_checking": update.get("checking"),
            # 身份：端口上听着的到底是不是本目录这套。四个值 ok/foreign/unknown/down，
            # 语义与实现见 core\identity.py。**unknown 不是 ok**，页面必须分开显示。
            "identity": who.get("state"),
            "identity_note": who.get("note"),
            # 那个网关实际读的配置路径（它自己命令行里的 -config）。foreign 时这一格
            # 就是"数据是从哪儿来的"的答案；读不到时是 null，不编一个本目录的路径顶上。
            "identity_config": identity.identity_config(who),
            "error": "; ".join(errors) or None,
        },
        "routing": _routing(plan, config_usable),
        "sources": _sources(plan, config_usable, auth_files, usage, mapping, quota),
        "quota": quota,
        "sampling": sampling.sample_state(),
        "cached": False,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }
    if not gate["blocked"]:
        # 只缓存"真的问过网关"的那一份。存副本：返回出去的那份归调用方，它要是随手改了
        # 字段，别把缓存一起改坏。_cached_monitor 回的也是它。
        with _monitor_lock:
            _monitor_cache["payload"] = copy.deepcopy(payload)
    return payload


if __name__ == "__main__":  # 手动看一眼真实输出：python core/health.py
    for _ in range(20):      # 版本检查在后台线程里跑，进程会先退出，这里等它落地
        if not update_info()["checking"]:
            break
        time.sleep(0.3)
    print(json.dumps(monitor_state(), ensure_ascii=False, indent=2))
