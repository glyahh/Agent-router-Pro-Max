# 同服务商的多个来源靠 X-Route-Tag 区分

CLIProxyAPI 靠 `(配置段, base-url)` 定位一条凭据。当同一家服务商开了两个账号（本机上的 ProxyHub 与 Hub 大号 都指向 `api-proxyhub.jzzcg.com/v1`），两条凭据的 base-url 完全相同，匹配不再唯一，控制台会直接报"供应商配置缺失或重复"。

决定：给这两条（以及所有同 url 的多账号）各挂一个自定义请求头 `X-Route-Tag`，值在本地唯一（`hub-main` / `hub-big`），控制台按它消歧。`route_selector.py:52` 的 `find_entry()` 里已经为这个场景留了分支。

**代价是实打实的**：这个头会随请求发往上游。这是借用 `headers` 字段当本地标记——CLIProxyAPI 的凭据 schema 里没有别的可放自定义标记的位置，而改动匹配逻辑本身风险更大。

实测上游忽略它：kkgait 与 aiapi2025 两家早就在用同样的机制（`X-Route-Tag: gpt` / `cn`），今天给 ProxyHub 补上 tag 后其 `/models` 仍正常返回。

改动前备份在 `backups/manual-hubbig-20260926-155144-650008`。
