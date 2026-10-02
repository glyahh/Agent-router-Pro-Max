# Project: Prism Desktop Native Experience Refactoring

## Architecture
Prism 桌面端采用轻量原生前端架构（Vanilla JS + 原生 CSS + 原生 HTML），由 `app/static/index.html` 作为主容器，引入 `app/static/app.css`（全局样式体系）与 `app/static/app.js`（核心路由、全局 UI 组件库、窗口控制、事件监听与网络轮询）。五个核心视图（`home.js`, `usage.js`, `monitor.js`, `logs.js`, `settings.js`）放置于 `app/static/pages/`，以挂载函数形式注入到 `window.PrismPages`。
本项目全面遵循 **ponytail full 模式**，不引入外部重量级打包工具或依赖，优先复用原生 DOM API 与现有函数，保持极简、健壮、闭环。完整规则（含 caveman full 输出规范）沉淀在 [AGENTS.md](AGENTS.md)，以该文件为准。

### 2026-10-01 交付加固新增的可复用机制
| 机制 | 位置 | 说明 |
|---|---|---|
| 控制台令牌闸 | `server.py` 的 `_check_token` / `create_server(token=)` | 桌面版每次启动随机令牌，`/api/*` 必带 `X-Prism-Token`；静态放行；`python server.py` 调试形态不启用。契约见 `app/INTERFACES.md` |
| 启动体检 | `bridge.verify_startup_files()` | SWITCH_FILES 四件套可解析 + plan 自洽；失败阻塞启动并指向最近 `route-switch` 备份 |
| 首启引导 | `bridge.ensure_first_run_files()` | 缺 `.local-secrets.json`/`config.yaml` 时生成配对密钥；config 一律读磁盘 secrets；绝不覆盖 |
| 原子写 | `bridge._atomic_write_json/_atomic_write_text` | replace 前复查存在，并发抢先时放弃返回 False |
| 保存链渲染探针 | `script/_settings_probe.py` | 真 server 跑在探针进程内 + 服务端断言（CSP 拦跨源回传，见 DEV-RULES B8） |
| 测试面 | `app/tests/` 9 用例 | HTTP 层 / health 聚合 / 启动路径 / 窗口控制与拖拽缩放 API 均已覆盖，`run_all.py` 一条命令（用 `app\.venv` 的 Python 跑） |
| 性能基准 | `app/static/benchmark.html` + `app/tests/run_benchmark.py` | WebView2 真实环境渲染性能基准，`run_benchmark.py` 自动起窗口收集 `__BENCHMARK_RESULTS__` 并输出结构化指标 |

---

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| F1 | 满宽满铺外壳 | 消除 max-width 导致的宽屏两侧透空，自绘控制按钮紧贴物理屏幕右上角 | M1 | ORIGINAL_REQUEST §R1 |
| F2 | 文本选区管控 | 全局禁用界面控件与容器划选，仅代码块、日志与输入框开放选中 | M1 | ORIGINAL_REQUEST §R1 |
| F3 | 右键菜单拦截 | 全局拦截默认 contextmenu，杜绝在空白处与标题栏弹出浏览器菜单 | M1 | ORIGINAL_REQUEST §R1 |
| F4 | 破坏性弹窗安全焦点 | `ui.dialog` 破坏性操作默认聚焦取消按钮，禁用 Enter 键直通执行 | M2 | ORIGINAL_REQUEST §R2 |
| F5 | WAI-ARIA Focus Trap | 模态弹窗启用标准 Tab 键焦点循环，并在关闭后自动还原焦点 | M2 | ORIGINAL_REQUEST §R2 |
| F6 | 设置页脏状态与即时保存 | 补齐 `isDirty()` 协议，防抖即时自动保存，切 Tab 拦截防丢配置 | M3 | ORIGINAL_REQUEST §R3 |
| F7 | 配置输入微状态反馈 | 提供“保存中…”与“已保存”即时指示，增强用户心智确定感 | M3 | ORIGINAL_REQUEST §R3 |
| F8 | 全局桌面生产力快捷键 | 注册 `Ctrl+,`（快速打开设置）与 `Ctrl+1..5`（快速切换 5 个主页面） | M4 | ORIGINAL_REQUEST §R4 |
| F9 | 静默刷新与按键接管 | 拦截 `F5` 与 `Ctrl+R`，屏蔽整页刷新闪烁，就地静默重新加载数据 | M4 | ORIGINAL_REQUEST §R4 |
| F10 | 16×16 微矢量 SVG 图标库 | 全局替换字符图标（'▲','∅','!','◌'），统一微矢量 SVG 渲染规范 | M5 | ORIGINAL_REQUEST §R5 |
| F11 | 视觉质感收敛 (.statebox) | 消除 border: dashed 虚线框，改为 1px hairline 微弱实线与留白风格 | M6 | ORIGINAL_REQUEST §R6 |
| F12 | 跨供应商全局模型搜索 | 首页增加全局模糊过滤与“一键全部展开/全部折叠”卡片控制 | M6 | ORIGINAL_REQUEST §R6 |

