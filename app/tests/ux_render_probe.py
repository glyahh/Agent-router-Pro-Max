# -*- coding: utf-8 -*-
"""headless 渲染探针：测量静态读不出来的两件事。

1. 树线对齐——chevron 三角的水平中心与它引出的子树竖线是否同一条轴；
2. 窄窗溢出——最小窗口（900px）下监控页那张 9 列宽表是否把容器撑出横向滚动。

做法：把 app.css 原样复制到临时目录，用**真实类名**拼最小夹具（三层树 + 9 列表格），
交给系统 Chromium headless 渲染，页面内脚本把 getBoundingClientRect 的结果写进
<pre id="ux-measure">，再 dump-dom 取回 JSON。测的是真布局，不是源码推算。

直接运行可看报告：python app/tests/ux_render_probe.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "static"
APP_CSS = STATIC / "app.css"

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

MIN_WINDOW_W = 900        # main.py 的 min_size=(900,620)
PROBE_TIMEOUT = 35


def browser_path() -> str | None:
    for p in EDGE_CANDIDATES:
        if Path(p).exists():
            return p
    return None


# 与 home.js 的 renderTree 同构：gnode > grow(有 gchev) > gkids > ginner > gtree > gnode...
TREE_FIXTURE = """
<div class="graph">
  <div class="gnode lv1 has-kids open">
    <div class="grow"><span class="gchev"></span><span class="glabel">Codex</span></div>
    <div class="gkids"><div class="ginner"><div class="gtree">
      <div class="gnode lv2 has-kids open">
        <div class="grow"><span class="gchev"></span><span class="glabel">GPT 中转站</span></div>
        <div class="gkids"><div class="ginner"><div class="gtree">
          <div class="gnode lv3"><div class="grow"><span class="gchev"></span><span class="glabel">OpenAI 官方</span></div></div>
          <div class="gnode lv3"><div class="grow"><span class="gchev"></span><span class="glabel">Hub 大号</span></div></div>
        </div></div></div>
      </div>
    </div></div></div>
  </div>
</div>
"""

# 与 monitor.js 的 sourcesSection 同列数：来源/ID/类型/状态/质量/成功/失败/冷却/配额
WIDE_TABLE = """
<div class="tw" id="widewrap">
  <table class="t">
    <thead><tr>
      <th>来源</th><th class="c">ID</th><th class="c">类型</th><th class="c">状态</th><th class="c">质量</th>
      <th class="c">成功</th><th class="c">失败</th><th class="c">冷却</th><th class="c">配额</th>
    </tr></thead>
    <tbody>
    <tr>
      <td class="n">Command Code Goat</td><td class="c mono">goat-deepseek</td><td class="c">OAuth</td>
      <td class="c">启用</td><td class="c">100%</td><td class="c mono">128</td><td class="c mono">3</td>
      <td class="c">—</td><td class="c">—</td>
    </tr>
    <tr>
      <td class="n">SRAPI 备用账号</td><td class="c mono">srapi-deepseek-backup</td><td class="c">API 密钥</td>
      <td class="c">已停用</td><td class="c">92%</td><td class="c mono">1042</td><td class="c mono">17</td>
      <td class="c">2分30秒后重试</td><td class="c">5h 窗口 78%</td>
    </tr>
    </tbody>
  </table>
</div>
"""

PROBE_JS = """
function q(node, sel) { return node.querySelector(':scope > ' + sel); }
function chevronCenter(node) {
  var chev = q(node, '.grow > .gchev');
  if (!chev) return null;
  var cs = getComputedStyle(chev, '::before');
  var r = chev.getBoundingClientRect();
  var left = parseFloat(cs.left) || 0;
  var w = parseFloat(cs.borderLeftWidth) || 0;
  if (!w) return null;
  return r.left + left + w / 2;
}
function guideX(node) {
  var t = q(node, '.gkids > .ginner > .gtree');
  return t ? t.getBoundingClientRect().left : null;
}
function parseRGB(s) {
  var m = String(s).match(/[\\d.]+/g);
  return m ? [Number(m[0]), Number(m[1]), Number(m[2])] : null;
}
function srgb(c) {
  c /= 255;
  return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}
