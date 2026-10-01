# 本地模型路由

> **2026-09-30 起界面换了。** 首页（`#/home`）不再是一张"所有来源"表和三个下拉框，
> 而是四级节点树：**Agent → 分组 → 来源 → 模型**。一个分组现在可以**同时启用多家来源**，
> 用"渠道头"区分流量。旧的 `#/config` 会自动跳到 `#/home`。
> 决策与证据见 `docs/adr/0008-multi-source-and-agent-registry.md`。

## 当前生效的路由（2026-09-30 第二轮，逐条实测）

> 本节取代下面「两个选择器（旧描述）」和「目前真实检查」里与之冲突的条目。

| 分组 | 已启用来源 | 说明 |
|---|---|---|
| gpt | `openai-official`（**无头，主来源**）、`hub-big`（`head=hub-big`） | 需求 5 第一次真正用上：同名模型各有自己的客户端 ID |
| deepseek | `goat` | 未变 |
| glm | `goat-glm` | 未变 |

**网关注册 20 个模型 = 目录可见 20 个，死链 0、反向差 0**（判据是
「目录可见 slug 集 == `GET /v1/models` 的 id 集」，不是 §6.4 那个错判据）：

> **⚠️ 这个等式只在「网关刚重启 / 账号没用完」时成立。** 实测：官方 OAuth 账号打满配额后
> （primary 窗口 100%、credits 0、plan=plus），**运行中的网关自己收回该凭据的 8 个模型**，
> 死链当场变成 8 条，而配置文件一个字节没动。重启网关即恢复。
> 所以：**判断"配置写对了"看重启后的注册表；判断"现在能不能用"看当下的 `/v1/models`
> 与用量页的配额信号。** 两者不是一回事。

- 11 个干净 ID：`codex-auto-review`、`gpt-5.5`、`gpt-5.6-luna`、`gpt-5.6-sol`、
  `gpt-5.6-terra`、`gpt-6-astra`、`gpt-6-luna`、`gpt-6-sol`、`gpt-6.1-sol`、
  `deepseek-v4.1-flash`、`glm-5.3-flash`
- 9 个带头 ID：`hub-big/` 加上述 9 个 gpt 模型

**§6.4 的死链是「补齐」修掉的**，不是收紧。根因不是「expose 里有 models 里没有的模型」，
而是**官方 auth 文件的 `excluded_models` 里排除了三个官方其实提供的模型**
（`codex-auto-review` / `gpt-5.6-luna` / `gpt-5.6-sol`），而某家未启用来源的 `expose`
又把它们写进了目录。`auth_excluded()` 的算法是「官方列表 − 你勾选暴露的 + 图片模型」，
所以只要在 `openai-official` 的 expose 里勾上它们，保存时排除项自动重算。
`openai-official` 的 expose 因此是 **9 条**（原 5 + 补 3 + 保住 `gpt-6.1-sol`）——
最后那个必须带上：它在网关注册、却不在 expose 里，只补 3 个会被顺手排除掉。
`gpt-reserve` 与 5 个图片模型继续排除。

**hub-big 的 9 个模型全部做过最小推理验收**（各一次 `max_output_tokens=16`，均 200）。
端到端（经网关 8317）实测：

```
hub-big/gpt-5.6-sol  -> 200  status=completed  model=gpt-5.6-sol
                        ↑ 上游收到的是剥掉头的干净名，别名机制成立
gpt-5.6-sol (官方)   -> 429  usage_limit_reached（plan_type=plus，账号额度）
deepseek-v4.1-flash  -> 400  insufficient credits（Goat 账号余额）
```
后两条是**上游账号的额度/余额**，不是 Prism 的问题。

**用不了的来源（各记一条，别再重复排查）：**

| 来源 | `/models` | 结论 |
|---|---|---|
| `srapi` / `srapi-deepseek` | **HTTP 410 Gone** | 同站点。410 是上游返回，代理/直连都一样。`build_config` 有硬闸：`enabled and fetch_error` 就拒绝启用，所以**在 UI 里勾它会直接被拒**，不是能绕的 |
| `kkapi` | HTTP 403 | 密钥无效 |
| `proxyhub` | OK（14 候选 / 9 个 codex 可用） | 能启用，但挂着「账户余额不足」记录，所以第二来源选了 hub-big |
| `hub-big` | OK（同上 9 个） | 已启用，见上 |
| `deepseek-official` | OK | `disabled: true`，保持停用 |

