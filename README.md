<p align="center"><img src="app/design/icons/prism-128.png" width="80" height="80" alt="Prism logo"></p>

# Prism

**One local model gateway. Multiple sources. Your coding agents.**

A Windows desktop console for model sources, routing and coding-agent connections, powered by [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI).

English · [中文说明](#中文说明)

**[Download for Windows](https://github.com/glyahh/Agent-router-Pro-Max/releases/latest)** · [Release notes](https://github.com/glyahh/Agent-router-Pro-Max/releases) · [Report an issue](https://github.com/glyahh/Agent-router-Pro-Max/issues)

## Why Prism?

Using several AI services shouldn't mean editing several files every time you change a model source.

- **Sources and models in one place.** Organize groups, edit sources and choose which models to expose.
- **Identical names, distinct sources.** Give each source an identifier so the same model from two services remains distinguishable in your coding agent.
- **Preview before connecting.** Review configuration changes, then confirm. Connection changes are backed up before writing; disconnect only reverts Prism-managed fields that still match its written values.
- **Gateway control and request visibility.** Start or stop the gateway; check request charts, source status and logs.

Configuration adapters are available for **Codex, Claude Code, opencode and Hermes**. Model compatibility depends on the source protocol and coding-agent version. Copilot connection entries use direct-source configuration rather than the local gateway.

## Get started

**Requirements:** Windows 10/11 x64 and [Microsoft Edge WebView2 Runtime](https://developer.microsoft.com/en-us/microsoft-edge/webview2/).

1. Download the Windows ZIP from [the latest release](https://github.com/glyahh/Agent-router-Pro-Max/releases/latest).
2. Extract the entire ZIP into a writable folder. Keep `Prism.exe`, `_internal/` and `cli-proxy-api.exe` together. Prism automatically creates `.runtime/` on first launch (do not bundle `.runtime/` in release archives, as it stores local credentials and history).
3. Run `Prism.exe`.
4. Add a source using your existing credentials, choose models and save routing.
5. Preview the connection changes for your coding agent, then confirm. Restart the agent if its model list does not refresh.

Prism is a control console, not a model service. You need access to the selected AI service; its usage charges and limits still apply.

## Same model, two sources

Keep one source as the main source and give the other an identifier such as `secondary`:

```text
Main source      GPT-6.1 Sol
Second source    secondary/GPT-6.1 Sol
```

Both can appear in your coding agent's model list. You choose the source explicitly; this is not automatic failover.

## Under the hood

Prism uses Python + WebView2 with native HTML/CSS/JavaScript. It manages routing and connections; **CLIProxyAPI handles inference traffic**, not Prism's console HTTP server. Default local ports: gateway `8317`, console `8318`, optional Cursor inference `8319`. Cursor BYOK uses an independently authenticated ngrok entry; native Cursor verification remains pending.

Credentials and user state are stored in `.runtime/`, including `.runtime/config.yaml`, `.runtime/.local-secrets.json`, `.runtime/auth/` and `.runtime/backups/`. Do not share this folder or credential-bearing backups. Redact credentials, account details and private endpoints in screenshots or logs.

## Feedback & contributions

[Open an issue](https://github.com/glyahh/Agent-router-Pro-Max/issues) with your Prism version, Windows version, coding agent, reproduction steps and expected versus actual behavior. Redacted screenshots and logs help.

Focused fixes are welcome. Discuss larger changes in an issue first.

## License & acknowledgements

Prism is [MIT licensed](LICENSE). The bundled gateway is [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI), an independent upstream project. Third-party components retain their own licenses; see the acknowledgements below.

## 中文说明

Prism 在 Windows 上统一管理模型来源、路由与编码代理端接入。完整解压发行包，保留 `Prism.exe`、`_internal` 和 `cli-proxy-api.exe`，运行 `Prism.exe`，首次启动会自动创建空白的 `.runtime` 目录（切勿在发行包中附带本机 `.runtime` 目录，其中包含本地凭据与历史数据）。随后添加来源、保存路由，再预览并确认代理端接入。

Cursor 首版仅 GPT：选择首页已启用、已暴露的模型；首次配置 ngrok 免费授权与 Cursor 的 URL、专用凭据和 prism-cursor 模型名。后续在 Prism 切换来源、模型和推理等级。真实 Cursor 的 Verify 与工具调用闭环尚未完成验收，不自动购买套餐。

源码入口为 app/main.py，Python 依赖见 app/requirements.lock，打包使用 app/build.bat。运行配置与私有状态在 .runtime/；项目约定见 AGENTS.md；本机开发历程只保存在 .runtime/DEVELOPMENT.md。

# 第三方组件与许可声明

Prism 桌面应用使用了以下第三方开源组件。各条目的许可以**该组件发行物内自带的许可文本为准**，
本节只做汇总指引；版本号与 `app/requirements.lock`（Python 依赖锁）保持一致。

## 随分发包内嵌的 Python 依赖

| 组件 | 版本 | 许可 | 项目 |
|---|---|---|---|
| pywebview | 6.2.1 | BSD-3-Clause | https://pywebview.flowrl.com |
| pystray | 0.19.5 | LGPL-3.0 | https://github.com/moses-palmer/pystray |
| Pillow | 12.3.0 | MIT-CMU（历史部分 HPND） | https://python-pillow.github.io |
| pythonnet | 3.1.0 | MIT | https://pythonnet.github.io |
| clr_loader | 0.3.1 | MIT | https://github.com/pythonnet/clr-loader |
| cffi | 2.1.1 | MIT-0 | https://cffi.readthedocs.io |
| bottle | 0.13.4 | MIT | https://bottlepy.org |
| PyInstaller | 6.22.3 | GPL-2.0-or-later（bootloader 有专用例外条款） | https://pyinstaller.org |
| pyinstaller-hooks-contrib | 2026.7 | GPL-2.0-or-later（构建期 hooks）/ Apache-2.0（运行时钩子，随包分发部分） | https://github.com/pyinstaller/pyinstaller-hooks-contrib |
| altgraph | 0.17.5 | MIT | https://altgraph.readthedocs.io |
| pefile | 2024.8.26 | MIT | https://github.com/erocarrera/pefile |
| pywin32-ctypes | 0.2.3 | BSD-3-Clause | https://github.com/enthought/pywin32-ctypes |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | https://github.com/pypa/packaging |
| pycparser | 3.0 | BSD-3-Clause | https://github.com/eliben/pycparser |
| proxy_tools | 0.1.0 | MIT | https://github.com/jtushman/proxy_tools |
| setuptools | 84.0.0 | MIT | https://github.com/pypa/setuptools |
| six | 1.17.0 | MIT | https://github.com/benjaminp/six |
| typing_extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |

（上表逐包核对自各包 dist-info 的 `METADATA` / `licenses/`，不是凭记忆填的。）

前端（`app/static/`）是纯原生 HTML/CSS/JS，不含第三方运行时依赖；图标为项目自绘。

## 随分发包分发的可执行组件

| 组件 | 许可 | 说明 |
|---|---|---|
| CLIProxyAPI（`cli-proxy-api.exe`） | MIT | 上游网关本体，版权与许可原文见 `LICENSE` 第二段（保持原样收录） |

## 关于 LGPL-3.0（pystray）的交付说明

`pystray` 采用 GNU LGPL-3.0，且以字节码形式内嵌进本分发单元（PyInstaller 打包）。按
LGPL 第 4 条的义务，本分发：

1. 附许可文本：<https://www.gnu.org/licenses/lgpl-3.0.html>（LGPL-3.0 与 GPL-3.0 一并适用）；
2. 声明该组件的**完整源码**可从其项目主页免费获取（链接见上表），不随二进制另行附带；
3. 声明使用者可以按 LGPL 把 pystray 替换为修改版后重新构建本应用：重建方法见
   `app/requirements.lock` 头部注释（`python -m venv .venv` → `pip install -r requirements.lock`
   → `app\build.bat`）。

## PyInstaller bootloader 的例外

PyInstaller 的 bootloader（运行时引导器）按 GPL-2.0 发布、但附带专用例外：以 PyInstaller
打包的**应用本身**不受 GPL 约束。例外原文见
<https://pyinstaller.org/en/stable/license.html>。

## 按需下载的 ngrok

ngrok agent 不内嵌分发，首次授权启用 Cursor 时从 ngrok 官方下载入口获取。当前 Windows amd64 版本锁为 3.39.11；执行前同时核对 SHA256、Windows Authenticode 信任链、发行者和版本输出。稳定下载入口更新导致指纹不匹配时停止，需开发者更新版本锁，不静默接受未知版本。

ngrok 是独立服务，不适用 Prism 的 MIT 许可；使用受 ngrok 的服务条款及免费账号限制约束：<https://ngrok.com/tos>。Prism 不购买域名、不购买套餐、不自动付费；免费授权、配额或固定入口错误停止隧道并显示错误。授权信息不进入命令行、运行配置、日志或 Git。
