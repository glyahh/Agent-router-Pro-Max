# 桌面控制台用 Python + WebView2，不用 Tauri 或 Electron

网关的配置逻辑已经是一份纯标准库的 Python（`script/route_selector.py`，615 行），界面是纯 HTML/CSS/JS。选壳时真正的约束是**能不能把这套东西原样装进去**。

Electron 要拖进 150MB 运行时且和现有 Python 逻辑隔着一层；Tauri 体积最优（约 10MB）但后端要么用 Rust 重写那 615 行，要么把 Python 打成 sidecar——两条路都在给一个"改配置 + 看数据"的工具引入不必要的复杂度。

结论：pywebview 做窗口，复用现有 Python 与 HTML。代价是 exe 约 40–60MB，且依赖系统自带的 WebView2 运行时（本机 153.0.4234.48 已装）。

如果以后要压体积或做移动端，这个决定是要重来的——所以记在这里。