---

## Milestones
> **状态列勘误（2026-10-01）**：下面这张表原先全标 `PLANNED`，但**代码早就做完了** ——
> 这份计划写于 09-30 23:30，实现是同晚 23:38–23:51 落地的，计划文件没跟着更新。
> 我逐条回源码核过（每题都给了 `文件:行号`），现改为实况。**M7 是唯一没做完的**
> （只做了 `node --check` 与后端回归，缺一份"每个特性到底成立"的渲染证据）。

| # | Milestone Name | Scope | Dependencies | Status |
|---|----------------|-------|-------------|--------|
| M1 | 桌面原生外壳与交互拦截 (P0) | F1 (满宽外壳), F2 (选区管控), F3 (右键拦截) | none | **DONE** — `app.css:123` `.win{max-width:none}`；`app.css:91-97,154,396` `body{user-select:none}` + 日志/代码/输入框放行；`app.js:1479` 全局 `contextmenu` 拦截 |
| M2 | 模态焦点陷阱与破坏性安全防御 (P0) | F4 (默认安全焦点与Enter防护), F5 (Focus Trap与还原) | none | **DONE** — `app.js:631-693`：`prevFocus` 记录与还原、`danger` 默认聚焦取消、`getFocusableElements()` 做 Tab 循环 |
| M3 | 设置页脏状态与微状态守卫 (P1) | F6 (isDirty与即时保存), F7 (微状态指示) | none | **DONE（结构已验 / 行为未验）** — `app.js:980-1007` 的 `isDirty`/`flushSave` 协议存在且被路由守卫调用；`settings.js` 是实现侧。**行为**（防抖自动保存、"保存中…/已保存"）在夹具里测不了：夹具服务器对 `/api/settings` 只回固定响应、不接受写入，见 `docs/evidence-desktop-shell.txt` 的"没有验证的"第 2 条 |
| M4 | 桌面快捷键与静默刷新接管 (P1) | F8 (Ctrl+, / Ctrl+1..5), F9 (F5/Ctrl+R静默刷新) | M1 | **DONE** — `app.js:1533`（Ctrl+,）、`:1540`（Ctrl+1..5 路由表）、`:1556-1559`（F5 / Ctrl+R 拦截 → `ctx.reload()` 就地刷新）。**已在真实渲染里验过**（Ctrl+2/Ctrl+5/Ctrl+,、F5、Ctrl+R 全 PASS） |
| M5 | 统一微矢量 SVG 图标库体系 (P1) | F10 (替换全部字符图标并规范化 SVG 图标组件) | none | **DONE（结构已验，观感未验）** — `app.js:366-399` 的 `renderIcon` + 命名图标表（empty/warn/bad/error/info/triangle/caret…，16×16 viewBox）；`▲ ∅ ◌` 已从图标位清掉。**残留**（文本语境，非图标位）：`home.js:532` 的 `'✕'`、`:1018` 的 `'✓ 连通'/'✗ 失败'`、`:1060` 的 `'已复制 ✓'`。**"图标看起来怎样"需要看图**，探针只读计算样式与 DOM |
| M6 | 视觉质感优化与全局模型搜索 (P2) | F11 (statebox hairline), F12 (全局搜索与一键折叠) | M5 | **DONE** — `app.css:347` `.statebox{border:1px solid var(--line)}`（虚线已去）；`home.js:98-111,423-448` 全局过滤栏 + `expandAllNodes()`/`collapseAllNodes()`。**均已验**：F11 `border-top-style=solid`；F12 过滤出统计徽章、展开 52→61、折叠 61→0 |
| M7 | 全局集成验收与全套自动化测试回归 | 全量 JS 语法验证 (`node --check`) + 后端自动化测试 (`app/tests/`) | M1-M6 | **DONE** — ①`node --check` 6 个 JS 文件全通过；②后端回归 **5 个用例**全绿（2026-10-01 新增 `test_hardening.py`）；③**新增渲染验收**：`script/_desktop_probe.py` + `docs/evidence-desktop-shell.txt`，**21 条断言全 PASS、JS 报错 0**，覆盖 F1/F2/F3/F4/F5/F8/F9/F11/F12。F6/F7 与 F10 的观感部分**未验**，理由与验法写在证据文件里 |

