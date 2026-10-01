"""设置页保存链的**渲染级**验证（审查遗留的两条"只有代码级验证"）：

  S1  F5 自动保存的收敛：文本字段防抖 800ms 内连改两次，服务端只应收到**一次**
      POST 且值是最后一次的；静置后计数不再增长（D8.1 那个"无限微任务循环"
      若复发，这里会发散）。
  S2  排队链不被单次失败毒化（D8.2）：第一次保存让网关写入桩抛 RouteError，
      紧接着的第二次保存**仍然真的发请求**并落盘成功。

与 _desktop_probe.py 的两个关键差异：

* **为什么这次能收到 POST**：那份探针的夹具服务器对 /api/settings 不实现 GET
  （`no fixture`），设置页 mount 第一步就失败，控件根本没建出来，后面派发
  change/input 自然 0 次请求（HANDOFF §6.10 记录的"原因未查明"——这就是原因）。
  本探针把**真 server**（create_server）跑在本进程里，设置接口完全真实。
* **为什么断言全在服务端**：页面 CSP 是 `connect-src 'self'`，探针结果没法
  回传到第二个端口（实测被拦，这正是第一版探针"探针没回传"的原因）。而
  POST 计数与落盘值本来就都在服务端——页面只负责做动作，判断全在这里做。

真 server 上桩掉的三处（全部为了 E4，不碰生产数据）：
  * server.SETTINGS_PATH → 临时目录
  * bridge.gateway_get / gateway_put → 内存桩（不打 8317、不经过 auth_gate；
    桩带"前 N 次写入抛 RouteError"的失败注入，用于 S2）
  * sampling.prune → 空函数

跑法（独立跑，不需要先起 8318）：
    python script\\_settings_probe.py
"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from functools import partial

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT / 'script'))

from core import bridge, sampling   # noqa: E402
import server                       # noqa: E402

P = []                              # 断言结果 (id, ok, name, detail)
_post_log = []                      # 每次 POST /api/settings 的时间戳


def note(pid, ok, name, detail=''):
    P.append((pid, bool(ok), name, detail))
    print('  %-4s %-6s %s' % (pid, 'PASS' if ok else 'FAIL', name))
    if detail and not ok:
        print('           ' + detail)


# ---------------------------------------------------------------- 网关桩（含失败注入）

GW = {'debug': False, 'proxy-url': '', 'request-retry': 0, 'request-log': False}
FAIL_FIRST = {'n': 0}               # 前 n 次 gateway_put 抛 RouteError（S2 用）


def fake_gateway_get(endpoint):
    return GW.get(endpoint, '')


def fake_gateway_put(endpoint, payload):
    if FAIL_FIRST['n'] > 0:
        FAIL_FIRST['n'] -= 1
        raise bridge.RouteError('注入的网关写入失败（探针 S2）')
    for field, value in (payload or {}).items():
        if field in GW:
            GW[field] = value
    return None


def fake_prune(days):
    return None


def counted_apply_settings(original, body):
    _post_log.append(time.time())
    return original(body)


PROBE_JS = r"""
(function () {
  'use strict';
  /* 页面侧只做动作，不做判断、不回传（CSP connect-src 'self' 拦跨源 fetch，
     回传这条路实测不通——判断全在 Python 服务端做）。 */
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  function rowByLabel(text) {
    var rows = document.querySelectorAll('.ps-row');
    for (var i = 0; i < rows.length; i++) {
      var lbl = rows[i].querySelector('.ps-lbl');
      if (lbl && lbl.textContent === text) return rows[i];
    }
    return null;
  }
  function fire(el, type) {
    el.dispatchEvent(new Event(type, { bubbles: true }));
  }

  async function run() {
    /* 等设置页真的 mount 出来。夹具探针当年 0 POST，就是这一步没等到：
       页面 mount 失败、控件不存在，后面派发事件全部落空。 */
    var dbg = null;
    for (var i = 0; i < 60 && !dbg; i++) {
      var row = rowByLabel('Debug 模式');
      dbg = row && row.querySelector('input[type=checkbox]');
      if (!dbg) await sleep(250);
    }
    if (!dbg) return;               // mount 失败：服务端会看到 0 次 POST，判 FAIL

    /* S2 排队链：第一次保存被网关桩拒绝（Python 侧 FAIL_FIRST=1），紧接着的
       第二次保存必须真的发出去。两次点击间隔 50ms —— 第二次必然撞进第一次
       的队列，这正是 D8.2 要守的时序。 */
    dbg.click();
    await sleep(50);
    dbg.click();
    await sleep(1200);

    /* S1 防抖收敛：800ms 防抖窗口内连改两次，只能发出一次 POST、值取最后一次 */
    var retryRow = rowByLabel('请求重试次数');
    var retry = retryRow && retryRow.querySelector('input');
    if (!retry) return;
    retry.focus();
    retry.value = '2';
    fire(retry, 'input');
    await sleep(120);
    retry.value = '3';
    fire(retry, 'input');
    await sleep(1400);

    /* S1b 静置：不再有动作（无限循环若复发，服务端计数在这里发散） */
    await sleep(1800);
  }

  var n = 0;
  var iv = setInterval(function () {
    n++;
    var view = document.getElementById('view');
    if ((view && view.querySelector('.ps-row')) || n > 40) {
      clearInterval(iv);
      run();
    }
  }, 250);
})();
"""


def find_browser():
    for p in (r'C:\Program Files\Google\Chrome\Application\chrome.exe',
              r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'):
        if pathlib.Path(p).exists():
            return p
    return None


def main() -> int:
    work = pathlib.Path(tempfile.mkdtemp(prefix='prism-settings-probe-'))
    static = work / 'static'
    shutil.copytree(ROOT / 'app' / 'static', static)
    (static / 'probe.js').write_text(PROBE_JS, encoding='utf-8')
    idx = static / 'index.html'
    html = idx.read_text(encoding='utf-8')
    assert '</body>' in html, 'index.html 结构变了，探针注入点失效'
    idx.write_text(html.replace('</body>', '<script src="probe.js"></script>\n</body>'),
                   encoding='utf-8')

    # ---- 打桩（在 create_server 之前）：三处都为 E4 ----
    orig_static, orig_spath = server.STATIC_DIR, server.SETTINGS_PATH
    orig_gget, orig_gput = bridge.gateway_get, bridge.gateway_put
    orig_prune, orig_apply = sampling.prune, server.apply_settings
    server.STATIC_DIR = static
    server.SETTINGS_PATH = work / 'settings.json'
    bridge.gateway_get = fake_gateway_get
    bridge.gateway_put = fake_gateway_put
    sampling.prune = fake_prune
    server.apply_settings = partial(counted_apply_settings, orig_apply)
    FAIL_FIRST['n'] = 1                 # S2：第一次 POST 的网关写入必失败

    import socket
    _probe = socket.socket()
    _probe.bind(('127.0.0.1', 0))
    free_port = _probe.getsockname()[1]
    _probe.close()
    httpd = server.create_server(free_port)
    threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.2},
                     daemon=True).start()
    live = 'http://127.0.0.1:%d' % httpd.console_port

    browser = find_browser()
    if not browser:
        print('找不到 Chrome/Edge')
        return 3
    cmd = [browser, '--headless=new', '--disable-gpu', '--no-first-run',
           '--no-default-browser-check', '--disable-extensions',
           '--user-data-dir=' + str(work / 'profile'),
           '--virtual-time-budget=90000', '--window-size=1600,900', live + '/#/settings']
    print('启动:', pathlib.Path(browser).name, '控制台:', live)
    proc = None
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired as exc:
        print('浏览器超时（探针动作可能没跑完）')
        proc = exc

    # 页面动作做完的标志是 POST 计数到位（S2 两次 + S1 一次 = 3）；等不到就超时收场
    deadline = time.time() + 90
    while time.time() < deadline and len(_post_log) < 3:
        time.sleep(0.25)
    time.sleep(1.0)                     # 再给静置窗口留一段，观察计数有没有继续涨
    stable_count = len(_post_log)
    time.sleep(1.6)
    after_stable = len(_post_log)
    try:
        httpd.shutdown()
    except Exception:
        pass

    # ---- 恢复桩（E4：先恢复，再判——判据用的快照都已取完）----
    server.STATIC_DIR, server.SETTINGS_PATH = orig_static, orig_spath
    bridge.gateway_get, bridge.gateway_put = orig_gget, orig_gput
    sampling.prune = orig_prune
    server.apply_settings = orig_apply

    if not _post_log:
        print('0 次 POST——设置页 mount 失败或动作没跑。浏览器 stderr 末尾：')
        print((getattr(proc, 'stderr', '') or '')[-1200:])
        return 4

    n_s2 = len([t for t in _post_log if t <= _post_log[1] + 0.001][:2]) if len(_post_log) >= 2 else len(_post_log)
    note('S2a', len(_post_log) >= 2,
         '排队链：第一次失败后第二次仍真的发了 POST（共 %d 次）' % len(_post_log),
         '时间线 %r' % [round(t - _post_log[0], 2) for t in _post_log])
    saved = {}
    try:
        saved = json.loads((work / 'settings.json').read_text(encoding='utf-8'))
    except Exception as exc:                       # noqa: BLE001
        note('S2b', False, '设置落盘可解析', repr(exc))
    else:
        note('S2b', saved.get('gateway', {}).get('debug') is False,
             '第二次保存落盘成功（debug 复位 false——失败的那次被这次重救回来）',
             str(saved)[:160])
    note('S1a', len(_post_log) == 3,
         '防抖收敛：800ms 窗口内连改两次只发一次 POST（总 3 = S2 两次 + S1 一次）',
         '共 %d 次 %r' % (len(_post_log), [round(t - _post_log[0], 2) for t in _post_log]))
    note('S1b', after_stable == stable_count, '静置后计数稳定（无限循环若复发这里会发散）',
         'stable=%r after=%r' % (stable_count, after_stable))
    note('S1c', saved.get('gateway', {}).get('request_retry') == 3,
         '防抖落盘值取最后一次（request_retry=3 而不是 2）',
         'gateway=%r' % saved.get('gateway'))
    note('GW', GW['request-retry'] == 3 and GW['debug'] is False,
         '网关桩收到了两处写入', 'GW=%r' % GW)

    print()
    print('POST 时间线: %d 次' % len(_post_log))
    print('工作目录:', work)
    return 0


if __name__ == '__main__':
    sys.exit(main())
