"""把本机各家的编码 Agent 接到 Prism 的网关（8317）。

Copilot 是例外，而且编辑聊天和 Agents 窗口是两套开关，分开写：
编辑把模型写进 chatLanguageModels.json，请求打到来源自己的地址；
Agent 只把 settings.json 里的 chat.agentHost.byokModels.enabled 设为 true，
让同一批自备模型出现在 Agents 窗口。两边都不经过本地网关。

覆盖的客户端（用户 2026-09-30 确认本轮完全不做 pi，所以它不在这里）：

    codex                 ~/.codex/config.toml                    /v1/responses
    claude-code           ~/.claude/settings.json                 /v1/messages
    claude-code-desktop   同一个 settings.json                    /v1/messages
    opencode              ~/.config/opencode/opencode.json        /v1/chat/completions
    hermes                %LOCALAPPDATA%/hermes/config.yaml       OpenAI 兼容
    copilot               %APPDATA%/Code/User/chatLanguageModels.json
                          编辑聊天的模型列表，直连来源，不写 127.0.0.1:8317
    copilot-agent         %APPDATA%/Code/User/settings.json
                          只改 chat.agentHost.byokModels.enabled，不写模型文件

五种配置形态都是**本机实测读出来的**（不是照文档抄的），要点：

  * Codex 走既有的 rs.connect_client()，一行不改——那条路径已经在线验证过。
  * Claude Code 的开关是 settings.json 的 env.ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY。
    它现在指向 https://api.commandcode.ai/provider/v1（实测值）。
  * opencode 是 provider.<id> = {name, npm, options:{baseURL, apiKey}, models:{...}}。
    它现在有 5 家真实供应商（CC Switch - DeepSeek Anthropic / - Lucis / - SeaGull /
    - SRAPI / KKAPI / WawAPI），所以只**新增**一个 provider.prism，绝不改它们。
  * Hermes 是 %LOCALAPPDATA%/hermes/config.yaml 的 providers.<id> = {api, api_key,
    name, models, default_model}，全局的 model.{default,provider,base_url} 不动。

两条硬约束，踩了会真出事：

  1. **不许 import yaml**。打包环境（app\\.venv 与 _internal\\）里根本没有 PyYAML，
     引入它会让打包版启动即 ImportError。Hermes 按行处理，正则的写法照
     route_selector.force_image_generation_off() 的先例。
     顺带：那也解决了注释问题——用户的 config.yaml 第 19-55 行是 Security /
     Fallback 的注释说明，yaml.safe_load→safe_dump 会把它们全删掉。
  2. **不许 whole-file 重排**。settings.json 与 opencode.json 实测都能在
     json.dumps(indent=2, ensure_ascii=False) 下逐字节还原（见 _json_write 的断言），
     所以 JSON 可以安全地整体写回；但一旦哪天不能还原了，_json_write 会抛错而不是
     悄悄把用户的文件重排一遍。

写入前一律先备份到 backups\\agent-connect-<agent>-<ts>\\（这个命名不匹配
sources.py 的 _AUTO_BACKUP_NAME 正则，prune_backups 不会删它）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path

from . import bridge

rs = bridge.rs
RouteError = bridge.RouteError

# ---------------------------------------------------------------- 常量

# 客户端要连的地址。bridge.GATEWAY_BASE 认 PRISM_GATEWAY_BASE，别在别处再写死 8317。
OPENAI_BASE_SUFFIX = '/v1'          # OpenAI 兼容客户端自己不加 /v1，要我们给全
ANTHROPIC_BASE_SUFFIX = ''          # Claude Code 自己会拼 /v1/messages
OPENCODE_PROVIDER_ID = 'prism'
HERMES_PROVIDER_ID = 'prism'
PRISM_DISPLAY = 'Prism 本地网关'


def _local_key() -> str:
    """网关的客户端密钥（api-keys 那一条）。不是管理密钥。"""
    key = (rs.secrets() or {}).get('api_key')
    if not isinstance(key, str) or not key:
        raise RouteError('.local-secrets.json 里没有 api_key，无法给客户端签发本地密钥。')
    return key


def _home() -> Path:
    return Path.home()


def _localappdata() -> Path:
    raw = os.environ.get('LOCALAPPDATA')
    return Path(raw) if raw else _home() / 'AppData' / 'Local'


def _mask(secret: str) -> str:
    """预览里不回显密钥全文。"""
    s = str(secret or '')
    if len(s) <= 10:
        return '（已设置）' if s else '（空）'
    return s[:6] + '…' + s[-4:]


def openai_base() -> str:
    return bridge.GATEWAY_BASE + OPENAI_BASE_SUFFIX


def anthropic_base() -> str:
    return bridge.GATEWAY_BASE + ANTHROPIC_BASE_SUFFIX


# ---------------------------------------------------------------- JSON 读写
#
# settings.json / opencode.json 实测能在 indent=2 + ensure_ascii=False + 无尾换行下
# 逐字节还原，所以整体写回是**格式无损**的。这里把这个前提写成断言：哪天用户的文件
# 换了格式，宁可报错让他手工处理，也不要悄悄重排他 34 KB 的配置。

def _json_read(path: Path):
    """读 JSON 配置。**顶层必须是对象**，否则抛 RouteError（中文原因 → HTTP 层映射成 409）。

    为什么在这里校验、不让各调用方各自判：`_claude_preview` / `_opencode_preview` /
    `_claude_apply` 都是 `data.get('env')` / `data.get('provider')` 直取，顶层是数组或
    字符串时抛 AttributeError → 冒到 HTTP 层变成 500 兜底，而不是"文件格式不对"的中文
    说明（复查轮 4 的 #3）。这几个文件本来就只能是 JSON 对象。
    """
    try:
        text = path.read_text(encoding='utf-8-sig')
    except OSError as exc:
        raise RouteError('读不了 ' + str(path) + '：' + type(exc).__name__ + ': ' + str(exc)) from None
    try:
        obj = json.loads(text)
    except ValueError as exc:
        raise RouteError(str(path) + ' 不是合法 JSON（' + str(exc) + '），已停止，未做任何改动。') from None
    if not isinstance(obj, dict):
        raise RouteError(str(path) + ' 的顶层不是 JSON 对象（是 ' + type(obj).__name__
                         + '），无法安全地改它，已停止，未做任何改动。')
    return obj, text


def _json_dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def _json_reformat_note(text: str, obj) -> str | None:
    """返回 None 表示整体写回是格式无损的；否则返回一句"会重排"的警告。"""
    if _json_dump(obj) == text:
        return None
    return '这个文件的排版和 json.dumps(indent=2) 不一致，写回会把缩进/空行规整一遍（内容不变）。'


def _json_write(path: Path, obj, expect_text: str | None = None) -> None:
    """原子写。expect_text 给了就先比对，防止把别的程序刚写的改动盖掉。"""
    if expect_text is not None:
        try:
            now = path.read_text(encoding='utf-8-sig')
        except OSError as exc:
            raise RouteError('写前重读失败：' + str(exc)) from None
        if now != expect_text:
            raise RouteError('这个文件在预览之后被其他程序改动了，已停止，未做任何改动：'
                             + str(path))
    # 临时名要**唯一到线程**：控制台是 ThreadingHTTPServer，两个并发的 /api/connect 若写同一个
    # 客户端文件，只带 pid 的临时名会互相覆盖（审查 NC-4）。加上线程 id 就互不干扰。
    temp = path.with_name(path.name + '.prism-%d-%d.tmp' % (os.getpid(), threading.get_ident()))
    try:
        temp.write_text(_json_dump(obj), encoding='utf-8')
        os.replace(temp, path)
    except OSError as exc:
        try:
            temp.unlink()
        except OSError:
            pass
        raise RouteError('写 ' + str(path) + ' 失败：' + type(exc).__name__ + ': ' + str(exc)) from None


def _json_read_array(path: Path):
    """读顶层必须是数组的 JSON。chatLanguageModels.json 是数组，不能走 _json_read。

    顶层不是数组就停：调用方看到 RouteError 之后不得改这个文件。
    """
    try:
        text = path.read_text(encoding='utf-8-sig')
    except OSError as exc:
        raise RouteError('读不了 ' + str(path) + '：' + type(exc).__name__ + ': ' + str(exc)) from None
    try:
        obj = json.loads(text)
    except ValueError as exc:
        raise RouteError(str(path) + ' 不是合法 JSON（' + str(exc) + '），已停止，未做任何改动。') from None
    if not isinstance(obj, list):
        raise RouteError(str(path) + ' 的顶层不是 JSON 数组（是 ' + type(obj).__name__
                         + '），已停止，未做任何改动。')
    return obj, text


def _replace_text(path: Path, text: str) -> None:
    """原子替换整个文件。临时名带 pid 和线程号，两个并发写入不会抢同一个半成品。"""
    temp = path.with_name(path.name + '.prism-%d-%d.tmp' % (os.getpid(), threading.get_ident()))
    try:
        temp.write_text(text, encoding='utf-8')
        os.replace(temp, path)
    except OSError as exc:
        try:
            temp.unlink()
        except OSError:
            pass
        raise RouteError('写 ' + str(path) + ' 失败：' + type(exc).__name__ + ': ' + str(exc)) from None


# ---------------------------------------------------------------- 备份

def _backup(agent_id: str, path: Path) -> str:
    root = Path(bridge.ROOT)
    target = root / 'backups' / ('agent-connect-%s-%s'
                                % (agent_id, datetime.now().strftime('%Y%m%d-%H%M%S-%f')))
    try:
        target.mkdir(parents=True)
        shutil.copy2(path, target / path.name)
        (target / 'WHERE.txt').write_text(
            '原文件：' + str(path) + '\n'
            '回滚：把 ' + path.name + ' 复制回上面的路径。\n'
            '（先退出对应的客户端；这些客户端都是启动时读配置。）\n',
            encoding='utf-8')
    except OSError as exc:
        raise RouteError('备份失败，已放弃写入：' + type(exc).__name__ + ': ' + str(exc)) from None
    return str(target)


# ---------------------------------------------------------------- 当前该给客户端哪些模型

def client_models(agent_id=None) -> list[str]:
    """当前 plan 里这个代理端要写进客户端的模型 ID（带它自己的渠道头），排序去重。

    不传 agent_id 时仍用来源行上的 head，和以前的调用兼容。
    Copilot Agent 不写模型，返回空列表。
    连当前没启用的来源也给出：客户端只在启动时读一次配置。
    """
    from . import sources as S
    if agent_id == 'copilot-agent':
        return []
    plan = S._read_plan()
    owner = S.head_owner(agent_id) if agent_id else None
    out = []
    for p in plan.get('providers', []):
        if not isinstance(p, dict):
            continue
        row = p
        if owner:
            row = dict(p, head=S.effective_head(plan, owner, p))
        for cid in S.row_client_ids(row):
            if cid not in out:
                out.append(cid)
    return sorted(out)


# ---------------------------------------------------------------- 各 agent 的适配

def _codex_path() -> Path:
    return _home() / '.codex' / 'config.toml'


def _claude_path() -> Path:
    return _home() / '.claude' / 'settings.json'


def _claude_user_config_path() -> Path:
    # 自定义密钥能不能用，记在这个文件，不在 settings.json。
    return _home() / '.claude.json'


def _opencode_path() -> Path:
    return _home() / '.config' / 'opencode' / 'opencode.json'


def _hermes_path() -> Path:
    return _localappdata() / 'hermes' / 'config.yaml'


# --- codex

def _codex() -> dict:
    path = _codex_path()
    exists = path.is_file()
    connected = False
    current = None
    note = ''
    if exists:
        try:
            connected = bool(rs.client_connected())
        except Exception:                      # noqa: BLE001
            connected = False
        try:
            import tomllib
            data = tomllib.loads(path.read_text(encoding='utf-8-sig'))
            prov = (data.get('model_providers') or {}).get(data.get('model_provider') or 'custom') or {}
            current = prov.get('base_url')
            note = '当前模型 ' + str(data.get('model') or '（未设置）')
        except Exception as exc:               # noqa: BLE001
            note = '读配置失败：' + type(exc).__name__
    return {'config_path': str(path), 'config_display': '~/.codex/config.toml',
            'exists': exists, 'connected': connected, 'current_base_url': current,
            'current_models': None, 'note': note, 'can_write': True,
            'dialect': 'responses', 'writes': 'model_providers.custom + model_catalog_json'}


def _codex_preview(models) -> dict:
    path = _codex_path()
    if not path.is_file():
        return {'blocked': '找不到 ' + str(path) + '；先装上 Codex 并至少登录一次。'}
    try:
        import tomllib
        data = tomllib.loads(path.read_text(encoding='utf-8-sig'))
    except Exception as exc:                   # noqa: BLE001
        return {'blocked': '读 ' + str(path) + ' 失败：' + type(exc).__name__ + ': ' + str(exc)}
    if data.get('model_provider') != 'custom':
        return {'blocked': '当前 model_provider 是 %r，不是 custom。'
                           'Codex 的接入会改写 model_providers.custom 段，'
                           '前缀对不上时改的不是你正在用的那个供应商，所以停在这里。'
                           % (data.get('model_provider'),)}
    prov = (data.get('model_providers') or {}).get('custom') or {}
    return {'blocked': None, 'reformat_note': None, 'changes': [
        {'kind': 'set', 'key': 'model_providers.custom.base_url',
         'from': prov.get('base_url'), 'to': openai_base()},
        {'kind': 'set', 'key': 'model_catalog_json',
         'from': data.get('model_catalog_json'), 'to': str(bridge.CATALOG_PATH)},
        {'kind': 'set', 'key': 'model_providers.custom.experimental_bearer_token',
         'from': _mask(prov.get('experimental_bearer_token')), 'to': _mask(_local_key())},
        {'kind': 'keep', 'key': 'model / model_provider / auth.json / 其他段',
         'from': '保持不动', 'to': '保持不动'},
    ]}


# --- claude code（桌面端与 CLI 共用同一个文件）

def _claude_env() -> dict:
    path = _claude_path()
    if not path.is_file():
        return {}
    try:
        data, _ = _json_read(path)
    except RouteError:
        return {}
    env = data.get('env')
    return env if isinstance(env, dict) else {}


def _claude() -> dict:
    path = _claude_path()
    exists = path.is_file()
    env = _claude_env()
    cur = env.get('ANTHROPIC_BASE_URL')
    models = sorted(k for k in env if k.startswith('ANTHROPIC_') and 'MODEL' in k)
    return {'config_path': str(path), 'config_display': '~/.claude/settings.json',
            'exists': exists, 'connected': bool(cur) and cur.rstrip('/') == anthropic_base().rstrip('/'),
            'current_base_url': cur,
            'current_models': [str(env[k]) for k in models][:8] or None,
            'note': ('settings.json 里 %d 个 ANTHROPIC_*MODEL* 变量；'
                     '本工具**不改**它们（语义未实测，改了会动坏模型选择）' % len(models))
                    if models else '没有 ANTHROPIC_*MODEL* 变量',
            'can_write': True, 'dialect': 'anthropic',
            'writes': 'env.ANTHROPIC_BASE_URL + env.ANTHROPIC_API_KEY'}


def _claude_preview(models) -> dict:
    path = _claude_path()
    if not path.is_file():
        return {'blocked': '找不到 ' + str(path) + '；先装上 Claude Code 并至少启动一次。'}
    data, text = _json_read(path)
    env = data.get('env')
    if env is None or not isinstance(env, dict):
        return {'blocked': str(path) + ' 里没有 env 对象，无法安全地写 base_url。'}
    changes = [
        {'kind': 'set', 'key': 'env.ANTHROPIC_BASE_URL',
         'from': env.get('ANTHROPIC_BASE_URL'), 'to': anthropic_base()},
        {'kind': 'set', 'key': 'env.ANTHROPIC_API_KEY',
         'from': _mask(env.get('ANTHROPIC_API_KEY')), 'to': _mask(_local_key())},
    ]
    mvars = sorted(k for k in env if k.startswith('ANTHROPIC_') and 'MODEL' in k)
    if mvars:
        changes.append({'kind': 'keep', 'key': '、'.join(mvars),
                        'from': '保持不动', 'to': '保持不动（语义未实测）'})
    changes.append(_claude_approval_change(_local_key()))
    return {'blocked': None, 'reformat_note': _json_reformat_note(text, data), 'changes': changes}


def _claude_key_mark(key: str) -> str:
    # Claude Code 的 sae()：trim 之后只留末尾 20 位，放进 approved 才算这把密钥可用。
    return str(key or '').strip()[-20:]


def _claude_approval_change(key: str) -> dict:
    """预览用。不写文件，也不把密钥末尾 20 位露出去。"""
    state = '未放行'
    path = _claude_user_config_path()
    mark = _claude_key_mark(key)
    if not path.is_file():
        state = '还没有这个文件'
    else:
        try:
            data, _ = _json_read(path)
        except RouteError:
            state = '读不了'
        else:
            block = data.get('customApiKeyResponses')
            approved = block.get('approved') if isinstance(block, dict) else None
            if isinstance(approved, list) and mark in approved:
                state = '已放行'
    return {'kind': 'set', 'key': '~/.claude.json customApiKeyResponses.approved',
            'from': state, 'to': '放行网关密钥'}


def _approve_claude_gateway_key(key: str) -> bool:
    """VS Code 里的 Claude Code 会忽略未放行的自定义密钥，然后显示没登录。

    只放行本网关这一条：末尾 20 位写进 ~/.claude.json 的
    customApiKeyResponses.approved。已经在里面就不改文件。
    排版对不上标准 JSON 时停手，避免把整份用户配置重排掉。
    """
    mark = _claude_key_mark(key)
    if not mark:
        raise RouteError('网关密钥是空的，无法给 Claude Code 放行。')
    path = _claude_user_config_path()
    if not path.is_file():
        _json_write(path, {'customApiKeyResponses': {'approved': [mark], 'rejected': []}})
        return True
    data, text = _json_read(path)
    block = data.get('customApiKeyResponses')
    if block is None:
        block = {}
    elif not isinstance(block, dict):
        raise RouteError('~/.claude.json 的 customApiKeyResponses 不是对象，已停止，未改登录许可。')
    approved = block.get('approved')
    if approved is None:
        approved = []
    elif not isinstance(approved, list):
        raise RouteError('~/.claude.json 的 customApiKeyResponses.approved 不是数组，已停止，未改登录许可。')
    rejected = block.get('rejected')
    if rejected is not None and not isinstance(rejected, list):
        raise RouteError('~/.claude.json 的 customApiKeyResponses.rejected 不是数组，已停止，未改登录许可。')
    in_rejected = isinstance(rejected, list) and mark in rejected
    if mark in approved and not in_rejected:
        return False
    if _json_dump(data) != text:
        raise RouteError('~/.claude.json 的排版和标准 JSON 不一致，已停止，没有改登录许可。')
    if in_rejected:
        block['rejected'] = [item for item in rejected if item != mark]
    if mark not in approved:
        approved.append(mark)
    block['approved'] = approved
    data['customApiKeyResponses'] = block
    _json_write(path, data, text)
    return True


def _revoke_claude_gateway_key(key: str) -> bool:
    """断开时只撤本网关这一条放行。别的密钥标记不动。"""
    mark = _claude_key_mark(key)
    path = _claude_user_config_path()
    if not mark or not path.is_file():
        return False
    data, text = _json_read(path)
    block = data.get('customApiKeyResponses')
    if not isinstance(block, dict):
        return False
    approved = block.get('approved')
    if not isinstance(approved, list) or mark not in approved:
        return False
    if _json_dump(data) != text:
        raise RouteError('~/.claude.json 的排版和标准 JSON 不一致，已停止，没有改登录许可。')
    block['approved'] = [item for item in approved if item != mark]
    _json_write(path, data, text)
    return True


def _claude_apply(models) -> dict:
    path = _claude_path()
    data, text = _json_read(path)
    env = data.get('env')
    if not isinstance(env, dict):
        raise RouteError('settings.json 里没有 env 对象，已停止。')
    # 先放行。失败就停，避免 settings 已经指向网关、插件却仍认为没登录。
    _approve_claude_gateway_key(_local_key())
    backup = _backup('claude-code', path)
    env['ANTHROPIC_BASE_URL'] = anthropic_base()
    env['ANTHROPIC_API_KEY'] = _local_key()
    _json_write(path, data, text)
    return {'backup_dir': backup, 'written': [
        'env.ANTHROPIC_BASE_URL', 'env.ANTHROPIC_API_KEY',
        '~/.claude.json customApiKeyResponses.approved']}


# --- opencode

def _opencode() -> dict:
    path = _opencode_path()
    exists = path.is_file()
    cur = None
    provs = []
    if exists:
        try:
            data, _ = _json_read(path)
            prov = data.get('provider') if isinstance(data.get('provider'), dict) else {}
            provs = sorted(prov)
            entry = prov.get(OPENCODE_PROVIDER_ID) or {}
            opt = entry.get('options') if isinstance(entry.get('options'), dict) else {}
            cur = opt.get('baseURL')
        except RouteError:
            provs = []
    return {'config_path': str(path), 'config_display': '~/.config/opencode/opencode.json',
            'exists': exists, 'connected': bool(cur) and cur.rstrip('/') == openai_base().rstrip('/'),
            'current_base_url': cur, 'current_models': None,
            'note': ('已有 %d 家供应商：%s。只新增 provider.%s，不改它们。'
                     % (len(provs), '、'.join(provs), OPENCODE_PROVIDER_ID)) if provs
                    else '没有读到 provider（文件可能还没生成）',
            'can_write': True, 'dialect': 'openai-compatible',
            'writes': 'provider.%s（新增）' % OPENCODE_PROVIDER_ID}


def _opencode_preview(models) -> dict:
    path = _opencode_path()
    if not path.is_file():
        return {'blocked': '找不到 ' + str(path) + '；先装上 opencode 并至少启动一次。'}
    data, text = _json_read(path)
    prov = data.get('provider')
    if prov is not None and not isinstance(prov, dict):
        return {'blocked': str(path) + ' 的 provider 不是对象，已停止。'}
    prov = prov or {}
    existing = prov.get(OPENCODE_PROVIDER_ID)
    # `options` 可能是字符串/数字（手工改坏过）—— 直取 .get 会 AttributeError → 500。
    # 同一文件 :346 对同一形状已有 isinstance 守卫，这里补齐，口径一致（复查轮 4 的 #3）。
    existing_opts = existing.get('options') if isinstance(existing, dict) else None
    if not isinstance(existing_opts, dict):
        existing_opts = {}
    changes = [{
        'kind': 'replace' if isinstance(existing, dict) else 'create',
        'key': 'provider.%s' % OPENCODE_PROVIDER_ID,
        'from': existing_opts.get('baseURL'),
        'to': openai_base(),
    }, {
        'kind': 'set', 'key': 'provider.%s.options.apiKey' % OPENCODE_PROVIDER_ID,
        'from': _mask(existing_opts.get('apiKey')),
        'to': _mask(_local_key()),
    }, {
        'kind': 'set', 'key': 'provider.%s.models' % OPENCODE_PROVIDER_ID,
        'from': len((existing or {}).get('models') or {}) if isinstance(existing, dict) else 0,
        'to': '%d 个模型' % len(models),
    }]
    others = sorted(k for k in prov if k != OPENCODE_PROVIDER_ID)
    if others:
        changes.append({'kind': 'keep', 'key': 'provider.' + ' / provider.'.join(others),
                        'from': '保持不动', 'to': '保持不动'})
    return {'blocked': None, 'reformat_note': _json_reformat_note(text, data), 'changes': changes}


def _opencode_apply(models) -> dict:
    path = _opencode_path()
    data, text = _json_read(path)
    prov = data.get('provider')
    if prov is not None and not isinstance(prov, dict):
        raise RouteError('opencode.json 的 provider 不是对象，已停止。')
    if not isinstance(prov, dict):
        # 键存在但值是 null：`setdefault('provider', {})` 会**原样返回 None**，
        # 下一行 prov[...] 就 TypeError → 500（预览那边用 `prov or {}` 容错，
        # apply 这边没对齐 —— 复查轮 7 的 #2）。显式替换成空对象。
        prov = {}
        data['provider'] = prov
    backup = _backup('opencode', path)
    prov[OPENCODE_PROVIDER_ID] = {
        'name': PRISM_DISPLAY,
        'npm': '@ai-sdk/openai-compatible',
        'options': {'baseURL': openai_base(), 'apiKey': _local_key()},
        'models': {cid: {'name': cid} for cid in models},
    }
    _json_write(path, data, text)
    return {'backup_dir': backup,
            'written': ['provider.%s（%d 个模型）' % (OPENCODE_PROVIDER_ID, len(models))]}


# --- hermes（按行处理，见文件头约束 1）

_HERMES_ID = re.compile(r'^(\s*)%s\s*:\s*(?:#.*)?$' % re.escape(HERMES_PROVIDER_ID))
_HERMES_API = re.compile(r'^\s+api\s*:\s*(\S+)')
_HERMES_KEY = re.compile(r'^\s+api_key\s*:\s*(\S+)')


def _yaml_scalar(text):
    """剥掉按行写入时加的 JSON 引号（`api: "http://…"`）。

    写入侧特意加引号（URL 里的 `//` 与中文名在 YAML 裸标量里容易踩坑），读取侧不剥的话
    `connected` 与预览里的 from 会带着引号去比 —— 实测过：Hermes 已经通了，Prism 还显示
    「未接入」（功能正常、显示错）。只影响读取，写入格式一个字不动。"""
    t = str(text or '').strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ('"', "'"):
        return t[1:-1]
    return t


def _hermes_provider_span(lines: list[str]):
    """返回 ((providers 段起, 止), (prism 块起, 止))。

    找不到的部分一律给 (None, None) —— **两层的形状永远一致**，调用方可以无条件解包。
    （第一版在没有 providers 段时直接 `return None, None`，调用方 `(a,b),(c,d)=...`
    当场 TypeError。契约要么永远一致，要么就别用解包。）
    """
    top = None
    for i, ln in enumerate(lines):
        if re.match(r'^providers\s*:\s*(?:#.*)?$', ln):
            top = i
            break
    if top is None:
        return (None, None), (None, None)
    end = len(lines)
    for j in range(top + 1, len(lines)):
        s = lines[j]
        if not s.strip() or s.lstrip().startswith('#'):
            continue
        if not s[:1].isspace():            # 回到顶层了
            end = j
            break
    pstart = pend = None
    for j in range(top + 1, end):
        m = _HERMES_ID.match(lines[j])
        if not m:
            continue
        indent = len(m.group(1))
        pstart = j
        pend = end
        for k in range(j + 1, end):
            s = lines[k]
            if not s.strip() or s.lstrip().startswith('#'):
                continue
            cur = len(s) - len(s.lstrip())
            if cur <= indent:
                pend = k
                break
        break
    return (top, end), (pstart, pend)


def _hermes_block(models) -> list[str]:
    # api / api_key 走 json.dumps（YAML 双引号标量）：与下面 model 行同款，值里的
    # YAML 特殊字符（#、: 、引号）不再依赖"当前字符集恰好安全"。
    out = ['  %s:' % HERMES_PROVIDER_ID,
           '    api: %s' % json.dumps(openai_base(), ensure_ascii=False),
           '    name: %s' % PRISM_DISPLAY,
           '    api_key: %s' % json.dumps(_local_key(), ensure_ascii=False)]
    if models:
        out.append('    models:')
        for cid in models:
            out.append('      %s:' % json.dumps(cid, ensure_ascii=False))
            out.append('        name: %s' % json.dumps(cid, ensure_ascii=False))
        out.append('    default_model: %s' % json.dumps(models[0], ensure_ascii=False))
    return out


def _hermes_read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding='utf-8-sig').splitlines()
    except OSError as exc:
        raise RouteError('读 ' + str(path) + ' 失败：' + type(exc).__name__ + ': ' + str(exc)) from None


def _hermes() -> dict:
    path = _hermes_path()
    exists = path.is_file()
    cur = None
    note = ''
    if exists:
        try:
            lines = _hermes_read_lines(path)
            (top, _), (ps, pe) = _hermes_provider_span(lines)
            if ps is not None:
                for k in range(ps, pe):
                    m = _HERMES_API.match(lines[k])
                    if m:
                        cur = _yaml_scalar(m.group(1))
                        break
            if cur is None:
                note = '还没有 providers.%s' % HERMES_PROVIDER_ID
            else:
                note = 'providers.%s 已存在' % HERMES_PROVIDER_ID
        except RouteError as exc:
            note = str(exc)
    return {'config_path': str(path),
            'config_display': '%LOCALAPPDATA%/hermes/config.yaml',
            'exists': exists, 'connected': bool(cur) and cur.rstrip('/') == openai_base().rstrip('/'),
            'current_base_url': cur, 'current_models': None, 'note': note,
            'can_write': True, 'dialect': 'openai-compatible',
            'writes': 'providers.%s（新增/替换）' % HERMES_PROVIDER_ID}


def _hermes_preview(models) -> dict:
    path = _hermes_path()
    if not path.is_file():
        return {'blocked': '找不到 ' + str(path) + '；先装上 Hermes 并至少启动一次。'}
    lines = _hermes_read_lines(path)
    (top, end), (ps, pe) = _hermes_provider_span(lines)
    cur = None
    if ps is not None:
        for k in range(ps, pe):
            m = _HERMES_API.match(lines[k])
            if m:
                cur = _yaml_scalar(m.group(1))
                break
    changes = [{
        'kind': 'replace' if ps is not None else ('create' if top is not None else 'append'),
        'key': 'providers.%s' % HERMES_PROVIDER_ID,
        'from': cur, 'to': openai_base(),
    }, {
        'kind': 'set', 'key': 'providers.%s.models' % HERMES_PROVIDER_ID,
        'from': (pe - ps - 1) if ps is not None else 0,
        'to': '%d 个模型' % len(models),
    }]
    comments = sum(1 for s in lines if s.lstrip().startswith('#'))
    if comments:
        changes.append({'kind': 'keep', 'key': '文件里的 %d 行注释' % comments,
                        'from': '保持不动', 'to': '保持不动（按行插入，不做 YAML 往返）'})
    changes.append({'kind': 'keep', 'key': 'model（全局 default / provider / base_url）',
                    'from': '保持不动', 'to': '保持不动'})
    return {'blocked': None, 'reformat_note': None, 'changes': changes}


def _hermes_apply(models) -> dict:
    path = _hermes_path()
    lines = _hermes_read_lines(path)
    (top, end), (ps, pe) = _hermes_provider_span(lines)
    block = _hermes_block(models)
    if ps is not None:
        new = lines[:ps] + block + lines[pe:]
    elif top is not None:
        new = lines[:end] + block + lines[end:]
    else:
        new = lines + ([''] if lines and lines[-1].strip() else []) + ['providers:'] + block
    backup = _backup('hermes', path)
    text = '\n'.join(new) + '\n'
    _replace_text(path, text)
    # 写完复查：注释必须一行不少（这是不用 yaml 往返的全部理由）
    after = path.read_text(encoding='utf-8-sig').splitlines()
    if sum(1 for s in after if s.lstrip().startswith('#')) != \
       sum(1 for s in lines if s.lstrip().startswith('#')):
        shutil.copy2(Path(backup) / path.name, path)
        raise RouteError('写完后注释行数变了，已自动回滚到备份。请手工检查 ' + str(path))
    return {'backup_dir': backup,
            'written': ['providers.%s（%d 个模型）' % (HERMES_PROVIDER_ID, len(models))]}


# ---------------------------------------------------------------- 断开
#
# 只撤回仍等于我们写过的网关地址和密钥的字段。用户已经改过的字段留在原地，
# 并报错。写之前先备份。地址和密钥都还对得上时，opencode / Hermes 整块撤掉
# （那一块是接入时加的）；只对得上一项就只删那一项。

def _same_text(a, b) -> bool:
    return a == b


def _same_url(a, b) -> bool:
    return str(a or '').rstrip('/') == str(b or '').rstrip('/')


def _ours_or_dirty(current, expected, same) -> str | None:
    """'clear' 表示还是我们写的，可以撤；'dirty' 表示用户改过；None 表示字段空着。"""
    if current is None:
        return None
    if isinstance(current, str) and not current.strip():
        return None
    if not isinstance(current, str):
        return 'dirty'
    if same(current, expected):
        return 'clear'
    return 'dirty'


def _finish_disconnect(cleared, dirty, backup, written) -> dict:
    if not cleared:
        if dirty:
            raise RouteError('这些字段已被改过，没有断开：' + '、'.join(dirty))
        return {'backup_dir': None, 'written': []}
    if dirty:
        raise RouteError('已撤回仍是本网关的字段；这些字段已被改过，没有动：' + '、'.join(dirty))
    return {'backup_dir': backup, 'written': written}


def _toml_section_span(text: str, name: str):
    """返回 [name] 段正文的 (起, 止)。找不到返回 None。起止是原文下标。"""
    sections = list(re.finditer(r'(?m)^\[([^\r\n]+)\]\s*$', text))
    target = next((m for m in sections if m.group(1) == name), None)
    if target is None:
        return None
    end = next((m.start() for m in sections if m.start() > target.start()), len(text))
    return target.end(), end


def _toml_drop_keys(body: str, keys: set) -> str:
    out = []
    for line in body.splitlines(keepends=True):
        hit = re.match(r'\s*([A-Za-z0-9_]+)\s*=', line)
        if hit and hit.group(1) in keys:
            continue
        out.append(line)
    return ''.join(out)


def _codex_disconnect() -> dict:
    path = _codex_path()
    if not path.is_file():
        raise RouteError('找不到 ' + str(path) + '；没有可撤回的配置。')
    try:
        text = path.read_text(encoding='utf-8-sig')
        import tomllib
        data = tomllib.loads(text)
    except OSError as exc:
        raise RouteError('读 ' + str(path) + ' 失败：' + type(exc).__name__ + ': ' + str(exc)) from None
    except Exception as exc:                   # noqa: BLE001 - toml 解析失败要变成可读的拒绝
        raise RouteError('读 ' + str(path) + ' 失败：' + type(exc).__name__ + ': ' + str(exc)) from None
    prov = (data.get('model_providers') or {}).get('custom') or {}
    if not isinstance(prov, dict):
        prov = {}
    cleared, dirty = [], []
    url_flag = _ours_or_dirty(prov.get('base_url'), openai_base(), _same_url)
    key_flag = _ours_or_dirty(prov.get('experimental_bearer_token'), _local_key(), _same_text)
    if url_flag == 'clear':
        cleared.append('base_url')
    elif url_flag == 'dirty':
        dirty.append('model_providers.custom.base_url')
    if key_flag == 'clear':
        cleared.append('experimental_bearer_token')
    elif key_flag == 'dirty':
        dirty.append('model_providers.custom.experimental_bearer_token')
    if not cleared:
        return _finish_disconnect(cleared, dirty, None, [])
    span = _toml_section_span(text, 'model_providers.custom')
    if span is None:
        raise RouteError('找不到 model_providers.custom，已停止，未做任何改动。')
    start, end = span
    new_text = text[:start] + _toml_drop_keys(text[start:end], set(cleared)) + text[end:]
    try:
        now = path.read_text(encoding='utf-8-sig')
    except OSError as exc:
        raise RouteError('写前重读失败：' + str(exc)) from None
    if now != text:
        raise RouteError('这个文件在读取之后被其他程序改动了，已停止，未做任何改动：' + str(path))
    backup = _backup('codex', path)
    _replace_text(path, new_text)
    return _finish_disconnect(cleared, dirty, backup,
                              ['model_providers.custom.' + k for k in cleared])


def _claude_disconnect() -> dict:
    path = _claude_path()
    if not path.is_file():
        raise RouteError('找不到 ' + str(path) + '；没有可撤回的配置。')
    data, text = _json_read(path)
    env = data.get('env')
    if not isinstance(env, dict):
        raise RouteError('settings.json 里没有 env 对象，已停止。')
    cleared, dirty = [], []
    url_flag = _ours_or_dirty(env.get('ANTHROPIC_BASE_URL'), anthropic_base(), _same_url)
    key_flag = _ours_or_dirty(env.get('ANTHROPIC_API_KEY'), _local_key(), _same_text)
    if url_flag == 'clear':
        cleared.append('ANTHROPIC_BASE_URL')
    elif url_flag == 'dirty':
        dirty.append('env.ANTHROPIC_BASE_URL')
    if key_flag == 'clear':
        cleared.append('ANTHROPIC_API_KEY')
    elif key_flag == 'dirty':
        dirty.append('env.ANTHROPIC_API_KEY')
    if not cleared:
        return _finish_disconnect(cleared, dirty, None, [])
    # 密钥还是我们写的才撤放行。用户改过的密钥留着，放行记录也留着。
    revoked = False
    if 'ANTHROPIC_API_KEY' in cleared:
        revoked = _revoke_claude_gateway_key(_local_key())
    backup = _backup('claude-code', path)
    for key in cleared:
        env.pop(key, None)
    _json_write(path, data, text)
    written = ['env.' + k for k in cleared]
    if revoked:
        written.append('~/.claude.json customApiKeyResponses.approved')
    return _finish_disconnect(cleared, dirty, backup, written)


def _opencode_disconnect() -> dict:
    path = _opencode_path()
    if not path.is_file():
        raise RouteError('找不到 ' + str(path) + '；没有可撤回的配置。')
    data, text = _json_read(path)
    prov = data.get('provider')
    if prov is None:
        return {'backup_dir': None, 'written': []}
    if not isinstance(prov, dict):
        raise RouteError('opencode.json 的 provider 不是对象，已停止。')
    entry = prov.get(OPENCODE_PROVIDER_ID)
    if not isinstance(entry, dict):
        return {'backup_dir': None, 'written': []}
    opts = entry.get('options') if isinstance(entry.get('options'), dict) else {}
    cleared, dirty = [], []
    url_flag = _ours_or_dirty(opts.get('baseURL'), openai_base(), _same_url)
    key_flag = _ours_or_dirty(opts.get('apiKey'), _local_key(), _same_text)
    if url_flag == 'clear':
        cleared.append('baseURL')
    elif url_flag == 'dirty':
        dirty.append('provider.prism.options.baseURL')
    if key_flag == 'clear':
        cleared.append('apiKey')
    elif key_flag == 'dirty':
        dirty.append('provider.prism.options.apiKey')
    if not cleared:
        return _finish_disconnect(cleared, dirty, None, [])
    backup = _backup('opencode', path)
    # 地址和密钥都还是我们写的：整块是接入时加的，一起撤掉。
    if 'baseURL' in cleared and 'apiKey' in cleared:
        prov.pop(OPENCODE_PROVIDER_ID, None)
    else:
        if 'baseURL' in cleared:
            opts.pop('baseURL', None)
        if 'apiKey' in cleared:
            opts.pop('apiKey', None)
        entry['options'] = opts
    _json_write(path, data, text)
    return _finish_disconnect(cleared, dirty, backup,
                              ['provider.prism.' + k for k in cleared])


def _hermes_disconnect() -> dict:
    path = _hermes_path()
    if not path.is_file():
        raise RouteError('找不到 ' + str(path) + '；没有可撤回的配置。')
    lines = _hermes_read_lines(path)
    _span, (ps, pe) = _hermes_provider_span(lines)
    if ps is None:
        return {'backup_dir': None, 'written': []}
    api = key = None
    for i in range(ps, pe):
        hit = _HERMES_API.match(lines[i])
        if hit:
            api = _yaml_scalar(hit.group(1))
        hit_key = _HERMES_KEY.match(lines[i])
        if hit_key:
            key = _yaml_scalar(hit_key.group(1))
    cleared, dirty = [], []
    url_flag = _ours_or_dirty(api, openai_base(), _same_url)
    key_flag = _ours_or_dirty(key, _local_key(), _same_text)
    if url_flag == 'clear':
        cleared.append('api')
    elif url_flag == 'dirty':
        dirty.append('providers.prism.api')
    if key_flag == 'clear':
        cleared.append('api_key')
    elif key_flag == 'dirty':
        dirty.append('providers.prism.api_key')
    if not cleared:
        return _finish_disconnect(cleared, dirty, None, [])
    if 'api' in cleared and 'api_key' in cleared:
        new = lines[:ps] + lines[pe:]
    else:
        new = []
        for i, ln in enumerate(lines):
            if ps <= i < pe:
                if 'api' in cleared and re.match(r'^\s+api\s*:', ln):
                    continue
                if 'api_key' in cleared and re.match(r'^\s+api_key\s*:', ln):
                    continue
            new.append(ln)
    backup = _backup('hermes', path)
    _replace_text(path, '\n'.join(new) + '\n')
    after = path.read_text(encoding='utf-8-sig').splitlines()
    if sum(1 for s in after if s.lstrip().startswith('#')) != \
       sum(1 for s in lines if s.lstrip().startswith('#')):
        shutil.copy2(Path(backup) / path.name, path)
        raise RouteError('写完后注释行数变了，已自动回滚到备份。请手工检查 ' + str(path))
    return _finish_disconnect(cleared, dirty, backup,
                              ['providers.prism.' + k for k in cleared])


# ---------------------------------------------------------------- Copilot（直连来源，不经网关）

COPILOT_PREFIX = 'Prism · '
COPILOT_VENDOR = 'customendpoint'
COPILOT_MAX_OUTPUT = 32768
COPILOT_DEFAULT_WINDOW = 272000
_GATEWAY_MARK = '127.0.0.1:8317'


def _appdata() -> Path:
    raw = os.environ.get('APPDATA')
    return Path(raw) if raw else _home() / 'AppData' / 'Roaming'


def _vscode_user_dir() -> Path:
    return _appdata() / 'Code' / 'User'


def _copilot_path() -> Path:
    return _vscode_user_dir() / 'chatLanguageModels.json'


def _copilot_config():
    """来源凭据在网关读到的 config 里。测试桩掉这个函数，避免打到本机 8317。"""
    return bridge.gateway_get('config')


def _join_api_path(base: str, suffix: str) -> str:
    """把 suffix 接到 base 后面。base 已经带了整段或它的前缀时不重复拼。

    codex-api-key 补 /v1/responses：base 已是 .../v1 时只补 /responses。
    openai-compatibility 补 /chat/completions。
    """
    base = str(base or '').rstrip('/')
    parts = [p for p in str(suffix or '').strip('/').split('/') if p]
    if not base or not parts:
        return base
    full = '/' + '/'.join(parts)
    if base.endswith(full):
        return base
    for n in range(len(parts) - 1, 0, -1):
        prefix = '/' + '/'.join(parts[:n])
        if base.endswith(prefix):
            return base + '/' + '/'.join(parts[n:])
    return base + full


def _copilot_api(section, model_id):
    """这个模型该走哪种接口。

    自定义来源一律写在 codex-api-key，那段本来表示 Responses。
    DeepSeek 没有 Responses，OpenCode Go 只提供 /chat/completions。
    """
    low = str(model_id or '').lower()
    if low.startswith('deepseek'):
        return 'chat-completions', '/chat/completions'
    if section == 'codex-api-key':
        return 'responses', '/v1/responses'
    return 'chat-completions', '/chat/completions'


def _opencode_session_header(url):
    """OpenCode Go 缺 x-opencode-session 会 400。

    VS Code 还不会按对话自动带这个头。固定 ses_prism 让编辑和 Agent 的请求能过。
    ponytail: 所有对话共用这一个值，提示缓存会串。等 VS Code 按对话带头发了再删。
    """
    low = str(url or '').lower()
    if '://opencode.ai/' not in low and not low.startswith('https://opencode.ai'):
        return None
    return {'x-opencode-session': 'ses_prism'}


def _copilot_request_headers(url, api_key):
    """customendpoint 的 apiKey 在 VS Code 里是 secret。

    明文密钥解不进请求，Authorization 是空的，上游回 401 Missing API key，
    Agent 代理再包成 502。Custom Endpoint 允许 requestHeaders 覆盖 Authorization。
    """
    headers = {'Authorization': 'Bearer ' + str(api_key or '')}
    session = _opencode_session_header(url)
    if session:
        headers.update(session)
    return headers


def _window_of(row) -> int:
    window = row.get('context_window') if isinstance(row, dict) else None
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        return COPILOT_DEFAULT_WINDOW
    return window


def _copilot_rows(include_inactive: bool = False) -> list[dict]:
    """expose 非空的来源，收成 Copilot 的 provider 块。

    默认只收已启用的，接入时用。include_inactive 只给断开用：来源停用后
    文件里那块仍是上次写的，不能因为这次不会再写它就把这块当成用户改过。
    启用判据复用 route_selector.active：openai-compatibility 看 disabled，
    其余看 excluded-models 里有没有 *。auth-file 没有直连地址和密钥的写法，不收。
    地址里出现 127.0.0.1:8317 的来源整段跳过，直连不能写网关。
    """
    from . import sources as S
    plan = S._read_plan()
    config = _copilot_config()
    catalog = _catalog_efforts()
    if not isinstance(config, dict):
        raise RouteError('读不到来源配置，已停止。')
    out = []
    for row in plan.get('providers') or []:
        if not isinstance(row, dict):
            continue
        section = row.get('section')
        if section not in ('codex-api-key', 'openai-compatibility'):
            continue
        expose = [a for a in (row.get('expose') or []) if isinstance(a, str) and a.strip()]
        if not expose:
            continue
        hits = S._match_entries(config, row)
        if len(hits) != 1 or not isinstance(hits[0], dict):
            continue
        entry = hits[0]
        if not include_inactive and not rs.active(entry, section):
            continue
        key = S._api_key_of(entry)
        if not isinstance(key, str) or not key:
            continue
        base = str(row.get('base_url') or entry.get('base-url') or '').rstrip('/')
        if not base or _GATEWAY_MARK in base:
            continue
        by_alias = {}
        for item in row.get('models') or []:
            if not isinstance(item, dict):
                continue
            alias = str(item.get('alias') or '').strip()
            name = str(item.get('name') or '').strip()
            if alias and name:
                by_alias[alias] = name
        settings = row.get('model_settings') if isinstance(row.get('model_settings'), dict) else {}
        window = _window_of(row)
        label = str(row.get('label') or row.get('id') or '来源')
        head = S.effective_head(plan, 'copilot', row)
        models = []
        defaults = {}
        for alias in expose:
            upstream = by_alias.get(alias)
            if not upstream:
                continue
            # 有渠道头时选择器里的显示名带来源标签，发给上游的 id 仍是上游模型名。
            display = (label + ' · ' + alias) if head else alias
            # 自定义来源都落在 codex-api-key，但 DeepSeek 没有 Responses 接口。
            # OpenCode Go 的 deepseek-v4.1-flash 只在 /chat/completions；
            # 打到 /responses 时网关回 Missing API key。编辑和 Agent 读同一份列表。
            api_type, suffix = _copilot_api(section, upstream)
            url = _join_api_path(base, suffix)
            if _GATEWAY_MARK in url:
                continue
            model = {
                'id': upstream,
                'name': display,
                'url': url,
                'apiType': api_type,
                'toolCalling': True,
                'vision': True,
                'maxInputTokens': window - COPILOT_MAX_OUTPUT,
                'maxOutputTokens': COPILOT_MAX_OUTPUT,
            }
            # 明文 apiKey 不会被 VS Code 放进请求，密钥从这里带出去。
            headers = _copilot_request_headers(url, key)
            model['requestHeaders'] = headers
            # VS Code 对 supportsReasoningEffort 调用 .map。写成 true 时语言模型
            # 列表报 "t.map is not a function"，必须是字符串数组。
            # 编辑和 Agent 读同一份列表，这里补上之后两边都能选思考深度。
            levels, default = _lookup_effort(alias, upstream, settings.get(alias), catalog)
            model['supportsReasoningEffort'] = levels
            model['reasoningEffortFormat'] = (
                'responses' if api_type == 'responses' else 'chat-completions')
            models.append(model)
            if default in levels:
                defaults[upstream] = default
        if not models:
            continue
        kinds = {m.get('apiType') for m in models}
        block = {
            'vendor': COPILOT_VENDOR,
            'name': COPILOT_PREFIX + label,
            'apiKey': key,
            'apiType': next(iter(kinds)) if len(kinds) == 1 else 'responses',
            'models': models,
        }
        if defaults:
            block['settings'] = {mid: {'reasoningEffort': effort}
                                 for mid, effort in defaults.items()}
        out.append(block)
    return out


# 来源和目录都没声明档位时用这一套。Copilot 两个界面读同一份列表，
# 没有 supportsReasoningEffort 就不会出现思考深度。
COPILOT_EFFORT_FALLBACK = ('low', 'medium', 'high', 'xhigh', 'max', 'ultra')


def _effort_of(spec):
    """从 model_settings 的一档里取出力度列表和默认值。没有档位就返回空。"""
    if not isinstance(spec, dict):
        return [], ''
    levels = []
    for lv in spec.get('levels') or []:
        if isinstance(lv, str) and lv.strip() and lv.strip() not in levels:
            levels.append(lv.strip())
    default = str(spec.get('default') or '').strip()
    return levels, default


def _catalog_efforts():
    """目录里每个模型名对应的思考深度。读失败就当没有。"""
    try:
        data = json.loads(bridge.CATALOG_PATH.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}
    index = {}
    models = data.get('models') if isinstance(data, dict) else None
    if not isinstance(models, list):
        return {}
    for entry in models:
        if not isinstance(entry, dict):
            continue
        slug = str(entry.get('slug') or '').strip()
        levels = []
        for item in entry.get('supported_reasoning_levels') or []:
            if isinstance(item, dict):
                effort = str(item.get('effort') or '').strip()
            elif isinstance(item, str):
                effort = item.strip()
            else:
                continue
            if effort and effort not in levels:
                levels.append(effort)
        if not slug or not levels:
            continue
        default = str(entry.get('default_reasoning_level') or '').strip()
        if default not in levels:
            default = levels[0]
        hit = (levels, default)
        index[slug] = hit
        tail = slug.rsplit('/', 1)[-1]
        # 无头的那一行优先于 gly/模型名 这种副本。
        if tail not in index or slug == tail:
            index[tail] = hit
    return index


def _lookup_effort(alias, upstream, spec, catalog):
    """来源里勾过的档位优先，否则用目录，再没有就用标准阶梯。"""
    levels, default = _effort_of(spec)
    if levels:
        if default not in levels:
            default = levels[0]
        return levels, default
    table = catalog if isinstance(catalog, dict) else {}
    for key in (upstream, alias):
        hit = table.get(key)
        if hit:
            return hit
    return list(COPILOT_EFFORT_FALLBACK), 'medium'


def _guard_no_gateway(providers) -> None:
    for prov in providers:
        for model in prov.get('models') or []:
            if _GATEWAY_MARK in str((model or {}).get('url') or ''):
                raise RouteError('直连地址不能写成 127.0.0.1:8317，已停止，未做任何改动。')


def _copilot_merge(existing, providers) -> list:
    """只换掉名称以「Prism · 」开头的块，用户自己加的其它块原样留下。"""
    kept = []
    for item in existing:
        if isinstance(item, dict) and str(item.get('name') or '').startswith(COPILOT_PREFIX):
            continue
        kept.append(item)
    return kept + list(providers)


def _copilot_blocked_reason():
    user = _vscode_user_dir()
    if not user.is_dir():
        return '找不到 ' + str(user) + '；先装上 VS Code 并至少启动一次。'
    path = _copilot_path()
    if path.is_file():
        try:
            _json_read_array(path)
        except RouteError as exc:
            return str(exc)
    return None


def _copilot() -> dict:
    path = _copilot_path()
    user = _vscode_user_dir()
    exists = path.is_file()
    connected = False
    note = ''
    if not user.is_dir():
        note = '没有找到 VS Code 的用户目录'
    elif not exists:
        note = '还没有 chatLanguageModels.json'
    else:
        try:
            data, _ = _json_read_array(path)
            ours = [x for x in data if isinstance(x, dict)
                    and str(x.get('name') or '').startswith(COPILOT_PREFIX)]
            connected = bool(ours)
            note = ('已写入 %d 个来源' % len(ours)) if ours else '还没有 Prism 写入的模型'
        except RouteError as exc:
            note = str(exc)
    return {'config_path': str(path),
            'config_display': '%APPDATA%/Code/User/chatLanguageModels.json',
            'exists': bool(exists and user.is_dir()),
            'connected': connected, 'current_base_url': None, 'current_models': None,
            'note': note, 'can_write': user.is_dir(), 'dialect': 'copilot-direct',
            'writes': 'chatLanguageModels.json 里名称以「Prism · 」开头的 provider'}


def _copilot_preview(_models) -> dict:
    reason = _copilot_blocked_reason()
    if reason:
        return {'blocked': reason, 'reformat_note': None, 'changes': []}
    path = _copilot_path()
    data = []
    if path.is_file():
        data, _ = _json_read_array(path)
    providers = _copilot_rows()
    old_keys = {}
    kept_names = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '')
        if name.startswith(COPILOT_PREFIX):
            old_keys[name] = str(item.get('apiKey') or '')
        elif name:
            kept_names.append(name)
    changes = []
    for prov in providers:
        for model in prov['models']:
            changes.append({
                'kind': 'set',
                'key': prov['name'] + ' · ' + str(model.get('name') or ''),
                'from': None,
                'to': str(model.get('id') or '') + ' @ ' + str(model.get('url') or ''),
            })
        changes.append({
            'kind': 'set',
            'key': prov['name'] + '.apiKey',
            'from': _mask(old_keys.get(prov['name'])),
            'to': _mask(prov['apiKey']),
        })
    if kept_names:
        changes.append({'kind': 'keep', 'key': '、'.join(kept_names),
                        'from': '保持不动', 'to': '保持不动'})
    return {'blocked': None, 'reformat_note': None, 'changes': changes, 'direct': True}


def _copilot_apply(_models) -> dict:
    reason = _copilot_blocked_reason()
    if reason:
        raise RouteError(reason)
    path = _copilot_path()
    providers = _copilot_rows()
    _guard_no_gateway(providers)
    if path.is_file():
        data, text = _json_read_array(path)
        backup = _backup('copilot', path)
        _json_write(path, _copilot_merge(data, providers), text)
    else:
        backup = None
        _json_write(path, providers, None)
    return {'backup_dir': backup,
            'written': ['%s（%d 个模型）' % (p['name'], len(p['models'])) for p in providers]}


def _copilot_block_ours(item, want) -> bool:
    """地址和密钥都还等于这次会写的值，才算这块仍是我们的。"""
    if item.get('apiKey') != want.get('apiKey'):
        return False
    urls = [m.get('url') for m in (item.get('models') or [])
            if isinstance(m, dict) and m.get('url')]
    ours = ''
    for model in want.get('models') or []:
        if isinstance(model, dict) and model.get('url'):
            ours = model['url']
            break
    return bool(ours) and bool(urls) and all(_same_url(u, ours) for u in urls)


def _copilot_disconnect() -> dict:
    user = _vscode_user_dir()
    if not user.is_dir():
        raise RouteError('找不到 ' + str(user) + '；先装上 VS Code 并至少启动一次。')
    path = _copilot_path()
    if not path.is_file():
        return {'backup_dir': None, 'written': []}
    data, text = _json_read_array(path)
    # 停用的来源不在「这次会写」的名单里，但名称、地址、密钥仍对得上就还是我们的。
    desired = {p['name']: p for p in _copilot_rows(include_inactive=True)}
    kept = []
    cleared, dirty = [], []
    for item in data:
        if not isinstance(item, dict) or not str(item.get('name') or '').startswith(COPILOT_PREFIX):
            kept.append(item)
            continue
        want = desired.get(item.get('name'))
        if want and _copilot_block_ours(item, want):
            cleared.append(str(item.get('name')))
            continue
        dirty.append(str(item.get('name') or COPILOT_PREFIX))
        kept.append(item)
    if not cleared:
        return _finish_disconnect(cleared, dirty, None, [])
    backup = _backup('copilot', path)
    _json_write(path, kept, text)
    return _finish_disconnect(cleared, dirty, backup, cleared)


# ---------------------------------------------------------------- Copilot Agent（Agents 窗口，只开开关）

AGENT_HOST_KEY = 'chat.agentHost.byokModels.enabled'
_AGENT_HOST_RE = re.compile(
    r'("' + re.escape(AGENT_HOST_KEY) + r'"\s*:\s*)(true|false)\b')


def _vscode_settings_path() -> Path:
    return _vscode_user_dir() / 'settings.json'


def _agent_host_state(text: str):
    """true / false / None（没有这个键）/ 'other'（键在，但值不是布尔）。"""
    matched = _AGENT_HOST_RE.search(text)
    if matched:
        return matched.group(2) == 'true'
    if AGENT_HOST_KEY in text:
        return 'other'
    return None


def _patch_agent_host(text: str, on: bool) -> str:
    """只改这一个布尔。键已存在就替换 true/false；没有就插在最后一个 } 前面。

    其余字节不动，避免把用户的 settings.json 整文件重排。
    """
    value = 'true' if on else 'false'
    matched = _AGENT_HOST_RE.search(text)
    if matched:
        return text[:matched.start(2)] + value + text[matched.end(2):]
    if AGENT_HOST_KEY in text:
        raise RouteError(AGENT_HOST_KEY + ' 的值不是 true/false，已停止，未做任何改动。')
    close = text.rstrip()
    if not close.endswith('}'):
        raise RouteError('settings.json 的末尾不是 }，已停止，未做任何改动。')
    body = close[:-1].rstrip()
    if body.endswith('{') or body == '':
        inserted = body + '\n  "' + AGENT_HOST_KEY + '": ' + value + '\n}'
    else:
        comma = '' if body.endswith(',') else ','
        inserted = body + comma + '\n  "' + AGENT_HOST_KEY + '": ' + value + '\n}'
    if text.endswith('\n'):
        inserted += '\n'
    return inserted


def _copilot_agent_blocked():
    user = _vscode_user_dir()
    if not user.is_dir():
        return '找不到 ' + str(user) + '；先装上 VS Code 并至少启动一次。'
    path = _vscode_settings_path()
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding='utf-8-sig')
    except OSError as exc:
        return '读不了 ' + str(path) + '：' + type(exc).__name__ + ': ' + str(exc)
    if _agent_host_state(text) == 'other':
        return AGENT_HOST_KEY + ' 的值不是 true/false，已停止，未做任何改动。'
    if text.strip() and not text.rstrip().endswith('}'):
        return 'settings.json 的末尾不是 }，已停止，未做任何改动。'
    return None


def _copilot_agent() -> dict:
    path = _vscode_settings_path()
    user = _vscode_user_dir()
    exists = path.is_file()
    connected = False
    note = ''
    if not user.is_dir():
        note = '没有找到 VS Code 的用户目录'
    elif not exists:
        note = '还没有 settings.json'
    else:
        try:
            state = _agent_host_state(path.read_text(encoding='utf-8-sig'))
        except OSError as exc:
            state = None
            note = type(exc).__name__ + ': ' + str(exc)
        if not note:
            connected = state is True
            if state is True:
                note = 'Agents 窗口已打开自备模型'
            elif state == 'other':
                note = AGENT_HOST_KEY + ' 的值不是 true/false'
            else:
                note = 'Agents 窗口还没打开自备模型'
    return {'config_path': str(path),
            'config_display': '%APPDATA%/Code/User/settings.json',
            'exists': bool(exists and user.is_dir()),
            'connected': connected, 'current_base_url': None, 'current_models': None,
            'note': note, 'can_write': user.is_dir(), 'dialect': 'copilot-agent',
            'writes': 'settings.json 的 ' + AGENT_HOST_KEY}


def _copilot_agent_preview(_models) -> dict:
    reason = _copilot_agent_blocked()
    if reason:
        return {'blocked': reason, 'reformat_note': None, 'changes': [], 'direct': True}
    path = _vscode_settings_path()
    current = None
    if path.is_file():
        current = _agent_host_state(path.read_text(encoding='utf-8-sig'))
    shown = '（无）' if current is None else ('true' if current is True else 'false')
    return {'blocked': None, 'reformat_note': None, 'direct': True, 'changes': [{
        'kind': 'set', 'key': AGENT_HOST_KEY, 'from': shown, 'to': 'true',
    }]}


def _copilot_agent_apply(_models) -> dict:
    reason = _copilot_agent_blocked()
    if reason:
        raise RouteError(reason)
    path = _vscode_settings_path()
    if path.is_file():
        text = path.read_text(encoding='utf-8-sig')
        backup = _backup('copilot-agent', path)
        _replace_text(path, _patch_agent_host(text, True))
    else:
        backup = None
        _replace_text(path, '{\n  "' + AGENT_HOST_KEY + '": true\n}\n')
    return {'backup_dir': backup, 'written': [AGENT_HOST_KEY]}


def _copilot_agent_disconnect() -> dict:
    user = _vscode_user_dir()
    if not user.is_dir():
        raise RouteError('找不到 ' + str(user) + '；先装上 VS Code 并至少启动一次。')
    path = _vscode_settings_path()
    if not path.is_file():
        return {'backup_dir': None, 'written': []}
    text = path.read_text(encoding='utf-8-sig')
    state = _agent_host_state(text)
    if state == 'other':
        return _finish_disconnect([], [AGENT_HOST_KEY], None, [])
    if state is not True:
        return {'backup_dir': None, 'written': []}
    backup = _backup('copilot-agent', path)
    _replace_text(path, _patch_agent_host(text, False))
    return _finish_disconnect([AGENT_HOST_KEY], [], backup, [AGENT_HOST_KEY])


# ---------------------------------------------------------------- 注册表

AGENTS: tuple[dict, ...] = (
    {'id': 'codex', 'label': 'Codex', 'detect': _codex,
     'preview': _codex_preview, 'apply': None, 'disconnect': _codex_disconnect},
    {'id': 'claude-code', 'label': 'Claude Code', 'detect': _claude,
     'preview': _claude_preview, 'apply': _claude_apply, 'disconnect': _claude_disconnect},
    {'id': 'claude-code-desktop', 'label': 'Claude Code 桌面端', 'detect': _claude,
     'preview': _claude_preview, 'apply': _claude_apply, 'disconnect': _claude_disconnect,
     'note': '与 Claude Code 共用 ~/.claude/settings.json'},
    {'id': 'opencode', 'label': 'opencode', 'detect': _opencode,
     'preview': _opencode_preview, 'apply': _opencode_apply, 'disconnect': _opencode_disconnect},
    {'id': 'hermes', 'label': 'Hermes', 'detect': _hermes,
     'preview': _hermes_preview, 'apply': _hermes_apply, 'disconnect': _hermes_disconnect},
    {'id': 'copilot', 'label': 'Copilot 编辑', 'detect': _copilot,
     'preview': _copilot_preview, 'apply': _copilot_apply, 'disconnect': _copilot_disconnect,
     'note': '编辑聊天的模型列表，直连来源，不经过本地网关'},
    {'id': 'copilot-agent', 'label': 'Copilot Agent', 'detect': _copilot_agent,
     'preview': _copilot_agent_preview, 'apply': _copilot_agent_apply,
     'disconnect': _copilot_agent_disconnect,
     'note': '只打开 Agents 窗口的自备模型，不写模型文件'},
)

_BY_ID = {a['id']: a for a in AGENTS}


def get(agent_id: str) -> dict:
    a = _BY_ID.get(agent_id)
    if a is None:
        raise RouteError('未知的客户端：' + str(agent_id) + '（可用：'
                         + '、'.join(_BY_ID) + '）')
    return a


def connected_head_owners() -> set[str]:
    """当前已接入的代理端。渠道头冲突和网关别名只算这些。

    键和 sources.HEAD_STORE_AGENTS / HEAD_OWNER 同一套：桌面端并进 claude-code，
    Copilot Agent 不存渠道头。没接入的不在集合里。
    """
    fold = {'claude-code-desktop': 'claude-code'}
    keep = {'codex', 'claude-code', 'opencode', 'hermes', 'copilot'}
    owners = set()
    for a in AGENTS:
        owner = fold.get(a['id'], a['id'])
        if owner not in keep:
            continue
        try:
            state = a['detect']()
        except Exception:                          # noqa: BLE001 - 读配置失败就当没接入
            continue
        if isinstance(state, dict) and state.get('connected'):
            owners.add(owner)
    return owners


def list_agents() -> list[dict]:
    """给界面用：每个 agent 的注册信息 + 实测状态。全部只读，不写任何文件。"""
    out = []
    for a in AGENTS:
        item = {'id': a['id'], 'label': a['label']}
        try:
            item.update(a['detect']())
            item['error'] = None
        except Exception as exc:                   # noqa: BLE001
            item.update({'config_path': None, 'exists': False, 'connected': False,
                         'current_base_url': None, 'current_models': None,
                         'can_write': False, 'error': type(exc).__name__ + ': ' + str(exc)})
        # 注册表里补的说明要**并进** detect 的 note，不能盖掉它（detect 的 note 是实测出来的）
        extra = a.get('note')
        if extra:
            base = item.get('note') or ''
            item['note'] = (extra + '；' + base) if base else extra
        item['implemented'] = a['id'] == 'codex' or a['apply'] is not None
        out.append(item)
    return out


def preview_agent(agent_id: str) -> dict:
    """接入会改哪几行。**不写任何文件。**"""
    a = get(agent_id)
    models = client_models(agent_id)
    p = a['preview'](models)
    p['agent'] = agent_id
    p['label'] = a['label']
    p['models'] = models
    p['models_count'] = len(models)
    p['gateway_base'] = bridge.GATEWAY_BASE
    return p


def connect_agent(agent_id: str, payload: dict | None = None) -> dict:
    """执行接入。codex 走既有的 rs.connect_client；其余走本模块的适配器。

    非 codex 的适配器要求 payload['confirm'] is True —— 界面必须先展示过预览。
    disconnect 为 true 时改为断开：同样要 confirm，而且只撤回仍是我们写过的字段。
    """
    payload = payload or {}
    a = get(agent_id)
    if payload.get('disconnect') is True:
        if payload.get('confirm') is not True:
            raise RouteError('断开 %s 会改写它的配置文件，需要先确认（confirm: true）。' % a['label'])
        fn = a.get('disconnect')
        if fn is None:
            raise RouteError('%s 的断开还没实现。' % a['label'])
        result = dict(fn() or {})
        result.update({'agent': agent_id, 'label': a['label'], 'disconnected': True})
        return result
    if a['id'] == 'codex':
        # 既有路径，行为一个字不改：它自己会备份、比对 revision、校验 auth.json 未变。
        return bridge.connect_client(payload)
    if a['apply'] is None:
        raise RouteError('%s 的接入还没实现。' % a['label'])
    if payload.get('confirm') is not True:
        raise RouteError('接入 %s 会改写它的配置文件，需要先确认（confirm: true）。'
                         '先调 /api/connect/preview 看会改哪几行。' % a['label'])
    p = a['preview'](client_models(agent_id))
    if p.get('blocked'):
        raise RouteError(p['blocked'])
    result = a['apply'](client_models(agent_id))
    result.update({'agent': agent_id, 'label': a['label'],
                   'reformat_note': p.get('reformat_note'),
                   'restart': '改完要完全退出并重开 %s 才生效（配置只在启动时读一次）。' % a['label']})
    return result
