# Prism —— 本地模型网关的桌面控制台

Prism 管两件事：把 `cli-proxy-api.exe`（8317 网关）拉起来并看着它，以及提供一个 8318 上的界面来改路由、看用量、翻日志。

**它不转发推理请求。** Codex 请求直接打到 8317，Prism 在旁边，只碰配置和统计数据。所以 Prism 崩了，你正在跑的对话不受影响。

三份配套文档：术语看 `CONTEXT.md`，路由现状看 `ROUTING.md`，决策记录看 `docs\adr\`。

---

## 启动

开发态（现在就能用）：

```
D:\MY_DESIGN\Agent-router-Pro-Max\app\.venv\Scripts\python.exe D:\MY_DESIGN\Agent-router-Pro-Max\app\main.py
```

打包之后直接双击 `Prism.exe`，不用带路径。

启动顺序是固定的：

1. 单实例检查（`Global\PrismConsole-<控制台端口>` 互斥体）。锁名带端口，所以拿不同端口起的第二份照样能开。重复启动时不会再开一个窗口，而是弹个框告诉你「Prism 已经在运行」，并给出托盘和网页两条找回路径；`--no-window` 下不弹，模态框会把脚本挂住。
2. 探测 8317。**没在监听**才去拉 `cli-proxy-api.exe -config config.yaml`，隐藏窗口。已经在跑就不动它——你自己的网关不会被顶掉。
3. 起 8318 控制台服务。
4. 开 WebView2 窗口，指向 `http://127.0.0.1:8318/`。

关窗口默认收到托盘，不是退出。托盘图标右键才有「退出」。想把关窗口改成直接退出，去设置页改「关闭到托盘」。开机自启也在设置页，写的是 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`。

---

## 五个页面

左边导航，hash 路由，不用 F5 刷新。默认落在**首页**（`#/home`）；
旧的 `#/config` 会自动跳过去。

### 首页

改路由的地方，也是你平时用得最多的一页。四级节点树：**Agent → 分组 → 来源 → 模型**。

- **Agent 行**是手风琴（同时只展开一个——五个代理端共享同一套网关路由，
  摊开五份只是把同一棵子树画五遍）。行上有「接入预览」，先给你看会改哪几行，**不写盘**，
  确认之后才写，并备份到 `backups\agent-connect-<代理端>-*`。
- **分组行**（GPT / DeepSeek / GLM）下面列出该组的**全部来源**。
- **来源行**：勾选框 = 在这个分组里启用它。**可以勾多家**（2026-09-30 起）。
  行上还有「测试 / 编辑 / 删除」，以及一个**渠道头徽章**——点它就能改（内置来源也能改，
  因为渠道头只写 `routing-plan.json`，不碰 `config.yaml`）。
- **模型行**默认**收着**，点来源那一行才展开。勾选 = 暴露给客户端。
  **没勾的模型不会写进模型目录，客户端的模型列表里就看不到。**
- **渠道头**：同一分组启用多家来源时，最多一家可以无头（它保留干净的模型 ID，
  是该分组的**主来源**）；其余的各自设一个头（如 `srapi`），客户端就会同时看到
  `gpt-5.6-sol` 与 `srapi/gpt-5.6-sol`。头**只拼在客户端可见的 ID 上**，
  上游收到的仍是干净模型名。
- 「保存路由」：落盘。这条链是 `fetch_all → 改 expose → build_config → regen_catalog
  → clean_plan → write_json`，顺序不能变——少了第一步的 live 数据，
  会把你勾的模型静默清空，还告诉你「保存成功」。
- 每个来源一个「测试」按钮：只拉上游 `/models` 验密钥通不通，**不发推理请求，不计费**。
- 底部「＋ 添加供应商」：新增来源默认是**停用**的，加完在树里勾上它再保存。

保存路由会先备份再改，任一步失败自动回滚。备份在 `D:\MY_DESIGN\Agent-router-Pro-Max\backups\route-switch-*`。

> 原来那张 9 列的「所有来源」表已经去掉；来源的排序与成功/失败计数在**监控**页
> （`monitor.js` 的 `sourcesSection`），首页不再摆第二份。

### 用量

分上下两区，别指望它们画在一张图上——两种数据的口径完全不同。

**上区 · 配额信号**：只有官方 OAuth 账号有。已用百分比、窗口长度、重置倒计时。按模型的读数在 `model_quotas` 里，同一个账号不同模型能差很远（实测见过 3% 和 75% 同时存在）。**配额读不到时显示「尚无配额观测」，不是 0%。**

**下区 · 请求计数**：各来源的成功/失败次数，10 分钟一个桶，窗口约 3.3 小时。第三方中转站只有这个，没有 token 数。

