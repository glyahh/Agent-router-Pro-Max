"""Agent 适配器回归测试。

跑法（在项目根目录）：
    python app\\tests\\test_agents.py

分两层，故意的：

  * **预览**：直接读**真实的**配置文件。预览是只读的，所以可以拿真文件验解析器
    —— 这比拿合成样本验更有价值（Hermes 那份有 30 多行注释和两层嵌套）。
  * **写入**：只在合成的临时副本上做，并把 bridge.ROOT 重定向到临时目录。
    **不把真实配置（含密钥）复制到 %TEMP%** —— 那个毛病以前犯过（HANDOFF §5.5
    记录过 %TEMP% 里躺着 30 个含明文凭据的副本）。生产文件全程只读、逐字节不变。
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT / 'script'))

from core import agents, bridge   # noqa: E402

fails = []


def check(name, got, want):
    ok = got == want
    print(('  PASS  ' if ok else '  FAIL  ') + name)
    if not ok:
        print('        got : %r' % (got,))
        print('        want: %r' % (want,))
        fails.append(name)


def check_true(name, cond, detail=''):
    check(name + (('  -> ' + detail) if (detail and not cond) else ''), bool(cond), True)


# ═══════════════════════════════════════════ 1. 预览：拿真实文件验解析

print('== 预览（读真实配置，只读）==')
for aid in ('codex', 'claude-code', 'opencode', 'hermes'):
    p = agents.preview_agent(aid)
    print('  %-14s blocked=%r models=%d reformat=%r'
          % (aid, bool(p.get('blocked')), p['models_count'], p.get('reformat_note')))
    if p.get('blocked'):
        print('        blocked: %s' % p['blocked'])
    for c in p.get('changes', []):
        print('        %-7s %-46s %r -> %r' % (c['kind'], c['key'], c['from'], c['to']))

# codex 是个特例，**它现在本来就该是 blocked**：用户 2026-09-30 把 Codex 切回了纯官方
# 登录（HANDOFF §6.5），config.toml 里 model_provider 是注释掉的。而 rs.patch_client_text
# 的第一行就是 `if parsed.get('model_provider')!='custom': raise`——前缀对不上时它改的
# 不是用户正在用的那个 provider，所以拒绝是对的。这里断言"拒绝并且说清了原因"，
# 而不是断言"没被拒"。
print()
print('== codex 现在应被拒，且理由可读 ==')
codex_pv = agents.preview_agent('codex')
if codex_pv.get('blocked'):
    check_true('codex 的拒绝理由点明了 model_provider',
               'model_provider' in codex_pv['blocked'], codex_pv.get('blocked'))
else:
    # 用户以后把 model_provider 改回 custom 时走这一支
    check_true('codex 可接入时给出改动清单', len(codex_pv.get('changes') or []) > 0)

for aid in ('claude-code', 'claude-code-desktop', 'opencode', 'hermes'):
    p = agents.preview_agent(aid)
    # **不要断言"必须没被拒"** —— 那是把用例绑死在"本机装了哪些客户端"上：没装 opencode /
    # Hermes / Claude Code 的机器上，blocked 恰恰是**正确**行为，用例却会红（审查 NEW-03）。
    # 断言"要么能预览、要么给出可读的拒绝原因"，两者都是合格行为。
    if p.get('blocked'):
        check_true('%s 被拒时给出了可读原因' % aid,
                   isinstance(p['blocked'], str) and len(p['blocked']) >= 6, p.get('blocked'))
    else:
        check_true('%s 可预览时返回了约定字段' % aid,
                   isinstance(p.get('models_count'), int) and 'changes' in p, repr(p)[:120])

print()
print('== 预览绝不回显密钥全文 ==')
# 没有 .local-secrets.json / 里面没有 api_key 的机器上，_local_key() 会抛 —— 那是环境缺失，
# 不是被测代码出错。跳过这条断言并如实说明，别让它把整个用例带红（审查 NEW-03）。
try:
    secret = agents._local_key()
except Exception as exc:                            # noqa: BLE001
    secret = None
    print('  （本机读不到 api_key：%s；跳过"预览里没有密钥全文"的断言）'
          % type(exc).__name__)
if secret:
    for aid in ('codex', 'claude-code', 'opencode', 'hermes'):
        blob = json.dumps(agents.preview_agent(aid), ensure_ascii=False)
        check_true('%s 预览里没有密钥全文' % aid, secret not in blob)

print()
print('== 预览不写任何文件（比对 5 个配置文件的 mtime+大小）==')
targets = [agents._codex_path(), agents._claude_path(),
           agents._opencode_path(), agents._hermes_path()]
before = [(str(t), t.stat().st_mtime_ns, t.stat().st_size) for t in targets if t.is_file()]
for aid in ('codex', 'claude-code', 'claude-code-desktop', 'opencode', 'hermes'):
    agents.preview_agent(aid)
after = [(str(t), t.stat().st_mtime_ns, t.stat().st_size) for t in targets if t.is_file()]
check('预览后所有配置文件未变', after, before)

print()
print('== 未知 agent 报错可读 ==')
try:
    agents.preview_agent('nope')
    check_true('未知 agent 抛错', False)
except bridge.RouteError as e:
    check_true('未知 agent 抛错', '未知的客户端' in str(e))
    print('        -> ' + str(e))

print()
print('== codex 未接 confirm 时不能走非 codex 路径 ==')
try:
    agents.connect_agent('opencode', {})
    check_true('opencode 缺 confirm 被拒', False)
except bridge.RouteError as e:
    check_true('opencode 缺 confirm 被拒', 'confirm' in str(e))
    print('        -> ' + str(e))


# ═══════════════════════════════════════════ 2. 写入：合成副本 + 沙箱 ROOT

REAL = {name: fn for name, fn in (
    ('_codex_path', agents._codex_path), ('_claude_path', agents._claude_path),
    ('_opencode_path', agents._opencode_path), ('_hermes_path', agents._hermes_path))}
REAL_ROOT = bridge.ROOT

TMP = Path(tempfile.mkdtemp(prefix='prism-agents-'))
try:
    (TMP / 'backups').mkdir()
    # client_models() 要读 plan。routing-plan.json 自己的 note 就写着 "Provider mappings,
    # no secrets"，所以复制一份进沙箱是安全的（不像 settings.json / hermes 那样带密钥）。
    shutil.copy2(ROOT / 'routing-plan.json', TMP / 'routing-plan.json')
    bridge.ROOT = TMP
    # 签发密钥桩死：rs.secrets() 读的是 rs.ROOT（重定向 bridge.ROOT 盖不住它），
    # 不桩的话每跑一次测试就读一遍真实 .local-secrets.json（审查 HI-02）。
    REAL_KEY = agents._local_key
    agents._local_key = lambda: 'prism-test-local-key'

    print()
    print('== Claude Code：只改两个 env 键，其余逐字节不动 ==')
    claude = TMP / 'claude-settings.json'
    original = {
        'env': {
            'ANTHROPIC_API_KEY': 'user_OLDKEY_should_be_replaced_xxxx',
            'ANTHROPIC_BASE_URL': 'https://api.commandcode.ai/provider/v1',
            'ANTHROPIC_DEFAULT_HAIKU_MODEL': 'deepseek/deepseek-v4.1-flash',
            'ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME': 'deepseek-v4.1-flash',
            'ANTHROPIC_MODEL': 'deepseek/deepseek-v4.1-flash[1M]',
        },
        'permissions': {'allow': ['Bash(ls:*)'], 'deny': []},
        'model': 'opus',
        'statusLine': {'type': 'command', 'command': 'x'},
    }
    claude.write_text(json.dumps(original, indent=2, ensure_ascii=False), encoding='utf-8')
    agents._claude_path = lambda: claude
    user_cfg = TMP / 'dot-claude.json'
    agents._claude_user_config_path = lambda: user_cfg

    prev = agents._claude_preview(agents.client_models())
    check('Claude 预览无重排警告', prev.get('reformat_note'), None)
    out = agents._claude_apply(agents.client_models())
    now = json.loads(claude.read_text(encoding='utf-8'))
    check('ANTHROPIC_BASE_URL 指向网关', now['env']['ANTHROPIC_BASE_URL'], agents.anthropic_base())
    check('ANTHROPIC_API_KEY 换成网关密钥', now['env']['ANTHROPIC_API_KEY'], agents._local_key())
    check('模型变量一个都没动', now['env']['ANTHROPIC_MODEL'],
          original['env']['ANTHROPIC_MODEL'])
    check('其他顶层键原样保留', [now['permissions'], now['model'], now['statusLine']],
          [original['permissions'], original['model'], original['statusLine']])
    check('顶层键顺序不变', list(now.keys()), list(original.keys()))
    check_true('备份目录已建', Path(out['backup_dir']).is_dir(), out['backup_dir'])
    check('备份是改动前的原文件', Path(out['backup_dir'], claude.name).read_text(encoding='utf-8'),
          json.dumps(original, indent=2, ensure_ascii=False))
    mark = agents._claude_key_mark(agents._local_key())
    check('放行标记只取密钥末尾 20 位', agents._claude_key_mark('x' * 30 + 'ABCDEFGHIJ1234567890'),
          'ABCDEFGHIJ1234567890')
    saved = json.loads(user_cfg.read_text(encoding='utf-8'))
    check('接入时放行网关密钥', saved['customApiKeyResponses']['approved'], [mark])
    # 别的标记留着；被拒过的同一条要挪到放行。
    saved['numStartups'] = 3
    saved['customApiKeyResponses']['approved'] = ['keep-this-other-mark']
    saved['customApiKeyResponses']['rejected'] = [mark]
    user_cfg.write_text(json.dumps(saved, indent=2, ensure_ascii=False), encoding='utf-8')
    check_true('再次放行有改动', agents._approve_claude_gateway_key(agents._local_key()))
    saved = json.loads(user_cfg.read_text(encoding='utf-8'))
    check('别的放行标记还在，本网关密钥补上', saved['customApiKeyResponses']['approved'],
          ['keep-this-other-mark', mark])
    check('拒绝名单里不再有本网关密钥', saved['customApiKeyResponses']['rejected'], [])
    check('放行没有碰别的字段', saved['numStartups'], 3)
    check_true('已经放行时不再改文件', not agents._approve_claude_gateway_key(agents._local_key()))

    print()
    print('== opencode：只新增 provider.prism，7 家现有供应商不动 ==')
    oc = TMP / 'opencode.json'
    oc_original = {
        'provider': {
            'cc-seagull': {'name': 'CC Switch - SeaGull', 'npm': '@ai-sdk/openai-compatible',
                           'options': {'baseURL': 'https://zz.aiapi2025.top/v1'},
                           'models': {'gpt-5.6-sol': {'name': 'GPT 5.6 Sol'}}},
            'kkapi': {'name': 'KKAPI', 'npm': '@ai-sdk/openai',
                      'options': {'baseURL': 'https://kkgait.com/v1'}, 'models': {}},
        },
        'theme': 'dark',
        'autoupdate': True,
    }
    oc.write_text(json.dumps(oc_original, indent=2, ensure_ascii=False), encoding='utf-8')
    agents._opencode_path = lambda: oc

    models = agents.client_models()
    prev = agents._opencode_preview(models)
    check('opencode 新增时 kind=create',
          [c['kind'] for c in prev['changes']][0], 'create')
    check('opencode 预览标出不改现有供应商',
          any(c['kind'] == 'keep' and 'cc-seagull' in c['key'] for c in prev['changes']), True)
    out = agents._opencode_apply(models)
    now = json.loads(oc.read_text(encoding='utf-8'))
    check('现有供应商原样', now['provider']['cc-seagull'], oc_original['provider']['cc-seagull'])
    check('kkapi 原样', now['provider']['kkapi'], oc_original['provider']['kkapi'])
    check('顶层其他键原样', [now['theme'], now['autoupdate']], ['dark', True])
    check('provider 键数 +1', len(now['provider']), len(oc_original['provider']) + 1)
    check('prism 的 baseURL', now['provider']['prism']['options']['baseURL'], agents.openai_base())
    check('prism 的 npm', now['provider']['prism']['npm'], '@ai-sdk/openai-compatible')
    check('prism 的模型清单', sorted(now['provider']['prism']['models']), sorted(models))
    check('顶层键顺序不变', list(now.keys()), list(oc_original.keys()))

    print()
    print('== Hermes：按行插入，注释一行不少 ==')
    hm = TMP / 'hermes.yaml'
    hm_original = '''model:
  default: deepseek-v4-flash-free
  provider: opencode-free
  base_url: https://opencode.ai/zen/v1
providers:
  existing-hermes:
    api: https://api.hermes.example/v1
    name: existing-hermes
    api_key: test-key
    models:
      gpt-4o:
        name: GPT-4o Updated
    default_model: gpt-4o
plugins:
  enabled: []
_config_version: 44
agent: {}

# ── Security ──────────────────────────────────────────────────────────
# Secret redaction is ON by default. Set redact_secrets
# to false to disable.
#
# security:
#   redact_secrets: true

# ── Fallback Model ────────────────────────────────────────────────────
# fallback_model:
#   provider: openrouter
#   model: anthropic/claude-sonnet-4
'''
    hm.write_text(hm_original, encoding='utf-8')
    agents._hermes_path = lambda: hm

    n_comments_before = sum(1 for s in hm_original.splitlines() if s.lstrip().startswith('#'))
    prev = agents._hermes_preview(models)
    check('Hermes 预览 kind=create（providers 已在，块不在）',
          [c['kind'] for c in prev['changes']][0], 'create')
    check('Hermes 预览声明保住注释',
          any('注释' in c['key'] and c['kind'] == 'keep' for c in prev['changes']), True)
    check('Hermes 预览声明不动 model 段',
          any(c['kind'] == 'keep' and c['key'].startswith('model') for c in prev['changes']), True)
    out = agents._hermes_apply(models)
    text = hm.read_text(encoding='utf-8')
    lines = text.splitlines()
    n_comments_after = sum(1 for s in lines if s.lstrip().startswith('#'))
    check('注释行数不变', n_comments_after, n_comments_before)
    check_true('Security 注释正文还在', 'Secret redaction is ON by default' in text)
    check_true('Fallback 注释正文还在', 'anthropic/claude-sonnet-4' in text)
    check_true('原有 providers.existing-hermes 还在', 'existing-hermes:' in text)
    check_true('model 段没被动', text.startswith('model:\n  default: deepseek-v4-flash-free\n'))
    check_true('plugins / _config_version / agent 都还在',
               all(k in text for k in ('plugins:', '_config_version: 44', 'agent: {}')))
    check_true('新增了 providers.prism', '\n  prism:\n' in text)
    check_true('prism 指向网关（api 行是 JSON 引号形式，ME-11）',
               ('    api: %s' % json.dumps(agents.openai_base(), ensure_ascii=False)) in text)

    # 用模块自己的解析器复查一遍（没有 yaml 库，就用同一套行解析）
    (top, end), (ps, pe) = agents._hermes_provider_span(lines)
    check_true('重新解析能定位到 prism 块', ps is not None)
    found = None
    for k in range(ps, pe):
        m = agents._HERMES_API.match(lines[k])
        if m:
            found = m.group(1)
            break
    check('重新解析读到的 api', found, json.dumps(agents.openai_base(), ensure_ascii=False))
    check_true('api_key 行也是 JSON 引号形式（ME-11）',
               ('    api_key: %s' % json.dumps('prism-test-local-key', ensure_ascii=False)) in text)
    check_true('prism 块在 providers 段内', top is not None and top < ps < end)
    check_true('prism 块后面还有原内容', pe <= len(lines))

    # 幂等：再写一次应该是替换而不是叠加
    agents._hermes_apply(models)
    text2 = hm.read_text(encoding='utf-8')
    check('第二次写入不叠加 prism 块', text2.count('\n  prism:\n'), 1)
    check('第二次写入注释仍不变',
          sum(1 for s in text2.splitlines() if s.lstrip().startswith('#')), n_comments_before)

    print()
    print('== Hermes：没有 providers 段时要能新建 ==')
    hm2 = TMP / 'hermes2.yaml'
    hm2.write_text('model:\n  default: x\n# tail comment\n', encoding='utf-8')
    agents._hermes_path = lambda: hm2
    agents._hermes_apply(models)
    t2 = hm2.read_text(encoding='utf-8')
    check_true('新建了 providers 段', '\nproviders:\n  prism:\n' in t2 or t2.startswith('providers:\n  prism:\n'))
    check_true('原有 model 段保留', '  default: x' in t2)
    check_true('尾部注释保留', '# tail comment' in t2)

    print()
    print('== 断开：只撤回仍是我们写过的地址和密钥 ==')
    out = agents._claude_disconnect()
    now = json.loads(claude.read_text(encoding='utf-8'))
    check_true('Claude 断开后网关地址已撤', 'ANTHROPIC_BASE_URL' not in now['env'])
    check_true('Claude 断开后网关密钥已撤', 'ANTHROPIC_API_KEY' not in now['env'])
    kept_cfg = json.loads(user_cfg.read_text(encoding='utf-8'))
    check('断开后只撤本网关的放行', kept_cfg['customApiKeyResponses']['approved'],
          ['keep-this-other-mark'])
    check('Claude 断开后模型变量还在', now['env']['ANTHROPIC_MODEL'],
          original['env']['ANTHROPIC_MODEL'])
    check_true('Claude 断开有备份', Path(out['backup_dir']).is_dir())
    try:
        agents.connect_agent('claude-code', {'disconnect': True})
        check_true('断开缺 confirm 被拒', False)
    except bridge.RouteError as e:
        check_true('断开缺 confirm 被拒', 'confirm' in str(e), str(e))
    agents._claude_apply(agents.client_models())
    dirty = json.loads(claude.read_text(encoding='utf-8'))
    dirty['env']['ANTHROPIC_API_KEY'] = 'user-changed-key-not-ours'
    claude.write_text(json.dumps(dirty, indent=2, ensure_ascii=False), encoding='utf-8')
    try:
        agents._claude_disconnect()
        check_true('改过的密钥要报错', False)
    except bridge.RouteError as e:
        check_true('改过的密钥要报错', 'ANTHROPIC_API_KEY' in str(e), str(e))
    kept = json.loads(claude.read_text(encoding='utf-8'))
    check('改过的密钥没有动', kept['env']['ANTHROPIC_API_KEY'], 'user-changed-key-not-ours')
    check_true('仍是网关的地址已撤', 'ANTHROPIC_BASE_URL' not in kept['env'])
    kept_cfg = json.loads(user_cfg.read_text(encoding='utf-8'))
    check('改过的密钥不断开时，放行记录留着',
          agents._claude_key_mark(agents._local_key()) in kept_cfg['customApiKeyResponses']['approved'],
          True)

    out = agents._opencode_disconnect()
    now = json.loads(oc.read_text(encoding='utf-8'))
    check_true('opencode 断开后没有 provider.prism', 'prism' not in now['provider'])
    check('opencode 其它供应商还在', now['provider']['cc-seagull'],
          oc_original['provider']['cc-seagull'])
    check_true('opencode 断开有备份', bool(out.get('backup_dir')))

    agents._hermes_path = lambda: hm
    before_comments = sum(1 for s in hm.read_text(encoding='utf-8').splitlines()
                          if s.lstrip().startswith('#'))
    agents._hermes_disconnect()
    hermes_text = hm.read_text(encoding='utf-8')
    check('Hermes 断开后 prism 块没了', hermes_text.count('\n  prism:\n'), 0)
    check_true('Hermes 原有来源还在', 'existing-hermes:' in hermes_text)
    check('Hermes 断开后注释还在',
          sum(1 for s in hermes_text.splitlines() if s.lstrip().startswith('#')),
          before_comments)

    codex_toml = TMP / 'codex-config.toml'
    gateway_url = agents.openai_base()
    codex_toml.write_text(
        'model = "gpt-5"\n'
        'model_provider = "custom"\n'
        '\n'
        '[model_providers.custom]\n'
        'name = "Local Gateway"\n'
        'base_url = %s\n'
        'wire_api = "responses"\n'
        'experimental_bearer_token = "prism-test-local-key"\n'
        '\n'
        '[other]\n'
        'base_url = "https://keep.example/v1"\n' % json.dumps(gateway_url),
        encoding='utf-8')
    agents._codex_path = lambda: codex_toml
    agents._codex_disconnect()
    codex_now = codex_toml.read_text(encoding='utf-8')
    check_true('Codex 断开后 custom 里没有网关地址',
               ('base_url = %s' % json.dumps(gateway_url)) not in codex_now)
    check_true('Codex 断开后密钥行没了', 'experimental_bearer_token' not in codex_now)
    check_true('Codex 别的段没动', 'base_url = "https://keep.example/v1"' in codex_now)
    check_true('Codex 的 name 行还在', 'name = "Local Gateway"' in codex_now)

    print()
    print('== Copilot：直连来源，不碰真实 VS Code、不碰 8317 ==')
    saved_copilot = (agents._copilot_path, agents._vscode_user_dir, agents._copilot_config)
    try:
        user_dir = TMP / 'Code' / 'User'
        user_dir.mkdir(parents=True)
        chat_path = user_dir / 'chatLanguageModels.json'
        foreign = {
            'name': '用户自己的',
            'vendor': 'customendpoint',
            'apiKey': 'user-byok-key-should-stay',
            'apiType': 'chat-completions',
            'models': [{
                'id': 'user-model',
                'name': '用户模型',
                'url': 'https://user.example/v1/chat/completions',
            }],
        }
        old_prism = {
            'name': 'Prism · 旧来源',
            'vendor': 'customendpoint',
            'apiKey': 'old-prism-key-should-go',
            'models': [{'id': 'old', 'url': 'http://127.0.0.1:8317/v1/chat/completions'}],
        }
        chat_path.write_text(json.dumps([foreign, old_prism], indent=2, ensure_ascii=False),
                             encoding='utf-8')
        secret = 'sk-copilot-plaintext-secret-0123456789'
        secret_oc = 'sk-openai-plaintext-secret-abcdef'
        secret_off = 'sk-disabled-should-not-leak-9999'
        secret_trap = 'sk-gateway-trap-should-not-write'
        plan = {'providers': [
            {'id': 'hub', 'label': '枢纽', 'group': 'gpt', 'section': 'codex-api-key',
             'base_url': 'https://up.example/v1', 'tag': 'hub', 'head': 'hub',
             'expose': ['sol'], 'context_window': 200000,
             'models': [{'name': 'gpt-5.4-upstream', 'alias': 'sol'}],
             'model_settings': {'sol': {'levels': ['low', 'high'], 'default': 'low'}}},
            {'id': 'off', 'label': '停用', 'group': 'gpt', 'section': 'codex-api-key',
             'base_url': 'https://off.example/v1', 'tag': 'off', 'expose': ['gone'],
             'models': [{'name': 'gone-up', 'alias': 'gone'}]},
            {'id': 'empty', 'label': '未暴露', 'group': 'gpt', 'section': 'codex-api-key',
             'base_url': 'https://empty.example/v1', 'tag': 'empty', 'expose': [],
             'models': [{'name': 'empty-up', 'alias': 'e'}]},
            {'id': 'trap', 'label': '陷阱', 'group': 'gpt', 'section': 'codex-api-key',
             'base_url': 'http://127.0.0.1:8317/v1', 'tag': 'trap', 'expose': ['x'],
             'models': [{'name': 'trap-up', 'alias': 'x'}]},
            {'id': 'go', 'label': 'Opencode', 'group': 'deepseek', 'section': 'codex-api-key',
             'base_url': 'https://opencode.ai/zen/go/v1', 'tag': 'go',
             'expose': ['ds-flash'],
             'models': [{'name': 'deepseek-v4.1-flash', 'alias': 'ds-flash'}],
             'model_settings': {'ds-flash': {'levels': ['low', 'high'], 'default': 'high'}}},
            {'id': 'sg', 'label': '海鸥', 'group': 'deepseek', 'section': 'openai-compatibility',
             'base_url': 'https://oc.example/v1/chat/completions', 'tag': 'sg',
             'expose': ['ds'], 'models': [{'name': 'deepseek-v4', 'alias': 'ds'}]},
            {'id': 'plain', 'label': '裸地址', 'group': 'gpt', 'section': 'openai-compatibility',
             'base_url': 'https://plain.example', 'tag': 'plain',
             'expose': ['plain-alias'],
             'models': [{'name': 'plain-up', 'alias': 'plain-alias'}]},
            {'id': 'dis', 'label': '关掉', 'group': 'gpt', 'section': 'openai-compatibility',
             'base_url': 'https://gone.example/v1', 'tag': 'dis', 'expose': ['nope'],
             'models': [{'name': 'nope-up', 'alias': 'nope'}]},
        ]}
        (TMP / 'routing-plan.json').write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
        agents._copilot_config = lambda: {
            'codex-api-key': [
                {'api-key': secret, 'base-url': 'https://up.example/v1',
                 'headers': {'X-Route-Tag': 'hub'}},
                {'api-key': secret_off, 'base-url': 'https://off.example/v1',
                 'headers': {'X-Route-Tag': 'off'}, 'excluded-models': ['*']},
                {'api-key': 'sk-empty-not-written-0000', 'base-url': 'https://empty.example/v1',
                 'headers': {'X-Route-Tag': 'empty'}},
                {'api-key': secret_trap, 'base-url': 'http://127.0.0.1:8317/v1',
                 'headers': {'X-Route-Tag': 'trap'}},
                {'api-key': 'sk-opencode-go-plaintext-secret-aaaa',
                 'base-url': 'https://opencode.ai/zen/go/v1',
                 'headers': {'X-Route-Tag': 'go'}},
            ],
            'openai-compatibility': [
                {'base-url': 'https://oc.example/v1/chat/completions',
                 'headers': {'X-Route-Tag': 'sg'},
                 'api-key-entries': [{'api-key': secret_oc}]},
                {'base-url': 'https://plain.example',
                 'headers': {'X-Route-Tag': 'plain'},
                 'api-key-entries': [{'api-key': 'sk-plain-plaintext-secret-wxyz'}]},
                {'base-url': 'https://gone.example/v1', 'disabled': True,
                 'headers': {'X-Route-Tag': 'dis'},
                 'api-key-entries': [{'api-key': 'sk-disabled-openai-should-not-zzzz'}]},
            ],
        }
        agents._vscode_user_dir = lambda: user_dir
        agents._copilot_path = lambda: chat_path

        preview = agents._copilot_preview([])
        blob = json.dumps(preview, ensure_ascii=False)
        check('Copilot 预览没有被拒', preview.get('blocked'), None)
        check_true('预览明文里没有来源密钥', secret not in blob and secret_oc not in blob, blob)
        check_true('预览里的地址不是网关', '127.0.0.1:8317' not in blob)
        wrapped = json.dumps(agents.preview_agent('copilot'), ensure_ascii=False)
        check_true('接入预览明文里也没有来源密钥', secret not in wrapped and secret_oc not in wrapped)

        agents._catalog_efforts = lambda: {}
        agents._copilot_apply([])
        written = json.loads(chat_path.read_text(encoding='utf-8'))
        text = chat_path.read_text(encoding='utf-8')
        check('用户自己的 provider 还在', written[0], foreign)
        names = [x.get('name') for x in written if isinstance(x, dict)]
        check_true('旧的 Prism 块被换掉', 'Prism · 旧来源' not in names)
        check_true('文件里没有网关地址', '127.0.0.1:8317' not in text)
        check_true('文件里没有渠道头形式的 id', 'hub/sol' not in text)
        check_true('停用和未暴露的密钥没写进去',
                   secret_off not in text and secret_trap not in text
                   and 'sk-disabled-openai-should-not-zzzz' not in text)
        by_name = {x['name']: x for x in written if isinstance(x, dict)}
        hub = by_name['Prism · 枢纽']
        check('codex 来源用 responses', hub['apiType'], 'responses')
        check('模型上也标明 responses', hub['models'][0]['apiType'], 'responses')
        check('DeepSeek 走 chat completions',
              agents._copilot_api('codex-api-key', 'deepseek-v4.1-flash'),
              ('chat-completions', '/chat/completions'))
        check('OpenCode Go 的 DeepSeek 地址',
              agents._join_api_path('https://opencode.ai/zen/go/v1', '/chat/completions'),
              'https://opencode.ai/zen/go/v1/chat/completions')
        check('OpenCode Go 带会话头',
              agents._opencode_session_header('https://opencode.ai/zen/go/v1/chat/completions'),
              {'x-opencode-session': 'ses_prism'})
        check('别的来源不带会话头',
              agents._opencode_session_header('https://sraiapi.com/v1/responses'), None)
        check('来源档位优先于目录',
              agents._lookup_effort('sol', 'gpt-5.4-upstream',
                                    {'levels': ['low', 'high'], 'default': 'low'},
                                    {'gpt-5.4-upstream': (['max'], 'max')}),
              (['low', 'high'], 'low'))
        check('目录补上思考深度',
              agents._lookup_effort('gpt-6.1-sol', 'gpt-6.1-sol', {},
                                    {'gpt-6.1-sol': (['low', 'medium', 'high'], 'low')}),
              (['low', 'medium', 'high'], 'low'))
        check('都没有就用标准阶梯',
              agents._lookup_effort('plain-up', 'plain-up', None, {}),
              (list(agents.COPILOT_EFFORT_FALLBACK), 'medium'))
        go = by_name['Prism · Opencode']
        check('OpenCode Go 整组走 chat-completions', go['apiType'], 'chat-completions')
        check('写进文件的 DeepSeek 地址', go['models'][0]['url'],
              'https://opencode.ai/zen/go/v1/chat/completions')
        check('写进文件的会话头',
              go['models'][0]['requestHeaders'].get('x-opencode-session'), 'ses_prism')
        check('DeepSeek 力度按 chat-completions 发出',
              go['models'][0]['reasoningEffortFormat'], 'chat-completions')
        check('Responses 模型用请求头带密钥',
              hub['models'][0]['requestHeaders']['Authorization'], 'Bearer ' + secret)
        check_true('Responses 模型不写会话头',
                   'x-opencode-session' not in hub['models'][0]['requestHeaders'])
        check('OpenCode 请求头同时带密钥和会话',
              go['models'][0]['requestHeaders']['Authorization'],
              'Bearer sk-opencode-go-plaintext-secret-aaaa')
        check('codex 直连补 /v1/responses', hub['models'][0]['url'],
              'https://up.example/v1/responses')
        check('上游 id 不带渠道头', hub['models'][0]['id'], 'gpt-5.4-upstream')
        check('有渠道头时显示名带来源标签', hub['models'][0]['name'], '枢纽 · sol')
        check('密钥写的是来源自己的', hub['apiKey'], secret)
        check('toolCalling 默认 true', hub['models'][0]['toolCalling'], True)
        check('vision 默认 true', hub['models'][0]['vision'], True)
        check('maxOutputTokens', hub['models'][0]['maxOutputTokens'], 32768)
        check('maxInputTokens 为窗口减去输出', hub['models'][0]['maxInputTokens'], 200000 - 32768)
        check('有档位时 supportsReasoningEffort 是字符串数组',
              hub['models'][0]['supportsReasoningEffort'], ['low', 'high'])
        check('responses 的力度按 responses 发出',
              hub['models'][0]['reasoningEffortFormat'], 'responses')
        check('默认档位写进 provider settings',
              hub['settings']['gpt-5.4-upstream']['reasoningEffort'], 'low')
        # 编辑器对这个字段调用 .map。布尔值会抛，数组能遍历。
        try:
            mapped = [x for x in hub['models'][0]['supportsReasoningEffort']]
        except TypeError:
            mapped = None
        check('数组可以遍历', mapped, ['low', 'high'])
        try:
            list(True)  # 旧写法 true.map 在编辑器里就是这一类错误
            check_true('布尔值不能当档位列表', False)
        except TypeError:
            check_true('布尔值不能当档位列表', True)
        seagull = by_name['Prism · 海鸥']
        check('openai-compatibility 用 chat-completions', seagull['apiType'], 'chat-completions')
        check('已有 /chat/completions 不重复拼', seagull['models'][0]['url'],
              'https://oc.example/v1/chat/completions')
        check('无渠道头时显示名就是别名', seagull['models'][0]['name'], 'ds')
        check('没有 model_settings 也写出标准阶梯',
              seagull['models'][0]['supportsReasoningEffort'],
              list(agents.COPILOT_EFFORT_FALLBACK))
        check('标准阶梯按 chat-completions 发出',
              seagull['models'][0]['reasoningEffortFormat'], 'chat-completions')
        check('标准阶梯默认 medium',
              seagull['settings']['deepseek-v4']['reasoningEffort'], 'medium')
        plain = by_name['Prism · 裸地址']
        check('裸地址补 /chat/completions', plain['models'][0]['url'],
              'https://plain.example/chat/completions')
        check('没有 context_window 时按 272000', plain['models'][0]['maxInputTokens'], 272000 - 32768)
        check('Prism 前缀的块数量',
              sum(1 for n in names if str(n).startswith('Prism · ')), 4)
        agents._copilot_apply([])
        again = json.loads(chat_path.read_text(encoding='utf-8'))
        check('再写一次不叠加',
              sum(1 for x in again if isinstance(x, dict) and str(x.get('name') or '').startswith('Prism · ')),
              4)
        check('再写一次用户的块还在', again[0], foreign)

        # 来源停用后文件里仍是接入时写的块。断开只跟「这次还会写」的来源比时，
        # 这块对不上，开关就关不掉。
        saved_cfg = agents._copilot_config
        held = saved_cfg()
        for entry in held['codex-api-key']:
            entry['excluded-models'] = ['*']
        for entry in held['openai-compatibility']:
            entry['disabled'] = True
        agents._copilot_config = lambda: held
        agents._copilot_disconnect()
        quiet = json.loads(chat_path.read_text(encoding='utf-8'))
        check('停用后来源的块也能断开', quiet, [foreign])
        check('停用后不再算已接入', agents._copilot()['connected'], False)
        stale = {
            'name': 'Prism · 枢纽',
            'vendor': 'customendpoint',
            'apiKey': secret,
            'models': [{'id': 'gpt-5.4-upstream',
                        'url': 'https://user-edited.example/v1/responses'}],
        }
        chat_path.write_text(json.dumps([foreign, stale], ensure_ascii=False), encoding='utf-8')
        try:
            agents._copilot_disconnect()
            check_true('改过地址的块不能断开', False)
        except bridge.RouteError as e:
            check_true('改过地址要说明没有断开',
                       '已被改过' in str(e) and 'Prism · 枢纽' in str(e), str(e))
        check('改过的块原样留下',
              json.loads(chat_path.read_text(encoding='utf-8')), [foreign, stale])
        agents._copilot_config = saved_cfg

        bad = user_dir / 'not-array.json'
        raw_bad = '{"vendor": "x"}\n'
        bad.write_text(raw_bad, encoding='utf-8')
        agents._copilot_path = lambda: bad
        try:
            agents._copilot_apply([])
            check_true('顶层不是数组要停', False)
        except bridge.RouteError as e:
            check_true('顶层不是数组要停', '数组' in str(e), str(e))
        check('顶层不是数组时文件一个字节不动', bad.read_text(encoding='utf-8'), raw_bad)

        missing = TMP / 'no-vscode-user'
        agents._vscode_user_dir = lambda: missing
        agents._copilot_path = lambda: missing / 'chatLanguageModels.json'
        blocked = agents._copilot_preview([])
        check_true('没装 VS Code 时预览 blocked',
                   isinstance(blocked.get('blocked'), str) and 'VS Code' in blocked['blocked'],
                   blocked.get('blocked'))
        check_true('没装 VS Code 时不创建目录', not missing.exists())
    finally:
        agents._copilot_path, agents._vscode_user_dir, agents._copilot_config = saved_copilot

    print()
    print('== Copilot Agent：只改 Agents 窗口开关，不动模型文件 ==')
    saved_user = agents._vscode_user_dir
    try:
        user_dir = TMP / 'CodeAgent' / 'User'
        user_dir.mkdir(parents=True)
        settings = user_dir / 'settings.json'
        chat = user_dir / 'chatLanguageModels.json'
        original = '{\n  "editor.fontSize": 18,\n  "chat.tips.enabled": false\n}\n'
        settings.write_text(original, encoding='utf-8')
        chat.write_text('[{"name": "保持不动"}]\n', encoding='utf-8')
        agents._vscode_user_dir = lambda: user_dir
        preview = agents._copilot_agent_preview([])
        check('Agent 预览没有被拒', preview.get('blocked'), None)
        check('Agent 预览不走网关文案', preview.get('direct'), True)
        check('Agent 预览只改那一个键', preview['changes'][0]['key'], agents.AGENT_HOST_KEY)
        check('没有这个键时 from 是（无）', preview['changes'][0]['from'], '（无）')
        agents._copilot_agent_apply([])
        after = settings.read_text(encoding='utf-8')
        check_true('原有设置还在', '"editor.fontSize": 18' in after and '"chat.tips.enabled": false' in after)
        check_true('开关写成 true', '"chat.agentHost.byokModels.enabled": true' in after)
        check('模型文件一个字节不动', chat.read_text(encoding='utf-8'), '[{"name": "保持不动"}]\n')
        check('接入后算已接入', agents._copilot_agent()['connected'], True)
        agents._copilot_agent_apply([])
        check('再写一次不把 true 写成别的',
              after.count('"chat.agentHost.byokModels.enabled": true'),
              settings.read_text(encoding='utf-8').count('"chat.agentHost.byokModels.enabled": true'))
        agents._copilot_agent_disconnect()
        closed = settings.read_text(encoding='utf-8')
        check_true('断开后变成 false', '"chat.agentHost.byokModels.enabled": false' in closed)
        check_true('断开后字体设置还在', '"editor.fontSize": 18' in closed)
        check('断开后不再算已接入', agents._copilot_agent()['connected'], False)
        commented = '{\n  // 注释留下\n  "editor.fontSize": 18\n}\n'
        settings.write_text(commented, encoding='utf-8')
        agents._copilot_agent_apply([])
        kept = settings.read_text(encoding='utf-8')
        check_true('注释没有被重排掉', '// 注释留下' in kept and '"editor.fontSize": 18' in kept)
        check_true('带注释的文件也能插上开关', '"chat.agentHost.byokModels.enabled": true' in kept)
        other = '{\n  "chat.agentHost.byokModels.enabled": "yes"\n}\n'
        settings.write_text(other, encoding='utf-8')
        blocked = agents._copilot_agent_preview([])
        check_true('值不是布尔时预览停住', isinstance(blocked.get('blocked'), str), blocked.get('blocked'))
        check('值不是布尔时文件不动', settings.read_text(encoding='utf-8'), other)
        missing = TMP / 'no-agent-vscode'
        agents._vscode_user_dir = lambda: missing
        blocked = agents._copilot_agent_preview([])
        check_true('没装 VS Code 时 Agent 预览 blocked',
                   isinstance(blocked.get('blocked'), str) and 'VS Code' in blocked['blocked'],
                   blocked.get('blocked'))
        check_true('没装 VS Code 时不创建目录', not missing.exists())
    finally:
        agents._vscode_user_dir = saved_user

    print()
    print('== 未知 agent / 缺 confirm 的拦截 ==')
    check_true('codex 走既有路径（不需要 confirm）',
               agents.get('codex')['apply'] is None)
    for aid, a in ((x['id'], x) for x in agents.AGENTS):
        if aid == 'codex':
            continue
        check_true('%s 有 preview 与 apply' % aid, callable(a['preview']) and callable(a['apply']))

finally:
    for name, fn in REAL.items():
        setattr(agents, name, fn)
    agents._local_key = REAL_KEY
    bridge.ROOT = REAL_ROOT
    shutil.rmtree(TMP, ignore_errors=True)
    print('  沙箱已删除: %s' % TMP)

print()
print('== 网关停止：foreign / unknown 不结束进程，不碰 8317 ==')
from core import gateway, identity          # noqa: E402
_saved_identity = identity.gateway_identity
_saved_kill = gateway.terminate_pid
_kills = []
try:
    gateway.terminate_pid = lambda pid: _kills.append(pid)

    def _refuse(state, note):
        identity.gateway_identity = lambda port=None: {
            'state': state, 'note': note, 'pid': 4242,
            'config': r'D:\other\config.yaml', 'image': 'other.exe'}
        try:
            gateway.stop_gateway()
            return None
        except bridge.RouteError as exc:
            return str(exc)

    got = _refuse('foreign', '外来网关，没有停')
    check('foreign 拒绝并且原样返回 note', got, '外来网关，没有停')
    check('foreign 没有去结束进程', list(_kills), [])
    got = _refuse('unknown', '身份不明，没有停')
    check('unknown 拒绝并且原样返回 note', got, '身份不明，没有停')
    check('unknown 没有去结束进程', list(_kills), [])
    identity.gateway_identity = lambda port=None: {
        'state': 'down', 'note': '8317 端口上没有进程在听', 'pid': None,
        'config': None, 'image': None}
    stopped = gateway.stop_gateway()
    check('端口上没人时不算失败', stopped.get('state'), 'down')
    check('down 也没有去结束进程', list(_kills), [])
finally:
    identity.gateway_identity = _saved_identity
    gateway.terminate_pid = _saved_kill

print()
print('RESULT: %s (%d failed)' % ('ALL PASS' if not fails else 'FAILURES', len(fails)))
for f in fails:
    print('  - ' + f)
sys.exit(1 if fails else 0)
