/* Prism · 日志页
 *
 * 数据来源（见 app/INTERFACES.md 与 app/server.py 实测）：
 *   GET    /api/logs?limit=N      → {lines:[str], line_count:int, truncated:bool}   取尾部 N 行
 *                                   单次读取撞到 server.py 的 MAX_TAIL_BYTES 时带
 *                                   X-Prism-Truncated: 1（truncated 是同一个值的 JSON 副本）
 *   DELETE /api/logs              → {cleared:true}
 *   GET    /api/error-logs        → {files:[{name,size,modified}], dir}
 *   GET    /api/error-logs/<name> → 原始文本（可能被服务端截断，用 X-Prism-Truncated 标出）
 *
 * 注意 modified：server.py 给的是 ISO 字符串（不是 unix 时间戳），要按字符串解析。
 *
 * 与壳的关系：壳不轮询日志，所以本页自带增量轮询（这也是任务要求的开关）。
 * 注册/清理/取数走 PrismPages + Prism.registerPage + ctx.api + ctx.onUnmount，
 * 样式用 app.css 的类名（.sec/.toolbar/.logview/.ln/.tw/table.t/.statebox/.notice/.tag/.btn/.sw2），
 * 和其余四页保持一致。不注入 <style>、不写 style=""：server.py 的 CSP 是
 * script-src 'self'（无 unsafe-inline）→ 内联 <script> 会被静默拦掉；
 * style-src 'self' 'unsafe-inline' → 注入 <style> 与内联样式**是允许的**（本页就靠它）。
 */
