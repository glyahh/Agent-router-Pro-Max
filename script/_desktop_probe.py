r"""前端验收探针：把 PROJECT.md 的 F1~F12 逐条在**真实渲染**里验一遍。

为什么要有它：DEV-RULES **B1** 写着"文件改了 / 编译过了 / 接口 200"不是 UI 的验证。
PROJECT.md 的 M7 要求"全局集成验收"，而本计划的那批改动（09-30 23:38–23:51）落地时
**没有留下任何渲染证据**，只有 `node --check` 与后端回归 —— 那两样都证明不了
"右键真的被拦住了""Tab 真的在弹窗内循环""Ctrl+2 真的切了页"。

做法沿用 docs/evidence-home-tree-render.txt 建立的口径：
  * 夹具服务器端点 app/static 的**原文件**（复制到临时目录，仓库文件一个不动）；
  * /api/* 用从源码态 8318 上抓下来的**真实响应**回放；
  * CSP 与 server.py 逐字一致 → 内联 <script> 会被拦，所以探针走外部文件（B4）；
  * 断言由**页面自己**做：派发真实事件、读 getComputedStyle 与 document.activeElement，
    再把结果 POST 回来。

一次性工具，**不在 run_all 里**（它要 Chrome 与一个在跑的控制台，进回归会变成 HI-02
那种"测试绑本机真实服务"）。产物写 docs/evidence-desktop-shell.txt。

用法：先在源码态起控制台，再跑它：
    python app/server.py --port 8318
    python script/_desktop_probe.py
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
PORT = 18419
LIVE = 'http://127.0.0.1:8318'
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
ENDPOINTS = ['state', 'sources', 'usage', 'monitor', 'logs', 'error-logs', 'settings', 'agents']
PLACEHOLDER = b'data-theme="light"'

PROBE_JS = r"""
/* 桌面外壳验收探针（外部文件；内联会被 CSP 拦掉）。 */
(function () {
  var out = [], errs = [];
  window.addEventListener('error', function (e) { errs.push(String(e.message || e)); });
  window.addEventListener('unhandledrejection', function (e) {
    errs.push('unhandledrejection ' + String((e.reason && e.reason.message) || e.reason));
  });

  function rec(id, name, ok, detail) {
    out.push({ id: id, name: name, ok: !!ok, detail: String(detail == null ? '' : detail) });
  }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function css(sel, prop) {
    var el = document.querySelector(sel);
    return el ? getComputedStyle(el)[prop] : null;
  }
  function textOf(el) { return el ? String(el.textContent || '').trim() : ''; }

  function go(hash) {
    location.hash = hash;
    return sleep(700);
  }

  function key(target, init) {
    var e = new KeyboardEvent('keydown', Object.assign({ bubbles: true, cancelable: true }, init));
    (target || document).dispatchEvent(e);
    return e;
  }

  /* ── F4/F5：两个**独立**弹窗分头测 —— 不能共用一个：
         Enter 那一步（正确行为）会把弹窗关掉（焦点在「取消」上，Enter=取消），
         在前一个已销毁的弹窗上测 Tab 循环，focus() 全部落空，会得出假失败。 ── */

  function openDialog(opts) {
    var api = window.Prism && window.Prism.ui;
    if (!api || typeof api.dialog !== 'function') return null;
    api.dialog(opts);
    return document.querySelector('#modal .dlg');
  }

  function dlgButtons(dlg) {
    return Array.prototype.slice.call(dlg.querySelectorAll('button'));
  }

  async function testDangerDialog(trigger) {
    var okFired = false;
    var dlg = openDialog({
      title: '探针A', body: '危险操作', danger: true, okText: '确定删除', cancelText: '取消',
      onOk: function () { okFired = true; }
    });
    await sleep(140);
    if (!dlg) { rec('F4a', '对话框已打开', false, '#modal .dlg 找不到'); return; }
    rec('F4a', '对话框已打开', true, '');

    var btns = dlgButtons(dlg);
    rec('F4b', 'danger=true 默认聚焦取消按钮',
        document.activeElement === btns[0] && /取消/.test(textOf(btns[0])),
        'activeElement=' + textOf(document.activeElement));

    var ev = key(document.activeElement, { key: 'Enter' });
    await sleep(100);
    rec('F4c', 'Enter 不会执行破坏性操作（onOk 未被调用）', !okFired,
        'defaultPrevented=' + ev.defaultPrevented + ' onOkFired=' + okFired);

    // 兜底关掉弹窗，免得它挡住下一个用例（正常路径下 Enter 已经把它关了）
    var still = document.querySelector('#modal');
    if (still) {
      var c = still.querySelector('.dlg button');
      if (c) c.click();
      else still.remove();
      await sleep(160);
    }
    rec('F4d', '弹窗已关闭（Enter 在取消上按 = 取消）', !document.querySelector('#modal'), '');
    return trigger;
  }

  async function testFocusTrap(trigger) {
    var dlg = openDialog({
      title: '探针B', body: '焦点陷阱', okText: '确定', cancelText: '取消'
    });
    await sleep(140);
    if (!dlg) { rec('F5a', 'Tab 焦点循环', false, '第二个弹窗没打开'); return; }
    var btns = dlgButtons(dlg);
    if (btns.length < 2) { rec('F5a', 'Tab 焦点循环', false, '可聚焦按钮不足 2 个'); return; }
    var last = btns[btns.length - 1], first = btns[0];
    last.focus();
    var ev = key(last, { key: 'Tab' });
    await sleep(80);
    // 断言的是**应用自己的处理逻辑**：合成事件浏览器不会做原生焦点移动，
    // 所以这条只有在 onKey 主动 first.focus() 时才会通过（默认行为帮不上忙）。
    rec('F5a', '焦点在末尾按 Tab 绕回开头（由 onKey 主动移动）',
        ev.defaultPrevented && document.activeElement === first,
        'defaultPrevented=' + ev.defaultPrevented
        + ' activeElement=' + textOf(document.activeElement));
    first.click();                     // 取消关闭
    await sleep(160);
    rec('F5b', '关闭后焦点还原到打开前的元素', document.activeElement === trigger,
        'activeElement=' + (document.activeElement && document.activeElement.className));
  }

  async function run() {
    freeze();

    /* F1 满宽满铺 */
    var win = document.querySelector('.win');
    var w = win ? win.getBoundingClientRect().width : 0;
    rec('F1', '外壳满宽（无 max-width 限宽）',
        css('.win', 'maxWidth') === 'none' && Math.abs(w - window.innerWidth) <= 2,
        'maxWidth=' + css('.win', 'maxWidth') + ' winWidth=' + Math.round(w)
        + ' innerWidth=' + window.innerWidth);

    /* F2 选区管控 */
    rec('F2a', '界面容器默认不可划选（body user-select:none）',
        css('body', 'userSelect') === 'none', 'body user-select=' + css('body', 'userSelect'));
    await go('#/logs');
    rec('F2b', '日志区允许划选（user-select:text）',
        css('.logview', 'userSelect') === 'text', '.logview user-select=' + css('.logview', 'userSelect'));

    /* F3 右键菜单拦截 */
    var ce = new MouseEvent('contextmenu', { bubbles: true, cancelable: true });
    document.body.dispatchEvent(ce);
    rec('F3', '默认右键菜单被拦截（defaultPrevented）', ce.defaultPrevented,
        'defaultPrevented=' + ce.defaultPrevented);

    /* F11 statebox 实线：**先跳到会渲染空状态的页**再查。
       （原先没跳转就查，量到的是上一页，得出假失败。） */
    await go('#/usage');
    await sleep(400);
    if (document.querySelector('.statebox')) {
      rec('F11', '.statebox 用 1px 实线（不再 dashed）',
          css('.statebox', 'borderTopStyle') === 'solid',
          'border-top-style=' + css('.statebox', 'borderTopStyle'));
    } else {
      rec('F11', '.statebox 用 1px 实线（不再 dashed）', false,
          '本页没有渲染出 .statebox（有数据时它是空的），本轮未验到');
    }

    /* F8 快捷键 */
    await go('#/home');
    key(document, { key: '2', ctrlKey: true });
    await sleep(400);
    rec('F8a', 'Ctrl+2 切到用量页', location.hash === '#/usage', 'hash=' + location.hash);
    key(document, { key: '5', ctrlKey: true });
    await sleep(400);
    rec('F8b', 'Ctrl+5 切到设置页', location.hash === '#/settings', 'hash=' + location.hash);
    key(document, { key: ',', ctrlKey: true });
    await sleep(400);
    rec('F8c', 'Ctrl+, 打开设置页', location.hash === '#/settings', 'hash=' + location.hash);

    /* F9 F5 / Ctrl+R 静默刷新 */
    var before = location.hash;
    var f5 = key(document, { key: 'F5' });
    await sleep(400);
    rec('F9a', 'F5 被拦截且不发生整页刷新', f5.defaultPrevented && location.hash === before,
        'defaultPrevented=' + f5.defaultPrevented + ' hash=' + location.hash);
    var cr = key(document, { key: 'r', ctrlKey: true });
    await sleep(300);
    rec('F9b', 'Ctrl+R 被拦截', cr.defaultPrevented, 'defaultPrevented=' + cr.defaultPrevented);

    /* F12 全局搜索 + 一键展开/折叠 */
    await go('#/home');
    await sleep(500);
    var inp = document.querySelector('.home-global-filter');
    rec('F12a', '首页有全局搜索输入框', !!inp, inp ? 'placeholder=' + inp.getAttribute('placeholder') : '找不到');
    if (inp) {
      var vis0 = Array.prototype.slice.call(document.querySelectorAll('.lv4'))
        .filter(function (e) { return getComputedStyle(e).visibility !== 'hidden'; }).length;
      inp.value = 'gpt';
      inp.dispatchEvent(new Event('input', { bubbles: true }));
      await sleep(400);
      var badge = document.querySelector('.home-filter-badge');
      var badgeShown = badge && getComputedStyle(badge).display !== 'none';
      rec('F12b', '输入后出现匹配统计徽章', !!badgeShown,
          badge ? 'text=' + textOf(badge) + ' display=' + getComputedStyle(badge).display : '徽章不存在');
      var expandBtn = Array.prototype.slice.call(document.querySelectorAll('.home-tool-btn'))
        .filter(function (b) { return /展开/.test(textOf(b)); })[0];
      var collapseBtn = Array.prototype.slice.call(document.querySelectorAll('.home-tool-btn'))
        .filter(function (b) { return /折叠/.test(textOf(b)); })[0];
      rec('F12c', '有「全部展开 / 全部折叠」按钮', !!expandBtn && !!collapseBtn,
          'expand=' + !!expandBtn + ' collapse=' + !!collapseBtn);
      if (expandBtn && collapseBtn) {
        // 先清掉过滤，否则只剩匹配项，展开数会被过滤影响
        inp.value = '';
        inp.dispatchEvent(new Event('input', { bubbles: true }));
        await sleep(400);
        var visBefore = visibleLv4();
        expandBtn.click();
        await sleep(400);
        var visAfterExpand = visibleLv4();
        collapseBtn.click();
        await sleep(400);
        var visAfterCollapse = visibleLv4();
        rec('F12d', '全部展开让可见模型行变多', visAfterExpand > visBefore,
            visBefore + ' -> ' + visAfterExpand);
        rec('F12e', '全部折叠让可见模型行变少', visAfterCollapse < visAfterExpand,
            visAfterExpand + ' -> ' + visAfterCollapse);
      }
      void vis0;
    }

    /* NEW-01 / NEW-02：页面自带 h() 必须支持 data{} / style{} 对象
       （少了分支不报错，只会静默变成 "[object Object]"） */
    await go('#/home');
    await sleep(500);
    rec('NEW-01a', 'data:{} 写进了 dataset（能按 [data-role] 查到）',
        !!document.querySelector('[data-role]'),
        '[data-role] 命中 ' + document.querySelectorAll('[data-role]').length + ' 个');
    rec('NEW-01b', 'data:{} 没有退化成 data="[object Object]"',
        !document.querySelector('[data="[object Object]"]'),
        '命中 ' + document.querySelectorAll('[data="[object Object]"]').length + ' 个');
    var _badge = document.querySelector('.home-filter-badge');
    var _clear = document.querySelector('.home-clear-btn');
    rec('NEW-02', 'style:{} 生效：筛选徽章与清空按钮初始就是隐藏的',
        !!(_badge && _clear) && getComputedStyle(_badge).display === 'none'
            && getComputedStyle(_clear).display === 'none',
        'badge=' + (_badge ? getComputedStyle(_badge).display : 'n/a')
        + ' clear=' + (_clear ? getComputedStyle(_clear).display : 'n/a'));

    /* N3-03 / N3-04：脏页面上 **hash 变更**与 **F5** 都不能静默丢弃未保存的改动。
       "有没有未保存改动"用首页自带的**稳定可观察标志**判定：.home-sticky-bar 的 dirty 类
       与徽章文案（"配置已同步" ↔ "待保存：N 处改动"）。
       不用勾选框的 checked —— 实测它不反映状态（视觉走类名），按它断言会得出假失败。 */
    await go('#/home');
    await sleep(700);

    function dirtyState() {
      var bar = document.querySelector('.home-sticky-bar');
      var badge = document.querySelector('[data-role="sb-badge"]');
      return {
        cls: bar ? (bar.className || '') : '',
        text: badge ? textOf(badge) : '',
        dirty: !!(bar && /(^|\s)dirty(\s|$)/.test(bar.className || ''))
      };
    }

    var _rows = document.querySelectorAll('.graph [data-key^="s:"]');
    var dirtyBox = null;
    for (var ri = 0; ri < _rows.length; ri++) {
      var cb = _rows[ri].querySelector('input[type=checkbox]');
      if (cb && !cb.disabled) { dirtyBox = cb; break; }
    }
    var _before = dirtyState();
    if (!dirtyBox) {
      rec('N3-04', 'F5 在脏页面上不丢改动', false, '找不到可勾选的来源行，本轮未验到');
      rec('N3-03', '外部 hash 变更不绕过脏守卫', false, '同上');
    } else {
      dirtyBox.click();
      await sleep(400);
      var _dirty1 = dirtyState();
      rec('N3-04a', '点击制造出"有未保存改动"状态（sticky 条变 dirty）',
          !_before.dirty && _dirty1.dirty, '前="' + _before.text + '" 后="' + _dirty1.text + '"');

      key(document, { key: 'F5' });
      await sleep(700);
      var _afterF5 = dirtyState();
      var _toasts = Array.prototype.slice.call(document.querySelectorAll('.toast'))
        .map(function (t) { return textOf(t); }).join(' | ');
      rec('N3-04', 'F5 不就地刷新、给出提示，且未保存的改动还在',
          /未保存/.test(_toasts) && _afterF5.dirty,
          'toast=' + (_toasts || '（无）') + ' sticky="' + _afterF5.text + '"');

      location.hash = '#/usage';            // 等同后退/前进触发的那次 hashchange
      await sleep(700);
      var _dlg = document.querySelector('#modal .dlg');
      rec('N3-03', '外部 hash 变更在脏页面上弹确认（不再静默跳走丢改动）',
          !!_dlg && /未保存/.test(textOf(_dlg)), _dlg ? textOf(_dlg).slice(0, 40) : '没有弹窗');
      if (_dlg) {
        var _cancel = _dlg.querySelector('button');   // danger → 第一个是"留在此页"
        if (_cancel) _cancel.click();
        await sleep(400);
      }
      rec('N3-03b', '取消后仍留在首页（改动保住）',
          location.hash === '#/home' && dirtyState().dirty, 'hash=' + location.hash);
      // 复原：再点一次把改动撤掉。**这里不断言** —— 它测的是探针自己的收尾，不是产品；
      // 后面的 F4/F5 用例只关心弹窗与焦点，与脏状态无关（实测在脏状态下照样全过）。
      var _rows2 = document.querySelectorAll('.graph [data-key^="s:"]');
      for (var r2 = 0; r2 < _rows2.length; r2++) {
        var cb2 = _rows2[r2].querySelector('input[type=checkbox]');
        if (cb2 && !cb2.disabled) { cb2.click(); await sleep(400); break; }
      }
    }

    /* F4/F5 最后做（它会切换焦点），两个独立弹窗 */
    await go('#/home');
    var trigger = document.querySelector('.tabs a');
    trigger.focus();
    await testDangerDialog(trigger);
    trigger.focus();                  // Enter 已把焦点还回来，这里再明确一次作为基准
    await testFocusTrap(trigger);

    finish();
  }

  function visibleLv4() {
    return Array.prototype.slice.call(document.querySelectorAll('.lv4')).filter(function (e) {
      var n = e, vis = true;
      while (n && n !== document.documentElement) {          /* DEV-RULES A6：走一遍祖先链 */
        if (getComputedStyle(n).visibility === 'hidden') { vis = false; break; }
        n = n.parentElement;
      }
      return vis && e.getBoundingClientRect().height > 0;
    }).length;
  }

  /* 量之前关掉动效，否则读到的是过渡起点的值（DEV-RULES B5） */
  function freeze() {
    var s = document.createElement('style');
    s.textContent = '*,*::before,*::after{transition:none!important;animation:none!important}';
    (document.head || document.documentElement).appendChild(s);
  }

  function finish() {
    fetch('/__probe', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ results: out, errors: errs, ua: navigator.userAgent })
    });
  }

  var n = 0;
  var iv = setInterval(function () {
    n++;
    var view = document.getElementById('view');
    if ((view && view.children.length > 1) || n > 24) {
      clearInterval(iv);
      setTimeout(function () { run().catch(function (e) { errs.push('run 失败: ' + e); finish(); }); }, 900);
    }
  }, 250);
})();
"""


def fetch_fixtures(into: pathlib.Path) -> None:
    into.mkdir(parents=True, exist_ok=True)
    for name in ENDPOINTS:
        req = urllib.request.Request('%s/api/%s' % (LIVE, name), headers={'Host': '127.0.0.1:8318'})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read()
            json.loads(body)
            (into / (name + '.json')).write_bytes(body)
        except Exception as exc:                   # noqa: BLE001
            print('  夹具 %-11s 失败：%s' % (name, exc))


class Fixture(SimpleHTTPRequestHandler):
    collected: list = []
    seen: list = []            # 收到的 (method, path)，用来数“有没有真的发请求”

    def log_message(self, *a):
        pass

    def _note(self):
        Fixture.seen.append((self.command, self.path.split('?')[0]))

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
        self._note()
        if self.path != '/__probe':
            # 故意不实现 /api/settings：让设置页的保存**必然失败**，用来验证
            # “一次保存失败之后，后续保存仍然会真的发请求”（复查轮 5 的 #A）
            self._send(404, b'{}', 'application/json')
            return
        raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        Fixture.collected.append(json.loads(raw.decode('utf-8')))
        self._send(200, b'{"ok":true}', 'application/json')

    def do_GET(self):
        self._note()
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
            raw = raw.replace(PLACEHOLDER, b'data-theme="dark"')
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


def main() -> int:
    try:
        urllib.request.urlopen('http://127.0.0.1:8318/api/theme', timeout=5)
    except Exception:
        print('先起源码态控制台：python app/server.py --port 8318')
        return 2

    work = pathlib.Path(tempfile.mkdtemp(prefix='prism-desktop-probe-'))
    static = work / 'static'
    shutil.copytree(SRC, static)
    (static / 'probe.js').write_text(PROBE_JS, encoding='utf-8')
    idx = static / 'index.html'
    html = idx.read_text(encoding='utf-8')
    assert '</body>' in html, 'index.html 结构变了，探针注入点失效'
    idx.write_text(html.replace('</body>', '<script src="probe.js"></script>\n</body>'),
                   encoding='utf-8')
    fetch_fixtures(static / '_fixtures')

    srv = ThreadingHTTPServer(('127.0.0.1', PORT), partial(Fixture, directory=str(static)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.4)

    browser = find_browser()
    if not browser:
        print('找不到 Chrome/Edge')
        return 3
    cmd = [browser, '--headless=new', '--disable-gpu', '--no-first-run',
           '--no-default-browser-check', '--disable-extensions',
           '--user-data-dir=' + str(work / 'profile'),
           '--virtual-time-budget=90000', '--window-size=1600,900',
           'http://127.0.0.1:%d/' % PORT]
    print('启动:', pathlib.Path(browser).name)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    for _ in range(120):
        if Fixture.collected:
            break
        time.sleep(0.25)
    srv.shutdown()

    if not Fixture.collected:
        print('探针没回传。stderr 末尾：')
        print((proc.stderr or '')[-1200:])
        return 4

    data = Fixture.collected[0]
    (work / 'probe.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

    # 备注：本想用"夹具让 POST /api/settings 必失败，再从设置页连改两次、数服务端收到几次请求"
    # 来渲染级验证 settings.js 的排队链不被毒化，但这个夹具里设置页**根本不发起保存请求**
    # （两次尝试：派发 change / input 都是 0 次 POST，原因未查明）。按 DEV-RULES B1/B2 不硬凑，
    # 那条修复只做**代码级**验证（见 HANDOFF §6.9）。Fixture.seen 留着，以后要查方便。
    bad = 0
    for r in data['results']:
        flag = 'PASS' if r['ok'] else 'FAIL'
        if not r['ok']:
            bad += 1
        print('  %-4s %-6s %s' % (r['id'], flag, r['name']))
        if r['detail']:
            print('           %s' % r['detail'])
    print()
    print('JS 报错:', data['errors'] or '无')
    print('结果 JSON:', work / 'probe.json')
    print('合计 %d 条，失败 %d 条' % (len(data['results']), bad))
    return 0


if __name__ == '__main__':
    sys.exit(main())
