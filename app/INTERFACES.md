# Prism 接口契约

本文件是 `app/server.py` 对外暴露的 HTTP 接口的**唯一契约来源**。以前这份契约只存在于
`Handler._dispatch()` 的代码里，而 `server.py`、`core/sources.py` 的注释都指向本文件却指向了
不存在的路径——现在补上。改接口先改这里，再改代码。

---

## 1. 谁是调用方

只有两个：

| 调用方 | 说明 |
|---|---|
| 内嵌前端 | `app/static/` 下的五个页面，跑在 WebView2 里，经 `app/static/app.js` 的 `api` 封装发请求 |
| 调试 | 浏览器直接开 `http://127.0.0.1:8318/`，或在控制台里 `Prism.api.get('api/state')` |

**没有第三方调用方。** 这个服务只监听本机回环，只服务本机窗口。

> 术语提醒：这里的"客户端"指**调本服务 HTTP 接口的**东西（就是上面这两个）。
> "把 Codex / Claude Code 接到网关"里那个客户端是**代理端 (Agent)**，见 `CONTEXT.md`。
> 两者不是一回事——代理端**从不**直接调 Prism 的接口，它们的推理流量直接打 8317。

### 1.1 CSP：内联 `<script>` 是被拦的

`script-src 'self'`（**没有** `unsafe-inline`；`style-src` 才有）。所以：

- 页面注入 `<style>` —— **可以**（`config.js` / `home.js` / `settings.js` 都靠它）；
- 页面内联 `<script>…</script>` —— **不行**，静默拦掉。

做验收探针 / 注入脚本时必须走 `<script src="…">`。详见 `docs/DEV-RULES.md` B4。

---

## 2. 安全模型（每个请求都过）

照搬 `script/route_selector.py` 的 `Handler.check()`，不是形式主义——控制台能改用户的网关配置，
DNS rebinding 和跨站表单都能把请求送到本机端口上。

| 检查 | 规则 | 违反时 |
|---|---|---|
| `Host` | 必须**精确等于** `127.0.0.1:<console_port>`。写 `localhost`、局域网 IP 一律拒绝 | `409` |
| `Origin` | 有该头时必须等于 `http://127.0.0.1:<console_port>`；没有该头则放行（同源 GET 不带） | `409` |

用 `409` 而不是 `403`，是为了和既有选择页的行为一致——前端 `app.js` 对 `404/409/5xx` 各有一套
中文文案，行为一致才不会让用户看错原因。

请求体的读取顺序也是契约的一部分：**先读完 body，再校验 Host/Origin**。反过来的话，被拒绝的
请求会把没读完的字节留在连接里，HTTP/1.1 keep-alive 的下一个请求被解析成垃圾。

---

## 3. 请求与响应约定

- `protocol_version = HTTP/1.1`（前端每 5 秒轮询，连接复用；日志页是 3 秒）；`timeout = 60` 秒
- 请求体必须是 **JSON 对象**（不是数组、不是裸字符串）
- 必须带 `Content-Length`；带 `Transfer-Encoding: chunked` 直接 `411`
- 请求体上限 `MAX_BODY = 1 MiB`（来源表单要带整张模型目录），超限 `413` 并主动关连接
- `POST /api/select`、`/api/connect`、`/api/sources`、`/api/settings` 的 body **必填**；
  `/api/recheck` 允许不带。判"有没有带"要看 `None`，不能判真假——`{}` 是合法 body 但 falsy

**成功**：

```json
{ "ok": true, "data": <任意 JSON> }
```

**失败**：

```json
{ "ok": false, "error": "<中文原因>" }
```

每个响应都带 `Cache-Control: no-store`、`X-Content-Type-Options: nosniff`、`Content-Security-Policy`。

---

## 4. 错误到状态码的映射

| 异常 | 状态码 | 含义 |
|---|---|---|
| `HttpError` | 自带 | 参数错、未知路径、方法不对 |
| `RouteError` / `sampling.SamplingError` | `409` | 网关不可达/被封、配置冲突、业务校验失败。**消息本身就是给用户看的中文原因** |
| 其他任何 `Exception` | `500` | 兜底。响应里带异常类型 + 提示 + `prism.log` 路径，完整 traceback 只进日志 |

未知路径 `404`，不支持的方法 `405`。

---

## 5. 接口表

### GET

| 路径 | 查询参数 | 转调 |
|---|---|---|
| `/api/state` | — | `bridge.snapshot()`。响应里另有 `unreadable`：**算不出启用状态**的来源数组（每项 `{id,label,group,error}`）—— config 里那条条目缺失或重复、行缺 `id`、auth 文件读不出来等。首页会把它渲染成一条警示条；**它不是为了调试**，别去掉 |
| `/api/theme` | — | `{"theme": "dark"\|"light", "system": "dark"\|"light"}`。`theme` 是**有效主题**（设置里的 `theme_mode` 显式为 light/dark 时优先，否则跟随系统），`system` 是系统原始值 —— 设置页要靠它显示"跟随系统（当前：深色）"。实时判定、**不缓存**。前端每 5 秒跟一次；服务 `index.html` 时也用它写 `<html data-theme>`。见 [ADR-0010](../docs/adr/0010-dark-theme-follows-system.md) |
| `/api/sources` | — | `sources.list_sources()` |
| `/api/usage` | `days`，1–365，默认 7 | `usage_payload(days)` |
| `/api/monitor` | — | `health.monitor_state()` |
| `/api/logs` | `limit`，1–5000，默认 200 | `read_logs(limit)` |
| `/api/error-logs` | — | `list_error_logs()` |
| `/api/error-logs/{name}` | — | `read_error_log(name)`，返回 `text/plain`，不是 JSON 包装 |
| `/api/settings` | — | `settings_payload()`（含网关四项） |
| `/api/agents` | — | `agents.list_agents()`（5 个客户端的注册信息 + 实测状态） |

