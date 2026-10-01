# ADR-0009：窗口改成无原生标题栏 + Mica，壳不动（仍是 pywebview）

- 状态：**已采纳**（2026-09-30）
- 相关：ADR-0001（为什么是 pywebview 而不是 Electron）、ADR-0007（浅色 UI）、
  [DEV-RULES.md](../DEV-RULES.md) A8/A9

## 1. 起因

用户提的是「重构 Prism，使用 Electron，赋予其原生流畅的 Windows 应用体验」，
随后把诉求收窄为「窗口边框/标题栏/材质不像现代 Windows 应用，要和最新版 Codex 一致」。

## 2. 先否掉 Electron 化

**理由一：两者同一个渲染引擎，换壳买不到流畅度。**
pywebview 在 Windows 上用系统 WebView2（Chromium），Electron 也是 Chromium。
实测 Prism 进程树里的子进程全是 `msedgewebview2.exe`。HTML/CSS/JS 的渲染、滚动、
动画性能完全一样，前端那 284 KB 代码是原样搬过去的。

**理由二：代价明确且大。**

| | 现在 | Electron 化 |
|---|---|---|
| 安装体积 | 36.5 MB（实测） | ≥ 230 MB（本机 DSH Desktop 为 exe 233 MB + asar 116 MB，整目录 1015 MB） |
| 后端 5598 行 Python | 直接跑 | sidecar（多一层进程、零收益）或重写成 Node |
| `route_selector.py`（用户存量资产，Python） | import 进来用（ADR-0006） | Node 只能 spawn 子进程调 |

重写后端等于把实测过的坑全部重做：`auth_gate()` 的 5×401 封 IP、原子切换 +
`_compensate()` 回滚、`route_selector.py` 指纹锁、`identity.py` 的 ctypes TCP 表。
ADR-0001 就是为这件事写的，没有出现推翻它的新事实。

## 3. 真正的问题在哪（实测，不是猜）

用 ctypes 读运行中 Prism 窗口的真实属性：

| 属性 | 改造前实测 | 结论 |
|---|---|---|
| `USE_IMMERSIVE_DARK_MODE` (20) | **1** | 深色标题栏**早已生效** |
| `SYSTEMBACKDROP_TYPE` (38) | **2** | Mica **早已生效** |
| `WS_CAPTION` | **有** | 原生标题栏还在 |
| `WS_THICKFRAME` | 有 | 可缩放 |

所以「材质没生效」是错的。真正难看的是两件事：

1. **两条横栏叠着**：上面 Win11 原生标题栏（图标 + 标题 + 三个系统按钮），下面紧贴
   Prism 自己的顶栏（`PRI SM` + 5 个 tab + 端口灯）。Windows 7 时代那种结构。
2. **Mica 被自己的白底盖死**：`--bg:#FFFFFF` 铺满整窗，`SYSTEMBACKDROP_TYPE=2`
   等于白设。做法照 Codex 皮肤的 `@michengai/dsh-codex-ui`（`lib/client.js:9500-9510`）：
   容器透明放行 Mica，导航半透明，主内容不透明。

## 4. 决策

**不换壳。** 在 pywebview 里做「`titleBarStyle:'hidden'` 的等价物」：

1. `main.py` 的 `hide_native_titlebar()`：只摘 `WS_CAPTION`，
   **保留** `WS_THICKFRAME|WS_SYSMENU|WS_MINIMIZEBOX|WS_MAXIMIZEBOX` + `SWP_FRAMECHANGED`。
   边缘缩放与 Snap 因此不丢。
2. 补 `DWMWA_WINDOW_CORNER_PREFERENCE = 2`：摘框架后系统不再画圆角，实测是直角。
3. 前端自绘最小化/最大化/关闭三个按钮（pywebview 没有 `titleBarOverlay`），
   接 `js_api` 的 `minimize/is_maximized/toggle_maximize/close`。
   最大化状态用 `IsZoomed` 问系统，不自己记 —— 双击顶栏、贴边、Win+↑ 都会改它。
4. 拖动：`.top` 上挂 `.pywebview-drag-region`，并把
   `DRAG_REGION_DIRECT_TARGET_ONLY` 设成 `True`（另一个模式会让点按钮也拖窗口）。
5. 双击顶栏空白 = 最大化/还原（原生标题栏没了，这个系统行为要自己补）。
6. Mica 放行：`html/body/.win` 透明、顶栏与状态栏 `rgba(255,255,255,.72)` + `blur(30px)`、
   `main` 不透明。

## 5. 后果

**好的**：外观与 Codex 一致（单条顶栏、无系统标题栏、Mica、圆角）；体积不变；
后端与刚做完的多来源功能一行没动；缩放/贴边/托盘退出等系统行为保留。

**代价与已知取舍**：

- **Mica 颜色跟随系统主题，Prism 是浅色 UI（ADR-0007）。** 系统深色时透上来的是深色
  Mica，浅色半透明层盖上去会偏灰。所以顶栏透明度压得克制（.72）。
  要不要给 Prism 补一套跟随系统的深色主题，**留给用户决定**，见 HANDOFF。
- 摘掉 `WS_CAPTION` 后，`WM_NCCALCSIZE` 相关的最大化溢出问题**本轮没实测**。
  缩放与最大化待用户实机确认。
- 自绘按钮的**悬停红**（关闭键）是全站唯一用饱和红的地方，属系统级约定，写在 A9 附近。

## 6. 被否决的方案

- **`frameless=True`**：等于 `frame:false`，连 `WS_THICKFRAME` 一起丢，缩放与 Snap 全没。
- **自绘 `WM_NCHITTEST` 子类化**：能做，但 Python 回调跨 Win32 边界要保证引用不被 GC，
  风险高于收益 —— 保留 `WS_THICKFRAME` 已经拿到同样的边缘缩放。
- **Electron 化**：见第 2 节。
- **完全照搬 Codex 的 `titleBarOverlay`**：那是 Chromium 自绘窗口按钮的配套设施，
  pywebview 没有对应能力，只能自己画按钮。
