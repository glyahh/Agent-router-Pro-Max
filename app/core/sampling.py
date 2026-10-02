"""用量采样与长期历史（SQLite）。

网关的 /v0/management/api-key-usage 只保留最近 20 个 10 分钟桶（约 3.3 小时），
而且 success/failed 是**进程内计数**，网关一重启就清零。这里定时把桶抄一份进库，
跨重启的长期趋势才有得看。

桶标签形如 "14:10-14:20"，**不含日期**——只能按采样时刻补，规则见 bucket_date()。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

APP_DIR = Path(__file__).resolve().parent.parent
# 运行时数据（采样库、日志、设置）必须落在 exe 旁边，不能进 _internal：那里是
# PyInstaller 的只读资源区，写进去用户看不到、备份不到，而且包内那份
# app\usage-history.db 里已有的历史会读不到（托盘会每 5 秒报一次 no such table）。
RUN_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else APP_DIR.parent
DB_PATH = RUN_DIR / "usage-history.db"

# route_selector 是唯一一份业务实现（38 个函数、11 个常量）。这里只借它的
# ROOT 常量与 read_json（utf-8-sig），不搬任何逻辑，也不改写它。
_SCRIPT_DIR = APP_DIR.parent / "script"
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
import route_selector as rs  # noqa: E402  模块级无副作用

# bridge 是"封禁 / 401 暂停"闸门的唯一实现（auth_gate 查、note_gate_* 上报）。采样
# 这条轮询路径以前一次都不查闸门：管理密钥一旦对不上，监控页开着时约 70 秒就能凑满
# 网关的 5 次失败闸门，把本机 IP 封 30 分钟。这里只 import 它、只调它的查询与上报，
# 判据一个字都不复制。无循环 import：bridge 只 import route_selector，不 import sampling。
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
from core import bridge  # noqa: E402  模块级无副作用（不碰网关、不写盘）

ROOT = rs.ROOT
# 转发给 health 之类只 import sampling 的地方用，不必再认识 bridge。
auth_gate = bridge.auth_gate
# 与 bridge 对齐：PRISM_GATEWAY_BASE 可把控制台指向沙箱桩，便于在真后端上复现
# "网关不可达"的降级场景；不设时就是本地 8317。
GATEWAY = os.environ.get("PRISM_GATEWAY_BASE", "http://127.0.0.1:8317").rstrip("/")
DEFAULT_INTERVAL_SEC = 600
DEFAULT_RETENTION_DAYS = 90

log = logging.getLogger(__name__)


class SamplingError(Exception):
    """采样失败。消息是中文，直接可以给用户看。"""


class GatewayAuthError(SamplingError):
    """管理密钥被拒（401），或者闸门正处在暂停窗口里（那时根本没发包）。**不能重试**。"""

    def __init__(self, message: str, status: int = 0, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class GatewayBanned(GatewayAuthError):
    """网关封了本机 IP。**一律按 403 判**——未封禁时错误的管理请求是 401、未知路径是 404，
    403 只出现在封禁期间，所以宁可少打网关也不冒再被封一次的风险（与 bridge 同一条口径）。"""


# ---------------------------------------------------------------- 网关只读访问

def management_key() -> str:
    """现读现用：用户换了密钥不必重启 Prism。文件很小，读一次的开销可以忽略。"""
    try:
        data = rs.read_json(ROOT / ".local-secrets.json")
        key = data.get("management_key")
    except (OSError, ValueError) as exc:
        raise SamplingError("读不到 .local-secrets.json 里的管理密钥：" + type(exc).__name__) from None
    if not key:
        raise SamplingError(".local-secrets.json 缺少 management_key")
    return key


def _timeout_message(timeout: float) -> str:
    """超时不是"连不上"。口径与 bridge._timeout_error 一致（group 3）。"""
    return (f"本地网关 {GATEWAY} 对管理接口 {timeout:g} 秒没有响应：可能卡住，"
            "或正在处理大请求。网关进程本身是活着的，不要重启它（重启会清掉它自己的"
            "封禁计数和正在跑的请求）。稍后重试，或先看网关日志确认卡在哪个请求上。")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """绝不跟随重定向。请求头里带的是网关管理密钥，而 urllib 会把 Authorization
    原样复制到 3xx 指向的新主机（只剔除 content-length/content-type），
    一个 302 就能把它送出去。见 bridge.MGMT_OPENER 的同名说明。"""

    def redirect_request(self, *args, **kwargs):
        return None


# 本地网关绝不能走系统代理（用户开了 Clash 时 127.0.0.1 也可能被劫持成远端请求），
# 再加上免重定向（管理密钥不外流）。
_MGMT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def mgmt_get(path: str, timeout: float = 10.0):
    """GET /v0/management/<path>。

    只读。故意不做 PUT/POST/DELETE 的入口——写配置是 bridge/sources 的事，
    那边要带备份与回滚，不能被采样的顺手调用绕过。

    **打网络之前先看闸门**：监控页 / 用量页走的就是这条路径，以前它一次都不省，
    管理密钥一旦对不上，采样轮询约 70 秒就能凑满网关的 5 次失败闸门、本机 IP 被
    封 30 分钟（prism.log 里"网关已封禁本机 IP"命中 41 次）。闸门状态在 bridge，
    这里只问、不复制判据。

    两件做不到的也说清楚：闸门是"问的时候"的快照，权威判断仍是 bridge 每次发包前
    自己做一遍；封禁窗口不会因为这里不发请求就早点解除。
    """
    if not path.startswith("/v0/management/"):
        raise SamplingError("只允许读取 /v0/management 下的接口，收到：" + path)

    gate = bridge.auth_gate()          # 只读本地计数，不打网络
    if gate["blocked"]:
        # 窗口期直接抛，绝不改成"我先试一次再说"——那一次就是压垮闸门的那一次。
        if gate["kind"] == "ban":
            raise GatewayBanned(gate["message"], status=403,
                                retry_after=gate["remaining_seconds"])
        raise GatewayAuthError(gate["message"], status=401)

    req = urllib.request.Request(
        GATEWAY + path,
        headers={"Authorization": "Bearer " + management_key(), "Accept": "application/json"},
    )
    # 免代理 + 免重定向，见 _MGMT_OPENER 的说明
    try:
        with _MGMT_OPENER.open(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        if exc.code == 401:
            # 让闸门也看见这次失败。不上报的话采样这条轮询永远凑不满阈值，闸门只看得到
            # bridge 自己发的那几次请求——那等于只做了一半。
            bridge.note_gate_auth_failure()
            raise GatewayAuthError(
                f"网关接口 HTTP {exc.code}：管理密钥被拒绝。请检查 .local-secrets.json，"
                "不要反复重试（连续失败会让网关封本机 IP）。",
                status=exc.code,
            ) from None
        if exc.code == 403:
            # 403 在实测里只出现在封禁期间（未封禁时错误的管理请求是 401、未知路径是 404），
            # 所以一律按封禁处理，宁可少打网关也不冒再被封一次的风险。
            #
            # 时长解析一律用 bridge._ban_evidence：那是全项目唯一正确的实现，而且已经
            # 被 bridge 自己的封禁时钟用着，两边不会分叉。原来这里自带一个正则，把
            # Go 的 "30m0s" 解析成 0 秒、把 "29m59.99s" 解析成 99 秒；那个值只写进
            # retry_after（全 app 没人读），真正生效的时钟是 bridge._ban_until，于是
            # 监控页显示"约 1 分钟后恢复"而实际要等 30 分钟——文案层 bug。
            confirmed, remaining = bridge._ban_evidence(body)
            if remaining is None:
                remaining = bridge._retry_after_header(exc)
            estimated = remaining is None
            seconds = bridge.note_gate_banned(remaining)   # 顺手把 bridge 的时钟也拨上
            note = "" if confirmed else "（响应体 " + (body[:120] or "为空") + "）"
            raise GatewayBanned(bridge._ban_message(seconds, estimated) + note,
                                status=exc.code, retry_after=seconds) from None
        raise SamplingError(f"网关接口 HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        # urllib 会把 connect/read 超时包进 URLError(reason=...)，也可能直接抛
        # socket.timeout（3.10 起就是 TimeoutError）。超时和"网关没开"是两件事，文案
        # 必须分开：照着"请先启动 CLIProxyAPI"去重启，恰好会清掉网关自己的封禁计数和
        # 正在跑的请求。同口径见 bridge._timeout_error。
        if isinstance(getattr(exc, "reason", None), TimeoutError):
            raise SamplingError(_timeout_message(timeout)) from None
        raise SamplingError("无法连接本地网关，请先启动 CLIProxyAPI") from None
    except TimeoutError:
        raise SamplingError(_timeout_message(timeout)) from None
    except OSError:
        raise SamplingError("无法连接本地网关，请先启动 CLIProxyAPI") from None
    except ValueError:
        # 连上了但响应不是 JSON。说成"无法连接"会把排查方向带偏（网关明明活着）。
        raise SamplingError("网关接口返回的不是 JSON，可能被代理或中间人改了响应") from None
    bridge.note_gate_success()         # 成功一次就清掉 401 暂停窗口
    return payload


# ---------------------------------------------------------------- 来源键与标签

def _api_key_of(entry: dict) -> str:
    """config 条目里的密钥。openai-compatibility 可能写成 api-key-entries 列表。"""
    if isinstance(entry.get("api-key"), str):
        return entry["api-key"]
    entries = entry.get("api-key-entries")
    if isinstance(entries, list) and entries and isinstance(entries[0], dict):
        return entries[0].get("api-key") or ""
    return ""


def vendor_of(base_url: str, default: str) -> str:
    try:
        return urlparse(base_url).netloc or default
    except ValueError:
        return default


def source_map(config: dict | None = None) -> dict:
    """把 api-key-usage 的键映射到人能看懂的来源。

    返回 {"by_key": {api_key: {...}}, "by_provider": {provider_id: {...}}}。

    映射靠 `|` 之后的 api-key 去比对 config 条目里的 api-key（实测 6 条互不相同，
    6/6 命中）。**不能按分组名映射**——分组是 `codex` / `command code (goat)`，
    与来源名、段名都不对应。
    """
    if config is None:
        config = mgmt_get("/v0/management/config")
    plan = rs.read_json(ROOT / "routing-plan.json")
    if not isinstance(plan, dict):
        # 顶层被改成 [] 或 "..." 时 rs.read_json 不报错，但下面 plan.get 会抛
        # AttributeError——那既不是 SamplingError 也不是 OSError/ValueError，
        # 调用方的降级分支接不住。这里统一成"读不到标签"。
        raise SamplingError(
            "routing-plan.json 顶层不是对象，读不到来源标签：" + str(type(plan).__name__))

    # config 条目按 (段, base-url 去尾斜杠, X-Route-Tag) 建索引，与 route_selector 的
    # entry_identity 一致，同一 base-url 挂两个凭据时才分得开。
    entries = {}
    for section in rs.SECTIONS:
        raw = config.get(section, []) or []
        if not isinstance(raw, list):
            # 段被写成映射等形状时，遍历出来的是**字符串键**，下面 entry.get 直接
            # AttributeError → /api/monitor 整页 500（复查轮 4 的 #4）。这里按"这个段没有
            # 可读条目"处理；坏条目逐个跳过，别让一个坏段把整个标签表带崩。
            log.warning('config 的 %s 段不是列表（%s），跳过', section, type(raw).__name__)
            continue
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            key = _api_key_of(entry)
            ident = (section, (entry.get("base-url") or "").rstrip("/"), (entry.get("headers") or {}).get("X-Route-Tag"))
            entries[ident] = {"api_key": key, "base_url": entry.get("base-url") or "", "name": entry.get("name")}

    by_key: dict[str, dict] = {}
    by_provider: dict[str, dict] = {}
    raw_rows = plan.get("providers") or []
    rows = [r for r in raw_rows if isinstance(r, dict)] if isinstance(raw_rows, list) else []
    for provider in rows:
        ident = (provider.get("section"), (provider.get("base_url") or "").rstrip("/"), provider.get("tag"))
        entry = entries.get(ident)
        record = {
            "provider_ids": [provider.get("id")],
            "labels": [provider.get("label") or provider.get("id")],
            "group": provider.get("group"),
            "groups": [provider.get("group")],
            "tags": [provider.get("tag")],
            "section": provider.get("section"),
        }
        if entry and entry["api_key"]:
            key = entry["api_key"]
            # 一个凭据可能同时撑起两行（Goat 兼职 DeepSeek 与 GLM）：标签相同就合并。
            slot = by_key.setdefault(key, dict(record, source_key=f"{entry['base_url']}|{key}",
                                               vendor=vendor_of(entry["base_url"], "本地网关")))
            if record["provider_ids"][0] not in slot["provider_ids"]:
                slot["provider_ids"] += record["provider_ids"]
                slot["labels"] = list(dict.fromkeys(slot["labels"] + record["labels"]))
                slot["groups"] = list(dict.fromkeys(slot["groups"] + record["groups"]))
                slot["tags"] = list(dict.fromkeys(slot["tags"] + record["tags"]))
            record["source_key"] = slot["source_key"]
            record["vendor"] = slot["vendor"]
            record["api_key"] = key
            record["label"] = " / ".join(slot["labels"])
        by_provider[provider.get("id")] = record

    for key, slot in by_key.items():
        slot["label"] = " / ".join(slot["labels"])
        slot["provider_id"] = slot["provider_ids"][0]
    return {"by_key": by_key, "by_provider": by_provider}


def source_key_label(source_key: str, mapping: dict | None = None) -> dict:
    """给一个 `base_url|api_key` 找出标签与厂商；认不出来就退化成主机名。"""
    base, _, key = source_key.partition("|")
    slot = ((mapping or {}).get("by_key") or {}).get(key)
    if slot:
        return {"label": slot["label"], "vendor": slot["vendor"]}
    return {"label": base or source_key, "vendor": vendor_of(base, "")}


def public_source_key(source_key: str) -> str:
    """外发用的来源标识：`base_url|api_key` 的定长短哈希。

    响应里不能带完整凭据。前端确实做了掩码，但掩码只挡 DOM —— 完整值照样进
    WebView 的 JS 上下文，DevTools 的网络面板里一看就有。哈希定长，前端拿它当
    唯一键用就够，不需要认出原文（显示走的是 label / vendor）。

    本地库 usage_history 里存的仍是原始 source_key，那是本地文件不外发。同一个
    原始值算出来的哈希是同一个，所以前端把 counts 和 history 对起来时不用额外
    的映射表。空值原样返回：没有 key 的行本来也没有可泄漏的东西。
    """
    if not source_key:
        return source_key
    return "sk-" + hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- 桶与日期

_BUCKET_HEAD = re.compile(r"^\s*(\d{1,2}):(\d{2})")


def bucket_date(bucket: str, now: datetime | None = None) -> str | None:
    """把不含日期的桶标签补成 YYYY-MM-DD。补不出日期返回 None（丢弃，不塞垃圾进库）。

    规则：桶起始 HH:MM 晚于采样时刻 T 的 HH:MM，说明这个桶是**昨天**开的。
    T=00:05 时看到 "23:50-00:00" → 昨天；T=19:30 时看到 "19:30-19:40" → 今天。
    桶窗口只有 3.3 小时，跨天最多只跨一天，所以这条规则是完备的。

    控制台关机那几天没有采样，历史里就是空缺。**不补零**——补零会把
    "网关在跑但没人用"和"根本没开机"混成同一种数据。
    """
    now = now or datetime.now()
    hit = _BUCKET_HEAD.match(bucket or "")
    if not hit:
        return None
    hour, minute = int(hit.group(1)), int(hit.group(2))
    if hour > 23 or minute > 59:
        return None
    day = now.date()
    if hour * 60 + minute > now.hour * 60 + now.minute:
        day = day - timedelta(days=1)
    return day.isoformat()


def parse_usage(payload) -> list[dict]:
    """摊平 api-key-usage：{分组: {base_url|api_key: {success, failed, recent_requests}}}。"""
    rows: list[dict] = []
    seen: set[str] = set()
    if not isinstance(payload, dict):
        return rows
    for stats in payload.values():
        if not isinstance(stats, dict):
            continue
        for source_key, stat in stats.items():
            if not isinstance(stat, dict) or "|" not in source_key or source_key in seen:
                # 同一凭据万一出现在两个分组里，只算一次，避免重复计数。
                continue
            seen.add(source_key)
            for item in stat.get("recent_requests") or []:
                if not isinstance(item, dict):
                    continue
                bucket = item.get("time")
                if not isinstance(bucket, str) or not bucket:
                    continue
                rows.append({
                    "source_key": source_key,
                    "bucket": bucket,
                    "success": _to_int(item.get("success")),
                    "failed": _to_int(item.get("failed")),
                })
    return rows


def _to_int(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- 落库

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_history (
    date       TEXT    NOT NULL,
    source_key TEXT    NOT NULL,
    bucket     TEXT    NOT NULL,
    success    INTEGER NOT NULL DEFAULT 0,
    failed     INTEGER NOT NULL DEFAULT 0,
    label      TEXT,
    PRIMARY KEY (date, source_key, bucket)
);
CREATE INDEX IF NOT EXISTS idx_usage_date ON usage_history(date);
"""


