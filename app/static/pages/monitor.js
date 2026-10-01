/* Prism · 监控页
 *
 * 数据来源：GET /api/monitor（见 app/INTERFACES.md）
 *   { gateway:{running,port,version,latest,version_stale,config_ok,
 *              identity,identity_note,identity_config},
 *     routing:{selected:{gpt,deepseek,glm}, exposed_models:[...]},
 *     sources:[{id,label,kind,enabled,success,failed,cooldown,counters_since,quota_hint}] }
 *
 * 与壳的关系：
 *   - 注册走 window.PrismPages.monitor，同时尽力调 Prism.registerPage（两个壳都认）。
 *   - 壳每 5 秒已经在轮询 /api/monitor 并 bus.emit('monitor', data) 了。
 *     所以有 ctx.bus 时本页**不再自己轮询**——两个轮询器一起敲网关只会让它更热，
 *     而网关连续 401 是会封本机 IP 的（见 plan 修正 B11）。没有壳时才自带轮询。
 *   - 清理走 ctx.onUnmount（壳的约定）外加返回 unmount（两边都安全）。
 *
 * 样式：全部用 app.css 的类名（.sec/.sechead/.tag/.btn/.tw/table.t/.statebox/
 * .notice/.card/.kv/.cols/.toolbar/.spacer/.sw2），和配置/用量/设置三页一致。
 * 不注入 <style>、不写 style="" 是照 DEV-RULES A1（设计系统只有 app.css 一份），
 * **不是** CSP 挡的：server.py 的 CSP 是
 *   script-src 'self'（无 unsafe-inline）→ 内联 <script> 会被静默拦掉；
 *   style-src 'self' 'unsafe-inline' → 内联样式与注入 <style> 都是允许的。
 */
