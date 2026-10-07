r"""双主题渲染证据采集（一次性工具，不进打包产物，也不属于 run_all 的回归）。

做法照 docs/evidence-home-tree-render.txt 建立的口径：
  * 夹具服务器端点 app/static 的**原文件**（复制到临时目录，仓库文件一个不动）；
  * /api/* 用从我起的 8318 上抓下来的**真实响应**回放，形状因此必然对得上；
  * CSP 与 server.py 逐字一致 —— 内联 <script> 同样会被拦，所以探针走外部文件
    （DEV-RULES B4）；
  * 探针由**页面自己**读 getComputedStyle 并 POST 回来，拿到的是渲染后的真实值
    （DEV-RULES A6 / B1）。

两个必须注意的量法坑（都踩过，已固化在这里）：
  * **切完主题要立刻读**会拿到过渡起点：.btn / .tabs a 带 transition:background，
    不禁用过渡就会得出"浅色下按钮白字白底"这种假结论。所以量之前注入
    `*{transition:none!important}`。
  * 对比度**只按令牌算**，不猜祖先链 —— 组件一律引用令牌，这是权威口径。
    半透明令牌（--topbg / --scrim）不参与：它们的实际颜色取决于背后的 Mica。

用法：python script/_theme_probe.py
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / 'app' / 'static'
PORT = 18418
LIVE = 'http://127.0.0.1:8318'          # 源码态起的真控制台（夹具就从这里抓）
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
ENDPOINTS = ['state', 'sources', 'usage', 'monitor', 'logs', 'error-logs', 'settings', 'agents']
PAGES = ['home', 'usage', 'monitor', 'logs', 'settings']
PLACEHOLDER = b'data-theme="light"'

PROBE_JS = r"""
/* 主题探针（外部文件；内联会被 CSP 静默拦掉）。 */
(function () {
  var errs = [];
  window.addEventListener('error', function (e) { errs.push(String(e.message || e)); });
  window.addEventListener('unhandledrejection', function (e) {
    errs.push('unhandledrejection ' + String((e.reason && e.reason.message) || e.reason));
  });

  var SEL = {
    html: 'html', body: 'body', win: '.win', top: '.top', brand: '.brand', kbd: '.kbd',
    wbtn: '.wbtn', view: '.view', statusbar: '.statusbar',
    card: '.card', btn: '.btn', btnPri: '.btn.pri', notice: '.notice', table: 'table.t',
    th: 'table.t th', td: 'table.t td', tag: '.tag', logview: '.logview',
    statebox: '.statebox', row: '.modelrow', psrow: '.ps-row',
    input: 'input:not([type=checkbox])', select: 'select'
  };

  /* 量之前关掉过渡：.btn / .tabs a 带 transition:background .1x s，切完主题立刻读
     getComputedStyle 拿到的是**过渡起点**（上一套主题的颜色）。CSP 的 style-src
     有 unsafe-inline，注入 <style> 是允许的。 */
  function freezeTransitions() {
    var s = document.createElement('style');
    s.textContent = '*,*::before,*::after{transition:none!important;animation:none!important}';
    (document.head || document.documentElement).appendChild(s);
  }

  function stylesOf() {
    var out = {};
    Object.keys(SEL).forEach(function (k) {
      var el = document.querySelector(SEL[k]);
      if (!el) { out[k] = null; return; }
      var cs = getComputedStyle(el);
      out[k] = { bg: cs.backgroundColor, color: cs.color, border: cs.borderTopColor };
    });
    return out;
  }

  /* DEV-RULES B1：找"被撑变形"的元素。真实故障是复选框被 width:100% 撑到 604px，
     所以判据取 >60px。注意**不要**用 35px 当线：设置页的开关本身就是
     `.ps-sw input{width:36px}` 的药丸形复选框，那是设计值不是变形（实测 36px ×3，
     全是开关）。这里连宽度分布一起报出来，看的人能自己判断。 */
  function deformations() {
    var bad = [], widths = [];
    Array.prototype.forEach.call(document.querySelectorAll('input[type=checkbox]'), function (el) {
      var w = parseFloat(getComputedStyle(el).width);
      widths.push(Math.round(w));
      if (w > 60) bad.push('checkbox 被撑宽 ' + Math.round(w) + 'px');
    });
    Array.prototype.forEach.call(document.querySelectorAll('button'), function (el) {
      var h = parseFloat(getComputedStyle(el).height);
      if (h > 60) bad.push('button 被撑高 ' + Math.round(h) + 'px class=' + el.className);
    });
    return { bad: bad, checkboxWidths: widths };
  }

  /* 文本不该超出所在卡片的右边界 */
  function overflows() {
    var bad = [];
    Array.prototype.forEach.call(document.querySelectorAll('.card'), function (card) {
      var cr = card.getBoundingClientRect();
      if (!cr.width) return;
      Array.prototype.forEach.call(card.querySelectorAll('span, b, .t, .d, td'), function (t) {
        var r = t.getBoundingClientRect();
        if (r.width > 4 && r.right > cr.right + 1) {
          bad.push((t.className || t.tagName) + ' right=' + Math.round(r.right)
                   + ' > card ' + Math.round(cr.right));
        }
      });
    });
    return bad.slice(0, 6);
  }

  function lum(color) {
    var s = String(color || '').trim(), m, r, g, b, a = 1;
    if ((m = /^#([0-9a-f]{3})$/i.exec(s))) {
      r = parseInt(m[1][0] + m[1][0], 16); g = parseInt(m[1][1] + m[1][1], 16);
      b = parseInt(m[1][2] + m[1][2], 16);
    } else if ((m = /^#([0-9a-f]{6})$/i.exec(s))) {
      r = parseInt(m[1].slice(0, 2), 16); g = parseInt(m[1].slice(2, 4), 16);
      b = parseInt(m[1].slice(4, 6), 16);
    } else if ((m = /^rgba?\(([^)]+)\)$/i.exec(s))) {
      var parts = m[1].split(',').map(function (x) { return x.trim(); });
      r = parseFloat(parts[0]); g = parseFloat(parts[1]); b = parseFloat(parts[2]);
      if (parts.length > 3) a = parseFloat(parts[3]);
    } else { return null; }
    if (a < 1) return null;      /* 半透明层不参与：实际颜色取决于背后的 Mica */
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
  }

  function ratio(fg, bg) {
    var lf = lum(fg), lb = lum(bg);
    if (lf === null || lb === null) return null;
    var hi = Math.max(lf, lb), lo = Math.min(lf, lb);
    return +((hi + 0.05) / (lo + 0.05)).toFixed(2);
  }

  var TOKENS = ['--bg', '--panel', '--panel2', '--panel3', '--fg', '--fg2', '--fg3',
                '--line', '--line2', '--ok', '--okbg', '--bad', '--badbg',
                '--warn', '--warnbg', '--accent', '--onaccent', '--hl',
                '--topbg', '--scroll', '--scrim', '--knob-shadow'];

  function tokensOf() {
    var cs = getComputedStyle(document.documentElement), out = {};
    TOKENS.forEach(function (n) { out[n] = cs.getPropertyValue(n).trim(); });
    return out;
  }

  function contrastOf(tk) {
    var pairs = [['--fg', '--bg'], ['--fg', '--panel'], ['--fg2', '--bg'], ['--fg3', '--bg'],
                 ['--fg', '--panel2'], ['--fg', '--panel3'],
                 ['--ok', '--okbg'], ['--bad', '--badbg'], ['--warn', '--warnbg'],
                 ['--onaccent', '--accent'], ['--fg', '--hl']];
    var out = {};
    pairs.forEach(function (p) {
      var r = ratio(tk[p[0]], tk[p[1]]);
      if (r !== null) out[p[0] + ' on ' + p[1]] = r;
    });
    return out;
  }

  function measure() {
    return {
      theme: document.documentElement.getAttribute('data-theme'),
      styles: stylesOf(),
      tokens: tokensOf(),
      deform: deformations(),
      overflow: overflows()
    };
  }

  function finish(payload) {
    return fetch('/__probe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
  }

  var PAGES = ['home', 'usage', 'monitor', 'logs', 'settings'];

  function run() {
    freezeTransitions();
    var served = document.documentElement.getAttribute('data-theme');
    var pages = [], i = 0;

    function next() {
      if (i >= PAGES.length) {
        finish({ served: served, pages: pages, errors: errs });
        return;
      }
      var name = PAGES[i++];
      location.hash = '#/' + name;
      setTimeout(function () {
        var light, dark;
        document.documentElement.setAttribute('data-theme', 'light');
        light = measure();
        document.documentElement.setAttribute('data-theme', 'dark');
        dark = measure();
        document.documentElement.setAttribute('data-theme', served);
        pages.push({
          page: name, light: light, dark: dark,
          contrast: { light: contrastOf(light.tokens), dark: contrastOf(dark.tokens) }
        });
        next();
      }, 1100);
    }
    next();
  }

  var n = 0;
  var iv = setInterval(function () {
    n++;
    var view = document.getElementById('view');
    if ((view && view.children.length > 1) || n > 24) {
      clearInterval(iv);
      setTimeout(run, 900);
    }
  }, 250);
})();
"""


def is_dark() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as k:
            return not int(winreg.QueryValueEx(k, 'AppsUseLightTheme')[0])
    except Exception:                              # noqa: BLE001
        return False


def fetch_fixtures(into: pathlib.Path) -> None:
    into.mkdir(parents=True, exist_ok=True)
    for name in ENDPOINTS:
        req = urllib.request.Request('%s/api/%s' % (LIVE, name),
                                     headers={'Host': '127.0.0.1:8318'})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read()
            json.loads(body)
            (into / (name + '.json')).write_bytes(body)
            print('  夹具 %-11s %6d 字节' % (name, len(body)))
        except Exception as exc:                   # noqa: BLE001
            print('  夹具 %-11s 失败：%s' % (name, exc))


class Fixture(SimpleHTTPRequestHandler):
    collected: list = []

    def log_message(self, *a):
        pass

    def _send(self, status, raw, ctype):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', CSP)
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        if self.path != '/__probe':
            self._send(404, b'{}', 'application/json')
            return
        raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        Fixture.collected.append(json.loads(raw.decode('utf-8')))
        self._send(200, b'{"ok":true}', 'application/json')

    def do_GET(self):
        path = self.path.split('?')[0]
        if path.startswith('/api/'):
            f = pathlib.Path(self.directory) / '_fixtures' / (path[len('/api/'):] + '.json')
            if not f.exists():
                self._send(404, b'{"ok":false,"error":"no fixture"}', 'application/json')
                return
            self._send(200, f.read_bytes(), 'application/json; charset=utf-8')
            return
        target = pathlib.Path(self.directory) / (path.lstrip('/') or 'index.html')
        if target.is_dir():
            target = target / 'index.html'
        if not target.exists():
            self._send(404, b'not found', 'text/plain')
            return
        raw = target.read_bytes()
        if target.name == 'index.html':
            # 与 server.py 的 _serve_static 同一件事：按系统主题替换占位。
            raw = raw.replace(PLACEHOLDER,
                              ('data-theme="%s"' % ('dark' if is_dark() else 'light')).encode())
        ctype = {'.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
                 '.js': 'application/javascript; charset=utf-8',
                 '.svg': 'image/svg+xml'}.get(target.suffix, 'application/octet-stream')
        self._send(200, raw, ctype)


def find_browser():
    for p in (r'C:\Program Files\Google\Chrome\Application\chrome.exe',
              r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'):
        if pathlib.Path(p).exists():
            return p
    return None


SHOW = ['top', 'view', 'card', 'btn', 'btnPri', 'table', 'th', 'notice', 'logview',
        'statebox', 'tag', 'row', 'psrow']


def main() -> int:
    work = pathlib.Path(tempfile.mkdtemp(prefix='prism-theme-probe-'))
    static = work / 'static'
    shutil.copytree(SRC, static)
    (static / 'probe.js').write_text(PROBE_JS, encoding='utf-8')
    idx = static / 'index.html'
    html = idx.read_text(encoding='utf-8')
    assert '</body>' in html, 'index.html 结构变了，探针注入点失效'
    idx.write_text(html.replace('</body>', '<script src="probe.js"></script>\n</body>'),
                   encoding='utf-8')

    print('夹具根目录:', work)
    print('从 %s 抓真实响应当夹具：' % LIVE)
    fetch_fixtures(static / '_fixtures')

    srv = ThreadingHTTPServer(('127.0.0.1', PORT), partial(Fixture, directory=str(static)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.5)

    browser = find_browser()
    if not browser:
        print('找不到 Chrome/Edge，无法采集渲染证据。')
        return 3
    cmd = [browser, '--headless=new', '--disable-gpu', '--no-first-run',
           '--no-default-browser-check', '--disable-extensions',
           '--user-data-dir=' + str(work / 'profile'),
           '--virtual-time-budget=60000', '--window-size=1280,900',
           'http://127.0.0.1:%d/' % PORT]
    print('启动:', pathlib.Path(browser).name)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    for _ in range(80):
        if Fixture.collected:
            break
        time.sleep(0.25)
    srv.shutdown()

    if not Fixture.collected:
        print('探针没有回传结果。浏览器 stderr 末尾：')
        print((proc.stderr or '')[-1500:])
        return 4

    data = Fixture.collected[0]
    (work / 'probe.json').write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                     encoding='utf-8')
    print()
    print('页面拿到的 data-theme =', data['served'], ' JS 报错 =', data['errors'] or '无')
    for p in data['pages']:
        print()
        print('== %s ==' % p['page'])
        print('  变形/越界: %s / %s' % (p['light']['deform']['bad'] or '无',
                                         p['light']['overflow'] or '无'))
        print('  复选框宽度: %s' % (p['light']['deform']['checkboxWidths'] or '无'))
        for k in SHOW:
            a, b = p['light']['styles'].get(k), p['dark']['styles'].get(k)
            if not a and not b:
                continue
            print('  %-9s 浅 bg=%-22s 深 bg=%-22s' % (
                k, (a or {}).get('bg'), (b or {}).get('bg')))
        if p['page'] == 'settings':
            print('  令牌(浅):', json.dumps(p['light']['tokens'], ensure_ascii=False))
            print('  令牌(深):', json.dumps(p['dark']['tokens'], ensure_ascii=False))
            print('  对比度(浅):', json.dumps(p['contrast']['light'], ensure_ascii=False))
            print('  对比度(深):', json.dumps(p['contrast']['dark'], ensure_ascii=False))
    print()
    print('（结果 JSON 在 %s）' % (work / 'probe.json'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
