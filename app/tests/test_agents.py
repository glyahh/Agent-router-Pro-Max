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
print('RESULT: %s (%d failed)' % ('ALL PASS' if not fails else 'FAILURES', len(fails)))
for f in fails:
    print('  - ' + f)
sys.exit(1 if fails else 0)
