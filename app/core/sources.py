"""来源（provider）的增删改与连通性测试。

route_selector.py 只管读 plan、切换路由，没有"新增来源"的能力，这块补在这里。
本模块的命门是**写入顺序**：

    新增/修改：① 写 config.yaml 的条目 → ② 写 routing-plan.json 的行
    删除（三步）：① 停用 config 条目 → ② 删 plan 行 → ③ 删 config 条目

新增时若反过来先写 plan，find_entry() 在 config 里找不到条目会抛
"供应商配置缺失或重复"，用户下一次点"保存路由"就报错（B5 实测）。
删除时若跳过第 ① 步，被删来源的条目仍是 active，build_config() 会判定
"检测到额外已启用供应商"，用户下一次保存必然 409。
"""
from __future__ import annotations

import copy
import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from . import bridge

rs = bridge.rs
RouteError = bridge.RouteError

# 自定义来源只写 codex-api-key 段。openai-compatibility 段要 api-key-entries 数组，
# 形状不同，等真有需求再开。
CUSTOM_SECTION = 'codex-api-key'

# Codex 的推理档位枚举，取自 codex-model-catalog-templates.json（已核实：23 个模型
# 里出现过的 effort 就是这六个）。写别的值进去 Codex 的 /model 面板不认。
REASONING_LEVELS = ('low', 'medium', 'high', 'xhigh', 'max', 'ultra')

# ---------------------------------------------------------------- 渠道头（head）

# 同一个分组里启用多家来源时，客户端要能分辨"这个模型走哪一家"。做法是给来源一个
# **渠道头**，拼在客户端可见的模型 ID 前面：srapi + '/' + gpt-5.6-sol。
#
# 为什么用别名而不是网关的 entry['prefix']：
#   config.example.yaml:820-822 明确写着 "For strict backend pinning, use unique
#   aliases/prefixes or avoid overlapping names"，而 prefix 那个字段的示例只出现在
#   codex-api-key / gemini-api-key / xai-api-key / claude-api-key / vertex-api-key，
#   openai-compatibility 的示例里没有它 —— 而 deepseek / glm 两组走的正是
#   openai-compatibility。别名机制两个段都吃，一处实现覆盖全部。
#
# 已实测（docs/evidence-p0-head-routing.txt，用临时网关实例 + 假上游，未碰生产配置）：
#   gpt-5.6-sol            -> 200 -> 无头凭据 A
#   srapi/gpt-5.6-sol      -> 200 -> 有头凭据 B，且上游收到的是剥掉头的 gpt-5.6-sol
# 两个段（codex-api-key / openai-compatibility）都验过，force-model-prefix: true 无干扰。
HEAD_SEP = '/'

# 头的字符集。**禁止 '/'** 是硬要求：这样"有头 ID"里一定含分隔符、"无头 ID"里一定不含，
# 两类 ID 结构上就不可能撞车。也禁止空格与大写，避免出现在 URL / JSON 里还要转义。
HEAD_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{0,23}$')
HEAD_MAX = 24


def client_id(head, alias):
    """干净别名 → 客户端可见 ID。无头时原样返回，所以当前行为一位不变。"""
    h = (head or '').strip()
    return h + HEAD_SEP + alias if h else alias


def strip_head(client, heads):
    """客户端 ID → (head, 干净别名)。heads 是候选头集合，**最长优先**匹配，
    避免 head=a 与 head=ab 这种前缀包含关系把 ab/x 误判成 a 的 b/x。"""
    for h in sorted((x for x in (heads or ()) if x), key=len, reverse=True):
        if client.startswith(h + HEAD_SEP):
            return h, client[len(h) + len(HEAD_SEP):]
    return '', client


def row_client_ids(row):
    """一行来源会产出的全部客户端可见 ID（不含 legacy A/ 别名）。"""
    head = (row or {}).get('head') or ''
    out = []
    for alias in ((row or {}).get('expose') or []):
        if isinstance(alias, str) and alias and not alias.startswith('A/'):
            out.append(client_id(head, alias))
    return out


def plan_head_conflicts(providers, enabled_ids=None):
    """渠道头的不变量，返回人类可读的冲突列表（空列表 = 通过）。

    分两类，**判据不同**，别混：

    **静态**（无论启用与否都查，enabled_ids 无关）：
      S1 头的格式 —— 只能用 24 字符以内的小写字母/数字/._-，禁 '/'。禁斜杠是硬要求：
         这样"有头 ID"必含分隔符、"无头 ID"必不含，两类 ID 结构上不可能撞车。
      S2 头全局唯一 —— 否则报错文案没法指认是谁。

    **动态**（只查 enabled_ids 里的来源，因为它们才可能同时生效）：
      D1 一个分组最多一个**已启用**的无头来源 —— 两个都无头又暴露同名模型时无法消歧，
         而 regen_catalog 的 picked 是 slug 唯一键字典，会静默二选一。
      D2 已启用来源产出的客户端 ID 不许重复 —— 权威判据，也兜住遗留 A/ 别名
         （老任务钉死的 ID，和我们的头在同一命名空间里）。

    为什么动态那两条必须按"已启用"判、不能按全部行判：
      现在的 routing-plan.json 有 9 行、0 个 head，gpt 组 5 行全无头 —— 按全部行判会得
      13 条"冲突"，直接把这个本来就合法的存量配置判成非法，而且以后每加一个来源都会被
      拦住。真正的歧义只发生在"同时生效"的那几行之间（一个组一次只可能启用若干行）。
    """
    problems = []
    heads = {}
    for p in providers or []:
        if not isinstance(p, dict):
            continue
        h = (p.get('head') or '').strip()
        if not h:
            continue
        if not HEAD_RE.match(h):
            problems.append('来源 %s 的渠道头「%s」不合法：只能用 %d 个字符以内的小写字母、'
                            '数字、点、下划线、连字符，且不能以点或连字符开头。'
                            % (p.get('id'), h, HEAD_MAX))
            continue
        if h in heads:
            problems.append('渠道头「%s」被两个来源同时使用（%s 与 %s）；渠道头必须唯一。'
                            % (h, heads[h], p.get('id')))
        else:
            heads[h] = p.get('id')

    if enabled_ids is None:
        return problems

    live = [p for p in (providers or [])
            if isinstance(p, dict) and p.get('id') in enabled_ids]
    by_group = {}
    for p in live:
        by_group.setdefault(p.get('group'), []).append(p)
    for group, rows in by_group.items():
        bare = [r.get('id') for r in rows if not (r.get('head') or '').strip()]
        if len(bare) > 1:
            problems.append('分组 %s 里同时启用了 %d 个没有渠道头的来源（%s）。同一分组最多'
                            '只能启用一个无头来源（它保留干净的模型 ID）；其余的请各自设置'
                            '渠道头，或先停用。'
                            % (group, len(bare), '、'.join(str(x) for x in bare)))

    seen = {}
    for p in live:
        for cid in row_client_ids(p):
            if cid in seen and seen[cid] != p.get('id'):
                problems.append('模型 ID「%s」会被 %s 与 %s 同时暴露给客户端，网关无法判断'
                                '走哪一家；请给其中一个来源换一个渠道头，或取消其中一个对该'
                                '模型的暴露。' % (cid, seen[cid], p.get('id')))
            else:
                seen[cid] = p.get('id')
        # 遗留 A/ 别名（老任务钉死的 ID）也在同一命名空间里，一起查
        for m in (p.get('models') or []):
            alias = m.get('alias') if isinstance(m, dict) else None
            if isinstance(alias, str) and alias.startswith('A/'):
                if alias in seen and seen[alias] != p.get('id'):
                    problems.append('模型 ID「%s」既是 %s 的遗留别名，又被 %s 的渠道头产出；'
                                    '请给 %s 换一个渠道头。'
                                    % (alias, seen[alias], p.get('id'), p.get('id')))
                else:
                    seen[alias] = p.get('id')
    return problems

