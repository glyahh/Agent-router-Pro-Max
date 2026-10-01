# 供应商预配置状态

此阶段只保存供应商与候选模型，未决定启用项，未修改 Codex/CC Switch。

- **OpenAI Official：认证文件 auth/codex-official.json；`disabled: false`（已启用）**，
  `excluded_models` = `gpt-reserve` + 5 个图片模型。原 Codex 登录文件保持不变。
  **这里以前写的是「disabled=true 且 excluded_models=["*"]」，2026-09-30 实测已不成立** ——
  它现在就是 GPT 分组的**无头主来源**，官方账号的模型由上游发现注册（10 条）。
  当前生效的路由见 ROUTING.md 顶部那一节。
- ProxyHub · Codex：config.yaml 的 codex-api-key 第 1 项；全部模型排除。2026-09-26 补 X-Route-Tag: hub-main，用于与同站点的 Hub 大号区分。
- Hub 大号：codex-api-key 第 5 项；https://api-proxyhub.jzzcg.com 的第二个账号，X-Route-Tag: hub-big；全部模型排除，未勾选模型。
- KKAPI-小号：codex-api-key 第 2 项；用户指定 https://kkgait.com/v1；全部模型排除。
- SRAPI：codex-api-key 第 3 项；全部模型排除。
- DeepSeek Official：openai-compatibility；disabled=true。Chat 协议适配方案待真实调用验收。
- Command Code (Goat)：openai-compatibility；disabled=true。

Codex API key 配置没有与 OpenAI compatibility 相同的 disabled 字段。当前使用 excluded-models=["*"] 禁止所有模型参与路由，不能将条目存在或状态显示 active 理解为有可调用模型。

候选目录保存在 provider-inventory.json，不含密钥。ProxyHub/SRAPI/Goat 使用本次任务之前读取的模型列表；KKAPI/DeepSeek 仅使用 CC Switch 保存的候选，目录完整性及可用性未验收。各类图像/语音/嵌入模型不能仅因为出现在列表就当作 Codex 主代理模型。

## 同名模型方案（2026-09-30 **已实施**）

> 这一节以前标题是"（未实施）"。现在落地了，方案与原文设想基本一致，只是把
> "model → selected provider → actual upstream model ID" 换成了**别名**：
> 客户端可见 ID = `head` + `/` + 干净别名，上游收到的仍是干净模型名。
> 决策全文与实测证据见 `docs/adr/0008-multi-source-and-agent-registry.md`
> 与 `docs/evidence-p0-head-routing.txt`。

Codex 现在能看到同名模型的两个条目：`gpt-5.6-sol`（无头来源）与
`hub-big/gpt-5.6-sol`（Hub 大号）。显示名是 `Hub 大号 · gpt-5.6-sol`。
（第一稿举的例子是 `srapi/gpt-5.6-sol`；SRAPI 的 `/models` 上游返回 **410**，
已不能启用，第二来源改用 `hub-big`。见 ROUTING.md。）

- 一个分组可以**同时启用多家来源**（以前是"多家同时启用 = 冲突"）。
- 最多一家**无头**（主来源），它保留干净的模型 ID；其余各自设渠道头。官方按惯例无头。
- 供应商的 `A/`、`B/` 等上游渠道 ID **仍然保留**，且**永不加渠道头**——那些 ID 被老任务
  钉死了（HANDOFF §6.5.1）。
- 切换的作用范围仍是"下一个请求"；跨供应商的 `previous_response_id`、推理状态、缓存
  仍然**不能假定兼容**（未变）。

## 验证

本地假上游：三次被排除模型请求返回 model_not_found，上游收到 0 次请求。真实网关重启后读取到 3 家 Codex API key、2 家 OpenAI compatibility 和 1 个禁用 OAuth 文件；模型列表为空。9 项检查通过。未执行真实套餐推理。

**2026-09-30 新增**：用**临时网关实例 + 假上游**（外网端口 18317 / 19001-19004，
**没碰生产 `config.yaml`**）验证了带斜杠的客户端别名：四个别名原样进 `/v1/models`，
且各自路由到正确的凭据、上游收到的是剥掉头的干净模型名。两个 `section`
（`codex-api-key` / `openai-compatibility`）都验过。证据见 `docs/evidence-p0-head-routing.txt`。
**仍未做**：任何真实中转站的推理验收。

本次原子替换配置没有自动反映到运行时；重启网关后读取验证成功。后续逐模型热切换需要单独验收，不能据项目宣称而默认可用。

配置含敏感凭据，不能上传或提交 Git。原配置的备份在 `backups\` 下（`route-switch-*` / `client-connect-*` / `manual-*` 等目录，命名见 `app\core\sources.py` 的 `AUTO_BACKUP_LABELS`）。

> 早先这里写的是"见 `provider-preconfiguration-verification.json`"，但**该文件在仓库里并不存在**（2026-09-29 实测）。要找回滚点请看 `backups\`。

## 后续实现状态

两个分组选择器已实现：http://127.0.0.1:8318/ ，原管理页右下角提供入口。详见 ROUTING.md。
仍未替用户选择或启用生产供应商，未修改原客户端配置。已有候选目录不会被当作全部可用模型。首次保存选择时才应用精选映射与禁用策略。
KKAPI现有凭据/model列表验证401；SRAPI gpt-5.5推理403；ProxyHub、DeepSeek官方、Goat完成最小Responses/SSE测试。2026-09-26 复测 /models：KKAPI 403、SRAPI 410，均拉取失败，与本次新增无关；ProxyHub 与 Hub 大号 /models 正常。