长期历史在这个页面底部，Prism 每 10 分钟采一次 `api-key-usage` 落进 `app\usage-history.db`，默认留 90 天。

### 监控

- **网关自身**：8317 端口在不在听、版本号、跟最新版比是不是落后、当前路由和已暴露的模型清单。
- **来源健康**：OAuth 来源能拿到配额、冷却、失败数、订阅到期；API key 来源只有成功/失败计数。

两个必须知道的口径问题：

- **API key 来源的冷却状态拿不到**，字段是 `null`，界面就不显示。别信一个猜出来的值。
- **成功/失败是网关进程内的计数，网关一重启就清零。** 表头写「网关启动以来」就是为了这个。一个跑了几百次的来源重启后显示 0，不是它没被用过。

版本号没有专门的端点，是从 `logs\main.log` 里抠最后一条 `CLIProxyAPI Version:` 行拿的。这行埋得很深——实测 8444 行的日志里它出现在第 8246 行（98% 处），只看日志尾巴是搜不到的。所以 Prism 启动时全量读一次，之后用缓存。

### 日志

- 尾部增量轮询，可以搜。
- 「隐藏管理流量」开关：把含 `/v0/management` 的行滤掉，不然 Prism 自己的请求会把日志刷满。
- 清空：调 `DELETE /logs`。
- 「下载错误日志」：`logs\` 下的 `error-*` 文件。注意单个文件能有 3MB 以上（实测最大的一个 3,194,845 字节），点之前心里有数。

### 设置

- **网关四项**：debug、proxy-url、request-retry、request-log。只碰这四个键，不整份 PUT `/v0/management/config`——那会丢 `host`/`port`/`auth-dir`/`remote-management`。
- **应用四项**：开机自启、关闭到托盘、采样间隔、历史保留天数。这几项存在**项目根**的 `settings.json`（跟 `config.yaml` 同一层，不在 `app\` 下；首次保存设置时才创建，所以它现在可能不存在）。
- **「高级设置 →」**：跳官方面板 `http://127.0.0.1:8317/management.html`。OAuth 登录、认证文件、YAML 编辑这些 Prism 不做，去那边。

**这里没有「控制台端口」这一项，是故意的。** 8318 写死在代码里的模块级常量上，改端口会让所有请求被判成非法 Host 然后 409。想换端口得改代码，不是改设置。

---

## 加自定义来源

配置页 →「新增来源」。

表单是两层：

**来源卡片**

| 字段 | 说明 |
|---|---|
| Agent | 下拉，现在只有 codex。**纯装饰**，网关侧没有这个字段 |
| 分组 | GPT / DeepSeek / GLM。决定它出现在哪个下拉里 |
| 来源 ID | 自动生成，`custom-<slug>`。**它就是 `X-Route-Tag` 的值**，同 base-url 多账号全靠它区分，别跟别的来源重名 |
| 显示名称 | 界面上显示的名字 |
| 端点 | `https://.../v1`，写到 base-url |
| API 密钥 | 存进 config.yaml 的 `codex-api-key` 段 |
| 上下文窗口 | 可选。GPT 分组会被固定成 272000/230000 的既有策略，填了也不生效 |

**模型行**（一个来源可以有多行）

模型 ID、显示名称、推理等级、默认等级。推理等级是**芯片多选**，不是逗号分隔的文本框，枚举用 Codex 那套：`low / medium / high / xhigh / max / ultra`。

保存时写两个文件，顺序不能反：

1. 先写 `config.yaml` 的 `codex-api-key` 段，带上 `headers.X-Route-Tag` 和 `excluded-models: ['*']`——**新建的来源默认是停用状态**，不会立刻参与路由。
2. 再写 `routing-plan.json` 的 provider 行。

删除是反过来三步：先把该条目设成 `excluded-models: ['*']` 停用，再删 plan 行，最后才删 config 条目。跳过第一步的话，删掉一个**当前正被选中**的来源，你下一次点「保存路由」必然 409。

失败会回滚，但**别手动去改这两个文件里的任意一个**——它俩有一致性约束，手改一边就是「检测到额外已启用供应商」那类错误。

两个容易踩的点：

- **别名归属。** 同一个模型别名（比如 `gpt-6-astra`）常被好几家同时暴露。推理等级设置取 **plan 里 providers 顺序中最先声明它的那个来源**。界面上模型行会显示「该别名当前归哪个来源」，看到了就不会以为自己设了却没生效。
- **新增模型后要重启 Codex。** 模型目录只在客户端启动时读一次。保存完不重启，`/model` 里就是不出现。

---

## 退回命令行模式

Prism 和老的 `route_selector.py` 是**同一套逻辑**（Prism 直接 import 它），配置格式完全一样，读写的是同一对文件。所以两边可以随便换，数据不用迁。

步骤：

