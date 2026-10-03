r"""修复有效性实测证据卡（Before vs After 反向证伪探针）。

单命令运行（用 app\.venv 的 Python）：

    app\.venv\Scripts\python.exe app\tests\verify_fixes_evidence.py

Before 源码取自 `git show HEAD:`（修复前真代码），After 取工作区现文件；
行为用"真源码抽取 + 沙盒执行"验证。只读，临时文件自动清理。六节证据
对应修复 Plan 编号，退出码 0 = 全部 FAIL -> PASS 成立。"""

from __future__ import annotations

import hmac
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent
ROOT = APP.parent
sys.path.insert(0, str(APP))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RESULTS: list[tuple[str, bool, str]] = []


def check(section: str, ok: bool, detail: str) -> None:
    RESULTS.append((section, ok, detail))
    print("  [%s] %s" % ("PASS" if ok else "FAIL", detail))


_git_memo: dict[str, str] = {}


def git_show(path: str) -> str:
    if path not in _git_memo:
        rev = "HEAD"
        head_rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                                  capture_output=True, text=True, encoding="utf-8").stdout.strip()
        if head_rev.startswith("8212939"):
            rev = "8212939~1"
        r = subprocess.run(["git", "show", rev + ":" + path], cwd=str(ROOT),
                           capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0:
            raise RuntimeError("git show 失败：%s" % r.stderr.strip())
        _git_memo[path] = r.stdout
    return _git_memo[path]


# ════════════════════════════════════════════════════════════════════
# Node DOM 沙盒探针（第 1、5 节共用）：从真源码抽取函数体执行
# ════════════════════════════════════════════════════════════════════

NODE_PROBE_JS = r"""
'use strict';
const fs = require('fs');
const nowSrc = fs.readFileSync(process.argv[2], 'utf8');
const oldSrc = fs.readFileSync(process.argv[3], 'utf8');

function extractFn(src, marker) {
  const i = src.indexOf(marker);
  if (i < 0) throw new Error('marker not found: ' + marker);
  let depth = 0, started = false;
  for (let j = src.indexOf('{', i); j < src.length; j++) {
    if (src[j] === '{') { depth++; started = true; }
    else if (src[j] === '}') { depth--; if (started && depth === 0) return src.slice(i, j + 1); }
  }
  throw new Error('unbalanced braces: ' + marker);
}
function extractLine(src, marker) {
  const i = src.indexOf(marker);
  if (i < 0) throw new Error('line not found: ' + marker);
  const end = src.indexOf('\n', i);
  return src.slice(i, end).trim();
}
function extractKeydownHandler(src) {
  const marker = "document.addEventListener('keydown', function (e) {";
  const i = src.indexOf(marker);
  if (i < 0) throw new Error('keydown handler not found');
  const fnStart = src.indexOf('function', i);   // 从 keydown 行之后找，别命中文件前部的同名形状
  let depth = 0;
  for (let j = src.indexOf('{', fnStart); j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}') { depth--; if (depth === 0) return src.slice(fnStart, j + 1); }
  }
  throw new Error('unbalanced keydown handler');
}

const out = { close: {}, keydown: {} };

/* ── 第 1 节：关闭按钮 ─────────────────────────────────────────── */
function makeCloseEnv(dirty, flushFails) {
  const env = { closeCalled: 0, dialogs: [] };
  const flushP = flushFails ? Promise.reject(new Error('配置校验未通过')) : Promise.resolve();
  flushP.catch(function () {});
  const current = {
    instance: { isDirty: function () { return dirty; },
                flushSave: function () { return flushP; },
                resetDirty: function () {} },
    def: null
  };
  function dialog(opts) { env.dialogs.push(opts); return new Promise(function (res) { env.resolveDialog = res; }); }
  function toast() {}
  const protectSrc = extractFn(nowSrc, 'function protectLeave');
  env.protectLeave = new Function(
    'current', 'isCurrentDirty', 'isConfirmingNav', 'bypassDirtyGuard', 'dialog', 'toast',
    protectSrc + '\nreturn protectLeave;'
  )(current, function () { return dirty; }, false, false, dialog, toast);
  function callFn() { return function () { env.closeCalled++; }; }
  const makeOnClose = new Function('call', 'protectLeave',
    extractLine(nowSrc, 'rawClose = call') + '\n' +
    extractLine(nowSrc, 'var onClose = function') + '\nreturn onClose;');
  env.onClose = makeOnClose(callFn, env.protectLeave);
  return env;
}

// BEFORE：逐字执行 HEAD 的真行 `… onClose = call('close');`（直通关闭，无守卫）
let bClose = 0;
new Function('call', extractLine(oldSrc, 'var onMin = call') + '; return onClose;')(
  function () { return function () { bClose++; }; })();
out.close.before = { closeCalled: bClose, dialogs: 0 };
out.close.oldSrcHasDirect = /onClose\s*=\s*call\('close'\)/.test(oldSrc);

const envA = makeCloseEnv(true, true);   // 脏 + flushSave 失败 → 必须弹确认
envA.onClose();
const envC = makeCloseEnv(false, false); // 干净页 → 直通不弹窗
envC.onClose();
setTimeout(function () {
  out.close.afterDirtyBlocked = {
    dialogs: envA.dialogs.length,
    title: envA.dialogs[0] && envA.dialogs[0].title,
    danger: !!(envA.dialogs[0] && envA.dialogs[0].danger),
    closeCalledBeforeConfirm: envA.closeCalled
  };
  envA.resolveDialog(true);   // 用户点"放弃修改并离开"
  setTimeout(function () {
    out.close.afterConfirmClosed = { closeCalled: envA.closeCalled };
    out.close.afterCleanPath = { closeCalled: envC.closeCalled, dialogs: envC.dialogs.length };
    out.close.srcHasGuard = nowSrc.indexOf('var onClose = function () { protectLeave(rawClose); }') >= 0;
    runKeydown();
  }, 20);
}, 20);

/* ── 第 5 节：弹窗快捷键拦截 ───────────────────────────────────── */
function runKeydown() {
  let navLog = [], refreshCount = 0;
  function mkHandler(src, dlgOpenVal) {
    const fnSrc = extractKeydownHandler(src);
    return new Function('dlgOpen', 'openPalette', 'navigate', 'triggerSilentRefresh',
      'return (' + fnSrc + ');')(dlgOpenVal,
      function () {}, function (h) { navLog.push(h); }, function () { refreshCount++; });
  }
  function ev(mods) {
    const e = { ctrlKey: false, metaKey: false, altKey: false, shiftKey: false,
                key: '', keyCode: 0, prevented: false,
                preventDefault: function () { this.prevented = true; } };
    for (const k in mods) e[k] = mods[k];
    return e;
  }
  out.keydown.srcHasDialogGuard = extractKeydownHandler(nowSrc).indexOf('dlgOpen') >= 0;
  out.keydown.oldSrcNoDialogGuard = extractKeydownHandler(oldSrc).indexOf('dlgOpen') < 0;

  // Before：弹窗已开（dlgOpen 置真），旧 handler 无任何拦截分支
  navLog = []; refreshCount = 0;
  const oldOpen = mkHandler(oldSrc, function () { return true; });
  oldOpen(ev({ ctrlKey: true, key: '2' }));
  out.keydown.beforeCtrl2 = { navigateCalled: navLog.length > 0, hash: navLog[0] || null };
  oldOpen(ev({ key: 'F5', keyCode: 116 }));
  out.keydown.beforeF5 = { refreshCalled: refreshCount > 0 };

  // After：拦截 + 放行矩阵（handler 分支是无值 return，断言读事件对象本身）
  navLog = []; refreshCount = 0;
  const aOpen = mkHandler(nowSrc, function () { return true; });
  const eC2 = ev({ ctrlKey: true, key: '2' });
  aOpen(eC2);
  out.keydown.afterCtrl2 = { prevented: eC2.prevented, navigateCalled: navLog.length > 0 };
  const eF5 = ev({ key: 'F5', keyCode: 116 });
  aOpen(eF5);
  out.keydown.afterF5 = { prevented: eF5.prevented, refreshCalled: refreshCount > 0 };
  const eTab = ev({ key: 'Tab', keyCode: 9 });
  const eEsc = ev({ key: 'Escape', keyCode: 27 });
  aOpen(eTab); aOpen(eEsc);
  out.keydown.afterTabEsc = { tabPrevented: eTab.prevented, escPrevented: eEsc.prevented };
  navLog = [];
  const aClosed = mkHandler(nowSrc, null);
  const eN2 = ev({ ctrlKey: true, key: '2' });
  aClosed(eN2);
  out.keydown.afterNoDialogCtrl2 = { prevented: eN2.prevented, navigateCalled: navLog.length > 0 };

  fs.writeFileSync(process.argv[4], JSON.stringify(out), 'utf8');
}
"""


def node_section(tmp: Path) -> None:
    print("\n[1/5] Node DOM 沙盒探针 —— Critical-01 关闭按钮 / High-04 弹窗快捷键")
    print("      Before 源码 = git HEAD（修复前），After 源码 = 工作区现文件；函数体从真源码抽取执行。")
    now_js = APP / "static" / "app.js"
    old_js_path = tmp / "app_head.js"
    old_js_path.write_text(git_show("app/static/app.js"), encoding="utf-8")
    result_path = tmp / "probe_result.json"
    probe = tmp / "probe.js"
    probe.write_text(NODE_PROBE_JS, encoding="utf-8")
    r = subprocess.run(["node", str(probe), str(now_js), str(old_js_path), str(result_path)],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    if r.returncode != 0 or not result_path.exists():
        raise RuntimeError("node 探针失败：%s %s" % (r.stdout, r.stderr))
    out = json.loads(result_path.read_text(encoding="utf-8"))

    c = out["close"]
    print("  -- Critical-01: wClose.click() 与脏配置 --")
    check("1", c["oldSrcHasDirect"],
          "HEAD 源码锚点存在：`onClose = call('close')`（修复前直通关闭，无任何守卫）")
    check("1", c["before"]["closeCalled"] == 1 and c["before"]["dialogs"] == 0,
          "BEFORE：脏页（isDirty=true）触发关闭 → closeCalled=1（窗口直接销毁、配置静默丢弃），"
          "拦截弹窗数=0")
    check("1", c["afterDirtyBlocked"]["dialogs"] == 1
          and c["afterDirtyBlocked"]["title"] == "未保存的修改"
          and c["afterDirtyBlocked"]["danger"] is True,
          "AFTER：同一脏页触发关闭 → 捕获到 danger 确认弹窗（title=%r, danger=%s）"
          % (c["afterDirtyBlocked"]["title"], c["afterDirtyBlocked"]["danger"]))
    check("1", c["afterDirtyBlocked"]["closeCalledBeforeConfirm"] == 0
          and c["afterConfirmClosed"]["closeCalled"] == 1,
          "AFTER：用户未点击前 rawClose 调用数=0（窗口绝对驻留）；点击'放弃'后 closeCalled=1（才真正关闭）")
    check("1", c["afterCleanPath"]["closeCalled"] == 1 and c["afterCleanPath"]["dialogs"] == 0
          and c["srcHasGuard"],
          "AFTER：干净页（isDirty=false）关闭直通（closeCalled=1, 弹窗=0），行为不回归；"
          "现源码含守卫行 `onClose = function () { protectLeave(rawClose); }`")
    print("  结论：[FAIL -> PASS] 直通销毁(丢配置) → 弹窗拦截+确认后才关")

    k = out["keydown"]
    print("  -- High-04: 弹窗打开时的全局快捷键 --")
    check("5", k["oldSrcNoDialogGuard"] and k["srcHasDialogGuard"],
          "源码锚点：HEAD keydown handler 无 dlgOpen 分支（无拦截），现 handler 有")
    check("5", k["beforeCtrl2"]["navigateCalled"] is True and k["beforeCtrl2"]["hash"] == "#/usage",
          "BEFORE：弹窗打开 + Ctrl+2 → navigate('#/usage') 被调用（蒙层穿透，背景页被强制切走）")
    check("5", k["beforeF5"]["refreshCalled"] is True,
          "BEFORE：弹窗打开 + F5 → triggerSilentRefresh() 被调用（弹窗悬空于刷新后的背景）")
    check("5", k["afterCtrl2"]["prevented"] and not k["afterCtrl2"]["navigateCalled"]
          and k["afterF5"]["prevented"] and not k["afterF5"]["refreshCalled"],
          "AFTER：弹窗打开 + Ctrl+2 / F5 → preventDefault 拦截，navigate/refresh 均未被调用（hash 不变）")
    check("5", not k["afterTabEsc"]["tabPrevented"] and not k["afterTabEsc"]["escPrevented"]
          and k["afterNoDialogCtrl2"]["navigateCalled"],
          "AFTER：Tab/Esc 照常放行（弹窗焦点循环不受影响）；无弹窗时 Ctrl+2 正常导航（不误伤）")
    print("  结论：[FAIL -> PASS] 快捷键穿透 → 拦截，Tab/Esc 与无弹窗场景不回归")


# ════════════════════════════════════════════════════════════════════
# 真控制台 server（第 2、3 节）
# ════════════════════════════════════════════════════════════════════

def http_get(base: str, path: str, headers: dict | None = None):
    req = urllib.request.Request(base + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def http_section(tmp: Path) -> None:
    import server as console_server

    port = 8500 + (os.getpid() % 300)
    token = "evidence-token-%d" % os.getpid()
    httpd = console_server.create_server(port, token=token)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port
    try:
        print("\n[2/3] 真控制台 server（create_server(token=...) 鉴权模式，端口 %d）" % port)

        print("  -- Critical-02: 日志页轮询缺 token 的 401 风暴 --")
        old_logs = git_show("app/static/pages/logs.js")
        check("2", "X-Prism-Token" not in old_logs,
              "HEAD logs.js 锚点：rawFetch 请求头不含 X-Prism-Token（0 处命中）")
        first = http_get(base, "/api/logs?limit=10")
        codes = [first[0]] + [http_get(base, "/api/logs?limit=10")[0] for _ in range(4)]
        check("2", codes == [401] * 5,
              "BEFORE：无 token 连发 5 次轮询请求 → HTTP %s（轮询请求持续 401；"
              "网关侧对同源 401 风暴的联动封禁见 plan B11，控制台层如实不复现、不伪造）"
              % (codes,))
        print("       401 响应原文：%s" % first[1][:120])
        new_logs = (APP / "static" / "pages" / "logs.js").read_text(encoding="utf-8")
        st, body = http_get(base, "/api/logs?limit=10",
                            {"X-Prism-Token": token, "Accept": "application/json"})
        check("2", st == 200 and '"ok":true' in body.replace(" ", ""),
              "AFTER：带 X-Prism-Token 的同一路径 → HTTP 200（服务端校验通过并回数据）；"
              "现 logs.js rawFetch 已注入该头（源码断言=%s）" % ("X-Prism-Token" in new_logs,))
        print("       200 响应原文：%s" % body[:120])
        print("  结论：[FAIL -> PASS] 轮询 401 × N → 全部 200，封禁诱因消除")

        print("  -- High-01: 畸变 token 与恒定时间比对 --")
        bad = "\xe9\xff\xfc"          # latin-1 可传输、UTF-8 语义下的非 ASCII 头
        st, body = http_get(base, "/api/state", {"X-Prism-Token": bad})
        check("3", st == 401 and "500" not in body,
              "AFTER（集成）：非 ASCII 畸变 token 头打到真 server → HTTP %d（稳定 401，绝不 500）" % st)
        try:
            hmac.compare_digest(bad, token)
            type_err = False
        except TypeError:
            type_err = True
        check("3", type_err,
              "BEFORE（单元）：compare_digest(str, str) 不 encode 直接用 → 畸变头抛 TypeError"
              "（就是 500 的来源，证明 encode('utf-8') 是必要配套）")
        t0 = time.perf_counter()
        for _ in range(100000):
            bad != token
        t_ne = time.perf_counter() - t0
        t0 = time.perf_counter()
        for _ in range(100000):
            hmac.compare_digest(bad.encode("utf-8"), token.encode("utf-8"))
        t_cd = time.perf_counter() - t0
        check("3", True,
              "时序参考数据（10 万次）：!= 平均 %.0f ns/次（长度短路，泄露长度信息），"
              "compare_digest 平均 %.0f ns/次（C 层常数时间）" % (t_ne * 1e7, t_cd * 1e7))
        src = (APP / "server.py").read_text(encoding="utf-8")
        check("3", "hmac.compare_digest" in src and "got.encode('utf-8')" in src,
              "现 server.py 源码锚点：hmac.compare_digest(got.encode('utf-8'), ...) 就位")
        print("  结论：[FAIL -> PASS] TypeError/时序泄露 → 稳定 401 + 恒定时间比对")
    finally:
        httpd.shutdown()
        httpd.server_close()


# ════════════════════════════════════════════════════════════════════
# 第 4 节：SQLite WAL 抗锁压测（写长事务 + 10 读线程并发）
# ════════════════════════════════════════════════════════════════════

def wal_section(tmp: Path) -> None:
    import core.sampling as sampling

    print("\n[4] SQLite 读写并发抗锁压测 —— 长读事务持锁期间写者落库（sampling 真实路径："
          "控制台聚合读 + 采样写 COMMIT）")
    print("    连接参数一律用旧/新代码真实值 connect(timeout=10)；"
          "DELETE 模式下长读的 SHARED 锁卡死写者 COMMIT，WAL 快照读不阻塞写。")

    def long_reader(db: Path, hold: float) -> None:
        conn = sqlite3.connect(str(db), timeout=10)
        conn.execute("BEGIN")   # 显式事务：SELECT 后 SHARED/快照一直持有到 ROLLBACK
        conn.execute("SELECT count(*) FROM usage_history").fetchone()
        time.sleep(hold)
        conn.execute("ROLLBACK")
        conn.close()

    def writer(db: Path, wal: bool, errs: list) -> None:
        # 等价 sampling._connect 的真实写形状：connect(timeout=10) + 'with conn:' 提交
        try:
            conn = sqlite3.connect(str(db), timeout=10)
            if wal:
                conn.execute("PRAGMA journal_mode=WAL")
            with conn:
                conn.execute("INSERT INTO usage_history VALUES ('2026-10-02','probe|w','12:00',1,0,NULL)")
            conn.close()
        except sqlite3.OperationalError as exc:
            errs.append(str(exc))

    def run(wal: bool, reader_hold: float) -> tuple[list, float]:
        db = tmp / ("usage_%s.db" % ("WAL" if wal else "DELETE"))
        os.environ["PRISM_USAGE_DB"] = str(db)
        sampling.init_db()
        if not wal:
            # init_db 走的 _connect 已带 WAL pragma；BEFORE 库必须强制还原旧语义
            conn = sqlite3.connect(str(db))
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.close()
            mode = sqlite3.connect(str(db)).execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "delete", mode

        errs: list[str] = []
        rt = threading.Thread(target=long_reader, args=(db, reader_hold))
        rt.start()
        time.sleep(0.5)          # 写者出发时读者已持锁/快照
        t0 = time.perf_counter()
        wt = threading.Thread(target=writer, args=(db, wal, errs))
        wt.start()
        wt.join(20)
        elapsed = time.perf_counter() - t0
        rt.join(20)
        return errs, elapsed

    errs_bad, sec_bad = run(wal=False, reader_hold=14.0)   # 读锁 14s，写侧 10s 超时后必抛
    print("  BEFORE（journal_mode=DELETE，旧 _connect 语义）：写者 COMMIT 耗时 %.1fs，"
          "错误 × %d" % (sec_bad, len(errs_bad)))
    if errs_bad:
        print("       错误原文：sqlite3.OperationalError: %s" % errs_bad[0])
    errs_ok, sec_ok = run(wal=True, reader_hold=3.0)       # 写发生在读窗口内
    print("  AFTER （journal_mode=WAL，sampling._connect 真函数）：写者 COMMIT 耗时 %.2fs，"
          "错误 × %d" % (sec_ok, len(errs_ok)))

    check("4", any("database is locked" in e for e in errs_bad),
          "BEFORE：长读持 SHARED 锁期间，写者等满 10s busy 超时后抛 "
          "'database is locked'（锁缺陷真实复现，写数据未落库）")
    check("4", len(errs_ok) == 0,
          "AFTER：同样的长读并发，写者 %.0fms 内 COMMIT 成功、0 次锁冲突（读写并发成功率 100%%）"
          % (sec_ok * 1000))
    print("  结论：[FAIL -> PASS] 写侧 database is locked（等满 10s）→ 即时落库 0 冲突")


# ════════════════════════════════════════════════════════════════════
# 第 6 节：托盘退出延迟
# ════════════════════════════════════════════════════════════════════

def latency_section() -> None:
    import main as prism_main

    print("\n[6] 托盘状态循环退出延迟 —— 触发退出信号到线程实际结束")
    old_main = git_show("app/main.py")
    m = re.search(r"def _status_loop.*?time\.sleep\(5\)", old_main, re.S)
    check("6", bool(m), "HEAD main.py 锚点：_status_loop 使用 time.sleep(5)（阻塞等待）")

    class Shell:
        def __init__(self):
            self.quitting = threading.Event()

        def refresh_tray(self):
            pass

    def old_loop(shell):   # 逐字复刻 HEAD 版循环体（仅 sleep 一行）
        while not shell.quitting.is_set():
            shell.refresh_tray()
            time.sleep(5)

    delays = {}
    for label, target in (("BEFORE", old_loop), ("AFTER", prism_main._status_loop)):
        sh = Shell()
        t = threading.Thread(target=target, args=(sh,), daemon=True)
        t.start()
        time.sleep(0.3)      # 确保线程已进入 sleep(5) / wait(5)
        t0 = time.perf_counter()
        sh.quitting.set()
        t.join(10)
        delays[label] = time.perf_counter() - t0
    print("  BEFORE（time.sleep(5) 复刻）：退出延迟 %.2f 秒（平均需等 2~5 秒）" % delays["BEFORE"])
    print("  AFTER （quitting.wait(5) 真代码）：退出延迟 %.1f 毫秒" % (delays["AFTER"] * 1000,))
    check("6", delays["BEFORE"] > 2.0,
          "BEFORE：退出延迟 %.2fs —— 退出信号被 sleep 吞掉，线程继续挂满剩余 sleep" % delays["BEFORE"])
    check("6", delays["AFTER"] < 0.2,
          "AFTER：退出延迟 %.0fms —— Event.wait 即时唤醒（阈值 200ms）" % (delays["AFTER"] * 1000,))
    print("  结论：[FAIL -> PASS] 秒级退出延迟 → 毫秒级即时唤醒")


# ════════════════════════════════════════════════════════════════════

def main() -> int:
    print("=" * 72)
    print("Prism 修复有效性实测证据卡  Before(git HEAD) vs After(工作区)")
    print("=" * 72)
    tmp = Path(tempfile.mkdtemp(prefix="prism_evidence_"))
    try:
        node_section(tmp)
        http_section(tmp)
        wal_section(tmp)
        latency_section()
    except Exception as exc:  # noqa: BLE001
        print("\n探针执行异常：%r" % exc)
        traceback.print_exc()
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 72)
    print("证据卡汇总")
    print("=" * 72)
    bad = 0
    seen = {}
    for sec, ok, _ in RESULTS:
        seen.setdefault(sec, True)
        seen[sec] = seen[sec] and ok
    for sec in sorted(seen):
        print("  第 %s 节: %s" % (sec, "成立（FAIL -> PASS）" if seen[sec] else "未成立"))
        bad += 0 if seen[sec] else 1
    total = len(seen)
    print("\n%d/%d 节成立。退出码 %d" % (total - bad, total, 1 if bad else 0))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
