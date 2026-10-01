# 本地模型网关

把远端多家 AI 服务商的模型收敛成本地一个入口供 Codex 使用。GPT / DeepSeek / GLM 三个分组各自可以在多个来源之间切换，切换不必改客户端配置。

## 语言

每个词只指一个东西。各条目末尾的 `_Avoid_` 列出的是**禁用词**，写文档时不要用。

其中「供应商」是被两边同时禁用的伞词：「来源」和「服务商」都曾被它指代，读的人无法判断说的
是哪一个。看到它一律展开成具体那一个。

### 路由

**来源 (Source)**：
一条可切换的上游凭据配置——一个端点、一把密钥、一组可用模型。界面上"选哪家"选的就是它。
_Avoid_: 供应商、provider、渠道、线路

**服务商 (Vendor)**：
提供上游 API 的公司或站点。一个服务商下可以挂多个来源（同一家的不同账号）。
_Avoid_: 供应商、上游、中转站

**分组 (Group)**：
模型家族的分类：GPT / DeepSeek / GLM。每个来源归属且只归属一个分组。
_Avoid_: 类别、类型、系列

**路由 (Routing)**：
当前生效的选择——三个分组各自选中哪个来源，以及每个来源暴露给客户端的模型清单。
_Avoid_: 配置、设置、供应商选择

**暴露 (Expose)**：
把一个模型写进客户端模型目录，使其在 Codex 的 `/model` 里可选。上游提供了但未暴露的模型不可选。
_Avoid_: 启用、勾选、注册

**渠道头 (Head)**：
来源上的一个短标识（`srapi`、`hub-main`），拼在**客户端可见的模型 ID** 前面，用来在
同一个分组启用了多家来源时区分流量归属。`gpt-5.6-sol` 是无头（主来源）的 ID，
`srapi/gpt-5.6-sol` 是 SRAPI 的 ID。**只写进 `routing-plan.json` 的 `head` 字段**，
不写进 `config.yaml`；只在生成 config 的 `alias` 与目录的 `slug` 时施加。
正弦：24 字符以内的小写字母/数字/`._-`，禁斜杠（禁斜杠让"有头 ID"与"无头 ID"
结构上不可能撞车）。
_Avoid_: 前缀、渠道前缀（那是**上游**加的 `A/`，两个不同的东西）、别名

**无头 (Headless)**：
没有渠道头的来源。一个分组最多启用一个——它保留干净的模型 ID，是该分组的**主来源**。
官方来源按惯例是无头的（用户 2026-09-30 明确要求"OpenAI 官方不要加任何前缀"）。
_Avoid_: 默认来源、首选来源

### 客户端

**代理端 (Agent)**：
本机要接到网关上的编码客户端。当前支持 5 个：Codex、Claude Code、
Claude Code 桌面端、opencode、Hermes。每个有自己的配置文件与协议方言
（responses / anthropic / openai-compatible）。**路由是网关级的、五个代理端共享**，
不存在"每个代理端一套来源"。
_Avoid_: 客户端（那指"谁在调 Prism 的 HTTP 接口"，见 INTERFACES.md）、模型、bot

**接入 (Connect)**：
把某个代理端的推理入口指向本地网关的一次性改动。Codex 改
`~/.codex/config.toml`；Claude Code 改 `~/.claude/settings.json`；opencode 改
`~/.config/opencode/opencode.json`；Hermes 改 `%LOCALAPPDATA%\hermes\config.yaml`。
一律先备份到 `backups\agent-connect-<代理端>-<时间戳>\`。
_Avoid_: 绑定、对接、安装

### 凭据与上游

**网关 (Gateway)**：
本机的 CLIProxyAPI 实例，监听 8317，负责真正的请求转发。
_Avoid_: 代理、后端

**控制台 (Console)**：
桌面应用 Prism，监听 8318。改配置、展示数据，并管理网关进程的生命周期（拉起、身份核对、重启），但不转发推理请求。
_Avoid_: 前端、管理页、选择页

**凭据 (Credential)**：
一条指向某个服务商的鉴权记录——API key 或 OAuth 令牌，加上端点地址。
_Avoid_: 密钥、token、账号

**渠道前缀 (Channel Prefix)**：
上游在模型 ID 前加的 `A/`、`B/` 这类标记，用于指定走哪条上游渠道。不能去掉前缀再发给上游。
**与「渠道头」是两个不同的东西**：渠道前缀是上游给的（我们只能原样保留），渠道头是我们
给来源起的（用来区分同分组的多家来源）。遗留的 `A/` 别名永不加渠道头——那些 ID 被老任务
钉死了。
_Avoid_: 别名前缀、渠道头

### 客户端

**接入 (Connect)**：
把 Codex 客户端的推理入口指向本地网关的一次性改动，改的是 `~/.codex/config.toml`。
_Avoid_: 绑定、对接、安装

**推理等级 (Reasoning Level)**：
单个模型支持的思考深度档位。Codex 侧枚举为 low / medium / high / xhigh / max / ultra。声明在模型目录里，决定客户端**能选哪些**档；实际用哪档由客户端 config.toml 的 `model_reasoning_effort` 决定。
_Avoid_: 思考力度、effort、推理强度

### 用量

**配额信号 (Quota Signal)**：
官方 OAuth 账号随响应头返回的额度信息——已用百分比、窗口长度、重置时间。只对官方账号有效。
_Avoid_: 用量、额度、余额

**请求计数 (Request Count)**：
按来源统计的请求成功/失败次数，10 分钟一个桶，窗口约 3.3 小时。第三方中转只有这个，没有 token 数。
_Avoid_: 用量、消耗、调用量