# ---------------------------------------------------------------- 跨进程互斥

_MUTEX_NAME = 'Global\\PrismRoutingPlan'
if os.name == 'nt':
    _k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    # 句柄是 64 位指针，默认 restype=c_int 会被截断，必须显式声明
    _k32.CreateMutexW.restype = ctypes.c_void_p
    _k32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    _k32.WaitForSingleObject.restype = ctypes.c_uint
    _k32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint)
    _k32.ReleaseMutex.restype = ctypes.c_int
    _k32.ReleaseMutex.argtypes = (ctypes.c_void_p,)
else:
    _k32 = None

_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_MUTEX_HANDLE = None


def _plan_mutex():
    global _MUTEX_HANDLE
    if _k32 is None:
        return None
    if _MUTEX_HANDLE is None:
        _MUTEX_HANDLE = _k32.CreateMutexW(None, False, _MUTEX_NAME)
    return _MUTEX_HANDLE


def _acquire(timeout_ms=None):
    """拿命名互斥体。rs.LOCK 只是 threading.Lock，挡不住第二个控制端；而
    rs.write_json 的临时名又不含 pid，两进程同写会抢同一个临时文件（B9 实测
    600 次里错 93%）。所以 pid 临时名 + 跨进程互斥缺一不可。

    等待上限取 bridge.MUTEX_TIMEOUT_MS（10s）。前端 api 封装的 AbortController 是
    15s，等满了也还是浏览器先掐断，用户看到的是"请求超时…网关可能卡住"，看不出
    真正原因是"另一个控制端正在写入"。"""
    handle = _plan_mutex()
    if handle is None:
        return None
    if timeout_ms is None:
        timeout_ms = bridge.MUTEX_TIMEOUT_MS
    if _k32.WaitForSingleObject(handle, timeout_ms) not in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
        raise RouteError('另一个控制端正在写入路由配置（已等 %d 秒），请稍后重试'
                         % int(timeout_ms // 1000))
    return handle


def _release(handle):
    if handle:
        _k32.ReleaseMutex(handle)


class _Transaction:
    """一次配置写入的独占区：先跨进程互斥体，再拿 route_selector 的进程内 LOCK
    （后者管住 apply_selection 那条写 plan 的链路，光有前者挡不住同进程并发）。"""

    def __enter__(self):
        self.handle = _acquire()
        try:
            # rs.LOCK 必须带超时拿：裸 acquire() 是无限等，实测等过 60.1 秒，早越过
            # 前端 15s 的 AbortController；浏览器掐断后这边还在等、等到了照样写。
            bridge.acquire_rs_lock()
        except BaseException:
            _release(self.handle)
            raise
        return self

    def __exit__(self, *exc):
        rs.LOCK.release()
        _release(self.handle)
        return False


# ---------------------------------------------------------------- 网关调用


def _gw_get(name):
    return bridge.gateway_get(name)


def _gw_put(name, data):
    return bridge.gateway_put(name, data)


def _gw_delete(name, params):
    return bridge.gateway_delete(name, params)


# bridge 的路径约定见 INTERFACES.md：gateway_get(path) 等于 GET /v0/management/<path>。
# 三处调用集中在这里，万一 bridge 改成收完整路径，只改这三个包装。


# ---------------------------------------------------------------- 文件读写


def _plan_path():
    return Path(bridge.ROOT) / 'routing-plan.json'


def _read_plan():
    path = _plan_path()
    try:
        plan = rs.read_json(path)
    except FileNotFoundError:
        raise RouteError('找不到 routing-plan.json：' + str(path)) from None
    except ValueError:
        raise RouteError('routing-plan.json 不是合法 JSON，修好它再操作：' + str(path)) from None
    if not isinstance(plan, dict) or not isinstance(plan.get('providers'), list):
        raise RouteError('routing-plan.json 结构异常：缺少 providers 列表')
    return plan


def _write_plan(plan):
    """写 plan。临时名带 pid 是硬要求——rs.write_json 的临时名固定为 <path>.tmp，
    两个控制端同时写会抢同一个临时文件，os.replace 直接失败。"""
    path = _plan_path()
    temp = path.parent / (path.name + '.tmp-%d' % os.getpid())
    temp.write_text(json.dumps(rs.clean_plan(plan), ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


# backups\ 里混着用户手工做的快照（manual-hubbig-* 这种）。自动备份的名字一律是
# <label>-YYYYmmdd-HHMMSS-ffffff，只有这个形状的才认领——名字对不上一律不碰，
# 删错一个就是删掉用户手里的回滚点。
AUTO_BACKUP_LABELS = ('route-switch', 'catalog-regen', 'client-connect',
                      'source-add', 'source-edit', 'source-delete')
_AUTO_BACKUP_NAME = re.compile(
    r'^(?:' + '|'.join(re.escape(x) for x in AUTO_BACKUP_LABELS) + r')-\d{8}-\d{6}-\d{6}$')

# 保留最近多少个自动备份。每次来源增删改、每次保存路由、每次 catalog regen 都建一个，
# 实测已堆到 164 个 / 6.6MB，不留上限迟早撑满。
BACKUP_KEEP = 50

_AUTO_BACKUP_TS = re.compile(r'-(\d{8}-\d{6}-\d{6})$')


def _backup_sort_key(path: Path) -> str:
    """按名字里的**时间戳段**排，不是按整个名字。

    名字是 `<label>-YYYYmmdd-HHMMSS-ffffff`，而 label 有六种。**按整个名字做字典序会
    先比 label**："名字里带微秒，字典序就是时间序"只在同一个 label 内成立。跨标签时会
    删错：实测同时存在多种标签、总数超过 BACKUP_KEEP 时，字典序靠前的标签
    （`route-switch` < `source-add` < …）的**新**备份会先被删，靠后标签的旧备份反而留下
    （审查 N3-01）。取时间戳段就没有这个问题。

    认不出时间戳的（理论上不会，_AUTO_BACKUP_NAME 已经筛过）退回 mtime，形状统一成
    同一个可比较的字符串，两类混排也仍然有序。
    """
    hit = _AUTO_BACKUP_TS.search(path.name)
    if hit:
        return hit.group(1)
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime('%Y%m%d-%H%M%S-%f')
    except OSError:
        return path.name


def prune_backups(keep: int = BACKUP_KEEP) -> list[str]:
    """删最旧的自动备份目录，返回删掉的名字。

    只认本程序自己建的那几种（见 _AUTO_BACKUP_NAME）。用户手工做的目录名不合这个
    形状，一个都不碰。

    排序口径见 _backup_sort_key。删不掉（被占用、没权限）就留着，清理失败不该把
    正在做的备份一起搞失败。
    """
    root = Path(bridge.ROOT) / 'backups'
    if not root.is_dir():
        return []
    try:
        ours = sorted((p for p in root.iterdir()
                       if p.is_dir() and _AUTO_BACKUP_NAME.match(p.name)),
                      key=_backup_sort_key)
    except OSError:
        return []
    removed = []
    for path in ours[:max(0, len(ours) - max(0, int(keep)))]:
        try:
            shutil.rmtree(path)
        except OSError:
            continue
        removed.append(path.name)
    return removed


def _backup(label, names=('config.yaml', 'routing-plan.json')):
    """把要动的文件先快照一份。不用 rs.backup()：它只复制 config.yaml，而这里最先被改的
    恰恰是 plan（B6）；"保存路由"还要动 catalog，所以 names 可扩。"""
    root = Path(bridge.ROOT)
    target = root / 'backups' / (label + '-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    try:
        target.mkdir(parents=True)
    except OSError as e:
        raise RouteError('创建备份目录失败，已放弃写入：' + str(e)) from None
    for name in names:
        src = root / name
        if src.exists():
            shutil.copy2(src, target / name)
    prune_backups()
    return target


# ---------------------------------------------------------------- 条目身份与匹配


def _api_key_of(entry):
    if entry.get('api-key'):
        return entry['api-key']
    first = (entry.get('api-key-entries') or [{}])[0]
    return first.get('api-key') or ''


def _identity(entry, section):
    """一个 config 条目的身份。codex-api-key 的凭据就是 api-key 本身；
    openai-compatibility 没有 api-key 字段，退回 base-url + tag/name。"""
    base = str(entry.get('base-url') or '').rstrip('/')
    if section == 'codex-api-key':
        return (base, entry.get('api-key') or '')
    headers = entry.get('headers') if isinstance(entry.get('headers'), dict) else {}
    return (base, headers.get('X-Route-Tag') or entry.get('name') or '')


def _match_entries(config, provider):
    """按 rs.find_entry 的规则找候选条目：同一 base-url，再用 tag 或 name 消歧。
    tag 先于 name，和 route_selector:57-59 一致。"""
    if provider.get('section') == 'auth-file':
        return [{'name': rs.AUTH_FILE}]
    items = config.get(provider.get('section')) or []
    base = str(provider.get('base_url') or '').rstrip('/')
    tag = provider.get('tag')
    name = provider.get('name')
    same_base = [v for v in items if str(v.get('base-url') or '').rstrip('/') == base]
    if tag:
        return [v for v in same_base if (v.get('headers') or {}).get('X-Route-Tag') == tag]
    if name:
        return [v for v in same_base if v.get('name') == name]
    return same_base


def _assert_unique(config, provider):
    """find_entry 要求匹配数正好是 1。不是 1 它会抛"供应商配置缺失或重复"，
    用户看不懂；这里提前用能指导下一步的话拦住。"""
    hits = _match_entries(config, provider)
    if len(hits) != 1:
        raise RouteError('端点 %s 上有 %d 条同标签来源，无法唯一定位；请改显示名称或端点'
                         % (provider.get('base_url'), len(hits)))


def _assert_heads_ok(providers, enabled_ids=None):
    """渠道头的不变量，见 plan_head_conflicts()。

    在**写盘之前**调用：这时候 config.yaml 和 routing-plan.json 都还没动，直接抛错
    就是干净的拒绝，不需要回滚。放在写之后就得走 _rollback_section 那一套，代价大得多。
    """
    problems = plan_head_conflicts(providers, enabled_ids)
    if problems:
        raise RouteError('；'.join(problems))


def _enabled_ids(config, plan):
    """当前真正生效的来源 id 集合。

    判据与 rs.snapshot() 的 selected 完全一致（同一个 rs.active / rs.auth_active），
    否则会出现"界面说在用的这两个来源"和"这里认为在用的"不是同一批，不变量就判错了。

    find_entry 在条目缺失或重复时抛错，auth_active 在 auth 文件缺失时抛错 —— 两种都
    按"这个来源没生效"处理，不让一个坏行把整次编辑拦死（那种坏行另有地方报出来）。
    """
    out = set()
    for p in (plan or {}).get('providers', []):
        if not isinstance(p, dict) or not p.get('id'):
            continue
        try:
            if p.get('section') == 'auth-file':
                if rs.auth_active():
                    out.add(p['id'])
                continue
            if rs.active(rs.find_entry(config, p), p.get('section')):
                out.add(p['id'])
        except Exception:                              # noqa: BLE001
            continue
    return out


# ---------------------------------------------------------------- 标签与 ID


def slugify(label):
    """标签 → ASCII 短标识。X-Route-Tag 要作为 HTTP 头发给上游，非 ASCII 字符会
    在 http.client 的 latin-1 编码处直接报错，所以中文一律丢弃；全丢光了（标签
    整个是中文）就用哈希兜底，保证不同标签仍得到不同标识。

    单字符标签保留（'X' → 'x'）——一个能读的 'custom-x' 比 'custom-src-11f6ad'
    有用得多，而 1 个字符的 HTTP 头值完全合法，重名也由 _unique_tag 加后缀兜住。"""
    text = str(label or '').strip().lower()
    slug = re.sub(r'[^a-z0-9]+', '-', text).strip('-')
    if not slug:
        slug = 'src-' + hashlib.sha1(text.encode('utf-8')).hexdigest()[:6]
    return slug


def _taken_tags(config, plan, section):
    """该 section 下所有已被占用的 tag。

    刻意不分 base-url：route_selector 的 entry_identity 只在同一 base-url 内比较，
    所以按 base 限定"技术上"也够用，但那样会造出两个不同端点顶着同一个 tag 的配置，
    用户看着像重复；改端点时更容易撞出"配置缺失或重复"。这里取全局唯一，代价只是
    偶尔多一个 -2 后缀。"""
    taken = set()
    for entry in (config.get(section) or []):
        tag = (entry.get('headers') or {}).get('X-Route-Tag')
        if tag:
            taken.add(tag)
    for p in plan.get('providers', []):
        if p.get('section') != section:
            continue
        if p.get('tag'):
            taken.add(p['tag'])
    return taken


def _unique_tag(label, section, config, plan, keep=None):
    base = 'custom-' + slugify(label)
    taken = _taken_tags(config, plan, section)
    taken.discard(keep)
    tag = base
    n = 2
    while tag in taken:
        tag = '%s-%d' % (base, n)
        n += 1
    return tag


def _unique_id(label, plan, keep=None):
    """plan 的 id 兼作来源主键与 selected[group] 的值，全局唯一。"""
    ids = {p.get('id') for p in plan.get('providers', [])}
    ids.discard(keep)
    base = slugify(label)
    pid = base
    n = 2
    while pid in ids:
        pid = '%s-%d' % (base, n)
        n += 1
    return pid


# ---------------------------------------------------------------- 表单 → 文件


def _clean_spec(spec, creating):
    if not isinstance(spec, dict):
        raise RouteError('来源参数格式无效')
    label = str(spec.get('label') or '').strip()
    if not label:
        raise RouteError('显示名称不能为空')
    group = spec.get('group')
    if group not in rs.GROUPS:
        raise RouteError('分组必须是 gpt / deepseek / glm 之一')
    base_url = str(spec.get('base_url') or '').strip().rstrip('/')
    if not re.match(r'^https?://[^\s/]+', base_url):
        raise RouteError('端点要写成完整的 http(s) 地址，例如 https://api.example.com/v1')
    api_key = str(spec.get('api_key') or '').strip()
    if creating and not api_key:
        raise RouteError('API 密钥不能为空')
    # 渠道头可空（空 = 该分组的无头主来源，模型保留干净 ID）。给了就必须合法。
    head = str(spec.get('head') or '').strip()
    if head and not HEAD_RE.match(head):
        raise RouteError('渠道头「%s」不合法：只能用 %d 个字符以内的小写字母、数字、点、'
                         '下划线、连字符，不能含斜杠或空格。例如 srapi、hub-main。'
                         % (head, HEAD_MAX))
    models, seen = [], set()
    for item in (spec.get('models') or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '').strip()
        alias = str(item.get('alias') or '').strip() or name
        if not name or not alias or alias in seen:
            continue
        seen.add(alias)
        models.append({'name': name, 'alias': alias})
    settings = {}
    raw = spec.get('model_settings')
    if isinstance(raw, dict):
        for alias, item in raw.items():
            if not isinstance(item, dict):
                continue
            levels = [l for l in (item.get('levels') or []) if l in REASONING_LEVELS]
            if not levels:
                continue
            default = item.get('default')
            if default not in levels:
                default = levels[0]
            settings[str(alias)] = {'levels': levels, 'default': default}
    # 模型行被删掉后，它残留的档位设置不该继续写进 plan（UI 上看不到也删不掉）
    settings = {a: v for a, v in settings.items() if a in seen}
    window = spec.get('context_window')
    # 前端用 Number(m.cw) 传进来的可能是**小数**（例如 272000.5）—— 原先只认 int，
    # 于是这类输入被静默丢掉、窗口设置悄悄不生效。取整，别丢（复查轮 5 待确认项）。
    # math.isfinite 是必须的：json.loads **默认接受** Infinity/NaN，而 int(float('inf'))
    # 会 OverflowError、int(float('nan')) 会 ValueError —— 都不被上层的
    # except (TypeError, ValueError) 接住，于是 500 而不是 409（复查轮 7 的 #3）。
    try:
        # math.isfinite 对 >1.8e308 的超大整数会 OverflowError（json.loads 能解析任意长度
        # 整数），那类输入按"不合法"处理（复查轮 8 的 T-3）。
        usable = (not isinstance(window, bool) and isinstance(window, (int, float))
                  and math.isfinite(window) and window > 0)
    except OverflowError:
        usable = False
    if not usable:
        window = None
    else:
        window = int(window)
        if window < 1:
            window = None          # 0<w<1 会被截断成 0，那不是合法窗口（复查轮 6 的 3-3）
    return {'label': label, 'group': group, 'base_url': base_url, 'api_key': api_key,
            'head': head, 'models': models, 'model_settings': settings,
            'context_window': window}


def _entry_from(clean, tag, api_key, existing=None):
    """按现有条目的形状造 codex-api-key 条目。excluded-models 只在新条目上设
    ['*']（新来源默认停用，符合"Exclude * until explicitly selected"）；改已有
    条目时保持原状态不动——否则编辑一下正在用的来源，就等于把它自己停掉了。"""
    entry = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    entry['api-key'] = api_key
    entry['base-url'] = clean['base_url']
    entry.setdefault('proxy-url', '')
    entry['models'] = [dict(m) for m in clean['models']]
    headers = entry.get('headers') if isinstance(entry.get('headers'), dict) else {}
    headers['X-Route-Tag'] = tag
    entry['headers'] = headers
    entry.setdefault('request-retry', 0)
    if existing is None:
        entry['excluded-models'] = ['*']
    return entry


def _row_from(clean, pid, tag, existing=None):
    row = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    row.update({'id': pid, 'label': clean['label'], 'group': clean['group'],
                'section': CUSTOM_SECTION, 'base_url': clean['base_url'],
                'head': clean['head'],
                'models': [dict(m) for m in clean['models']], 'custom': True,
                'tag': tag, 'model_settings': copy.deepcopy(clean['model_settings'])})
    # available / fetch_error 是 live 内存态，落盘前一律清掉（rs.clean_plan 也会清）
    row.pop('available', None)
    row.pop('fetch_error', None)
    row.setdefault('blocked', None)
    row.setdefault('expose', [])
    if clean['context_window'] is None:
        row.pop('context_window', None)
    else:
        row['context_window'] = clean['context_window']
    return row


# ---------------------------------------------------------------- 对外视图


def resolve_model_settings(plan=None):
    """B4：regen_catalog 的 picked 是 alias 唯一键的字典，多家暴露同一 alias 时
    **后者覆盖前者**（沙箱复现过）。所以档位归属取 providers 顺序里最先声明它的
    那一家（setdefault），不是最后声明的。UI 按这个显示归属，否则用户以为设了
    却没生效。返回 {alias: {'id','label','settings'}}。

    ⚠ 这里返回的键是**干净别名**，不是客户端可见 ID。目录里的一行是用客户端 ID 做
    slug 的，所以按 slug 找归属要用 resolve_client_settings()，别用这个。
    """
    plan = plan if plan is not None else _read_plan()
    owners = {}
    for p in plan.get('providers', []):
        settings = p.get('model_settings')
        # B7：model_settings 写成 null 或字符串也不能让这里崩
        if not isinstance(settings, dict):
            continue
        for alias in settings:
            owners.setdefault(alias, {'id': p.get('id'), 'label': p.get('label'),
                                      'settings': settings[alias]})
    return owners


def resolve_client_settings(plan=None):
    """和 resolve_model_settings 同一套归属规则，但键换成**客户端可见 ID**。

    bridge.sync_catalog_reasoning 是按目录条目的 slug 找归属的，而 slug 现在带渠道头
    （srapi/gpt-5.6-sol）。不换成这个映射，加了头之后所有推理档位都会静默失效。
    返回 {客户端ID: {'id','label','settings','head','alias'}}。"""
    plan = plan if plan is not None else _read_plan()
    owners = {}
    for p in plan.get('providers', []):
        settings = p.get('model_settings')
        if not isinstance(settings, dict):
            continue
        head = p.get('head') or ''
        for alias in settings:
            owners.setdefault(client_id(head, alias),
                              {'id': p.get('id'), 'label': p.get('label'),
                               'settings': settings[alias], 'head': head, 'alias': alias})
    return owners


def _shadow_warning(item, owners):
    shadowed = []
    for alias in (item.get('model_settings') or {}):
        owner = owners.get(alias)
        if owner and owner['id'] != item['id']:
            shadowed.append('%s（档位归 %s）' % (alias, owner['label']))
    if not shadowed:
        return None
    return '这些别名的推理档位不生效，同一别名以靠前的来源为准：' + '、'.join(shadowed)


def _view(row, owners=None):
    item = {'id': row.get('id'), 'label': row.get('label'), 'group': row.get('group'),
            'section': row.get('section'), 'base_url': row.get('base_url'),
            'tag': row.get('tag'), 'name': row.get('name'),
            'head': row.get('head') or '',
            'custom': bool(row.get('custom')), 'editable': bool(row.get('custom')),
            'expose': list(row.get('expose') or []),
            'client_ids': row_client_ids(row),
            'models': [dict(m) for m in (row.get('models') or []) if isinstance(m, dict)],
            'model_settings': copy.deepcopy(row.get('model_settings') or {}),
            'context_window': row.get('context_window'),
            'blocked': row.get('blocked'), 'warning': row.get('warning')}
    if owners is not None:
        extra = _shadow_warning(item, owners)
        if extra:
            item['warning'] = (item['warning'] + ' ' if item['warning'] else '') + extra
    return item


def list_sources():
    """从 routing-plan.json 读来源。剥离 available / fetch_error（live 数据不在
    这里给），custom 的标 editable —— 界面只让自定义来源可编辑可删。"""
    plan = _read_plan()
    owners = resolve_model_settings(plan)
    return [_view(p, owners) for p in plan['providers']]


# ---------------------------------------------------------------- 增删改


def _sync_reasoning(payload):
    """来源写完之后顺手把推理档位涂进 codex-model-catalog.json。

    必须在这里触发一次：regen_catalog 只在下一次"保存路由"时才跑，而它现在根本不读
    model_settings（grep 零命中），所以用户配完档位如果只等着下次保存，那下次也不会
    生效。单开新增来源时它通常什么都不改（新来源的 expose 是空的，catalog 里还没有
    它的行）；真正起作用的是"编辑已有来源、改档位"这条路径。

    写回失败不拦来源的保存（config / plan 已经写好了，来源是建成的），但一定记 ERROR
    并把结果挂进返回体，绝不静默。"""
    report = bridge.sync_catalog_reasoning()
    if isinstance(payload, dict):
        payload['reasoning_sync'] = report
    return payload


def create_source(spec):
    clean = _clean_spec(spec, creating=True)
    with _Transaction():
        config = _gw_get('config')
        plan = _read_plan()
        entries = config.get(CUSTOM_SECTION)
        if not isinstance(entries, list):
            raise RouteError('网关配置里没有 %s 段，无法新增来源' % CUSTOM_SECTION)
        tag = _unique_tag(clean['label'], CUSTOM_SECTION, config, plan)
        pid = _unique_id(clean['label'], plan)
        entry = _entry_from(clean, tag, clean['api_key'])
        _assert_unique({CUSTOM_SECTION: entries + [entry]},
                       {'section': CUSTOM_SECTION, 'base_url': clean['base_url'], 'tag': tag})
        row = _row_from(clean, pid, tag)
        # 新来源默认停用（entry 带 excluded-models:['*']，row 的 expose 是空的），
        # 所以它进不了 enabled 集合，不会因为"并存"被拦——只查头的格式与唯一性。
        _assert_heads_ok(plan['providers'] + [row], _enabled_ids(config, plan))
        backup_dir = _backup('source-add')
        # ① config 先写：反过来的话 find_entry 找不到条目，下一次"保存路由"报错
        _gw_put(CUSTOM_SECTION, entries + [entry])
        try:
            plan['providers'].append(row)          # ② plan 后写
            _write_plan(plan)
        except Exception as exc:
            lines = _rollback_section(CUSTOM_SECTION, entries,
                                      {'section': CUSTOM_SECTION, 'base_url': clean['base_url'],
                                       'tag': tag},
                                      backup_dir, exc)
            _rethrow('新增来源', exc, lines)
        return _sync_reasoning(_view(row, resolve_model_settings(plan)))


def update_source(source_id, spec):
    clean = _clean_spec(spec, creating=False)
    with _Transaction():
        config = _gw_get('config')
        plan = _read_plan()
        row = next((p for p in plan['providers'] if p.get('id') == source_id), None)
        if row is None:
            raise RouteError('找不到来源：' + str(source_id))
        if not row.get('custom'):
            raise RouteError('内置来源不可编辑；要自建请用"新增来源"另存一份')
        section = row.get('section')
        entries = config.get(section)
        if not isinstance(entries, list):
            raise RouteError('网关配置里没有 %s 段，无法编辑' % section)
        _assert_unique(config, row)
        old_entry = _match_entries(config, row)[0]
        # 表单不回显密钥（list_sources 里没有 api_key），空值一律理解为"不改动"
        api_key = clean['api_key'] or _api_key_of(old_entry)
        if not api_key:
            raise RouteError('该来源没有可用的 API 密钥，请重新填写密钥')
        tag = _unique_tag(clean['label'], section, config, plan, keep=row.get('tag'))
        new_entry = _entry_from(clean, tag, api_key, existing=old_entry)
        candidate = [e for e in entries
                     if _identity(e, section) != _identity(old_entry, section)] + [new_entry]
        _assert_unique({section: candidate},
                       {'section': section, 'base_url': clean['base_url'], 'tag': tag})
        new_row = _row_from(clean, source_id, tag, existing=row)
        # 编辑不改变条目的启用状态（_entry_from 保留原状态），所以 enabled 集合照旧。
        _assert_heads_ok([new_row if p.get('id') == source_id else p
                          for p in plan['providers']],
                         _enabled_ids(config, plan))
        backup_dir = _backup('source-edit')
        # ① config 先写（顺序同新增，身份变了也必须先落 config）
        _gw_put(section, candidate)
        try:
            plan['providers'] = [new_row if p.get('id') == source_id else p for p in plan['providers']]
            _write_plan(plan)                      # ② plan 后写
        except Exception as exc:
            # plan 未被改动（原子替换失败），只有 config 需要回滚
            lines = _rollback_section(section, entries,
                                      {'section': section, 'base_url': clean['base_url'],
                                       'tag': tag},
                                      backup_dir, exc)
            _rethrow('保存来源', exc, lines)
        # 改档位走的正是这条路径：plan 已落盘，这里把新档位涂进 catalog
        return _sync_reasoning(_view(new_row, resolve_model_settings(plan)))


def set_head(source_id, head):
    """只改 plan 里那一行的 head（渠道头）。**不碰 config.yaml。**

    为什么不复用 update_source：它要求 row['custom']，而 openai-official / srapi /
    proxyhub / kkapi / goat 这些内置来源全是 custom=false —— 而需求 5 恰恰就是要在
    它们之间做区分。head 是**纯 plan 字段**（build_config 只在写 config 的时候用它
    拼别名），改它不需要动网关配置，所以单开这一条窄路径是安全的，也不需要
    _assert_unique / find_entry 那些跟 config 相关的校验。

    生效时机：下一次「保存路由」。这一条只改 plan，config.yaml 与目录都还没变——
    两者于是仍然是**一致**的旧状态，不会出现"config 里是新 ID、目录里是旧 ID"。
    界面上表现为"有未保存的改动"。
    """
    clean = str(head or '').strip()
    if clean and not HEAD_RE.match(clean):
        raise RouteError('渠道头「%s」不合法：只能用 %d 个字符以内的小写字母、数字、点、'
                         '下划线、连字符，不能含斜杠或空格。例如 srapi、hub-main。'
                         % (clean, HEAD_MAX))
    with _Transaction():
        plan = _read_plan()
        row = next((p for p in plan['providers'] if p.get('id') == source_id), None)
        if row is None:
            raise RouteError('找不到来源：' + str(source_id))
        before = row.get('head') or ''
        if before == clean:
            return {'id': source_id, 'head': clean, 'changed': False}
        candidate = [dict(p, head=clean) if p.get('id') == source_id else p
                     for p in plan['providers']]
        # 判据按"当前真正生效的来源"算：改一个没启用的来源的头不应该被拦。
        config = _gw_get('config')
        _assert_heads_ok(candidate, _enabled_ids(config, plan))
        # 只动 head，其余字段用原对象，避免把 live 字段（available/fetch_error）带进 plan
        row['head'] = clean
        _write_plan(plan)
        return {'id': source_id, 'head': clean, 'changed': True, 'before': before}


def _disabled_copy(entry, target, section):
    if _identity(entry, section) != _identity(target, section):
        return entry
    out = copy.deepcopy(entry)
    if section == 'codex-api-key':
        out['excluded-models'] = ['*']
    else:
        out['disabled'] = True
    return out


def _still_present(section, entry):
    now = _gw_get('config')
    key = _identity(entry, section)
    return any(_identity(e, section) == key for e in (now.get(section) or []))


def _brief(exc):
    """把异常压成一行原因，塞进给用户看的文案里。

    server._handle() 只回 str(exc)，__context__ 那条链在 HTTP 边界就断了，所以
    "原始原因"必须进正文，光靠 raise ... from e 用户看不到。"""
    text = ' '.join(str(exc).split())
    return ('%s: %s' % (type(exc).__name__, text[:200])) if text else type(exc).__name__


def _rethrow(stage, exc, lines):
    """把写失败的裸异常翻成 RouteError，别让它走到 500。

    server._handle 只把 RouteError / sampling.SamplingError 映成 409，其余一律 500 +
    "控制台内部错误" + traceback。于是同一件事——plan 写不进去——点"保存路由"得到
    409 加中文说明加现场核对，点"新增来源"却是 500（两次实测：POST /api/sources 与
    瞬时故障下的 DELETE 都是 500）。

    异常类型要换，但原始原因（文件被占用 / 磁盘可能已满）和回滚结果都得留在正文里：
    HTTP 边界之后 from 链就断了，正文不带就等于没说。

    网关封禁、密钥错本来就是 RouteError 子类（'网关管理接口返回 HTTP 500' 这种也在
    里面），别降级成普通 RouteError —— GatewayBanned 还带着剩余等待时长。这类
    改用 bridge._with_tail 把回滚结果续在原文后面，类型不变。回滚成没成同样是用户
    要不要动 backups 的依据：明明全恢复了却不说，用户就会去做没必要的整体恢复。
    """
    if isinstance(lines, str):                     # 传进来单个字符串时 join 会把它拆成一字一行
        lines = [lines]
    tail = ('失败后的现场核对：' + '；'.join(lines) + '。') if lines \
        else '回滚结果未记录，请核对网关条目与 routing-plan.json。'
    if isinstance(exc, RouteError):
        # 网关自己的文案末尾不一定有句号（'网关管理接口返回 HTTP 500：{...}'），
        # _with_tail 是硬拼，少了这个分隔符会读成一句连体话。
        sep = '' if str(exc).rstrip().endswith(('。', '！', '？', '.')) else '。'
        raise bridge._with_tail(exc, sep + tail)
    raise RouteError('%s失败（%s）。%s' % (stage, _brief(exc), tail)) from exc


def _rollback_section(section, old_entries, provider, backup_dir=None, trigger=None):
    """把整段 PUT 回旧值，再确认刚写进去的条目真的没了。网关 PUT 会做归一化，
    整段相等比较会误判，所以只验证关键性质：那个身份还在不在。

    判据必须是"**旧值里没有**的身份还残留"，不能拿新身份直接去匹配：编辑一条正在
    使用的来源（只改模型，名称和端点不动）时新旧身份完全一样，整段 PUT 回旧值之后
    当然还能匹配到那一条 —— 于是回滚明明成功，却报"网关仍保留刚写入的来源"，
    还叫用户去管理面板手工删掉它。那条正是用户在用的来源，删了就断线（沙箱实测）。

    回滚自己也会失败，这时要把两件事都说出来，用户才知道该恢复哪一份文件：
      ① 触发回滚的那步为什么失败（trigger，调用方传进来的 plan 写失败）；
      ② config 回滚成没成——PUT 挂了就是没成，复查那个 GET 挂了就是没法确认。
    原来只丢一句"回滚不完整"，等于没说：config.yaml 和 routing-plan.json 是两份
    文件，用户手里有 backups，却不知道该盖哪一个回去（实测原始原因"磁盘满"被
    新异常顶掉，config 里留下幽灵条目）。

    没出事时返回**一行的列表**（与 _restore_after_delete 同形，_rethrow 直接 join），
    由调用方拼进最终文案：成了、没成、没推进，都是用户判断要不要人工恢复的依据，
    成功路径也不能不说话。
    """
    why = ('plan 写失败（%s）；' % _brief(trigger)) if trigger is not None else ''
    try:
        _gw_put(section, old_entries)
    except Exception as exc:                       # noqa: BLE001 - 回滚失败的原因必须留住
        raise RouteError(
            why + 'config 回滚也失败（%s）：网关里还留着刚写入的条目，'
            'routing-plan.json 没动过。先从 %s 恢复 config.yaml，'
            '或到管理面板手工删掉那一条' % (_brief(exc), backup_dir or 'backups')
        ) from exc
    try:
        now = _gw_get('config')
    except Exception as exc:                       # noqa: BLE001
        raise RouteError(
            why + 'config 已按旧值 PUT 回去，但复查失败（%s），无法确认网关侧的状态；'
            'routing-plan.json 没动过，请到管理面板核对该条目' % _brief(exc)
        ) from exc
    # 判据用 rs.entry_identity（base-url + X-Route-Tag），不用本模块的 _identity。
    # 后者对 codex-api-key 只认 base-url + api-key：用户改了显示名称时 tag 跟着变、
    # 密钥没变，新旧条目会被判成同一条，于是"回滚 PUT 没落地、网关里留着的其实是
    # 改名后的新条目"这种真正的失败会被放过去（沙箱实测：网关里明明是
    # custom-hubx-renamed，却报"刚写入的那条没有残留"）。base-url + tag 也正是
    # plan 里存着的东西，对不上就是 plan 与 config 打架，必须说。
    old_ids = {rs.entry_identity(section, e) for e in old_entries}
    still = [e for e in _match_entries(now, provider)
             if rs.entry_identity(section, e) not in old_ids]
    if still:
        hint = ('；备份在 ' + str(backup_dir)) if backup_dir else ''
        raise RouteError(why + '回滚失败：网关仍保留刚写入的来源，'
                               '请到管理面板手动删除后重试' + hint)
    return ['config 的 %s 段已按旧值 PUT 回去，刚写入的那条没有残留' % section]


def _restore_after_delete(plan, row, section, entries, providers_before, trigger=None):
    """回滚删除：先恢复 config 条目（含原来的启用状态），再把 plan 行写回去。

    两步各自记成败。原来把两者压成一个 ok 布尔，失败时只剩一句"回滚不完整"：
    既没有原因，也没说清哪一份文件是坏的，而 backups 里恰好就是 config.yaml 与
    routing-plan.json 两份，用户不知道该恢复哪一份。

    plan 那一步要先看磁盘上是什么再决定写不写。删除的第 ② 步本来就写不动时，磁盘
    上那份还完整留着这行 —— 硬写一次不但多余，写不动还会被记成"没写回"，把
    "两份文件此刻完全一致"升级成"对不上"，逼用户去做根本没有必要的整体恢复
    （沙箱实测：config 六条条目一条不少、plan 里这行也在，报的却是"对不上"）。

    恢复到 providers_before 这个顺序而不是简单 append：删掉的行不在末尾时，
    append 出来的 plan 与删除前不等价，界面上来源顺序会莫名其妙地变。

    没出事时返回若干行回滚结果，由调用方拼进最终文案。
    """
    why = ('删除失败于（%s）；' % _brief(trigger)) if trigger is not None else ''
    broke, lines, cause = [], [], None
    try:
        _gw_put(section, entries)
        lines.append('config.yaml 的 %s 段已恢复原样' % section)
    except Exception as exc:                       # noqa: BLE001
        broke.append('config.yaml 的 %s 段没恢复（%s）' % (section, _brief(exc)))
        cause = exc
    target = list(providers_before or plan['providers'])
    if row.get('id') not in [p.get('id') for p in target]:
        target.append(row)
    try:
        on_disk = _read_plan()
    except Exception as exc:                       # noqa: BLE001
        broke.append('routing-plan.json 读不出来，没法判断要不要写回（%s）' % _brief(exc))
        cause = cause or exc
    else:
        want = dict(plan)
        want['providers'] = target
        if rs.clean_plan(on_disk) == rs.clean_plan(want):
            lines.append('routing-plan.json 未推进（磁盘上那份本来就等于回滚目标），无需回滚')
        else:
            try:
                _write_plan(want)
                lines.append('routing-plan.json 里那行来源已写回')
            except Exception as exc:               # noqa: BLE001
                broke.append('routing-plan.json 里那行来源没写回（%s）' % _brief(exc))
                cause = cause or exc
    if broke:
        raise RouteError(
            why + '回滚只做了一半：' + '；'.join(broke)
            + '。两份文件现在对不上，请在网关暂停后按 backups 目录里那次备份'
              '把 config.yaml 与 routing-plan.json 一起恢复'
        ) from cause
    return lines


def delete_source(source_id):
    """三步删除，顺序不能变（B5）：
    ① PUT 该条目 excluded-models:['*'] → ② 删 plan 行 → ③ DELETE config 条目。
    跳过 ① 的话，条目仍是 active，build_config 判定"检测到额外已启用供应商"，
    用户下一次"保存路由"必然 409。"""
    with _Transaction():
        config = _gw_get('config')
        plan = _read_plan()
        row = next((p for p in plan['providers'] if p.get('id') == source_id), None)
        if row is None:
            raise RouteError('找不到来源：' + str(source_id))
        if not row.get('custom'):
            raise RouteError('内置来源不可删除；要停用它请在配置页切换到别的来源')
        section = row.get('section')
        if section != CUSTOM_SECTION:
            raise RouteError('目前只支持删除 %s 段的自定义来源' % CUSTOM_SECTION)
        entries = config.get(section)
        if not isinstance(entries, list):
            raise RouteError('网关配置里没有 %s 段，无法删除' % section)
        hits = _match_entries(config, row)
        if len(hits) > 1:
            raise RouteError('配置里有 %d 条同端点同标签的条目，先到管理面板理清重复项再删' % len(hits))
        entry = hits[0] if hits else None
        _backup('source-delete')
        if entry is not None:
            _gw_put(section, [_disabled_copy(e, entry, section) for e in entries])   # ① 停用
        providers_before = list(plan['providers'])
        try:
            plan['providers'] = [p for p in plan['providers'] if p.get('id') != source_id]
            _write_plan(plan)                                                       # ② 删 plan 行
            if entry is not None:                                                   # ③ 删 config 条目
                try:
                    _gw_delete(section, {'api-key': _api_key_of(entry),
                                         'base-url': str(entry.get('base-url') or '').rstrip('/')})
                except Exception:
                    # DELETE 可能已经生效只是响应丢了，先复查再决定要不要回滚
                    if _still_present(section, entry):
                        raise
            if entry is not None and _still_present(section, entry):
                raise RouteError('网关仍保留该配置条目，删除未完成')
        except Exception as exc:
            lines = _restore_after_delete(plan, row, section, entries, providers_before, exc)
            _rethrow('删除来源', exc, lines)
        # 删掉的行如果占着某个别名的档位归属，归属会落到下一个声明它的来源；
        # 一个都不剩的别名保持原样（不把人家配好的档位撤回去）
        return _sync_reasoning({'deleted': source_id,
                                'config_entry': 'removed' if entry is not None else 'absent'})


# ---------------------------------------------------------------- 连通性测试


def _probe(provider, entry):
    """向上游 GET <base_url>/models，一次。返回 (rows, error, status, elapsed_ms)。

    复用 rs.fetch_upstream：代理、超时、User-Agent 与配置页的"刷新模型列表"是同一套，
    测出来的结果才有可比性。只读，不发任何推理请求。"""
    start = time.perf_counter()
    try:
        rows, error = rs.fetch_upstream(provider, entry)
    except (OSError, ValueError) as e:
        # 本地文件缺失（比如官方凭据文件被移走）不该让"测试"变成 500，
        # 该给界面一个"测试失败"的结果。
        return [], '上游探测异常：' + str(e), 0, int((time.perf_counter() - start) * 1000)
    elapsed = int((time.perf_counter() - start) * 1000)
    if error:
        # fetch_upstream 把状态码压进文案（'上游拉取失败：HTTP 402'），抠回来给界面用
        m = re.search(r'HTTP (\d{3})', error)
        return [], error, int(m.group(1)) if m else 0, elapsed
    return rows, None, 200, elapsed


def _dedupe_aliases(rows):
    """把 'vendor/gpt-5.5' 与 'gpt-5.5' 收敛成一条，别名取叶子名——和
    rs.select_candidates 的收敛口径一致，界面才不会看到两行同一个模型。"""
    merged = {}
    for row in rows:
        upstream = row.get('id')
        if not isinstance(upstream, str) or not upstream:
            continue
        alias = rs.leaf(upstream)
        score = (upstream == alias, upstream)
        prev = merged.get(alias)
        if prev is None or score > prev[0]:
            merged[alias] = (score, {'name': upstream, 'alias': alias,
                                     'context_length': row.get('context_length')})
    return [merged[a][1] for a in sorted(merged)]


def test_source(source_id):
    """向上游拉 /models，不发推理请求。返回 {ok,status,message,model_count,elapsed_ms}。"""
    plan = _read_plan()
    row = next((p for p in plan['providers'] if p.get('id') == source_id), None)
    if row is None:
        raise RouteError('找不到来源：' + str(source_id))
    config = _gw_get('config')
    try:
        entry = rs.find_entry(config, row)
    except RouteError as e:
        return {'ok': False, 'status': 0, 'message': '配置条目不可用：' + str(e),
                'model_count': 0, 'elapsed_ms': 0}
    # OAuth 来源（auth-file）本来就没有 api-key，凭据在 auth/codex-official.json 里，
    # fetch_upstream 会自己走官方接口。这里不能拿"缺密钥"把它挡掉。
    if row.get('section') != 'auth-file' and not _api_key_of(entry):
        return {'ok': False, 'status': 0, 'message': '该来源没有 API 密钥，请先补上再测',
                'model_count': 0, 'elapsed_ms': 0}
    rows, error, status, elapsed = _probe(row, entry)
    if error:
        return {'ok': False, 'status': status, 'message': error,
                'model_count': 0, 'elapsed_ms': elapsed}
    return {'ok': True, 'status': 200, 'message': '上游返回 %d 个模型' % len(rows),
            'model_count': len(rows), 'elapsed_ms': elapsed}


def preview_source(base_url, api_key=None, headers=None):
    """给**还没保存**的来源拉一次模型列表。

    INTERFACES.md 的 HTTP 表里没有这个端点，但 static/pages/config.js 的"拉模型"
    按钮已经先试 POST /api/sources/preview、404 才退回旧快照；缺了它，新增来源
    就只能手敲模型 ID。server.preview_models 现在只做转调，实现就是这一份
    （返回里同时有 models 和 model_count，前端两种字段都认）。
    纯只读：不写文件、不发推理请求。"""
    base_url = str(base_url or '').strip().rstrip('/')
    if not re.match(r'^https?://[^\s/]+', base_url):
        raise RouteError('端点要写成完整的 http(s) 地址，例如 https://api.example.com/v1')
    key = str(api_key or '').strip()
    if not key:
        raise RouteError('先填 API 密钥再拉取模型')
    provider = {'section': CUSTOM_SECTION, 'base_url': base_url, 'group': 'gpt', 'label': '(预览)'}
    entry = {'api-key': key, 'base-url': base_url, 'proxy-url': '',
             'headers': dict(headers or {})}
    rows, error, status, elapsed = _probe(provider, entry)
    if error:
        return {'ok': False, 'status': status, 'message': error, 'models': [],
                'model_count': 0, 'elapsed_ms': elapsed}
    models = _dedupe_aliases(rows)
    return {'ok': True, 'status': 200, 'message': '上游返回 %d 个模型' % len(models),
            'models': models, 'model_count': len(models), 'elapsed_ms': elapsed}