function lum(rgb) { return 0.2126 * srgb(rgb[0]) + 0.7152 * srgb(rgb[1]) + 0.0722 * srgb(rgb[2]); }
function contrast(a, b) {
  var l1 = lum(a), l2 = lum(b);
  var hi = Math.max(l1, l2), lo = Math.min(l1, l2);
  return Math.round(((hi + 0.05) / (lo + 0.05)) * 100) / 100;
}
function run() {
  var res = { chevrons: [], overflow: null, warn_contrast: null };
  var nodes = document.querySelectorAll('.gnode.has-kids');
  for (var i = 0; i < nodes.length; i++) {
    var c = chevronCenter(nodes[i]);
    var g = guideX(nodes[i]);
    var label = q(nodes[i], '.grow > .glabel');
    res.chevrons.push({
      label: label ? label.textContent : '?',
      chevron: c,
      guide: g,
      delta: (c !== null && g !== null) ? Math.round((c - g) * 100) / 100 : null
    });
  }
  var warnEl = document.querySelector('.notice.warn');
  if (warnEl) {
    var wcs = getComputedStyle(warnEl);
    var fg = parseRGB(wcs.color), bg = parseRGB(wcs.backgroundColor);
    res.warn_contrast = {
      color: wcs.color,
      background: wcs.backgroundColor,
      ratio: (fg && bg) ? contrast(fg, bg) : null
    };
  }
  var wrap = document.getElementById('widewrap');
  var table = wrap.querySelector('table');
  res.overflow = {
    container: wrap.clientWidth,
    table: table.scrollWidth,
    overflows_container: table.scrollWidth > wrap.clientWidth,
    doc_scroll: document.documentElement.scrollWidth,
    doc_client: document.documentElement.clientWidth,
    page_hscrolls: document.documentElement.scrollWidth > document.documentElement.clientWidth
  };
  document.getElementById('ux-measure').textContent = JSON.stringify(res);
}
run();
"""

FIXTURE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<link rel="stylesheet" href="app.css">
<style>body{margin:0;padding:0;background:var(--bg)}</style>
</head>
<body>
%(tree)s
<div class="notice warn"><span class="k">!</span><span>网关身份未核实</span></div>
<div id="stage" style="width:780px">
%(table)s
</div>
<pre id="ux-measure">PENDING</pre>
<script>%(js)s</script>
</body></html>
""" % {"tree": TREE_FIXTURE, "table": WIDE_TABLE, "js": PROBE_JS}


def probe() -> dict:
    """跑一次真实渲染，返回 {'chevrons': [...], 'overflow': {...}}。找不到浏览器就抛错。"""
    exe = browser_path()
    if not exe:
        raise RuntimeError("找不到 Edge/Chromium 可执行文件")

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        (tmpdir / "app.css").write_text(APP_CSS.read_text(encoding="utf-8"), encoding="utf-8")
        fixture = tmpdir / "probe.html"
        fixture.write_text(FIXTURE, encoding="utf-8")

        proc = subprocess.run(
            [exe, "--headless=new", "--disable-gpu", "--no-sandbox",
             f"--user-data-dir={tmpdir / 'profile'}",
             f"--window-size={MIN_WINDOW_W},700",
             "--virtual-time-budget=1200",
             "--dump-dom", fixture.as_uri()],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=PROBE_TIMEOUT)

    m = re.search(r'<pre id="ux-measure">(.*?)</pre>', proc.stdout or "", re.S)
    if not m or m.group(1).strip() == "PENDING":
        raise RuntimeError("渲染探针没写出结果，DOM 尾部：%s" % (proc.stdout or "")[-300:])
    return json.loads(m.group(1).strip())


if __name__ == "__main__":
    data = probe()
    print("== 树线对齐 ==")
    for row in data["chevrons"]:
        flag = "OK " if row["delta"] is not None and abs(row["delta"]) <= 0.5 else ">>>"
        print("%s %-12s chevron=%.1f  guide=%.1f  delta=%s"
              % (flag, row["label"], row["chevron"] or -1, row["guide"] or -1, row["delta"]))
    o = data["overflow"]
    print("== 窄窗（最小窗口 %dpx）==" % MIN_WINDOW_W)
    print("    表格容器 %dpx, 表格内容 %dpx, 撑出容器: %s"
          % (o["container"], o["table"], o["overflows_container"]))
    print("    页面 %dpx / 视口 %dpx, 出现横向滚动: %s"
          % (o["doc_scroll"], o["doc_client"], o["page_hscrolls"]))
    w = data.get("warn_contrast")
    if w:
        print("== 浅色 warn 通告 ==")
        print("    %s on %s → 对比度 %.2f:1" % (w["color"], w["background"], w["ratio"] or -1))
    sys.exit(0)