> **`ORIGINAL_REQUEST` 不在仓库里。** 本文件多处的 `Source: ORIGINAL_REQUEST §R*` 指向一份
> 我看不到的文档，所以上面对各特性的判断只能依据代码行为与 `Feature Inventory` 的转述。
> 若原文对某项另有要求，以原文为准。


---

## Interface Contracts
### 1. 模态对话框接口 (`ui.dialog`)
```javascript
ui.dialog({
  title: '确认删除',
  body: '此操作不可逆，是否继续？',
  okText: '删除',
  cancelText: '取消',
  danger: true, // 关键标识：若为 true，默认聚焦 cancel 按钮，屏蔽 Enter 键直接确认
  onOk: function () { ... },
  onCancel: function () { ... }
})
```
- **焦点控制**：
  - 打开前记录 `var trigger = document.activeElement;`
  - 若 `danger === true`，默认执行 `cancelBtn.focus()`；否则 `okBtn.focus()`。
  - 拦截 `keydown`：若为 `Tab` 键，循环在弹窗内所有可聚焦元素 (`button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])`) 之间。
  - 关闭时执行 `if (trigger && trigger.focus) trigger.focus();`。

### 2. 设置页脏状态与切页守卫 (`settings.js` & `app.js`)
```javascript
// settings.js 导出协议
window.PrismPages.settings = {
  mount: function (el, ctx) { ... },
  isDirty: function () { return hasUnsavedChanges; },
  flushSave: function () { return performImmediateSave(); }
};

// app.js 路由守卫
function navigateTo(targetRoute) {
  var curPage = window.PrismPages[currentRoute];
  if (curPage && curPage.isDirty && curPage.isDirty()) {
    ui.dialog({
      title: '未保存的设置',
      body: '您有尚未保存的配置更改，是否放弃更改并离开？',
      okText: '放弃并离开',
      cancelText: '留在本页',
      danger: true,
      onOk: function () { curPage.resetDirty && curPage.resetDirty(); doRoute(targetRoute); }
    });
    return;
  }
  doRoute(targetRoute);
}
```

### 3. 微矢量 SVG 图标组件 (`ui.icon`)
```javascript
// app.js 导出统一 SVG 工具
ui.icon = function (name, extraClass) {
  // name: 'warn' | 'empty' | 'triangle' | 'spinner'
  // 返回统一 viewBox="0 0 16 16" width="16" height="16" 的 SVG 字符串或 DOM 元素
};
```

### 4. 静默数据刷新机制
```javascript
// 针对 F5 / Ctrl+R 全局拦截
window.addEventListener('keydown', function (e) {
  if (e.key === 'F5' || (e.ctrlKey && e.key.toLowerCase() === 'r')) {
    e.preventDefault();
    var curPage = window.PrismPages[currentRoute];
    if (curPage && typeof curPage.refresh === 'function') {
      curPage.refresh();
    } else {
      ctx.reload();
    }
  }
});
```

---

## Code Layout
- `app/static/index.html`：桌面端主外壳入口与静态容器结构（R1, R5）
- `app/static/app.css`：全局视觉样式、满宽布局、微实线边框、文本选区限制（R1, R6）
- `app/static/app.js`：全局对话框、SVG 微矢量组件、快捷键监听、右键拦截、路由脏状态守卫（R1, R2, R3, R4, R5）
- `app/static/pages/settings.js`：设置页表单、isDirty 协议、即时保存与微状态显示（R3）
- `app/static/pages/home.js`：首页卡片、跨供应商全局模型搜索、一键全部展开/折叠（R6）
- `app/static/pages/logs.js`、`app/static/pages/monitor.js`、`app/static/pages/usage.js`：字符图标替换为 SVG 图标（R5）
- `app/tests/`：后端既有自动化测试套件（严禁破坏）