第二来源没选 `proxyhub` 是因为它有余额记录；`hub-big` 是同一站点的第二个账号，
`/models` 与推理都通，且**入网前先探过** —— 顺序是「先探通再入网」，别反过来。

## 现在怎么用

1. 打开 http://127.0.0.1:8318/ ，默认就是首页。也可从
   http://127.0.0.1:8317/management.html 右下角「供应商选择」进入。
2. 在树里点开一个 **Agent**（默认展开第一个已接入/已安装的）→ 看到三个**分组**。
3. 每个分组下面列出**来源**。勾选要启用的（**可以勾多家**），点「保存路由」。
   - 不点击的时候只展到"来源"这一层；**点来源那一行**才展开它的模型清单。
4. 同一分组启用多家时：最多一家可以**无头**（点来源行上的「无头·主」徽章改），
   它保留干净的模型 ID；其余的各自设一个**渠道头**（例如 `srapi`），
   于是客户端会同时看到 `gpt-5.6-sol` 与 `srapi/gpt-5.6-sol`。
5. 要接到某个代理端：点 Agent 行上的「接入预览」→ 看清会改哪几行 → 确认。
   改完**完全退出并重开那个客户端**一次。

日常请在这里切换，不再用 CC Switch 切换各客户端的直连供应商；后者会绕过本地网关，
可能覆盖客户端入口。没有修改 CC Switch 数据库。

## 两个选择器（旧描述，保留作对照）

- GPT：ProxyHub、Hub 大号、KKAPI-小号、SRAPI。
- DeepSeek：DeepSeek 官方、Command Code Goat。
- 官方 OAuth 凭据副本仍在原网关管理页禁用；Chat 的原官方登录没有改动。
- 菜单使用干净模型 ID 和现有官方显示名。旧任务保存的 A/ 模型 ID 仅作为隐藏兼容别名，
  不产生同名菜单项，**也不会被加渠道头**。
- ProxyHub 保留你已有的 A/ 或无前缀渠道选择，不随机选其他渠道。
- ProxyHub 与 Hub 大号 的 base-url 相同，靠 X-Route-Tag 区分（hub-main / hub-big）。
  该头会随请求发往上游；补 tag 后 ProxyHub 的 /models 仍正常返回，未观察到影响。
- 只启用被选来源的明确模型映射，其他来源排除/禁用。无自动换源、无模型版本冒充。
- deepseek-flash 与 deepseek-v4.1-flash 没有被强行合并成同一个版本；切换到不支持当前 ID
  的来源会报错。

## 五个代理端

| 代理端 | 配置文件 | 谁去改 |
|---|---|---|
| Codex | `~/.codex/config.toml` | 首页「接入预览」→ 复用既有的 `connect_client`（要求 `model_provider = "custom"`） |
| Claude Code | `~/.claude/settings.json` | 写 `env.ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY`；**不动** 9 个 `*_MODEL*` 变量 |
| Claude Code 桌面端 | 同上一个文件 | 同 Claude Code |
| opencode | `~/.config/opencode/opencode.json` | **新增** `provider.prism`；现有 7 家供应商原样不动 |
| Hermes | `%LOCALAPPDATA%\hermes\config.yaml` | **按行插入** `providers.prism`；注释一行不丢；全局 `model` 段不动 |

`pi` 本轮**没做**（本机没装，读不出配置格式）。

## 目前真实检查

- ProxyHub：最小 Responses/SSE 请求成功，携带 function schema。
- Hub 大号：与 ProxyHub 同站点、另一个账号。只拉通 /models（13 个候选，含 5 个生图模型，Codex 不可用）；未做推理验收，未勾选任何模型。
- DeepSeek 官方：最小 Responses/SSE 请求成功，通过 CLIProxyAPI 兼容适配。
- Goat：最小 Responses/SSE 请求成功，通过 CLIProxyAPI 兼容适配。
- KKAPI：https://kkgait.com/v1/models 用当前保存的密钥返回401。入口保留但暂不可选。请在高级配置修正密钥，再点击「重新验证密钥」。不会自动替换为 ProxyHub 地址。
- SRAPI：gpt-5.5 的 Responses 请求返回403。没有断言其他模型也不可用，没有修改现有凭据。

