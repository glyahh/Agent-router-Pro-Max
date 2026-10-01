# ADR-0008：一个分组可同时启用多家来源；渠道头区分流量；新增客户端适配层

- 状态：**已采纳**（2026-09-30）
- 相关：ADR-0003（X-Route-Tag 消歧）、[DEV-RULES.md](../DEV-RULES.md) A1/A2/A5/D2/D5
- 证据：[evidence-p0-head-routing.txt](../evidence-p0-head-routing.txt)、[evidence-home-tree-render.txt](../evidence-home-tree-render.txt)

---

## 1. 背景

原来的路由模型是**一个分组一次只能启用一家来源**：

- `route_selector.snapshot()` 把 `selected[group]` 算成 `id | None | 'conflict'`
- `build_config()` 用 `if selected.get(p['group']) != p['id']: continue` 决定启用谁
- 界面上"多家同时启用"是一个**错误状态**（`⚠ 多个来源同时启用`，保存会被拒）

用户的需求（原话）：

> 即便同一个供应商大类也要使用不同的供应商……我在 codex 中使用 gpt 供应商大类，
> 我想要同时使用两个具体的供应商，比如 gpt 官方还有 SRAPI。我需要在模型列表中看到
> 这两个供应商中的所有模型；为了区分具体的请求流量走哪个供应商，可以在 agent 使用
> 的模型列表里增加供应商头来区分。

难点在于：两个来源可以暴露**同名**的上游模型（`gpt-5.6-sol` 官方有、SRAPI 也有）。
客户端发出去的模型 ID 必须唯一，否则网关无法判断该走哪一家。

另外 `provider-inventory.json` 与 `PROVIDERS.md` 里早就写着一条"同名模型方案（未实施）"：
"Codex 只显示一个官方名称。后台维护 model → selected provider → actual upstream model ID"。
也就是说这个问题当时就识别出来了，只是没有落地。

## 2. 决策

### 2.1 用**客户端可见别名**带渠道头，不用网关的 `entry['prefix']`

网关自己有两个候选机制，`config.example.yaml` 里都有原文：

| 机制 | 出处 | 判断 |
|---|---|---|
| 凭据级 `prefix: "test"`，调用写成 `test/gpt-5-codex` | `:501`（codex-api-key）、`:429`（gemini）、`:543`（xai）、`:594`（claude）、`:792`（vertex） | **不用**。`prefix` 的示例只出现在这些段，**`openai-compatibility` 的示例里没有它**；而 deepseek / glm 两组走的正是 `openai-compatibility`。用它就得为两个段各写一套。 |
| `models[].alias`：`name` 是上游收到的、`alias` 是客户端看到的 | `:450-452`、`:518-520`、`:764-766` | **采用**。两个段都支持，一处实现覆盖全部。上游自己的建议也是它（`:820-821`："For strict backend pinning, use unique aliases/prefixes or avoid overlapping names"）。 |

### 2.2 头是**来源级**的 `head` 字段，只施加在写出阶段

```jsonc
// routing-plan.json
{ "id": "srapi", "label": "SRAPI", "group": "gpt",
  "head": "srapi",                       // ← 新增的唯一字段
  "expose": ["gpt-5.6-sol"],             // 键空间不变：仍是干净别名
  "models":  [{ "name": "gpt-5.6-sol", "alias": "gpt-5.6-sol" }],
  "model_settings": { "gpt-5.6-sol": { "levels": ["max"], "default": "max" } } }
```

写 `config.yaml` 时变成：

```yaml
# 官方（无头，主来源）        # SRAPI（有头）
models:                       models:
  - name: gpt-5.6-sol           - name: gpt-5.6-sol
    alias: gpt-5.6-sol            alias: srapi/gpt-5.6-sol
```

**三个字段的键空间一位都没改**（仍是干净别名），所以：零数据迁移、零"picks ∩ available
求交把勾选清零"的风险（`route_selector.py:367-369` 那段求交逻辑一行没动）。
头只在两处施加：`build_config()` 写 alias、`regen_catalog()` 写 slug。

目录里的显示（用户选定"显示名写成 SRAPI · gpt-5.6-sol"）：

