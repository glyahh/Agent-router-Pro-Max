# 第三方组件与许可声明

Prism 桌面应用使用了以下第三方开源组件。各条目的许可以**该组件发行物内自带的许可文本为准**，
本文件只做汇总指引；版本号与 `requirements.lock`（Python 依赖锁）保持一致。

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
   `requirements.lock` 头部注释（`python -m venv .venv` → `pip install -r requirements.lock`
   → `app\build.bat`）。

## PyInstaller bootloader 的例外

PyInstaller 的 bootloader（运行时引导器）按 GPL-2.0 发布、但附带专用例外：以 PyInstaller
打包的**应用本身**不受 GPL 约束。例外原文见
<https://pyinstaller.org/en/stable/license.html>。