以上是最小协议测试，不等于所有模型、工具调用、真实长任务均已验收。

## 旧会话与重启

使用当前安装版 Codex 的 app-server 完成两项隔离测试：

1. 同一进程、同一任务，先请求假上游A，管理接口热切换后请求B；B收到历史用户消息和A的回答。网关、客户端均未重启。
2. 模拟已有直连任务：先在直连A时创建历史，首次改接网关并重新启动后端，恢复原任务ID，再向B续聊成功。没有重写历史记录。

真实桌面 Chat、local Work、Codex 的联合验收尚待首次接入后完成；含加密推理状态、压缩记录和复杂工具历史的跨供应商长会话也仍需验证。不能把简单文本假上游测试扩大为所有旧任务无条件兼容。

## 上下文

- 新目录：D:\MY_DESIGN\Agent-router-Pro-Max\codex-model-catalog.json。
- GPT 配置窗口272,000，有效95%；安装版 Codex 后端实测上报258,400。
- GPT 自动压缩阈值配置230,000；DeepSeek 保守配置128,000窗口、100,000压缩阈值，不声称这是其官方最大能力。
- 去除新目录内的 Fast/priority 选项；接入时设置默认服务等级。
- 目录与压缩阈值不是服务端计费硬闸。尚未验证三家中转站的1.5倍规则，也没有声称任何输入都绝不会越限。
- 你尚未点击首次接入，原客户端的大窗口配置当前仍未被替换。

## 一键脚本

```powershell
& 'D:\MY_DESIGN\Agent-router-Pro-Max\script\Start-Proxy.ps1'
& 'D:\MY_DESIGN\Agent-router-Pro-Max\script\Pause-Proxy.ps1'
& 'D:\MY_DESIGN\Agent-router-Pro-Max\script\Restart-Proxy.ps1'
```

以上现在同时管理网关与选择页。重复启动不会重复创建实例。暂停会打断正在使用本地网关的请求。

## 实现与恢复

- 选择页后端：D:\MY_DESIGN\Agent-router-Pro-Max\script\route_selector.py，Python标准库，只管理配置，不转发推理请求。
- 供应商映射：D:\MY_DESIGN\Agent-router-Pro-Max\routing-plan.json，不含密钥。
- 选择页资源：D:\MY_DESIGN\Agent-router-Pro-Max\static\selector.html、selector.js、selector.css。
- 热切换使用 CLIProxyAPI 原生管理 API，保存后核对实际模型注册表。
- 修正前自动备份到 D:\MY_DESIGN\Agent-router-Pro-Max\backups\route-switch-*；首次客户端接入备份到 client-connect-*；手工新增供应商到 manual-*（Hub 大号：backups\manual-hubbig-20260926-155144-650008）。
- 回滚客户端：先退出客户端，用该次备份的 codex-config.toml 恢复 C:\Users\user\.codex\config.toml。不要无故覆盖仍有效的 auth.json。
- 回滚网关：先暂停，用该次备份的 config.yaml 恢复 D:\MY_DESIGN\Agent-router-Pro-Max\config.yaml，再启动。不要仅原子覆盖文件并假定已热加载。
- 不要同时在高级配置和选择页保存同一组供应商。选择页拒绝过期配置版本；失败时尝试恢复自己的更新，不覆盖检测到的外部修改。
- 不上传 config.yaml、.local-secrets.json 或备份中的凭据文件。

## 复现测试

D:\codex\WorkDocument\2026-09-20\new-chat\work\gateway-routing 下的 test_selector.py、test_continuation.py、test_existing_session_migration.py、test_browser.cjs 可复核隔离测试。

test_real_smoke.py 会向真实供应商发送少量推理请求，可能计费；不应作为无成本回归循环执行。