1. **托盘图标右键 → 退出。** 别用任务管理器杀进程，虽然也能杀掉，但托盘和 SQLite 的收尾不走。
2. 确认 8318 空了：

   ```powershell
   Get-NetTCPConnection -LocalPort 8318 -State Listen -ErrorAction SilentlyContinue
   ```

   没有输出才算空。还有输出说明谁还占着（见下面第一条 FAQ）。

3. 起命令行那套：

   ```powershell
   & 'D:\MY_DESIGN\Agent-router-Pro-Max\script\Start-Proxy.ps1'
   ```

   它先确保网关起来，再起选择页，最后打印 `Provider selector: http://127.0.0.1:8318/`。浏览器打开这个地址，就是老界面。

4. 用完想全停：

   ```powershell
   & 'D:\MY_DESIGN\Agent-router-Pro-Max\script\Pause-Proxy.ps1'
   ```

   它停掉网关和老选择页。**正在跑的请求会被打断**（是停，不是挂起）。

---

## 常见问题

### 8318 已被占用（很可能是 route_selector.py），请先停止它

Prism 用的是 `SO_EXCLUSIVEADDRUSE`。端口被占时**直接报错**，不会 bind 成功然后拿老进程的页面糊弄你。

先看是谁占着：

```powershell
Get-NetTCPConnection -LocalPort 8318 -State Listen | Select-Object OwningProcess
Get-CimInstance Win32_Process -Filter "ProcessId=<上面那个PID>" | Select-Object CommandLine
```

如果是 `...\script\route_selector.py`，停它：

```powershell
& 'D:\MY_DESIGN\Agent-router-Pro-Max\script\Stop-Selector.ps1'
```

（这台机器上现在就是这个情况：8318 被 `route_selector.py` 占着，PID 20068。跑 Prism 之前先停掉它。Stop-Selector 只停选择页，网关不动。）

如果 CommandLine 不是 route_selector.py，说明是别的东西占了 8318，别硬来，换个端口或者把那个程序停掉。

### 上游 403

先分清是谁返回的 403——看监控页的来源状态，或者日志里是哪一段。

**上游中转站返回 403**，常见三种：

- **余额/额度**。`routing-plan.json` 里 ProxyHub 和 Goat 各挂了一条 `warning`，写的就是「账户余额不足」「周额度用尽」。充值或者换一家。
- **模型没授权**。密钥有效但不包含这个模型。ROUTING.md 记着 SRAPI 的 `gpt-5.5` 报 403，同家别的模型不受影响。
- **图片工具**。Codex 的 `image_generation` 开着时会在请求里塞 `image_gen` 工具，没有图片端点的中转站一律 403。接入 Codex 时会自动把它关掉（写进 `config.toml` 的 `[features]`）。

点那个来源的「测试」按钮能区分开：测试只拉 `/models`，过得了说明密钥没问题，403 是模型级的。

**网关自己返回 403**（`/v0/management/*`）说明管理密钥不对。实测日志里就是这么记的：

```
403 | 0s | 127.0.0.1 | GET "/v0/management/config"
```

Prism 拿 `.local-secrets.json` 里的 `management_key` 当 Bearer 发出去（实测这把它现在是通的，`GET /v0/management/config` 返回 200）。网关拿它跟 `config.yaml` 里 `remote-management.secret-key` 那条 bcrypt 记录比对。换过密钥、或者从别处拷来的 `.local-secrets.json`，就会一路 403。改完别再连点——见下一条。

### 网关封了 IP，所有管理接口都 403

网关对连续失败的鉴权有惩罚：**连续几次失败之后，它会把 `127.0.0.1` 封掉大约 30 分钟**，期间所有管理接口一律 403，密钥换对了也没用。

Prism 会识别这个情况，直接告诉你还封着多久，**并且停止重试**——继续戳只会把封禁续上。等倒计时走完，或者重启网关（托盘右键重启，或者 `Pause-Proxy.ps1` 之后 `Start-Proxy.ps1`）。

如果你在别的地方（官方面板、自己写的脚本）也调管理接口，注意那个地方有没有「失败立刻重试」的循环，那是最容易把封禁触发出来的写法。

### 保存路由报「检测到额外已启用供应商」

config.yaml 里有一条既不在 `routing-plan.json` 里、又是启用状态的条目。原生选择页 `route_selector.py` 的第 252-255 行就是这个检查，Prism 走的是同一份代码。

去官方面板 `http://127.0.0.1:8317/management.html` 把那条停用（加 `excluded-models: ['*']`），或者用配置页把它加成正式来源。

### 「配置已变化，请刷新后再保存」

revision 对不上。你（或者官方面板、或者另一台控制端）在两次操作之间改了配置。刷新页面，重新选，重新保存。并发两个控制端同时保存本来就会打架——别这么用。