(function () {
  'use strict';

  var PAGE_ID = 'monitor';
  var REFRESH_MS = 10000;   // 只有"没有壳"的独立场景才用到

  /* ── 小工具 ─────────────────────────────────────────────────── */
  function h(tag, attrs) {
    var el = document.createElement(tag);
    var kids = Array.prototype.slice.call(arguments, 2);
    if (attrs) for (var k in attrs) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = String(v);
      else if (k === 'style') el.setAttribute('style', v);
      else if (k.slice(0, 2) === 'on') el.addEventListener(k.slice(2).toLowerCase(), v);
      else el.setAttribute(k, v === true ? '' : String(v));
    }
    add(el, kids);
    return el;
  }
  function add(el, kids) {
    for (var i = 0; i < kids.length; i++) {
      var c = kids[i];
      if (c === null || c === undefined || c === false || c === '') continue;
      if (Array.isArray(c)) { add(el, c); continue; }
      el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
  // null/undefined 一律显示 —：宁可显示"没有"，也不要编一个 0 出来
  function num(v) { return (v === null || v === undefined || v === '') ? '—' : String(v); }
  function clock(d) {
    function p(n) { return (n < 10 ? '0' : '') + n; }
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }
  function dur(sec) {
    if (typeof sec !== 'number' || !isFinite(sec) || sec <= 0) return null;
    if (sec < 60) return Math.round(sec) + 's';
    if (sec < 3600) return Math.round(sec / 60) + 'm';
    return Math.floor(sec / 3600) + 'h' + Math.round((sec % 3600) / 60) + 'm';
  }
  // 冷却的渲染。后端（health._cooldown_of）给的是**对象** {until, reason}，不是秒数 ——
  // 原先只把它交给 dur()，而 dur() 对非数字一律返回 null，于是 OAuth 来源真有冷却时
  // 那一列也永远显示 —（审查 N3-02）。数字形状继续照收。
  function cooldownText(c) {
    if (c === null || c === undefined) return null;
    if (typeof c === 'number') return dur(c);
    if (typeof c !== 'object') return String(c);
    if (typeof c.remaining_seconds === 'number') return dur(c.remaining_seconds);
    var until = c.until || null;
    if (until) {
      // until 可能是 ISO 串，也可能是 epoch 秒/毫秒 —— 都试一遍；算不出剩余就原样显示
      // （总比显示 — 强：— 的意思是"取不到"，而这里我们取到了）
      var t = Date.parse(until);
      if (!isFinite(t) && /^\d+$/.test(String(until))) {
        var n = Number(until);
        t = n > 1e12 ? n : n * 1000;
      }
      if (isFinite(t)) {
        var left = Math.round((t - Date.now()) / 1000);
        return left > 0 ? dur(left) : '已到期';
      }
      return String(until);
    }
    return c.reason ? String(c.reason) : '冷却中';
  }
  function shortErr(e) {
    if (!e) return '未知错误';
    if (e.name === 'ApiError' || e.status !== undefined) return e.message || String(e);
    return (e.message || String(e)).slice(0, 200);
  }

  /* ── 公共组件（类名照 app.js 的 ui.*，视觉才和别的页一致） ───────
   * 只用 app.css 的类，不注入 <style>、不写 style=""：
   * server.py 的 CSP 是 script-src 'self'（没有 unsafe-inline）→ 内联 <script> 会被静默
   * 拦掉；style-src 那边**有** unsafe-inline，内联样式与注入 <style> 都是允许的。
   * （本页只用 class 是风格选择，不是被 CSP 逼的。） */
  function chip(label, kind, title) {
    return h('span', { class: 'tag' + (kind ? ' ' + kind : ''), title: title || null, text: label === null || label === undefined ? '—' : String(label) });
  }
  function button(label, onClick, kind, disabled) {
    return h('button', { class: 'btn' + (kind ? ' ' + kind : ''), type: 'button', disabled: disabled || null, onClick: onClick }, label);
  }
  function sechead(title, hint) {
    return h('div', { class: 'sechead' }, [
      h('span', { class: 'cmt', text: '//' }),
      h('span', { class: 'stitle', text: title }),
      h('span', { class: 'hr' }),
      hint ? h('span', { class: 'hint', text: hint }) : null
    ]);
  }
  function section(title, hint, body) {
    return h('div', { class: 'sec' }, [sechead(title, hint), body]);
  }
  // 把正文里的 Windows 路径摘出来套等宽字体。身份不符那条一句里塞了两个长路径，
  // 混在中文正文里既不好认、也不能在窄窗口里断行。只认盘符开头的串（D:\a\b），
  // 遇到空格或中文标点就停——后端 note 的路径后面跟的是「，」。
  var PATH_RE = /[A-Za-z]:\\[^\s，；。、）)】\]]+/g;
  function withPaths(text) {
    var frag = document.createDocumentFragment(), last = 0, m;
    PATH_RE.lastIndex = 0;
    while ((m = PATH_RE.exec(text)) !== null) {
      if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
      frag.appendChild(h('code', { class: 'pth', text: m[0] }));
      last = m.index + m[0].length;
    }
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    return frag;
  }

  // bullets：可选的要点数组。长结论（网关身份不符那种，一句里塞两个路径 + 三段后果）
  // 挤成一行没人读得下去，拆成竖排短行。文案一个字不少，只是换行。
  function notice(kind, titleText, detail, bullets) {
    var body = h('span', null, h('b', { text: titleText }));
    if (detail) {
      body.appendChild(h('span', { text: ' ' }));
      body.appendChild(withPaths(detail));
    }
    if (bullets && bullets.length) {
      var list = h('span', { class: 'nlist' });
      for (var i = 0; i < bullets.length; i++) list.appendChild(h('span', { text: bullets[i] }));
      body.appendChild(list);
    }
    var icName = kind === 'bad' ? 'bad' : kind === 'warn' ? 'warn' : 'info';
    return h('div', { class: 'notice' + (kind ? ' ' + kind : '') }, [
      h('span', { class: 'k' }, [renderIcon(icName)]),
      body
    ]);
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
  // cell: 字符串 | Node | {chip, kind} | {node} | {text, cls}
  function table(cols, rows, emptyNode) {
    if (!rows.length && emptyNode) return emptyNode;
    var thead = h('tr', null, cols.map(function (c) {
      return h('th', { class: c.cls || null, text: c.t });
    }));
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

  // pairs: [标签, 值|Node|Node[], 尾巴Node?]
  // .kv .r 本身是 flex，所以值可以直接放多个节点（圆点就是靠这个才能渲染出尺寸）
  function kv(pairs) {
    return h('div', { class: 'kv' }, pairs.map(function (p) {
      var v = p[1];
      var cells;
      if (Array.isArray(v)) cells = v.slice();
      else if (v instanceof Node) cells = [v];
      else cells = [h('span', { class: 'v', text: v === null || v === undefined || v === '' ? '—' : String(v), title: typeof v === 'string' ? v : null })];
      if (p[2]) cells.push(p[2]);
      return h('div', { class: 'r' }, [h('span', { class: 'k', text: p[0] })].concat(cells));
    }));
  }

  /* 本页私有样式一条都不注入：不是 CSP 挡的（style-src 有 unsafe-inline，注入 <style> 允许），
     而是照 DEV-RULES A1 —— 设计系统只有 app.css 一份，页面不复制它。
     需要的视觉全部走 app.css 已有的类（.spacer/.kv/.toolbar/.tag/...）。 */

  /* ── 状态 ───────────────────────────────────────────────────── */
  var S = {
    root: null,
    ctx: null,
    data: null,          // 最近一次成功的 /api/monitor
    err: null,
    lastAt: null,
    busy: false,
    followed: false,     // 有壳在替我轮询
    auto: true,
    paused: false,
    failStreak: 0,
    offBus: null,
    timer: null
  };

  /* ── 渲染 ───────────────────────────────────────────────────── */
  function render() {
    if (!S.root) return;
    var r = clear(S.root);
    r.appendChild(toolbar());

    if (S.err && !S.data) {
      r.appendChild(statebox('bad', '读取监控数据失败', S.err + (S.paused ? '（已暂停自动刷新）' : ''),
        [button('重试', function () { S.paused = false; load(true); }, 'pri')]));
      return;
    }
    if (!S.data) { r.appendChild(statebox('spinner', '正在读取网关状态…', '')); return; }

    if (S.err) {
      r.appendChild(notice('bad', '刷新失败：' + S.err,
        S.paused ? '已暂停自动刷新，当前显示缓存数据。' : '当前显示缓存数据。'));
    }

    r.appendChild(gatewaySection(S.data.gateway || {}));
    // sampling：/api/monitor 每轮都带回来（health.monitor_state 里的 "sampling" 键），
    // 老前端没消费它。两种键名都认，免得后端改过名就静默丢掉。
    r.appendChild(routingSection(S.data.routing || {}, S.data.sampling || S.data.sample_state || null));
    r.appendChild(sourcesSection(S.data.sources || []));
  }

  function toolbar() {
    var hint;
    if (S.err) hint = '读取失败';
    else if (S.lastAt) hint = '更新于 ' + clock(S.lastAt);
    else hint = '等待首次数据';
    hint += S.followed ? ' · 5s 轮询' : ' · ' + Math.round(nextDelay() / 1000) + 's 刷新';

    var kids = [
      h('span', { class: 'cmt', text: '//' }),
      h('span', { class: 'stitle', text: '网关监控' }),
      h('span', { class: 'hint', text: hint }),
      h('span', { class: 'spacer' }),
      button('刷新', function () { load(true); }, '', S.busy)
    ];
    if (!S.followed) kids.push(sw2('自动刷新', S.auto, function (v) { S.auto = v; S.failStreak = 0; schedule(); }));
    return h('div', { class: 'toolbar' }, kids);
  }

  // 开关：照 app.css 的 .sw2 结构（设置页那套），不用原生复选框
  function sw2(label, on, onchange) {
    var box = h('span', { class: 'sw2' + (on ? ' on' : ''), title: label }, [h('i'), h('b', { text: label })]);
    box.addEventListener('click', function () {
      on = !on;
      box.classList.toggle('on', on);
      onchange(on);
    });
    return box;
  }

  /* 网关身份：端口在听 ≠ 在听的是本目录这套配置。
   *
   * 8317 是公共端口，谁坐着它跟 Prism 装在哪没关系。实测本机坐的是
   * D:\My_Agent_Proxy 那个部署——它的来源、auth-files、版本、「清空日志」删的
   * 日志，全是那边的。这一页以前只看端口，于是同一时刻托盘说"不是本目录的配置"、
   * 页面说"在线"，页面是错的那一边。
   *
   * 四态来自后端 gateway.identity（实现见 app/core/identity.py）：
   *   ok      命令行里的 -config 就是本目录的 config.yaml
   *   foreign 端口在听，但读的是别人的配置
   *   unknown 端口在听，身份读不出来（提权/受保护/跨位数）
   *   down    端口上没人听
   * unknown **不许当成 ok**。后端没给这个字段（旧版后端）时按 unknown 显示
   * ——"身份未核实"，不是"在线"。
   */
  function identityOf(g) {
    var raw = typeof g.identity === 'string' ? g.identity : '';
    if (raw === 'ok' || raw === 'foreign' || raw === 'unknown' || raw === 'down') return raw;
    return 'unknown';
  }

  function gatewaySection(g) {
    var running = g.running === true;
    var port = (g.port === null || g.port === undefined) ? 8317 : g.port;
    var ident = identityOf(g);
    var note = (typeof g.identity_note === 'string' && g.identity_note.trim()) ? g.identity_note.trim() : null;
    var conf = (typeof g.identity_config === 'string' && g.identity_config.trim()) ? g.identity_config.trim() : null;

    var verCell;
    if (!g.version) {
      verCell = h('span', { class: 'v' }, chip('未知'));
    } else {
      // 版本对比全塞进一个 .v 里：它在 .kv .r 中是 flex:1 的值位，
      // 拆成多个 .v 会各自抢空间，把后面的说明挤散
      verCell = h('span', { class: 'v' }, [
        h('span', { text: g.version }),
        g.latest ? h('span', { class: 'hint', text: ' → ' }) : null,
        g.latest ? h('span', { text: g.latest }) : null,
        g.version_stale ? h('span', { text: ' ' }) : null,
        g.version_stale ? chip('落后', 'warn', '有更新版本，重启网关可更新') : (g.latest ? chip('最新', 'ok') : null)
      ]);
    }

    // 运行状态那一格：只有 identity 是 ok 才配说"在线"。其余三种一律降级措辞
    // ——foreign 和 unknown 都用了 .dot.warn（黄色），短板说清楚它为什么不是绿的。
    var runCell, runHint;
    if (!running) {
      runCell = [h('span', { class: 'dot off' }), h('span', { class: 'v', text: '离线' })];
      runHint = '未监听，检查 cli-proxy-api 进程';
    } else if (ident === 'ok') {
      runCell = [h('span', { class: 'dot' }), h('span', { class: 'v', text: '在线' })];
      runHint = port + ' 端口可达，配置匹配';
    } else if (ident === 'foreign') {
      runCell = [h('span', { class: 'dot warn' }),
        h('span', { class: 'v', text: '在线（配置不匹配）' })];
      runHint = '端口已连接，但配置来源不一致';
    } else {
      // unknown，以及"端口通、identity 却是 down"这种自相矛盾的情况，都落这里。
      // 矛盾时宁可说"没核实"，也不能挑好听的那半句说。
      runCell = [h('span', { class: 'dot warn' }),
        h('span', { class: 'v', text: '在线（身份未核实）' })];
      runHint = '端口已连接，无法核实配置路径';
    }

    // 身份那一格的值：芯片 + 那个网关实际读的配置路径（它自己命令行里的 -config）。
    var identCell, identHint;
    if (ident === 'ok') {
      identCell = h('span', { class: 'v' }, [
        chip('本目录', 'ok', '读取当前目录 config.yaml'),
        h('span', { text: ' ' }), h('span', { text: conf || '—' })]);
      identHint = '指向当前目录';
    } else if (ident === 'foreign') {
      identCell = h('span', { class: 'v' }, [
        chip('非本目录', 'warn', '指向其他目录配置'),
        h('span', { text: ' ' }), h('span', { text: conf || '配置路径不可读' })]);
      identHint = '未指向当前目录';
    } else if (ident === 'down') {
      identCell = h('span', { class: 'v' }, chip('未运行', 'sm', '端口未监听'));
      identHint = '端口未监听';
    } else {
      identCell = h('span', { class: 'v' }, [
        chip('未核实', 'warn', '无法获取进程配置信息'),
        h('span', { text: ' ' }), h('span', { text: conf || '—' })]);
      identHint = '配置路径无法确认';
    }

    var cfgOk = g.config_ok;
    // 圆点直接当 .kv .r 的孩子：.r 是 flex，它才拿得到 6px 的尺寸
    // （app.css 的 .dot 没有 display，放在非 flex 的父级里会缩成 0 宽）
    var body = kv([
      ['运行状态', runCell, h('span', { class: 'hint', text: runHint })],
      ['网关身份', identCell, h('span', { class: 'hint', text: identHint })],
      ['端口', String(port), null],
      ['网关版本', verCell, null],
      // 三态：true=一致 / false=读不通或不一致 / null=还读不到，无从判断。
      // 前两个走绿红，"未知"单独给黄色——它会显示成"异常"的话，
      // 用户会去改 config.yaml，而实际该做的是先把网关连上。
      ['配置健康', h('span', { class: 'v' }, [chip(cfgOk === true ? 'OK' : cfgOk === false ? '异常' : '未知',
          cfgOk === true ? 'ok' : cfgOk === false ? 'bad' : 'warn',
          cfgOk === true ? 'config.yaml 与 routing-plan.json 一致'
            : cfgOk === false ? 'config.yaml 读不通，或与路由计划不一致'
              : '还读不到 config.yaml，一致性无从判断')]),
        null]
    ]);

    var sec = section('网关', '127.0.0.1:' + port, h('div', { class: 'card' }, h('div', { class: 'cardb' }, body)));
    if (!running) {
      sec.insertBefore(notice('bad', '网关未运行', '状态与计数不可用，来源列表来自本地路由计划。'), sec.lastChild);
    } else if (ident === 'foreign') {
      sec.insertBefore(notice('warn', '网关身份不符',
        note || ('端口运行的网关读取了其他配置：' + (conf || '未知路径'))), sec.lastChild);
    } else if (ident === 'unknown') {
      sec.insertBefore(notice('warn', '网关身份未核实',
        (note || '端口运行的网关配置路径无法确认。')), sec.lastChild);
    } else if (g.version_stale && g.latest) {
      sec.insertBefore(notice('warn', '网关版本落后：', g.version + ' → ' + g.latest + '，重启网关即可更新。'), sec.lastChild);
    }
    return sec;
  }

  var GROUPS = [['gpt', 'GPT 中转站'], ['deepseek', 'DEEPSEEK'], ['glm', 'GLM']];

  // 采样器（用量历史的后台线程）的现状。health.monitor_state 每轮都带回 sampling，
  // 老前端没显示：采样线程不跑了，用量页只会"不再更新"，用户看不出是为什么。
  function samplingRow(sp) {
    if (!sp || typeof sp !== 'object') return null;
    var running = sp.running === true;
    var err = sp.last_error ? String(sp.last_error) : '';
    var every = dur(sp.interval_sec);
    var detail;
    if (err) detail = '采样失败：' + err;
    else if (!running) detail = '采样线程未运行。';
    else detail = (every ? every + ' 一次' : '定期采样') +
      (sp.last_at ? ' · 最近 ' + sp.last_at : '');
    return h('div', { class: 'toolbar' }, [
      
      chip(err ? '上次失败' : running ? 'RUNNING' : 'STOPPED', err ? 'bad' : running ? 'ok' : 'warn'),
      h('span', { class: 'hint', text: detail })
    ]);
  }

  function routingSection(rt, sampling) {
    // 网关不可达时后端会在 note 里写原因，此时 selected 全是 null。null 是"读不到"，
    // 不是"没选"——丢掉 note 再渲染 null，等于替后端做了它没做的断言。
    var sel = (rt && rt.selected && typeof rt.selected === 'object') ? rt.selected : null;
    var models = (rt && Array.isArray(rt.exposed_models)) ? rt.exposed_models : null;
    var note = (rt && typeof rt.note === 'string' && rt.note.trim()) ? rt.note.trim() : null;
    // 判不了的两种情形：note 说了原因，或者字段压根没给。两者都不许说成"没有"。
    var blind = note !== null || sel === null || models === null;
    var missing = [];
    if (sel === null) missing.push('selected');
    if (models === null) missing.push('exposed_models');
    var reason = note || (blind ? '后端返回的路由数据不完整（缺 ' + missing.join('、') + '），无法判定当前路由。' : null);
    if (models === null) models = [];

    var cols = h('div', { class: 'cols' }, GROUPS.map(function (g) {
      var picked = sel ? sel[g[0]] : null;
      var unknown = (picked === null || picked === undefined) && blind;
      var state = picked === 'conflict' ? 'bad' : unknown ? 'warn' : picked ? 'ok' : '';
      var what = picked === 'conflict' ? '多个来源同时启用'
        : picked ? String(picked)
          : unknown ? '无法判定' : '未选择来源';
      return h('div', { class: 'col' }, [
        h('div', { class: 'colh' }, [
          h('span', { class: 'nm', text: g[1] }),
          chip(picked === 'conflict' ? '冲突' : unknown ? '未知' : picked ? 'ACTIVE' : '未选', state,
            unknown ? reason : null)
        ]),
        h('div', { class: 'colb', text: what })
      ]);
    }));

    var body = h('div', null, [cols]);
    var cap = h('div', { class: 'toolbar' }, [h('span', { class: 'hint', text: blind ? '已暴露模型 无法判定' : '已暴露模型 ' + models.length })]);
    body.appendChild(cap);
    if (!models.length) {
      // 读不到模型列表和"一个模型都没暴露"是两回事，别让盲区显示成空
      body.appendChild(blind
        ? statebox('bad', '无法判定已暴露模型', reason)
        : statebox('empty', '暂无已暴露模型', '请在首页保存路由。'));
    } else {
      var MAX = 80;
      // 用 .toolbar 当容器：它本身就是 flex+wrap+gap，不必再定义一套 chips 类
      body.appendChild(h('div', { class: 'toolbar' }, models.slice(0, MAX).map(function (m) {
        return chip(m, '', String(m));
      }).concat(models.length > MAX ? [chip('+' + (models.length - MAX))] : [])));
    }
    var samp = samplingRow(sampling);
    if (samp) body.appendChild(samp);

    var sec = section('路由', GROUPS.length + ' 组 · ' + (blind ? '已暴露模型无法判定' : models.length + ' 模型已暴露'), body);
    // note 是后端自己写的原话，照原样显示，不替它改写
    if (blind && reason) sec.insertBefore(notice('warn', '当前路由无法判定：', reason), sec.lastChild);
    return sec;
  }

  function sourcesSection(srcs) {
    var since = null;
    for (var i = 0; i < srcs.length; i++) {
      if (srcs[i] && srcs[i].counters_since) { since = srcs[i].counters_since; break; }
    }
    if (!since) since = '网关启动以来';

    // 启用的排前面，其次按成功数降序：用得多的来源一眼能看到
    var rows = srcs.slice().sort(function (a, b) {
      var ea = a.enabled === true ? 1 : 0, eb = b.enabled === true ? 1 : 0;
      if (ea !== eb) return eb - ea;
      return (b.success || 0) - (a.success || 0);
    }).map(function (s) {
      var oauth = s.kind === 'oauth';
      // 三态：true / false 之外还有 null——网关不可达时后端只能给 null。
      // success、failed、cooldown 取不到都显示 —，enabled 照同一规矩来；
      // 把"取不到"写成 DISABLED 是在替后端断言"这个来源被停用了"。
      var enabled = s.enabled === true ? true : (s.enabled === false ? false : null);
      var failed = (typeof s.failed === 'number') ? s.failed : null;
      var cd = cooldownText(s.cooldown);   // API key 来源本来就取不到冷却 → null → 显示 —
      return [
        { text: s.label || s.id || '（未命名）', cls: 'n' },
        // 光看来源名分不清 goat / goat-glm、srapi / srapi-deepseek（label 撞车），
        // 所以 ID 单独占一列，和用量页 BY SOURCE 补凭据尾巴是同一个目的。
        { text: s.id || '—' },
        { chip: oauth ? 'OAuth' : 'API 密钥' },
        enabled === null ? { text: '—' } : { chip: enabled ? 'ACTIVE' : 'DISABLED', kind: enabled ? 'ok' : '' },
        { text: num(s.success), cls: 'r n' },
        // 失败和冷却只在非零时上芯片：cell 里能用的颜色只有 .n（亮）和默认（暗），
        // 红色只有 .tag 有，所以状态类的东西一律走芯片
        failed ? { chip: String(s.failed), kind: 'bad', cls: 'r' } : { text: num(s.failed), cls: 'r' },
        cd ? { chip: cd, kind: 'bad' } : { text: '—' },
        { text: s.quota_hint || '—' }
      ];
    });

    var empty = statebox('empty', '暂无上游来源',
      '请在首页添加来源。');

    var sec = section('来源', srcs.length + ' 来源',
      h('div', null, [
        notice('accent', '计数为「' + since + '」的进程内累计',
          '网关重启归零；长期历史见用量页。'),
        table([
          { t: '来源' }, { t: 'ID' }, { t: '类型' }, { t: '启用' },
          { t: '成功', cls: 'r' }, { t: '失败', cls: 'r' },
          { t: '冷却' }, { t: '配额' }
        ], rows, empty)
      ]));
    return sec;
  }

  /* ── 取数 ───────────────────────────────────────────────────── */
  function apiGet(path) {
    if (S.ctx && S.ctx.api && typeof S.ctx.api.get === 'function') return S.ctx.api.get(path);
    return fetch(path, { headers: { 'Accept': 'application/json' }, cache: 'no-store' }).then(function (r) {
      return r.text().then(function (t) {
        var body = null;
        try { body = t ? JSON.parse(t) : null; } catch (e) { body = null; }
        if (body && typeof body === 'object' && 'ok' in body) {
          if (body.ok) return body.data;
          throw new Error(body.error || ('请求失败（HTTP ' + r.status + '）'));
        }
        if (!r.ok) throw new Error('请求失败：HTTP ' + r.status);
        if (body === null) throw new Error('后端返回了无法解析的内容');
        return body;
      });
    }, function () { throw new Error('连不上控制台服务。确认 8318 上的 Prism 服务在运行。'); });
  }

  // 有壳在轮询时，本页只做"点刷新"这一次额外请求
  function load(manual) {
    if (!S.root || S.busy) return Promise.resolve();
    S.busy = true;
    if (manual) render();
    return apiGet('api/monitor').then(function (d) {
      S.data = d && typeof d === 'object' ? d : {};
      S.err = null; S.paused = false; S.failStreak = 0; S.lastAt = new Date();
    }, function (e) {
      S.err = shortErr(e);
      S.failStreak++;
      // 明确说封禁就先停手：一边显示"剩余 28 分钟"一边继续敲是自相矛盾的
      if (/封禁|banned/i.test(S.err)) { S.auto = false; S.paused = true; }
    }).then(function () {
      S.busy = false;
      render();
      schedule();
    });
  }

  // 连续失败退避：网关连错 5 次 401 会封本机 IP 30 分钟（plan 修正 B11）
  function nextDelay() {
    if (!S.failStreak) return REFRESH_MS;
    return Math.min(REFRESH_MS * Math.pow(2, S.failStreak), 60000);
  }
  function schedule() {
    if (S.timer) { clearTimeout(S.timer); S.timer = null; }
    if (S.followed || !S.auto || !S.root) return;
    S.timer = setTimeout(function () {
      S.timer = null;
      if (document.hidden) schedule();
      else load(false);
    }, nextDelay());
  }

  /* ── 生命周期 ───────────────────────────────────────────────── */
  function onBusData(m) {
    if (!S.root) return;
    if (m) { S.data = m; S.err = null; S.paused = false; S.lastAt = new Date(); }
    else {
      // 壳轮询失败：保留上一次的数据，但把原因说出来
      S.err = S.ctx && S.ctx.state ? shortErr(S.ctx.state.monitorError) : '壳的轮询失败';
      if (/封禁|banned/i.test(S.err)) S.paused = true;
    }
    render();
  }

  var MOUNTED = false;
  function mount(root, ctx) {
    if (!root) return;
    if (MOUNTED) unmount();   // 被重复挂载（render/mount 都被调）时先清干净，避免叠两份
    S.root = root;
    S.ctx = ctx || {};
    root.classList.add('pm-monitor-page');
    MOUNTED = true;

    if (S.ctx.bus && typeof S.ctx.bus.on === 'function') {
      S.followed = true;
      S.offBus = S.ctx.bus.on('monitor', onBusData);
      var st = S.ctx.state || {};
      if (st.monitor) { S.data = st.monitor; S.err = null; S.lastAt = new Date(); }
      else if (st.monitorError) S.err = shortErr(st.monitorError);
      render();
      if (!S.data && !S.err) load(false);   // 首轮还没回来，自己补一次，别让页面空等
    } else {
      S.followed = false;
      render();
      load(false);
    }
    if (typeof S.ctx.onUnmount === 'function') S.ctx.onUnmount(unmount);
  }

  function unmount() {
    if (S.timer) { clearTimeout(S.timer); S.timer = null; }
    if (typeof S.offBus === 'function') { try { S.offBus(); } catch (e) {} S.offBus = null; }
    var r = S.root;
    if (r) { clear(r); r.classList.remove('pm-monitor-page'); }
    S.root = null;
    S.followed = false;
    MOUNTED = false;
  }

  var page = { id: PAGE_ID, title: '监控', mount: mount, unmount: unmount, render: mount };
  window.PrismPages = window.PrismPages || {};
  window.PrismPages[PAGE_ID] = page;
  // 壳的注册口（app.js 在解析阶段就挂上了）；两个都认，谁的壳都能起来
  try {
    if (window.Prism && typeof window.Prism.registerPage === 'function') window.Prism.registerPage(PAGE_ID, page);
    else if (typeof window.registerPage === 'function') window.registerPage(PAGE_ID, page);
  } catch (e) { /* 注册口不接受就退回 PrismPages，onload 时壳仍能找到 */ }

  /* 兜底挂载：容器只认 #page-monitor。
     不能用 [data-page="monitor"]——壳的导航标签正是 <a data-page="monitor">，
     拿它当容器会把整页渲染进顶栏里。 */
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