| | `slug`（客户端真发的） | `display_name`（人看的） |
|---|---|---|
| 有头 | `srapi/gpt-5.6-sol` | `SRAPI · gpt-5.6-sol` |
| 无头 | `gpt-5.6-sol` | 沿用官方显示名（行为一位不变） |
| 遗留 `A/…` | `A/gpt-5.6-sol` | 不变，且 `visibility='hide'` |

### 2.3 四条不变量，分静态与**动态**两类

| | 规则 | 何时判 |
|---|---|---|
| S1 | `head` 只能是小写字母/数字/`._-`，≤24 字符，**禁 `/`** | 总是 |
| S2 | `head` 全局唯一 | 总是 |
| D1 | 一个分组最多启用**一个**无头来源 | 只对**已启用**的来源判 |
| D2 | 已启用来源产出的客户端 ID 不许重复 | 只对**已启用**的来源判 |

**禁 `/` 是硬要求**：这样"有头 ID"必含分隔符、"无头 ID"必不含，两类 ID 结构上
不可能撞车。`HEAD_RE` 禁大写，于是渠道头也永远撞不上遗留的 `A/` 别名。

> **踩过一次，记在这里**：第一版把 D1/D2 写成按 plan 的**全部行**判。结果现存的
> 9 行 / 0 个 head / gpt 组 5 行全无头的配置被判出 **13 条"冲突"** —— 一个本来就
> 合法的存量配置被判成非法，而且以后每加一个来源都会被拦。
> **真正的歧义只发生在"同时生效"的那几行之间。**
> 现在 D 类判据一律带 `enabled_ids` 参数；静态的 S 类才无参数。
> 回归测试：`app/tests/test_heads.py` 里"存量配置静态检查通过"那一条就是这道闸门。

**已知的遗留风险**：`proxyhub` 与 `kkapi` 的 `models` 里都带 `A/gpt-5.6-sol`。
两个都启用时两边都会把它写进 config，网关的合并列表就歧义了 —— D2 会拦下来并
指名道姓。这是**改造前就存在**的隐患，只是以前"一组一来源"天然把它挡住了。

### 2.4 不变量放在哪一层

`bridge._apply_selection_locked()` 在转调 `rs.apply_selection` **之前**调
`sources._assert_heads_ok(plan['providers'], enabled)`：

- 那一刻 `config.yaml` / `routing-plan.json` 一个字节都还没动 → **拒绝是干净的**，
  不需要 `_compensate()` 那一套回滚。实测：被拒时对网关的 PUT 数是 0。
- 放在 `route_selector.build_config` 里也能拦，但那是哈希锁定的存量文件，
  而且完整判据（含"哪些来源算生效"）已经在 `sources.py` 里，抄一份必然漂移。

### 2.5 `selected[group]` 从标量变列表

```python
# 旧：selected[group] = ids[0] if len(ids)==1 else (None if not ids else 'conflict')
# 新：selected[group] = ids
```

`'conflict'` 哨兵值**下线**——"多家同时启用"从错误状态变成正常状态。
为了不打断外部调用与旧前端，`_selected_ids()` 同时接受字符串（老的单个 id）、
列表、`None`；`build_config` 照收。三处 `.get(group) == p['id']` 的标量比较
（`apply_selection` 的 auth 分支、`wait_models` 的期望集合）一并改掉。

### 2.6 `wait_models` 的期望集合必须用**客户端 ID**

`apply_selection` 在写完 config 后会 `wait_models(expected)` 轮询 `/v1/models`。
期望集合原先取的是干净别名；加了头之后网关注册的是带头的 ID，那里会永远等不到，
报 **"配置已保存，但运行时模型目录尚未匹配"** —— 一个纯粹由口径不一致造成的假失败，
而配置其实已经正确写进去了。这一处和 `bridge.sync_catalog_reasoning` 的 slug 匹配
是同一个坑的两个面。

### 2.7 新增客户端适配层 `app/core/agents.py`

覆盖 5 个客户端（用户明确本轮**不做 pi**）：`codex` / `claude-code` /
`claude-code-desktop` / `opencode` / `hermes`。五种配置形态都是**本机实测读出来的**：