**截断是一个显式信号，不能悄悄发生**：`/api/logs` 和 `/api/error-logs/{name}` 撞到
`MAX_TAIL_BYTES = 8 MiB` 时只给一部分内容，此时响应带 `X-Prism-Truncated: 1`，
`data.truncated` 是同一件事的 JSON 副本（给不看响应头的调用方）。

越界的整数参数一律 `400`，不静默夹到边界。

### POST

| 路径 | body | 转调 |
|---|---|---|
| `/api/select` | 必填 | `bridge.apply_selection(body)` |
| `/api/connect` | 必填 | `agents.connect_agent(body.agent or 'codex', body)` |
| `/api/connect/preview` | 选填 | `agents.preview_agent(body.agent or 'codex')`，**不写盘** |
| `/api/recheck` | 可空 | `bridge.recheck_blocked()` |
| `/api/sources` | 必填 | `sources.create_source(body)` → `source_write_result()` |
| `/api/sources/preview` | 选填 | `preview_models(body)` |
| `/api/settings` | 必填 | `apply_settings(body)` |
| `/api/sources/{id}/test` | 可空 | `sources.test_source(id)` |

### PUT

| 路径 | body | 转调 |
|---|---|---|
| `/api/sources/{id}` | 必填 | `sources.update_source(id, body)` → `source_write_result(updated, id)` |
| `/api/sources/{id}/head` | 必填 `{head}` | `sources.set_head(id, head)` |

`/head` 是**独立的一条窄路径**，必须排在通配那条之前看：它只改 `routing-plan.json` 里那一行的
`head`，**不碰 `config.yaml`**。因此内置来源（`custom: false`）也能改——而 `update_source`
明确只让改自定义来源。`head` 是纯 plan 字段，改它不需要动网关配置，也不需要
`_assert_unique` / `find_entry` 那套跟 config 相关的校验。

### 多来源：`selected` 的形状变了（2026-09-30）

```jsonc
// 旧：一个分组一个 id；多家同时启用是错误态
"selected": { "gpt": "srapi", "deepseek": null, "glm": "conflict" }

// 新：一个分组一个**数组**；多家同时启用是正常态
"selected": { "gpt": ["openai-official", "srapi"], "deepseek": ["goat"], "glm": [] }
```

`POST /api/select` 的 `selected` **两种形状都吃**（字符串 = 老的单个 id），
归一化在 `route_selector._selected_ids()`。`'conflict'` 哨兵值已下线。

不变量（在 `bridge._apply_selection_locked` 里、**任何写盘之前**判）：
同一分组最多启用一个无头来源；已启用来源产出的客户端可见 ID 不许重复。
违反时 `409` 且**一个字节都不写**——实测被拒时对网关的 PUT 次数是 0。

### DELETE

| 路径 | 转调 |
|---|---|
| `/api/logs` | `clear_logs()` |
| `/api/sources/{id}` | `sources.delete_source(id)` → `{"deleted": id}` |

`{id}` 段用 `[^/]+` 匹配，不接受斜杠。

---

## 6. 静态文件

`GET` 之外的方法一律 `405`。

- `/` → `index.html`（`STATIC_ALIAS`）
- 路径开头的 `static/` 会被剥掉，所以前端写 `/static/app.js` 或 `/app.js` 都能命中
- 解析用 `resolve(strict=True)`，**必须落在 `STATIC_DIR` 之内**；`../` 这类目录穿越返回 `404`。
  静态目录之外的文件一个都不许端出去
- `Content-Type` 按扩展名查 `MIME` 表，查不到用 `application/octet-stream`

---

## 7. 日志与错误日志的路径来源

| 常量 | 位置 |
|---|---|
| `PRISM_LOG` | `<ROOT>/prism.log`，512 KiB × 3 轮转（与 `main.py` 的 `setup_logging` 保持一致） |
| `LOGS_DIR` / `MAIN_LOG` | `<ROOT>/logs/`、`<ROOT>/logs/main.log`（网关自己的日志） |
| `SETTINGS_PATH` | `<ROOT>/settings.json`，与 `config.yaml` 同一层 |

---

## 8. 控制台**不**经手的部分

本服务从不转发推理请求。推理流量由 Codex 直接打到网关 `127.0.0.1:8317`。

任何打网关管理接口 `/v0/management/*` 的代码路径（在 `core/bridge.py`、`core/sampling.py`），
**发请求前必须先问 `bridge.auth_gate()`**：网关连 5 次 401 会封本机 IP 约 30 分钟，
封禁期所有管理接口一律 `403` 且 0 秒返回，连不存在的路径也是 `403` 而不是 `404`。
`403` 一律按封禁处理、绝不重试。详细取证见 `core/bridge.py` 文件头的文档字符串。