(function () {
  'use strict';

  var PAGE_ID = 'logs';
  var POLL_MS = 3000;     // 3s 的尾部轮询：够实时，又不至于把网关敲热
  var TAIL = 200;         // 每次只取尾部 200 行。参数名是 limit——写成 lines 会被忽略并返回全量
  var KEEP = 5000;        // 内存里最多留这么多行
  var RENDER_MAX = 2000;  // DOM 里最多渲染这么多行，再多浏览器会卡
  var MGMT_RE = /\/v0\/management/;
  // server.py:52 的 ERROR_LOG_LIMIT：单个错误日志只回前 2MB，截断时带 X-Prism-Truncated: 1
  var TRUNC_LIMIT = 2 * 1024 * 1024;
  // server.py:56 的 MAX_TAIL_BYTES：/api/logs 从 main.log 尾部往回读时最多攒这么多字节，
  // 撞上就少给几行、并带 X-Prism-Truncated: 1。跟上面那个 2MB 不是一回事——那是单个
  // 错误日志文件的读取上限，这里是一次尾部读取的字节上限。
  var TAIL_LIMIT = 8 * 1024 * 1024;
  // 跟 app.js 的 DEFAULT_TIMEOUT 一致。本页自己发的请求也得有上限：挂住的请求会让
  // S.busy 永远停在 true，3s 轮询就此停摆（壳的封装有超时，自己写的这条不能没有）。
  var FETCH_TIMEOUT_MS = 15000;

  /* ── 小工具 ─────────────────────────────────────────────────── */
  // h/clear 用壳的单一实现（app.js），ME-10：私有副本修 bug 不传播。壳为超集。
  var h = window.Prism.h, clear = window.Prism.clear;
  function clock(d) {
    function p(n) { return (n < 10 ? '0' : '') + n; }
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }
  function size(n) {
    if (typeof n !== 'number' || !isFinite(n)) return '—';
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1048576).toFixed(2) + ' MB';
  }
  // server.py 给的是 ISO 字符串，别的地方可能给 unix 秒——两种都吃
  function mtime(v) {
    if (v === null || v === undefined || v === '') return '—';
    var d = (typeof v === 'number') ? new Date(v * 1000) : new Date(String(v));
    if (isNaN(d.getTime())) return String(v).slice(0, 16);
    function p(n) { return (n < 10 ? '0' : '') + n; }
    return p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }
  function shortErr(e) {
    if (!e) return '未知错误';
    if (e.name === 'ApiError' || e.status !== undefined) return e.message || String(e);
    return (e.message || String(e)).slice(0, 240);
  }

  /* 组件：只用 app.css 的类，不注入 <style>、不写 style=""。
     server.py 的 CSP 是 script-src 'self'（没有 unsafe-inline）→ 内联 <script> 会被静默拦掉；
     style-src 那边**有** unsafe-inline，注入 <style> 是允许的。 */
  function chip(label, kind, title) {
    return h('span', { class: 'tag' + (kind ? ' ' + kind : ''), title: title || null, text: label === null || label === undefined ? '—' : String(label) });
  }
  function button(label, onClick, kind, disabled) {
    return h('button', { class: 'btn' + (kind ? ' ' + kind : ''), type: 'button', disabled: disabled || null, onClick: onClick }, label);
  }
  function sechead(title, hint, extra) {
    return h('div', { class: 'sechead' }, [
      h('span', { class: 'stitle', text: title }),
      h('span', { class: 'hr' }),
      hint ? h('span', { class: 'hint', text: hint }) : null,
      extra || null
    ]);
  }
  function section(title, hint, body, extra) {
    return h('div', { class: 'sec' }, [sechead(title, hint, extra), body]);
  }
  function renderIcon(name, extraClass) {
    if (window.PrismUI && typeof window.PrismUI.icon === 'function') {
      return window.PrismUI.icon(name, extraClass);
    }
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 16 16');
    svg.setAttribute('width', '16');
    svg.setAttribute('height', '16');
    svg.setAttribute('class', 'ui-icon' + (extraClass ? ' ' + extraClass : ''));
    return svg;
  }
  function notice(kind, titleText, detail) {
    var icName = kind === 'bad' ? 'bad' : kind === 'warn' ? 'warn' : 'info';
    return h('div', { class: 'notice' + (kind ? ' ' + kind : '') }, [
      h('span', { class: 'k' }, [renderIcon(icName)]),
      h('span', null, [h('b', { text: titleText }), detail ? h('span', { text: ' ' + detail }) : null])
    ]);
  }
  function statebox(ic, titleText, detail, actions) {
    var icEl = h('span', { class: 'ic' });
    if (ic === '∅' || ic === 'empty') icEl.appendChild(renderIcon('empty'));
    else if (ic === '◌' || ic === 'spinner' || ic === 'loading') icEl.appendChild(renderIcon('spinner'));
    else if (ic === '!' || ic === 'bad') icEl.appendChild(renderIcon('bad'));
    else if (ic === '▲' || ic === 'warn') icEl.appendChild(renderIcon('warn'));
    else if (typeof ic === 'string' && ic.length > 2) icEl.appendChild(renderIcon(ic));
    else if (ic instanceof Node) icEl.appendChild(ic);
    else icEl.textContent = ic;

    return h('div', { class: 'statebox' }, [
      icEl,
      h('span', { class: 't', text: titleText }),
      detail ? h('span', { class: 'd', text: detail }) : null,
      actions && actions.length ? h('span', { class: 'do' }, actions) : null
    ]);
  }
  function table(cols, rows, emptyNode) {
    if (!rows.length && emptyNode) return emptyNode;
    var thead = h('tr', null, cols.map(function (c) { return h('th', { class: c.cls || null, text: c.t }); }));
    var tbody = h('tbody', null, rows.map(function (r) {
      return h('tr', null, r.map(function (cell) {
        var td = h('td', { class: (cell && cell.cls) || null });
        if (cell instanceof Node) td.appendChild(cell);
        else if (cell && typeof cell === 'object' && 'node' in cell) td.appendChild(cell.node);
        else if (cell && typeof cell === 'object' && 'chip' in cell) td.appendChild(chip(cell.chip, cell.kind));
        else if (cell && typeof cell === 'object' && 'text' in cell) td.textContent = String(cell.text);
        else td.textContent = cell === null || cell === undefined ? '—' : String(cell);
        return td;
      }));
    }));
    return h('div', { class: 'tw' }, h('table', { class: 't' }, [h('thead', null, thead), tbody]));
  }
  // .sw2 是 app.css 里的开关结构（设置页那套），不用原生复选框
  function sw2(label, on, onchange) {
    var box = h('span', { class: 'sw2' + (on ? ' on' : ''), title: label }, [h('i'), h('b', { text: label })]);
    box.addEventListener('click', function () {
      on = !on;
      box.classList.toggle('on', on);
      onchange(on);
    });
    return box;
  }

  /* ── 状态 ───────────────────────────────────────────────────── */
  var S = {
    root: null,
    ctx: null,
    timer: null,
    auto: true,
    hideMgmt: true,
    levelFilter: 'all',  // 'all' | 'error' | 'warn'
    unreadCount: 0,
    pinBottom: true,
    query: '',
    queryBad: null,
    raw: [],           // 原始行缓冲
    filtered: [],      // 当前筛选后的行
    hiddenCount: 0,
    errCount: 0,
    lastAt: null,
    err: null,
    gap: false,
    busy: false,
    filterKey: '',
    confirmClear: false,
    confirmTimer: null,
    errorFiles: null,
    errorErr: null,
    truncated: [],     // 被服务端截断过的错误日志文件名（靠 X-Prism-Truncated 判定）
    logTruncated: false,  // 本轮 /api/logs 的正文被服务端截断过（响应头 X-Prism-Truncated / data.truncated）
    truncProbed: {},   // name:size → 已探过，避免每 3s 重复发请求
  };

  /* ── 行解析与筛选 ───────────────────────────────────────────── */
  // 网关格式：[ts] [trace] [level] [file:line] message
  var LINE_RE = /^\[([^\]]*)\]\s*\[([^\]]*)\]\s*\[([^\]]*)\]\s*\[([^\]]*)\]\s?([\s\S]*)$/;

  function parseLine(text) {
    var m = LINE_RE.exec(text);
    if (m) {
      var lvl = m[3].trim().toLowerCase();
      return { ts: m[1], sev: /^(error|fatal|panic)$/.test(lvl) ? 'e' : /^(warn|warning)$/.test(lvl) ? 'w' : '' };
    }
    // 格式对不上（比如上游贴进来的多行堆栈）也别丢，按裸文本渲染
    if (/\b(panic|fatal|error|failed)\b/i.test(text) || /\|\s*5\d\d\s*\|/.test(text)) return { ts: '', sev: 'e' };
    if (/\bwarn(ing)?\b/i.test(text)) return { ts: '', sev: 'w' };
    return { ts: '', sev: '' };
  }

  // 支持 /正则/ 写法；写错了退回子串匹配并给出提示，不抛错打断整页
  function makeMatcher(q) {
    if (!q) return null;
    if (q.length > 2 && q[0] === '/' && q[q.length - 1] === '/') {
      try { var re = new RegExp(q.slice(1, -1), 'i'); return function (s) { return re.test(s); }; }
      catch (e) { return { bad: e.message }; }
    }
    var low = q.toLowerCase();
    return function (s) { return s.toLowerCase().indexOf(low) >= 0; };
  }

  function applyFilter() {
    var m = makeMatcher(S.query);
    if (m && m.bad) { S.queryBad = m.bad; m = null; } else { S.queryBad = null; }

    var hidden = 0, errs = 0, out = [];
    for (var i = 0; i < S.raw.length; i++) {
      var line = S.raw[i];
      if (S.hideMgmt && MGMT_RE.test(line)) { hidden++; continue; }
      var p = parseLine(line);
      if (p.sev === 'e') errs++;
      if (S.levelFilter === 'error' && p.sev !== 'e') continue;
      if (S.levelFilter === 'warn' && p.sev !== 'w' && p.sev !== 'e') continue;
      if (m && !m(line)) continue;
      out.push(line);
    }
    S.filtered = out;
    S.hiddenCount = hidden;
    S.errCount = errs;
    S.filterKey = S.query + '\u0000' + (S.hideMgmt ? '1' : '0') + '\u0000' + S.levelFilter;
  }

  /* ── 渲染 ───────────────────────────────────────────────────── */
  function render() {
    if (!S.root) return;
    var atBottom = atLogBottom();
    // 必须先重算：切「隐藏管理流量」时 S.hideMgmt 变了但 S.filtered 还是旧的，
    // 只重建 DOM 会看到"开关动了、内容没动"
    applyFilter();

    var r = clear(S.root);
    r.appendChild(section('日志', null, h('div', null, [
      toolbar(),
      notes(),
      logView(),
      footbar()
    ])));
    r.appendChild(errorSection());
    if (atBottom) scrollLogToEnd();
  }

  var filterDebounceTimer = null;
  function toolbar() {
    var input = h('input', {
      type: 'search', size: 28, placeholder: '搜索日志', value: S.query,
      oninput: function (e) {
        S.query = e.target.value;
        if (filterDebounceTimer) clearTimeout(filterDebounceTimer);
        filterDebounceTimer = setTimeout(function () {
          filterDebounceTimer = null;
          renderBody();
        }, 70);
      }
    });

    var kids = [
      h('span', { class: 'f' }, input),
      h('span', { class: 'spacer' }),
      clearButton()
    ];
    if (S.confirmClear) {
      kids.push(button('取消', function () { S.confirmClear = false; clearTimeout(S.confirmTimer); render(); }));
    }
    // id 是为了轮询时能就地改禁用态。搜索框所在的整条工具条不再被 swap 重建
    // （重建 = 换掉 input 节点 = 光标和焦点全丢），见 incremental()
    kids.push(h('button', { class: 'btn', type: 'button', id: 'pm-btn-refresh',
      disabled: S.busy || null, onclick: function () { pull(true); } }, '刷新'));
    kids.push(sw2('自动刷新', S.auto, function (v) { S.auto = v; schedule(); }));
    kids.push(sw2('隐藏管理流量', S.hideMgmt, function (v) { S.hideMgmt = v; render(); }));
    kids.push(sw2('滚屏锁定', S.pinBottom, function (v) {
      S.pinBottom = v;
      if (v) { S.unreadCount = 0; scrollLogToEnd(); updateBadge(); }
    }));
    return h('div', { class: 'toolbar', id: 'pm-logbar' }, kids);
  }

  function clearButton() {
    // 有壳就用壳的模态（和别的页一致），没有就退化成按钮两段式确认
    return button(S.confirmClear ? '确认清空' : '清空日志', function () {
      if (S.confirmClear) { return doClear(); }
      if (S.ctx && typeof S.ctx.dialog === 'function') {
        S.ctx.dialog({
          title: '清空网关日志',
          body: '会删掉网关当前保留的日志内容，清空后无法恢复。确认继续？',
          okText: '清空', danger: true
        }).then(function (yes) { if (yes) doClear(); });
        return;
      }
      S.confirmClear = true;
      S.confirmTimer = setTimeout(function () { S.confirmClear = false; render(); }, 6000);
      render();
    }, S.confirmClear ? 'danger' : '');
  }

  function doClear() {
    S.confirmClear = false;
    clearTimeout(S.confirmTimer);
    return apiSend('DELETE', 'api/logs').then(function () {
      S.raw = []; S.filtered = []; S.hiddenCount = 0; S.errCount = 0; S.gap = false; S.err = null;
      if (S.ctx && S.ctx.toast) S.ctx.toast('日志已清空', 'ok');
      render();
    }, function (e) {
      S.err = shortErr(e);
      if (S.ctx && S.ctx.toast) S.ctx.toast('清空失败：' + S.err, 'bad');
      render();
    });
  }

  // 提示区单独成块，搜索时能就地替换而不动工具条（否则输入框会失焦）
  function notes() {
    var box = h('div', { id: 'pm-notes' });
    if (S.queryBad) box.appendChild(notice('bad', '正则语法错误：', S.query + '（' + S.queryBad + '）'));
    // 服务端在读主日志时撞到字节上限才置这个标志：这时候给回的行数比要的少，
    // 界面必须说出来，不然用户会把"只有这几行"当成"日志就这么多"。
    if (S.logTruncated) {
      box.appendChild(notice('warn', '日志已截断',
        '单次读取达上限（' + size(TAIL_LIMIT) + '），完整内容请查看 logs/main.log。'));
    }
    if (S.gap) box.appendChild(notice('warn', '日志可能存在间断', '清空缓冲可重新同步。'));
    if (S.truncated.length) {
      box.appendChild(notice('warn', '错误日志已截断',
        '文件 ' + S.truncated.join('、') + ' 超过上限（' + size(TRUNC_LIMIT) + '），仅显示前部分。'));
    }
    return box;
  }

  var currentMatches = [];
  var currentMatchIdx = 0;

  function jumpMatch(idx) {
    if (!currentMatches.length) return;
    currentMatches.forEach(function (m) { m.classList.remove('active'); });
    currentMatchIdx = (idx + currentMatches.length) % currentMatches.length;
    var cur = currentMatches[currentMatchIdx];
    cur.classList.add('active');
    if (typeof cur.scrollIntoView === 'function') {
      cur.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
    var countEl = document.getElementById('pm-log-search-count');
    if (countEl) countEl.textContent = (currentMatchIdx + 1) + '/' + currentMatches.length;
  }

  function updateBadge() {
    var badge = document.getElementById('pm-log-unread');
    if (!badge) return;
    if (S.unreadCount > 0) {
      badge.textContent = '⬇ ' + S.unreadCount + ' 条新日志';
      badge.classList.add('visible');
    } else {
      badge.classList.remove('visible');
    }
  }

  function logView() {
    if (!S.raw.length) {
      return statebox('empty', S.err ? '未获取到日志' : '暂无日志');
    }
    if (!S.filtered.length) {
      return statebox('empty', '无匹配日志');
    }

    currentMatches = [];
    currentMatchIdx = 0;
    var from = Math.max(0, S.filtered.length - RENDER_MAX);
    var box = h('div', { class: 'logview', id: 'pm-logview' });
    var frag = document.createDocumentFragment();
    for (var i = from; i < S.filtered.length; i++) {
      frag.appendChild(lineEl(S.filtered[i], i + 1, currentMatches));
    }
    box.appendChild(frag);

    box.addEventListener('scroll', function () {
      if (atLogBottom()) {
        S.unreadCount = 0;
        updateBadge();
      }
    });

    var badge = h('button', {
      class: 'btn sm pri log-new-badge' + (S.unreadCount > 0 ? ' visible' : ''),
      id: 'pm-log-unread',
      type: 'button',
      onclick: function () {
        S.unreadCount = 0;
        updateBadge();
        scrollLogToEnd();
      }
    }, '⬇ ' + S.unreadCount + ' 条新日志');

    var wrapKids = [box, badge];

    if (S.query && !S.queryBad && currentMatches.length > 0) {
      var countSpan = h('span', { class: 'log-nav-count', id: 'pm-log-search-count', text: '1/' + currentMatches.length });
      var prevBtn = h('button', {
        class: 'log-nav-btn', type: 'button', title: '上一个匹配项 (Shift+F3)',
        onclick: function (e) { e.preventDefault(); e.stopPropagation(); jumpMatch(currentMatchIdx - 1); }
      }, [renderIcon('triangle')]);
      var nextBtn = h('button', {
        class: 'log-nav-btn', type: 'button', title: '下一个匹配项 (F3)',
        onclick: function (e) { e.preventDefault(); e.stopPropagation(); jumpMatch(currentMatchIdx + 1); }
      }, [renderIcon('triangle-down')]);
      var navFloat = h('div', { class: 'log-search-nav' }, [
        h('span', { class: 'log-nav-lbl', text: '匹配' }),
        countSpan,
        prevBtn,
        nextBtn
      ]);
      wrapKids.push(navFloat);
      setTimeout(function () { jumpMatch(0); }, 20);
    }

    return h('div', { class: 'logview-wrap' }, wrapKids);
  }

  // 行号 + 正文；级别只上色，不额外堆列——和 app.css 的 .logview 结构一致
  function lineEl(text, no, matchesArr) {
    var p = parseLine(text);
    var row = h('span', { class: 'ln' + (p.sev ? ' ' + p.sev : '') }, h('span', { class: 'no', text: String(no) }));
    if (S.query && !S.queryBad) highlightInto(row, text, S.query, matchesArr);
    else row.appendChild(document.createTextNode(text));
    return row;
  }

  function highlightInto(target, text, q, matchesArr) {
    var re = null;
    if (q.length > 2 && q[0] === '/' && q[q.length - 1] === '/') {
      try { re = new RegExp('(' + q.slice(1, -1) + ')', 'gi'); } catch (e) { re = null; }
    } else {
      re = new RegExp('(' + q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
    }
    if (!re) { target.appendChild(document.createTextNode(text)); return; }
    var last = 0, m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) target.appendChild(document.createTextNode(text.slice(last, m.index)));
      var hl = h('span', { class: 'hl', text: m[0] });
      if (matchesArr) matchesArr.push(hl);
      target.appendChild(hl);
      last = m.index + m[0].length;
      if (m[0] === '') re.lastIndex++;
    }
    if (last < text.length) target.appendChild(document.createTextNode(text.slice(last)));
  }

  function footbar() {
    return h('div', { id: 'pm-foot', style: 'display:none;' });
  }

  /* ── 错误日志下载 ───────────────────────────────────────────── */
  // INTERFACES.md 的接口表里没写这条；server.py 实测提供 /api/error-logs。
  // 仍然留一次回退探测，宿主实现不同名也能用；两个都不通就说明白，不装。
  var ERROR_LIST_URLS = ['api/error-logs', 'api/logs/error-logs'];

  function loadErrorFiles() {
    S.errorFiles = null;
    S.errorErr = null;
    S.truncated = [];         // 重新列一遍就重新判：文件可能已经被删掉或轮转了
    var tryNext = function (i, lastErr) {
      if (i >= ERROR_LIST_URLS.length) {
        S.errorErr = '后端没有错误日志接口（试过 /' + ERROR_LIST_URLS.join(' 与 /') + '）：' + (lastErr || 'HTTP 404');
        render();
        return;
      }
      apiGet(ERROR_LIST_URLS[i]).then(function (d) {
        var files = (d && Array.isArray(d.files)) ? d.files : (Array.isArray(d) ? d : []);
        S.errorFiles = { files: files, base: ERROR_LIST_URLS[i], dir: d && d.dir ? d.dir : '' };
        render();
        probeTruncated();
      }, function (e) {
        if (e && (e.status === 404 || e.status === 405)) tryNext(i + 1, shortErr(e));
        else { S.errorErr = shortErr(e); render(); }
      });
    };
    tryNext(0, null);
  }

  // 列表里的「下载」是 <a download>，响应头不进 JS，截断与否只能自己发一次请求看。
  // 只探体积已经超过 2MB 上限的文件（小于上限的服务器不截断，探了也是白探），
  // 每个文件每份大小只探一次；头到手就把正文流取消，不把几 MB 读进内存。
  function probeTruncated() {
    if (!S.errorFiles || !S.errorFiles.files) return;
    for (var i = 0; i < S.errorFiles.files.length; i++) {
      var f = S.errorFiles.files[i];
      var name = String(f.name || '');
      if (!name) continue;
      if (typeof f.size !== 'number' || f.size <= TRUNC_LIMIT) continue;
      if (S.truncProbed[name + ':' + f.size]) continue;
      S.truncProbed[name + ':' + f.size] = true;
      probeOne(name);
    }
  }

  function probeOne(name) {
    if (typeof fetch !== 'function' || typeof AbortController !== 'function') return;
    var ac = new AbortController();
    fetch(S.errorFiles.base + '/' + encodeURIComponent(name), { signal: ac.signal, cache: 'no-store' })
      .then(function (r) {
        var v = r.headers.get('X-Prism-Truncated');
        try { if (r.body && typeof r.body.cancel === 'function') r.body.cancel(); } catch (e) { /* 取消不掉也不影响判断 */ }
        ac.abort();
        return v;
      }, function () { return null; })
      .then(function (v) {
        if (v !== '1' && v !== 'true') return;
        if (S.truncated.indexOf(name) < 0) S.truncated.push(name);
        if (S.root) swap('#pm-notes', notes);
      });
  }

  function errorSection() {
    var extra = button('刷新列表', function () { loadErrorFiles(); }, 'sm');
    var sec = section('错误日志', null, errorBody(), extra);
    return sec;
  }

  function errorBody() {
    if (S.errorErr) return notice('bad', '错误日志不可用：' + S.errorErr, '正文仍可用。');
    if (!S.errorFiles) return statebox('spinner', '正在读取错误日志列表…', '');
    var files = S.errorFiles.files;
    if (!files.length) return statebox('empty', '暂无错误日志文件');

    var rows = files.map(function (f) {
      var name = String(f.name || '');
      var big = typeof f.size === 'number' && f.size > 1024 * 1024;   // 单个文件实测能到 3.19MB
      var href = S.errorFiles.base + '/' + encodeURIComponent(name);
      // 用原生 download 而不是 fetch 进内存：3MB 的文件走浏览器下载更省事
      var link = h('a', { class: 'btn sm', href: href, download: name, target: '_blank', rel: 'noopener' }, '下载');
      return [
        { text: name, cls: 'n' },
        { node: big ? chip('大文件', 'warn', '文件较大，可能已截断') : chip('正常') },
        { text: size(f.size), cls: 'r' },
        { text: mtime(f.modified) },
        { node: link, cls: 'act' }
      ];
    });

    return h('div', null, [
      table(
        [{ t: '文件' }, { t: '状态' }, { t: '大小', cls: 'r' }, { t: '修改时间' }, { t: '操作', cls: 'act' }],
        rows, null)
    ]);
  }

  /* ── 取数 ───────────────────────────────────────────────────── */
  function apiGet(path) {
    if (S.ctx && S.ctx.api && typeof S.ctx.api.get === 'function') return S.ctx.api.get(path);
    return rawFetch(path, 'GET');
  }
  function apiSend(method, path) {
    if (S.ctx && S.ctx.api && typeof S.ctx.api.del === 'function' && method === 'DELETE') return S.ctx.api.del(path);
    return rawFetch(path, method);
  }
  function rawFetch(path, method) {
    var ctl = (typeof AbortController === 'function') ? new AbortController() : null;
    var timer = null;
    var init = { method: method, headers: { 'Accept': 'application/json', 'X-Prism-Token': sessionStorage.getItem('prism-ct') || '' }, cache: 'no-store' };
    if (ctl) { init.signal = ctl.signal; timer = setTimeout(function () { ctl.abort(); }, FETCH_TIMEOUT_MS); }
    function stop() { if (timer) { clearTimeout(timer); timer = null; } }
    return fetch('/' + path, init).then(function (r) {
      // 只有自己发请求才看得到响应头。挂到 data 上（不可枚举，不污染页面拿到的对象），
      // pull() 直接读；拿不到头的那条路（壳的 ctx.api）读服务端给的 JSON 副本。
      var trunc = r.headers.get('X-Prism-Truncated');
      return r.text().then(function (t) {
        stop();
        var body = null;
        try { body = t ? JSON.parse(t) : null; } catch (e) { body = null; }
        if (body && typeof body === 'object' && 'ok' in body) {
          if (body.ok) return tagTruncated(body.data, trunc);
          var err = new Error(body.error || ('请求失败（HTTP ' + r.status + '）'));
          err.status = r.status;
          throw err;
        }
        if (!r.ok) { var e2 = new Error('请求失败：HTTP ' + r.status); e2.status = r.status; throw e2; }
        return tagTruncated(body, trunc);
      });
    }, function (err) {
      stop();
      if (err && err.name === 'AbortError') throw new Error('请求超时（' + (FETCH_TIMEOUT_MS / 1000) + ' 秒）：' + path);
      // 页面就是从控制台服务那儿加载的，location.host 就是该找的端口。以前这里写死
      // 8318，改了 PRISM_CONSOLE_PORT 的机器上这句会把人指错地方。
      var where = location.host ? '（' + location.host + '）' : '';
      throw new Error('无法连接控制台服务' + where);
    });
  }

  function tagTruncated(data, header) {
    if (data && typeof data === 'object') {
      try {
        Object.defineProperty(data, '_truncated', { value: header === '1', enumerable: false });
      } catch (e) { /* 挂不上就算了，pull() 会退回 data.truncated */ }
    }
    return data;
  }

  // /api/logs 自己发请求，为的是能读到 X-Prism-Truncated（壳的 ctx.api.get 只把 data
  // 交出来，响应头进不来）。file:// 下没有 fetch 的用武之地，退回壳，让它给出那句
  // "请用 Prism 窗口打开"的说明。
  function getLogs(limit) {
    var path = 'api/logs?limit=' + limit;
    if (typeof fetch !== 'function' || location.protocol === 'file:') return apiGet(path);
    return rawFetch(path, 'GET');
  }

  /* ── 增量合并 ───────────────────────────────────────────────── */
  // 网关只给尾部 N 行，两次之间会重叠。找最长的"旧尾部 == 新头部"来确定新增段。
  function mergeTail(old, inc) {
    if (!inc.length) return { add: [], gap: false };
    if (!old.length) return { add: inc.slice(), gap: false };
    var max = Math.min(old.length, inc.length);
    for (var k = max; k > 0; k--) {
      var ok = true;
      for (var i = 0; i < k; i++) {
        if (old[old.length - k + i] !== inc[i]) { ok = false; break; }
      }
      if (ok) return { add: inc.slice(k), gap: false };
    }
    return { add: inc.slice(), gap: true };
  }

  function pull(manual) {
    if (!S.root || S.busy) return Promise.resolve();
    S.busy = true;
    if (manual) render();
    return getLogs(TAIL).then(function (d) {
      // 飞行中的轮询响应可能在切页（unmount 清缓冲）之后才回来，那时再 push 会把刚清掉的
      // 缓冲重新填上，下次进来先渲染一份**上一挂载的残留**（复查轮 6 的 4-2）。
      if (!S.root) return;
      var lines = (d && Array.isArray(d.lines)) ? d.lines.map(String) : [];
      // 截断标志有两个来源，谁在就用谁：自己发的请求挂在 data._truncated（响应头原文），
      // 走壳的时候读 data.truncated —— server.py 里这两个就是同一个布尔值，头给不看 JSON
      // 的调用方，JSON 字段给看不到响应头的调用方。
      S.logTruncated = (d && typeof d._truncated === 'boolean') ? d._truncated : !!(d && d.truncated);
      var merged = mergeTail(S.raw, lines);
      var m = makeMatcher(S.query);
      if (m && m.bad) m = null;
      var sameFilter = (S.query + '\u0000' + (S.hideMgmt ? '1' : '0') + '\u0000' + S.levelFilter) === S.filterKey;

      var appended = null;   // null 表示"这一轮必须整块重画"
      if (sameFilter) {
        appended = [];
        for (var i = 0; i < merged.add.length; i++) {
          var line = merged.add[i];
          if (S.hideMgmt && MGMT_RE.test(line)) { S.hiddenCount++; continue; }
          var pl = parseLine(line);
          if (pl.sev === 'e') S.errCount++;
          if (S.levelFilter === 'error' && pl.sev !== 'e') continue;
          if (S.levelFilter === 'warn' && pl.sev !== 'w' && pl.sev !== 'e') continue;
          if (m && !m(line)) continue;
          appended.push(line);
        }
        if (S.filterKey === '') S.filterKey = S.query + '\u0000' + (S.hideMgmt ? '1' : '0') + '\u0000' + S.levelFilter;
      }

      Array.prototype.push.apply(S.raw, merged.add);
      if (S.raw.length > KEEP) {
        S.raw = S.raw.slice(S.raw.length - KEEP);
        appended = null;   // 缓冲被裁剪，DOM 与缓冲对不上，重算最稳
      }
      if (appended) {
        Array.prototype.push.apply(S.filtered, appended);
        if (S.filtered.length > RENDER_MAX * 2) appended = null;
      } else {
        applyFilter();
      }

      S.lastAt = new Date();
      S.err = null;
      S.gap = merged.gap;
      S.busy = false;
      incremental(appended);
      schedule();
    }, function (e) {
      S.err = shortErr(e);
      S.busy = false;
      // 失败会一直失败，每 3s 一次 render() 就是每 3s 换掉搜索框那个 input 节点，
      // 正在打字的人照样丢字——跟成功路径是同一个坑，只是换了个分支。
      // renderBody() 只换提示区/日志区/底栏，工具条原地不动。
      renderBody();
      patchToolbar();
      schedule();
    });
  }

  // 增量追加：只加新的行，避免每 3s 重建几千个 DOM 节点
  function incremental(appended) {
    if (!S.root) return;
    if (!appended) { render(); return; }        // 走的是"整块重算"分支
    var box = document.getElementById('pm-logview');
    // 空态（筛选不到任何行）切回有行：只能重画日志区，**不能**调 render() ——
    // render() 会 clear 整个 sec 再重建，把搜索框那个 input 节点一起换掉，
    // 焦点和已输入的字符全丢。实测：刚开始输一个新词、还没匹配上时每一轮
    // 轮询都会换一次输入框，"prismTest!" 只落进 "pris"（匹配得上的词反而不丢）。
    if (!box) { renderBody(); return; }

    if (appended.length) {
      var atBottom = atLogBottom();
      var frag = document.createDocumentFragment();
      var base = S.filtered.length - appended.length;
      for (var i = 0; i < appended.length; i++) frag.appendChild(lineEl(appended[i], base + i + 1));
      box.appendChild(frag);
      while (box.childNodes.length > RENDER_MAX) box.removeChild(box.firstChild);
      if (S.pinBottom || atBottom) {
        scrollLogToEnd();
        S.unreadCount = 0;
      } else {
        S.unreadCount += appended.length;
      }
      updateBadge();
    }
    swap('#pm-notes', notes);
    // 工具条不给 swap：它里面有搜索框，整块换掉就是换掉那个 input 节点，
    // 焦点和光标位置跟着一起没。实测 350ms/字符输入时每 3s 断一次，
    // "gin_logger" 只落进去 8 个字符。轮询里唯一会变的是「刷新」的禁用态。
    patchToolbar();
    swap('#pm-foot', footbar);
  }

  // 只碰会变的那一个按钮；工具条其余部分在轮询前后是恒等的
  function patchToolbar() {
    var btn = document.getElementById('pm-btn-refresh');
    if (btn) btn.disabled = !!S.busy;
  }

  function swap(sel, make) {
    var old = S.root.querySelector(sel);
    if (old) old.parentNode.replaceChild(make(), old);
  }

  // 只搜索框重画：保留光标和输入焦点
  function renderBody() {
    if (!S.root) return;
    applyFilter();
    swap('#pm-notes', notes);
    var old = document.getElementById('pm-logview');
    var oldWrap = old ? old.closest('.logview-wrap') : S.root.querySelector('.statebox');
    if (oldWrap) oldWrap.parentNode.replaceChild(logView(), oldWrap);
    swap('#pm-foot', footbar);
  }

  function atLogBottom() {
    var box = document.getElementById('pm-logview');
    return box ? (box.scrollHeight - box.scrollTop - box.clientHeight < 40) : true;
  }
  function scrollLogToEnd() {
    var box = document.getElementById('pm-logview');
    if (box) box.scrollTop = box.scrollHeight;
  }

  function schedule() {
    if (S.timer) { clearTimeout(S.timer); S.timer = null; }
    if (!S.auto || !S.root) return;
    S.timer = setTimeout(function () {
      S.timer = null;
      if (document.hidden) schedule();
      else pull(false);
    }, POLL_MS);
  }

  function onLogKey(e) {
    if (e.key === 'F3') {
      e.preventDefault();
      if (e.shiftKey) jumpMatch(currentMatchIdx - 1);
      else jumpMatch(currentMatchIdx + 1);
    }
  }

  /* ── 生命周期 ───────────────────────────────────────────────── */
  var MOUNTED = false;
  function mount(root, ctx) {
    if (!root) return;
    if (MOUNTED) unmount();
    S.root = root;
    S.ctx = ctx || {};
    root.classList.add('pm-logs-page');
    MOUNTED = true;
    document.addEventListener('keydown', onLogKey);
    render();
    pull(false);
    loadErrorFiles();
    if (typeof S.ctx.onUnmount === 'function') S.ctx.onUnmount(unmount);
  }

  function unmount() {
    document.removeEventListener('keydown', onLogKey);
    if (filterDebounceTimer) { clearTimeout(filterDebounceTimer); filterDebounceTimer = null; }
    if (S.timer) { clearTimeout(S.timer); S.timer = null; }
    if (S.confirmTimer) { clearTimeout(S.confirmTimer); S.confirmTimer = null; }
    var r = S.root;
    if (r) { clear(r); r.classList.remove('pm-logs-page'); }
    S.root = null;
    S.busy = false;
    // **行缓冲也要清。** 不清的话重新进来时 pull 会拿**上一次挂载的** raw 去合并新尾部；
    // 找不到重叠（离开期间网关写了超过一屏）时整个尾部被当成"新增"追加 → 界面出现重复行，
    // 而提示文案说的却是"可能有遗漏行"（复查轮 5 的 #C）。
    S.raw = []; S.filtered = []; S.hiddenCount = 0; S.errCount = 0;
    S.filterKey = ''; S.gap = false; S.err = null; S.unreadCount = 0;
    MOUNTED = false;
  }

  var page = { id: PAGE_ID, title: '日志', mount: mount, unmount: unmount, render: mount };
  window.PrismPages = window.PrismPages || {};
  window.PrismPages[PAGE_ID] = page;
  try {
    if (window.Prism && typeof window.Prism.registerPage === 'function') window.Prism.registerPage(PAGE_ID, page);
    else if (typeof window.registerPage === 'function') window.registerPage(PAGE_ID, page);
  } catch (e) {}

  // 兜底挂载：容器只认 #page-logs（壳的导航标签是 <a data-page="logs">，不能当容器）
  function autoMount() {
    if (MOUNTED) return;
    var el = document.getElementById('page-' + PAGE_ID);
    if (el) mount(el, {});
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', autoMount);
  else autoMount();
  window.addEventListener('prism:route', function (e) {
    var d = e && e.detail;
    if (!d || d.route !== PAGE_ID || !d.el) return;
    if (MOUNTED) unmount();
    mount(d.el, d.ctx || {});
  });
  window.addEventListener('hashchange', function () {
    if ((location.hash || '').replace(/^#\/?/, '') === PAGE_ID) autoMount();
  });
})();
