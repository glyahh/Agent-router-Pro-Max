"""把本机各家的编码 Agent 接到 Prism 的网关（8317）。

覆盖的 5 个（用户 2026-09-30 确认本轮完全不做 pi，所以它不在这里）：

    codex                 ~/.codex/config.toml                    /v1/responses
    claude-code           ~/.claude/settings.json                 /v1/messages
    claude-code-desktop   同一个 settings.json                    /v1/messages
    opencode              ~/.config/opencode/opencode.json        /v1/chat/completions
    hermes                %LOCALAPPDATA%/hermes/config.yaml       OpenAI 兼容

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

def client_models() -> list[str]:
    """当前 plan 里所有**被暴露**的客户端可见 ID（带渠道头），排序去重。

    口径与 rs.regen_catalog 一致：连"当前没启用的来源"也一并给出。理由是同一个——
    客户端只在启动时读一次配置，把全部已暴露的写进去，以后在 Prism 里换来源就不必
    重启客户端。真正能不能走通由网关按启用状态决定。
    """
    from . import sources as S
    plan = S._read_plan()
    out = []
    for p in plan.get('providers', []):
        for cid in S.row_client_ids(p):
            if cid not in out:
                out.append(cid)
    return sorted(out)


# ---------------------------------------------------------------- 各 agent 的适配

def _codex_path() -> Path:
    return _home() / '.codex' / 'config.toml'


def _claude_path() -> Path:
    return _home() / '.claude' / 'settings.json'


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
    return {'blocked': None, 'reformat_note': _json_reformat_note(text, data), 'changes': changes}


def _claude_apply(models) -> dict:
    path = _claude_path()
    data, text = _json_read(path)
    env = data.get('env')
    if not isinstance(env, dict):
        raise RouteError('settings.json 里没有 env 对象，已停止。')
    backup = _backup('claude-code', path)
    env['ANTHROPIC_BASE_URL'] = anthropic_base()
    env['ANTHROPIC_API_KEY'] = _local_key()
    _json_write(path, data, text)
    return {'backup_dir': backup, 'written': ['env.ANTHROPIC_BASE_URL', 'env.ANTHROPIC_API_KEY']}


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
                        cur = m.group(1)
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
                cur = m.group(1)
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
    # 临时名要**唯一到线程**：控制台是 ThreadingHTTPServer，两个并发的 /api/connect 若写同一个
    # 客户端文件，只带 pid 的临时名会互相覆盖（审查 NC-4）。加上线程 id 就互不干扰。
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
    # 写完复查：注释必须一行不少（这是不用 yaml 往返的全部理由）
    after = path.read_text(encoding='utf-8-sig').splitlines()
    if sum(1 for s in after if s.lstrip().startswith('#')) != \
       sum(1 for s in lines if s.lstrip().startswith('#')):
        shutil.copy2(Path(backup) / path.name, path)
        raise RouteError('写完后注释行数变了，已自动回滚到备份。请手工检查 ' + str(path))
    return {'backup_dir': backup,
            'written': ['providers.%s（%d 个模型）' % (HERMES_PROVIDER_ID, len(models))]}


# ---------------------------------------------------------------- 注册表

AGENTS: tuple[dict, ...] = (
    {'id': 'codex', 'label': 'Codex', 'detect': _codex,
     'preview': _codex_preview, 'apply': None},
    {'id': 'claude-code', 'label': 'Claude Code', 'detect': _claude,
     'preview': _claude_preview, 'apply': _claude_apply},
    {'id': 'claude-code-desktop', 'label': 'Claude Code 桌面端', 'detect': _claude,
     'preview': _claude_preview, 'apply': _claude_apply,
     'note': '与 Claude Code 共用 ~/.claude/settings.json'},
    {'id': 'opencode', 'label': 'opencode', 'detect': _opencode,
     'preview': _opencode_preview, 'apply': _opencode_apply},
    {'id': 'hermes', 'label': 'Hermes', 'detect': _hermes,
     'preview': _hermes_preview, 'apply': _hermes_apply},
)

_BY_ID = {a['id']: a for a in AGENTS}


def get(agent_id: str) -> dict:
    a = _BY_ID.get(agent_id)
    if a is None:
        raise RouteError('未知的客户端：' + str(agent_id) + '（可用：'
                         + '、'.join(_BY_ID) + '）')
    return a


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
    models = client_models()
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
    """
    payload = payload or {}
    a = get(agent_id)
    if a['id'] == 'codex':
        # 既有路径，行为一个字不改：它自己会备份、比对 revision、校验 auth.json 未变。
        return bridge.connect_client(payload)
    if a['apply'] is None:
        raise RouteError('%s 的接入还没实现。' % a['label'])
    if payload.get('confirm') is not True:
        raise RouteError('接入 %s 会改写它的配置文件，需要先确认（confirm: true）。'
                         '先调 /api/connect/preview 看会改哪几行。' % a['label'])
    p = a['preview'](client_models())
    if p.get('blocked'):
        raise RouteError(p['blocked'])
    result = a['apply'](client_models())
    result.update({'agent': agent_id, 'label': a['label'],
                   'reformat_note': p.get('reformat_note'),
                   'restart': '改完要完全退出并重开 %s 才生效（配置只在启动时读一次）。' % a['label']})
    return result