def _db_path() -> Path:
    # PRISM_USAGE_DB 只给测试把库指到沙箱用；正常运行永远是 app\usage-history.db。
    override = os.environ.get("PRISM_USAGE_DB")
    return Path(override) if override else DB_PATH


@contextmanager
def _connect(path: Path | None = None):
    """每次现开现关。**"关"是真的关**。

    原先它返回一个裸连接、调用方写 `with _connect() as conn:` —— 那是 **sqlite3.Connection
    自己的**上下文管理器，只负责事务提交/回滚，**不关连接**；连接是靠 CPython 引用计数在
    函数退出时回收的，换个实现（或连接被意外持有）就是句柄泄漏，而注释却写着"现开现关"
    （审查 NEW-05）。

    这里把两层都保留：内层 `with conn:` 保住原来的"成功提交 / 异常回滚"语义（调用方一行
    都不用改），外层 finally 保证一定 close。调用方原有的显式 `conn.commit()` 变成幂等操作。
    """
    conn = sqlite3.connect(path or _db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        # WAL 读写不互斥，控制台轮询读不再被采样写卡出 database is locked；busy 等待由 connect(timeout=10) 覆盖
        conn.execute("PRAGMA journal_mode=WAL")
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    """建表（幂等）。"""
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as conn:
        conn.executescript(SCHEMA)


def sample_once(usage=None, now: datetime | None = None) -> dict:
    """拉一次 api-key-usage 落库，返回 {"inserted": N}。

    usage / now 是测试注入点：不给就真的去问网关、真的取当前时间。
    重复采样不会产生重复行（主键 (date, source_key, bucket) + INSERT OR REPLACE），
    所以 inserted 第二次是 0。
    """
    now = now or datetime.now()
    payload = usage if usage is not None else mgmt_get("/v0/management/api-key-usage")
    rows = parse_usage(payload)

    mapping = {}
    try:
        mapping = source_map()
    except (SamplingError, OSError, ValueError) as exc:
        # 拿不到 config 或 routing-plan.json 坏了，标签退化成端点名，历史照样记——
        # total 比名字重要。异常类型必须放宽到 OSError/ValueError：rs.read_json 抛的是
        # JSONDecodeError(ValueError) 和 FileNotFoundError(OSError)，原来只捕 SamplingError
        # 会让 routing-plan.json 一旦被手工改坏就**永久停止累积**，桌面端只看得到
        # History 一直空着，用户读成"根本没流量"（沙箱实测 sample_once 直接抛、
        # 库里行数冻住不动）。同口径见 health.py 与 server.usage_payload。
        log.warning("采样时读不到来源标签，本次标签退化成端点名：%s", exc)

    init_db()
    inserted = 0
    updated = 0
    skipped_zero = 0
    skipped_undated = 0
    with _connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM usage_history").fetchone()[0]
        for row in rows:
            if row["success"] == 0 and row["failed"] == 0:
                # 空桶不写：6 个来源 × 144 桶/天 = 864 行/天的零，90 天就是 7.8 万行噪音，
                # 而按天求和时空桶本来也不贡献任何数字。
                skipped_zero += 1
                continue
            day = bucket_date(row["bucket"], now)
            if day is None:
                skipped_undated += 1
                continue
            conn.execute(
                "INSERT OR REPLACE INTO usage_history(date, source_key, bucket, success, failed, label)"
                " VALUES(?,?,?,?,?,?)",
                (day, row["source_key"], row["bucket"], row["success"], row["failed"],
                 source_key_label(row["source_key"], mapping)["label"]),
            )
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM usage_history").fetchone()[0]
        inserted = total - before
        updated = len(rows) - skipped_zero - skipped_undated - inserted

    return {
        "inserted": inserted,
        "updated": updated,
        "buckets": len(rows),
        "skipped_zero": skipped_zero,
        "skipped_undated": skipped_undated,
        "sources": len({r["source_key"] for r in rows}),
        "sampled_at": now.isoformat(timespec="seconds"),
        "db": str(_db_path()),
    }


def prune(retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """删掉超出保留期的行，返回删除条数。

    口径与 history(days) 对齐：retention_days=90 表示保留**最近 90 天（含今天）**。
    """
    if retention_days <= 0:
        return 0
    cutoff = (datetime.now().date() - timedelta(days=int(retention_days) - 1)).isoformat()
    init_db()
    with _connect() as conn:
        cur = conn.execute("DELETE FROM usage_history WHERE date < ?", (cutoff,))
        conn.commit()
        return cur.rowcount or 0


def history(days: int = 7) -> list[dict]:
    """按天聚合：[{date, source_key, label, vendor, success, failed}]，日期升序。

    升序是给图表用的（时间轴从左到右）。days<=0 表示不设下限。
    标签优先用当前配置里的名字；来源已被删掉时回落到入库时记下的名字。
    """
    mapping = {}
    try:
        mapping = source_map()
    except (SamplingError, OSError, ValueError) as exc:
        # 同上：plan 坏了只影响名字，历史该出还是要出（原来这里也只捕 SamplingError，
        # plan 一坏整个 History 接口就 500）。
        log.warning("读不到来源标签，历史用入库时记录的名字：%s", exc)

    sql = ("SELECT date, source_key, SUM(success) AS success, SUM(failed) AS failed,"
           " MAX(label) AS label FROM usage_history")
    params: list = []
    if days and days > 0:
        sql += " WHERE date >= ?"
        params.append((datetime.now().date() - timedelta(days=int(days) - 1)).isoformat())
    sql += " GROUP BY date, source_key ORDER BY date ASC, source_key ASC"

    with _connect() as conn:
        raw = [dict(r) for r in conn.execute(sql, params)]
    out = []
    for row in raw:
        named = source_key_label(row["source_key"], mapping)
        # 来源还在配置里就用当前的名字（用户可能改过显示名）；已被删掉就沿用它入库
        # 时的名字——历史不该因为删了一个来源就变成一串 base_url。
        if row["source_key"] in (mapping.get("by_key") or {}):
            row["label"] = named["label"]
        else:
            row["label"] = row.get("label") or named["label"]
        row["vendor"] = named["vendor"]
        out.append(row)
    return out


# ---------------------------------------------------------------- 调度

_scheduler_lock = threading.Lock()
_scheduler: threading.Thread | None = None
# 让设置页改完**立刻**生效。以前 _loop 的间隔是启动时传进来的固定值、prune() 又用默认
# 保留天数，于是设置页那两个数字改了要重启 Prism 才生效 —— 界面把它们列为可调项，
# 实际是**静默失效**（上线就绪度审查 HI-06）。
_wake = threading.Event()
_state = {"interval_sec": DEFAULT_INTERVAL_SEC,
          "retention_days": DEFAULT_RETENTION_DAYS,
          "last_at": None, "last": None, "last_error": None}


def sample_state() -> dict:
    """采样器的现状。监控页要能回答"它到底在跑吗"。"""
    with _scheduler_lock:
        alive = bool(_scheduler and _scheduler.is_alive())
        return dict(_state, running=alive)


def configure(interval_sec=None, retention_days=None) -> dict:
    """改**运行期**参数（设置页保存时调）。返回改完后的状态。

    只改内存里的值，并**唤醒**正在等待的采样循环 —— 否则用户改小间隔后还要等满
    旧间隔那一轮。传 None 表示该项不动。
    """
    with _scheduler_lock:
        if interval_sec is not None:
            _state["interval_sec"] = max(30, int(interval_sec))
        if retention_days is not None:
            _state["retention_days"] = max(1, int(retention_days))
        snapshot = dict(_state)
    _wake.set()
    return snapshot


def start_scheduler(interval_sec: int = DEFAULT_INTERVAL_SEC,
                    retention_days: int = DEFAULT_RETENTION_DAYS) -> threading.Thread:
    """起守护线程定时采样。重复调用返回已在跑的那个，不会起第二个。

    注意重复调用**仍然会应用新参数**（走 configure）—— 原先那个"已经在跑就直接返回"
    的分支正是设置静默失效的来源之一。
    """
    global _scheduler
    configure(interval_sec, retention_days)
    with _scheduler_lock:
        if _scheduler is not None and _scheduler.is_alive():
            return _scheduler
        thread = threading.Thread(target=_loop, name="prism-sampling", daemon=True)
        _scheduler = thread
        thread.start()
        return thread


def _wait_iteration(forced: float | None = None) -> None:
    """等一轮，但能被 configure() 提前唤醒。forced 给封禁退避那种固定等待用。"""
    with _scheduler_lock:
        wait = float(forced if forced is not None else _state["interval_sec"])
    _wake.wait(wait)
    _wake.clear()


def _loop() -> None:
    while True:
        try:
            result = sample_once()
            with _scheduler_lock:
                _state["last_at"] = result["sampled_at"]
                _state["last"] = result
                _state["last_error"] = None
        except GatewayAuthError as exc:
            # 密钥错或被封时继续按 10 分钟重试，只是把用户的 IP 封得更久。
            with _scheduler_lock:
                _state["last_error"] = str(exc)
            log.error("采样停止：%s", exc)
            _wait_iteration(1800)
            continue
        except SamplingError as exc:
            with _scheduler_lock:
                _state["last_error"] = str(exc)
            log.warning("采样失败：%s", exc)
        except Exception as exc:  # 调度线程不能死，死了就再也起不来
            with _scheduler_lock:
                _state["last_error"] = f"{type(exc).__name__}: {exc}"
            log.exception("采样异常")
        try:
            # 保留天数**每轮现读**：设置页改成 30 天，就不会被下一轮静默改回默认 90
            with _scheduler_lock:
                keep = int(_state["retention_days"])
            prune(keep)
        except Exception:
            log.exception("清理过期历史失败")
        _wait_iteration()