### 保存完模型还是不在 Codex 的 `/model` 里

按顺序查：

1. 这个模型在这个来源上勾了吗？没勾就不进目录。
2. 勾了但模型名以 `image` 结尾、或者带 `-expires-on-`？这类被主动过滤掉了，它们跑不了 `/v1/responses`。
3. 重启 Codex 了吗？目录只在客户端启动时读。
4. 还不行就翻 `D:\MY_DESIGN\Agent-router-Pro-Max\codex-model-catalog.json`，看那个 `slug` 在不在。

### 页面白屏

三种情况 Prism 都得撑住，不该白屏：网关没启动、上游全不可达、日志为空。真白屏了，先按 F5，再看 8318 是不是被别的东西应答了（第一条 FAQ）。

### 我该看哪个日志

- `D:\MY_DESIGN\Agent-router-Pro-Max\logs\main.log` —— 网关自己的日志，请求记录、版本号、403 都在这。
- `D:\MY_DESIGN\Agent-router-Pro-Max\logs\error-*` —— 网关的错误日志单。
- `D:\MY_DESIGN\Agent-router-Pro-Max\logs\selector-*.log` —— 老选择页的 stdout/stderr，Prism 不写这里。

---

## 文件在哪

```
D:\MY_DESIGN\Agent-router-Pro-Max\
├─ Prism.exe                  控制台本体（PyInstaller 打包产物，双击运行）
├─ _internal\                 Prism.exe 的运行时依赖，别删也别挪
├─ cli-proxy-api.exe          网关本体
├─ config.yaml                网关配置（密钥在这里）
├─ routing-plan.json          供应商映射，不含密钥
├─ codex-model-catalog.json   Codex 读的模型目录，regenerate 出来的
├─ .local-secrets.json        管理密钥 / 本地 api_key
├─ settings.json              Prism 自己的设置（首次保存时创建，可能不存在）
├─ usage-history.db           采样落库的 SQLite
├─ backups\                   每次改动的快照
├─ logs\
├─ script\route_selector.py   老的命令行选择页，Prism 直接 import 它
└─ app\                       源码（打包产物不需要它，但源码态要用）
    ├─ main.py                窗口 + 托盘 + 子进程 + 单实例 + 自启
    ├─ server.py              8318 HTTP 服务
    ├─ core\                  bridge / sources / sampling / health
    ├─ static\                五个页面的前端
    └─ .venv\                 开发态虚拟环境
```

**出问题怎么恢复：** 网关先 `Pause-Proxy.ps1`，然后用对应那次 `backups\route-switch-*` 里的四份文件覆盖回来，再 `Start-Proxy.ps1`。别只原子覆盖文件然后假定网关热加载了——它不一定。`backups\route-switch-config-*` 是选择器自己建的**只含 config.yaml** 的副本，只有配置坏了、别的文件没动时才用它。客户端回滚别指望 `backups\client-connect-*`——`prune_backups` 只留最近 50 个自动备份，它们早被清掉了；**做任何客户端级改动前先自己备份** `C:\Users\user\.codex\config.toml`。

**别上传** `config.yaml`、`.local-secrets.json` 或任何备份目录里的凭据文件。

---

## 关于本文件

这份说明对应的是 `app\` 下的 Prism 实现。有三条限制写在这里免得日后误判：

- Prism 依赖 `script\route_selector.py` **存在且内容未变**，用的是文件哈希校验，不匹配就报错。想改那个文件，先看 `docs\adr\0006-*.md`。
- 打包（PyInstaller `--onedir`）出的 `Prism.exe` 与 `_internal\` 放在项目根，和 `config.yaml` / `script\` / `auth\` 同级。源码态与打包态都实测过六端点与五个页面。托盘跨线程：pywebview 的 winforms 后端对 `show`/`destroy` 自带 UI 线程封送（`InvokeRequired`→`Invoke`，已核源码），托盘刷新另有 `_tray_lock` 防并发——2026-10-02 起此前的"线程安全没验"不再是悬案，但真机反复点托盘菜单仍未做过专门验收。
- **三个运行时文件都落在项目根（打包态则是 exe 旁边），不在 `app\`**：日志 `prism.log`（`main.py` 的 `LOG_PATH` 取 `bridge.ROOT`）、设置 `settings.json`（`server.py` 的 `SETTINGS_PATH` 取 `bridge.ROOT`）、采样库 `usage-history.db`（`core\sampling.py` 自己有 frozen 分支，`RUN_DIR` 取 exe 所在目录）。源码态与打包态的落点一致，因此两种模式读到的是同一份设置与历史。此前这里写过"打包后写进 `_internal\`"——那个说法已经过期，涉及 `APP_DIR` 的四个文件现在都带 frozen 分支。