| 客户端 | 配置文件 | 写法 |
|---|---|---|
| Codex | `~/.codex/config.toml` | **复用既有的 `rs.connect_client()`，一行不改** |
| Claude Code / 桌面端 | `~/.claude/settings.json` | `env.ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY` |
| opencode | `~/.config/opencode/opencode.json` | **新增** `provider.prism`，不动现有 7 家 |
| Hermes | `%LOCALAPPDATA%/hermes/config.yaml` | **按行插入** `providers.prism` |

一个网关同时提供三种协议，实测自 `cli-proxy-api.exe`：
`/v1/messages`（Claude Code）、`/v1/chat/completions`（opencode / Hermes）、
`/v1/responses`（Codex）。

两条硬约束：

1. **不许 `import yaml`。** `app\.venv` 与 `_internal\` 里都**没有 PyYAML**，
   引它会让打包版启动即 ImportError。Hermes 按行处理，写法照
   `route_selector.force_image_generation_off()` 的先例。**这顺带解决了注释问题**：
   用户 Hermes 配置里的 Security / Fallback 注释块（36 行）用
   `yaml.safe_load → safe_dump` 会被全部删掉。
2. **不许 whole-file 重排。** `settings.json` 与 `opencode.json` 实测都能在
   `json.dumps(indent=2, ensure_ascii=False)` 下**逐字节还原**，所以 JSON 整体写回是
   格式无损的；`_json_reformat_note()` 会先验证这个前提，不成立时**如实告诉用户
   会重排**，而不是悄悄改掉他 34 KB 的配置。

接入一律：先 `POST /api/connect/preview` 拿"会改哪几行"（不写盘）→ 用户确认 →
`POST /api/connect {agent, confirm:true}` → 写前备份到 `backups\agent-connect-<agent>-<ts>\`。

## 3. 后果

**好的**：

- 同一个分组可以同时用多家来源，同名模型各有自己的客户端 ID，流量归属明确。
- 上游收到的仍是**干净**模型名（实测：`srapi/gpt-5.6-sol` 打到 B 时 B 收到 `gpt-5.6-sol`），
  中转站不需要认识我们的头。
- 无头 = 主来源，官方保留干净 ID，**存量行为一位没变**：没配 head 的来源，
  `config.yaml` 与目录产出与改造前逐字段相同。
- 五个客户端共用一套路由与一份模型清单。

**代价 / 风险**：

- **`script/route_selector.py` 被改了**（哈希锁定的存量资产），两份指纹已重录，
  见 DEV-RULES D5。改动集中在 `snapshot` / `build_config` / `regen_catalog` /
  `apply_selection` 四处，全部保持向后兼容（老形状的 `selected` 仍然吃）。
- 加了渠道头会**改变客户端可见的模型 ID**，Codex 需要重启一次；老会话钉的是
  `model` 名（HANDOFF §6.5.1），所以**给来源加头会让那些会话里的模型失效**，
  要手动换模型。遗留 `A/` 别名按 H5 永不加头，那 3 个钉非官方模型的线程不受影响。
- 五个客户端里只有 Codex 那条路径**以前就在线验证过**。其余四个的"接入后确实能用"
  **本轮没做端到端实测**（用户明确说本轮不做实机测试）——只验证了"写入的字节正确、
  备份存在、注释没丢"。详见 HANDOFF 的未验证清单。

## 4. 被否决的方案

- **给网关加一层自研转发**：复用现成的 `alias` 机制就够，多一层就多一个故障点。
- **靠 `X-Route-Tag` 区分同名模型**：那是**凭据级**的 HTTP 头消歧（ADR-0003），
  解决的是"同端点两个账号"，解决不了"客户端发同一个模型名该走谁"。
- **把 `head` 也写进 `expose` / `models` / `model_settings`**：会让
  `route_selector.py:367-369` 的 `picks ∩ available` 求交全部落空，
  **静默清空用户勾选的模型**且不抛异常。这是本设计里最需要守住的一条。
- **按"当前是不是唯一启用"动态决定加不加以头**：模型 ID 会随启停来回变，
  客户端每次都要重启，老会话也会反复失效。头是来源的**静态**属性。
- **`pi`**：本机没有安装痕迹（`~/.pi/agent/auth.json` 与 `models-store.json` 都是
  2 字节的 `{}`，PATH 上没有 `pi`，npm 全局也没有这个包），读不出配置格式。
  用户决定本轮完全不做，树里也不出现。
