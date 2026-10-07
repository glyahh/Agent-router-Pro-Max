/* Prism · 前端壳
 *
 * 这个文件负责：hash 路由、按需加载六页、公共渲染组件、fetch 封装、状态灯轮询。
 * 它不含任何业务逻辑——六页的实现各自在 pages/ 下。
 *
 * ─── 页面模块的契约（写 pages/*.js 的人照这个来）────────────────────
 *
 *   Prism.registerPage('home', {
 *     title: '配置',                       // 可选
 *     async mount(root, ctx) { ... },      // 必需。root 是已清空的容器
 *     unmount() { ... },                   // 可选，切走时调用
 *   });
 *
 *   也可以写成 window.PrismPages = { config: {...}, usage: {...} }。
 *   两种写法都行；但不能用 ESM 的 export default —— 页面是被 <script src> 注入的
 *   普通脚本（这样 file:// 下也能跑），模块语法会直接语法错误。
 *   没注册（或文件 404）不会白屏，壳会渲染一段说明告诉你怎么修。
 *
 *   mount 拿到的 ctx：
 *     ctx.api         fetch 封装，见下方 api
 *     ctx.ui          渲染组件，见下方 ui
 *     ctx.state       跨页共享状态（ctx.state.monitor 是最新一次监控数据）
 *     ctx.bus         事件总线：ctx.bus.on('monitor', fn) 能在状态轮询后收到
 *     ctx.navigate(h) 切页，h 形如 '#/usage'
 *     ctx.toast(msg, kind)        轻提示，kind: ''|'ok'|'bad'|'warn'
 *     ctx.dialog({...})           模态确认，返回 Promise<boolean>
 *     ctx.onUnmount(fn)           注册清理函数（定时器一定要在这里清）
 *     ctx.reload()                重新 mount 当前页
 *
 *   mount 里抛错（同步或异步）不会白屏，壳会渲染带"重试"的错误面板。
 *   页面自己在"数据为空"时要调 ctx.ui.empty(...)，别留一片空。
 * ─────────────────────────────────────────────────────────────── */

(function () {
  'use strict';

  /* ══ 0. 工具 ══════════════════════════════════════════════════ */

  var FILE_MODE = location.protocol === 'file:';
  var API_BASE = FILE_MODE ? '' : '/';

  function cls() {
    var out = [];
    for (var i = 0; i < arguments.length; i++) {
      var a = arguments[i];
      if (!a) continue;
      if (Array.isArray(a)) { var s = cls.apply(null, a); if (s) out.push(s); }
      else out.push(String(a));
    }
    return out.join(' ');
  }

  // 全部组件都走它建节点：文本一律走 textContent，页面里拼 HTML 的机会就少了
  function h(tag, attrs) {
    var el = document.createElement(tag);
    var kids = Array.prototype.slice.call(arguments, 2);
    if (attrs) {
      for (var k in attrs) {
        if (!Object.prototype.hasOwnProperty.call(attrs, k)) continue;
        var v = attrs[k];
        if (v === null || v === undefined || v === false) continue;
        if (k === 'class' || k === 'className') el.className = v;
        else if (k === 'text') el.textContent = String(v);
        else if (k === 'html') el.innerHTML = v;
        else if (k === 'style') {
          if (typeof v === 'string') el.setAttribute('style', v);
          else for (var s in v) if (v[s] !== null && v[s] !== undefined) el.style[s] = v[s];
        }
        else if (k === 'data') { for (var d in v) if (v[d] !== null && v[d] !== undefined) el.dataset[d] = v[d]; }
        else if (k === 'on') { for (var e in v) if (typeof v[e] === 'function') el.addEventListener(e, v[e]); }
        // onClick 这种驼峰写法也要能用：事件名一律小写，否则 'Click' 永远不触发
        else if (typeof v === 'function') el.addEventListener(k.replace(/^on/, '').toLowerCase(), v);
        else if (v === true) el.setAttribute(k, '');
        else el.setAttribute(k, String(v));
      }
    }
    append(el, kids);
    return el;
  }

  function append(el, kids) {
    if (!el) return el;
    for (var i = 0; i < kids.length; i++) {
      var k = kids[i];
      if (k === null || k === undefined || k === false || k === '') continue;
      if (Array.isArray(k)) { append(el, k); continue; }
      if (k instanceof Node) { el.appendChild(k); continue; }
      el.appendChild(document.createTextNode(String(k)));
    }
    return el;
  }

  function clear(el) {
    if (!el) return el;
    while (el.firstChild) el.removeChild(el.firstChild);
    return el;
  }

  function text(v, dash) {
    if (v === null || v === undefined || v === '') return dash === undefined ? '—' : dash;
    return String(v);
  }

  function num(v, d) {
    if (v === null || v === undefined || v === '' || isNaN(Number(v))) return d === undefined ? '—' : d;
    return String(Number(v));
  }

  function pct(v) {
    if (v === null || v === undefined || isNaN(Number(v))) return '—';
    var n = Number(v);
    return (Math.round(n * 10) / 10) + '%';
  }

  // 秒 → 2h10m / 3d4h / 45s。倒计时和窗口长度都用它
  function dur(sec) {
    if (sec === null || sec === undefined || isNaN(Number(sec))) return '—';
    sec = Math.max(0, Math.floor(Number(sec)));
    var d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600),
        m = Math.floor(sec % 3600 / 60), s = sec % 60;
    if (d) return d + 'd' + h + 'h';
    if (h) return h + 'h' + m + 'm';
    if (m) return m + 'm' + s + 's';
    return s + 's';
  }

  function clock(ts, withDate) {
    if (!ts && ts !== 0) return '—';
    var d = new Date(typeof ts === 'number' && ts < 1e12 ? ts * 1000 : ts);
    if (isNaN(d.getTime())) return '—';
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    var base = p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
    if (!withDate) return base;
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + base;
  }

  function bytes(n) {
    if (n === null || n === undefined || isNaN(Number(n))) return '—';
    n = Number(n);
    var u = ['B', 'KB', 'MB', 'GB'], i = 0;
    while (n >= 1024 && i < u.length - 1) { n = n / 1024; i++; }
    return (i === 0 ? n : Math.round(n * 10) / 10) + ' ' + u[i];
  }

  function shortHost(url) {
    if (!url) return '—';
    try { return new URL(url).host; } catch (e) { return String(url).replace(/^https?:\/\//, '').split('/')[0] || '—'; }
  }

  /* ══ 1. fetch 封装 ═══════════════════════════════════════════ */

  function ApiError(message, status, detail) {
    this.name = 'ApiError';
    this.message = message || '请求失败';
    this.status = status || 0;
    this.detail = detail || '';
    this.stack = (new Error(this.message)).stack;
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  var DEFAULT_TIMEOUT = 15000;

  /* 控制台端口。页面就是控制台自己端出来的，location.port 比任何配置都准，
     所以整个文件只有这一个 8318 字面量，而且只当 file:// 下的兜底用。
     别的地方要显示端口，一律走 consolePort()。 */
  var DEFAULT_CONSOLE_PORT = 8318;

  function consolePort() {
    return location.port || String(DEFAULT_CONSOLE_PORT);
  }

  /* 控制台令牌（ME-12）：桌面版每次启动随机生成，经窗口 URL 的 ?t= 一次性交来。
     首次读到就转存 sessionStorage（不进历史记录、刷新不丢）；之后每个 /api/* 请求
     带 X-Prism-Token 头。浏览器直接打开（调试形态）没有 t，读到空串也无妨——
     那种服务端根本不启用令牌校验。见 INTERFACES.md 的安全模型一节。 */
  var _consoleToken = null;
  function consoleToken() {
    if (_consoleToken === null) {
      var m = location.search.match(/[?&]t=([^&]+)/);
      if (m) {
        try { _consoleToken = decodeURIComponent(m[1]); sessionStorage.setItem('prism-ct', _consoleToken); }
        catch (e) { _consoleToken = ''; }
        // 读到就抹掉地址栏里的 ?t=：令牌不留在可见 URL 与历史记录里（ME-12）
        try { history.replaceState(null, '', location.pathname + location.hash); } catch (e) {}
      } else {
        try { _consoleToken = sessionStorage.getItem('prism-ct') || ''; }
        catch (e) { _consoleToken = ''; }
      }
    }
    return _consoleToken;
  }

  function buildUrl(path, params) {
    var p = String(path || '');
    if (p.charAt(0) === '/') p = p.slice(1);
    var url;
    try { url = new URL(API_BASE + p, location.href); }
    catch (e) { throw new ApiError('接口地址不合法：' + path); }
    if (params) {
      for (var k in params) {
        if (!Object.prototype.hasOwnProperty.call(params, k)) continue;
        if (params[k] === null || params[k] === undefined) continue;
        url.searchParams.set(k, String(params[k]));
      }
    }
    return url.toString();
  }

  /* 统一处理 {ok,data} / {ok:false,error}、超时、以及"根本不是 JSON"。
     返回的是 data 本身，页面拿到就能用。
     401/403 一律不重试——网关连错 5 次 401 会封本机 IP 30 分钟。 */
  function request(method, path, opts) {
    opts = opts || {};
    var timeout = opts.timeout === undefined ? DEFAULT_TIMEOUT : opts.timeout;

    if (FILE_MODE) {
      return Promise.reject(new ApiError(
        '当前页面是以 file:// 直接打开的，浏览器禁止读取后台接口。' +
        '请用 Prism 窗口打开，或访问 http://127.0.0.1:' + consolePort() +
        '/（控制台实际监听的端口，默认 ' + DEFAULT_CONSOLE_PORT + '）。', 0));
    }

    var url;
    try { url = buildUrl(path, opts.params); }
    catch (e) { return Promise.reject(e); }

    var ctl = (typeof AbortController === 'function') ? new AbortController() : null;
    var timer = null;
    var init = { method: method, cache: 'no-store',
                 headers: { 'Accept': 'application/json', 'X-Prism-Token': consoleToken() } };
    if (ctl) init.signal = ctl.signal;
    if (opts.body !== undefined && opts.body !== null) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    if (ctl) timer = setTimeout(function () { ctl.abort(); }, timeout);

    function done() { if (timer) { clearTimeout(timer); timer = null; } }

    var t0 = (window.performance && performance.now) ? performance.now() : Date.now();

    return fetch(url, init).then(function (res) {
      return res.text().then(function (txt) {
        done();
        var elapsed = Math.round(((window.performance && performance.now) ? performance.now() : Date.now()) - t0);
        var payload = null, parseErr = null;
        if (txt) { try { payload = JSON.parse(txt); } catch (e) { parseErr = e; } }

        if (parseErr) {
          var tail = txt.slice(0, 160).replace(/\s+/g, ' ');
          if (res.status === 404) throw new ApiError('接口不存在（HTTP 404）：' + path + '。控制台服务可能不是 Prism 的 server.py。', 404, tail);
          if (res.status >= 500) throw new ApiError('控制台服务内部出错（HTTP ' + res.status + '）。详情见日志页。', res.status, tail);
          if (res.status === 401 || res.status === 403) throw new ApiError('没有权限（HTTP ' + res.status + '）。可能是 Host/Origin 校验没过。', res.status, tail);
          throw new ApiError('应答不是 JSON（HTTP ' + res.status + '），可能命中了静态文件或代理。', res.status, tail);
        }
        if (!payload || typeof payload !== 'object' || !('ok' in payload)) {
          throw new ApiError('响应格式不符合约定（缺 ok 字段）：' + path, res.status, txt.slice(0, 160));
        }
        if (payload.ok === false) {
          throw new ApiError(payload.error || payload.message || ('HTTP ' + res.status + ' 未给出原因'), res.status);
        }
        var data = payload.data === undefined ? null : payload.data;
        if (opts.wantRaw) return { data: data, status: res.status, elapsed_ms: elapsed };
        if (data && typeof data === 'object') {
          try { Object.defineProperty(data, '_status', { value: res.status, enumerable: false }); } catch (e) {}
          try { Object.defineProperty(data, '_elapsed_ms', { value: elapsed, enumerable: false }); } catch (e) {}
        }
        return data;
      });
    }, function (err) {
      done();
      if (err && (err.name === 'AbortError' || err.code === 20)) {
        throw new ApiError('请求超时（' + Math.round(timeout / 1000) + ' 秒）：' + path + '。网关可能卡住或正在重启。', 0);
      }
      throw new ApiError('连不上控制台服务（' + path + '）。确认 ' + consolePort() + ' 上的 Prism 服务在运行，且没有被别的进程占用端口。', 0, err && err.message ? err.message : '');
    });
  }

  var api = {
    ApiError: ApiError,
    request: request,
    get: function (p, params, o) { return request('GET', p, merge(o, { params: params })); },
    post: function (p, body, o) { return request('POST', p, merge(o, { body: body === undefined ? {} : body })); },
    put: function (p, body, o) { return request('PUT', p, merge(o, { body: body === undefined ? {} : body })); },
    del: function (p, params, o) { return request('DELETE', p, merge(o, { params: params })); },
    raw: function (p, params, o) { return request('GET', p, merge(o, { params: params, wantRaw: true })); },
    // 供页面判断是不是自己的 bug：非 ApiError 的一律是渲染/逻辑错误
    isApi: function (e) { return !!(e && e.name === 'ApiError'); },
    shortErr: function (e) {
      if (!e) return '未知错误';
      if (e.name === 'ApiError') return e.message;
      return (e.message || String(e)).slice(0, 200);
    }
  };

  function merge(a, b) {
    var o = {};
    if (a) for (var k in a) if (Object.prototype.hasOwnProperty.call(a, k)) o[k] = a[k];
    if (b) for (var k2 in b) if (Object.prototype.hasOwnProperty.call(b, k2)) o[k2] = b[k2];
    return o;
  }

  /* ══ 2. 渲染组件 ═════════════════════════════════════════════ */

  var ui = {};

  ui.h = h; ui.cls = cls; ui.clear = clear; ui.append = append;
  ui.text = text; ui.num = num; ui.pct = pct; ui.dur = dur; ui.clock = clock;
  ui.bytes = bytes; ui.shortHost = shortHost;
  // 这两个在下面才定义（函数声明会提升），挂到 ui 上方便页面和调试直接取用
  ui.toast = function () { return toast.apply(null, arguments); };
  ui.dialog = function () { return dialog.apply(null, arguments); };

  // 芯片。kind: ''|'ok'|'bad'|'warn'|'accent'|'on'
  ui.chip = function (label, kind, title) {
    return h('span', { class: cls('tag', kind), title: title || null, text: text(label) });
  };

  // 状态 → 芯片。三种状态映射统一在这里，免得每页各写一套
  ui.stateChip = function (state, labelOverride) {
    var map = { ok: ['ok', 'ACTIVE'], bad: ['bad', 'ERROR'], warn: ['warn', 'WARN'], idle: ['', 'IDLE'] };
    var m = map[state] || map.idle;
    return ui.chip(labelOverride || m[1], m[0]);
  };

  ui.sechead = function (comment, title, hint) {
    var actualTitle = title || (comment && comment !== '//' ? comment : '');
    return h('div', { class: 'sechead' }, [
      h('span', { class: 'stitle', text: actualTitle }),
      h('span', { class: 'hr' }),
      hint ? h('span', { class: 'hint', text: hint }) : null
    ]);
  };

  ui.section = function (comment, title, hint, body) {
    return h('div', { class: 'sec' }, [ui.sechead(comment, title, hint), body]);
  };

  ui.card = function (title, body, right) {
    var head = h('div', { class: 'cardh' }, [
      h('span', { text: title || '' }),
      h('span', { class: 'hr' }),
      typeof right === 'string' ? h('span', { class: 'hint', text: right }) : right
    ]);
    return h('div', { class: 'card' }, [title || right ? head : null, body]);
  };

  ui.cardbox = function (children, opts) {
    return h('div', { class: 'card' + (opts && opts.flat ? '' : '') }, [
      h('div', { class: 'cardb' + (opts && opts.pad0 ? ' p0' : '') }, children)
    ]);
  };

  // 按钮。kind: ''|'pri'|'sm'|'ghost'|'danger'；onClick 省略就是纯展示
  ui.button = function (label, onClick, kind, opts) {
    opts = opts || {};
    var b = h('button', {
      class: cls('btn', kind), type: 'button',
      title: opts.title || null, disabled: opts.disabled ? true : null,
      onClick: opts.disabled ? null : onClick
    }, [opts.icon ? h('span', { text: opts.icon }) : null, h('span', { text: label })]);
    b.__label = label;
    return b;
  };

  // 点了会自己转圈并禁用的按钮：所有会发写请求的按钮都该用它
  ui.actionButton = function (label, kind, onRun, opts) {
    opts = opts || {};
    var b = ui.button(label, null, kind, opts);
    b.addEventListener('click', function () {
      if (b.classList.contains('dis')) return;
      b.classList.add('dis');
      clear(b);
      b.appendChild(h('span', { class: 'spin' }));
      b.appendChild(h('span', { text: label }));
      var finish = function () {
        b.classList.remove('dis');
        clear(b);
        if (opts.icon) b.appendChild(h('span', { text: opts.icon }));
        b.appendChild(h('span', { text: label }));
      };
      Promise.resolve().then(onRun).then(finish, function (e) {
        finish();
        var m = api.isApi(e) ? e.message : ('出了个意外错误：' + api.shortErr(e));
        if (window.console && console.error) console.error(e);
        toast(m, 'bad');
      });
    });
    return b;
  };

  /* 统一微矢量 SVG 图标体系 (16×16 viewBox) */
  var SVG_ICONS = {
    empty: '<circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.3" fill="none"/><line x1="3.8" y1="3.8" x2="12.2" y2="12.2" stroke="currentColor" stroke-width="1.3"/>',
    warn: '<path d="M8 1.8l6.2 11.2c.3.5-.1 1-.7 1H2.5c-.6 0-1-.5-.7-1L8 1.8z" stroke="currentColor" stroke-width="1.2" fill="none" stroke-linejoin="round"/><line x1="8" y1="6" x2="8" y2="9.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/><circle cx="8" cy="11.8" r=".8" fill="currentColor"/>',
    bad: '<circle cx="8" cy="8" r="6.2" stroke="currentColor" stroke-width="1.3" fill="none"/><line x1="8" y1="4.5" x2="8" y2="8.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><circle cx="8" cy="11.5" r=".9" fill="currentColor"/>',
    error: '<circle cx="8" cy="8" r="6.2" stroke="currentColor" stroke-width="1.3" fill="none"/><line x1="8" y1="4.5" x2="8" y2="8.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><circle cx="8" cy="11.5" r=".9" fill="currentColor"/>',
    info: '<circle cx="8" cy="8" r="6.2" stroke="currentColor" stroke-width="1.3" fill="none"/><circle cx="8" cy="4.8" r=".9" fill="currentColor"/><line x1="8" y1="7.2" x2="8" y2="11.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>',
    triangle: '<path d="M4 10.5l4-5 4 5z" fill="currentColor"/>',
    caret: '<path d="M4 10.5l4-5 4 5z" fill="currentColor"/>',
    'triangle-down': '<path d="M4 5.5l4 5 4-5z" fill="currentColor"/>',
    'caret-down': '<path d="M4 5.5l4 5 4-5z" fill="currentColor"/>',
    spinner: '<circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.6" fill="none" stroke-dasharray="28" stroke-dashoffset="10" stroke-linecap="round"/>',
    loading: '<circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.6" fill="none" stroke-dasharray="28" stroke-dashoffset="10" stroke-linecap="round"/>',
    close: '<line x1="3.5" y1="3.5" x2="12.5" y2="12.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/><line x1="12.5" y1="3.5" x2="3.5" y2="12.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>',
    search: '<circle cx="6.5" cy="6.5" r="4.5" stroke="currentColor" stroke-width="1.3" fill="none"/><line x1="10" y1="10" x2="14" y2="14" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>'
  };

  ui.icon = function (name, extraClass) {
    var raw = SVG_ICONS[name] || SVG_ICONS.info;
    var isSpin = name === 'spinner' || name === 'loading';
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 16 16');
    svg.setAttribute('width', '16');
    svg.setAttribute('height', '16');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('class', 'ui-icon' + (isSpin ? ' icon-spin' : '') + (extraClass ? ' ' + extraClass : ''));
    svg.innerHTML = raw;
    return svg;
  };

  ui.iconHTML = function (name, extraClass) {
    var raw = SVG_ICONS[name] || SVG_ICONS.info;
    var isSpin = name === 'spinner' || name === 'loading';
    var clsStr = 'ui-icon' + (isSpin ? ' icon-spin' : '') + (extraClass ? ' ' + extraClass : '');
    return '<svg class="' + clsStr + '" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">' + raw + '</svg>';
  };

  ui.notice = function (kind, titleText, detail) {
    var icName = kind === 'bad' ? 'bad' : (kind === 'warn' ? 'warn' : 'info');
    return h('div', { class: cls('notice', kind) }, [
      h('span', { class: 'k' }, [ui.icon(icName)]),
      h('span', null, [h('b', { text: titleText }), detail ? h('span', { text: ' ' + detail }) : null])
    ]);
  };

  ui.sectionHead = ui.sechead;

  /* 空状态。标题说"没什么"，说明说"为什么没有 + 下一步做什么" */
  ui.empty = function (title, detail, actions) {
    var box = h('div', { class: 'statebox' }, [
      h('span', { class: 'ic' }, [ui.icon('empty')]),
      h('span', { class: 't', text: title || '暂无数据' }),
      // detail 按纯文本插，不当 HTML——后端字符串混进 detail 时不会变成标记。
      detail ? h('span', { class: 'd', text: detail }) : null
    ]);
    if (actions && actions.length) box.appendChild(h('span', { class: 'do' }, actions));
    return box;
  };

  ui.loading = function (label) {
    return h('div', { class: 'statebox' }, [
      h('span', { class: 'ic' }, [ui.icon('spinner')]),
      h('span', { class: 't', text: label || '加载中…' })
    ]);
  };

  ui.skeleton = function (rows) {
    var box = h('div', { class: 'card' }, h('div', { class: 'cardb' }, []));
    var body = box.firstChild;
    for (var i = 0; i < (rows || 4); i++) body.appendChild(h('div', { class: 'skel', style: { width: (95 - i * 11) + '%' } }));
    return box;
  };

  /* 错误状态。err 可以是 ApiError、Error 或字符串。
     永远给"下一步做什么"，不要只丢一句报错。 */
  ui.error = function (err, retry, extraHint) {
    var msg = api.isApi(err) ? err.message : ('界面出错：' + api.shortErr(err));
    var hint = extraHint;
    if (!hint) {
      if (api.isApi(err) && err.status === 0) hint = '无法连接后台服务，请检查服务状态。';
      else if (api.isApi(err) && err.status === 404) hint = '请求的后台接口不存在。';
      else hint = '请尝试重试或查看日志排查。';
    }
    var box = h('div', { class: 'statebox' }, [
      h('span', { class: 'ic' }, [ui.icon('bad')]),
      h('span', { class: 't', text: '加载失败' }),
      h('span', { class: 'd', text: msg }),
      h('span', { class: 'd', text: hint })
    ]);
    if (err && err.detail) box.appendChild(h('span', { class: 'd sub', text: '原始应答：' + err.detail }));
    if (retry) box.appendChild(h('span', { class: 'do' }, [ui.button('重试', retry, 'pri')]));
    return box;
  };

  /* 表格。
     opts = { cols:[{t:'来源', cls:'n'}|'来源'], rows:[[cell,...]], empty:{title,detail,actions}, raw:false }
     cell 可以是字符串、DOM 节点、或 {text, cls, chip}
     行数为 0 且给了 empty 时返回空状态而不是空表 */
  ui.table = function (opts) {
    var cols = (opts.cols || []).map(function (c) { return typeof c === 'string' ? { t: c } : c; });
    var rows = opts.rows || [];
    if (!rows.length && opts.empty) {
      var e = opts.empty;
      if (typeof e === 'string') return ui.empty(e);
      return ui.empty(e.title, e.detail, e.actions);
    }
    var thead = h('tr', null, cols.map(function (c) {
      return h('th', { class: cls(c.cls), text: c.t || '' });
    }));
    var tbody = h('tbody', null, rows.map(function (r) {
      // 行级 class：原先 `trClass` 被读出来就丢了（没应用到 <tr> 上），是个死分支 ——
      // 将来谁传了它也不会生效（复查轮 8 的 T-4）。这里补上应用。
      var trClass = (r && r.trClass) || '';
      if (r && r.trClass) r = r.cells;
      return h('tr', { class: trClass }, (r || []).map(function (cell) {
        var td = h('td', { class: cls(cell && cell.cls) });
        if (cell instanceof Node) td.appendChild(cell);
        else if (cell && typeof cell === 'object' && 'chip' in cell) td.appendChild(ui.chip(cell.chip, cell.kind));
        else if (cell && typeof cell === 'object' && 'node' in cell) td.appendChild(cell.node);
        else td.textContent = text(cell === null || cell === undefined ? '' : cell);
        return td;
      }));
    }));
    var table = h('table', { class: 't' }, [h('thead', null, thead), tbody]);
    return opts.plain ? table : h('div', { class: 'tw' }, table);
  };

  // 键值列表：[['标签','值'], ...]
  ui.kv = function (pairs) {
    return h('div', { class: 'kv' }, (pairs || []).map(function (p) {
      var v = p[1];
      return h('div', { class: 'r' }, [
        h('span', { class: 'k', text: p[0] }),
        v instanceof Node ? v : h('span', { class: cls('v', p[2]), text: text(v), title: typeof v === 'string' ? v : null }),
        p[3] || null
      ]);
    }));
  };

  // 百分比条。阈值 75% 就该红（设计样板里主窗口 75% 用的就是 hot）。
  // 样板文件 app/design/ui-mock.html 已不在仓库里，取值以本文件为准。
  ui.bar = function (percent, opts) {
    opts = opts || {};
    var p = Number(percent);
    if (isNaN(p)) p = 0;
    p = Math.max(0, Math.min(100, p));
    var kind = opts.kind || (p >= 75 ? 'hot' : p >= 50 ? 'warn' : '');
    return h('div', { class: cls('bar', kind) }, h('i', { style: { width: p + '%' } }));
  };

  /* 日志视图。高亮用 DOM 拼，不拼 HTML —— 日志内容来自外部文件。 */
  ui.logView = function (lines, opts) {
    opts = opts || {};
    var q = (opts.query || '').toLowerCase();
    var startNo = opts.startNo || 1;
    var wrap = h('div', { class: 'logview', id: opts.id || null, style: { position: 'relative' } });
    var matches = [];
    (lines || []).forEach(function (ln, i) {
      var s = typeof ln === 'string' ? ln : (ln && ln.line) || '';
      var low = s.toLowerCase();
      // 网关日志用的是 ERR / WRN / INF 这种三字母级别，别只认 error/warn 全拼
      var kind = /\b(error|err|fatal|panic|failed)\b/i.test(s) ? 'e' : /\b(warn|wrn|warning)\b/i.test(s) ? 'w' : '';
      var row = h('span', { class: 'ln ' + kind });
      row.appendChild(h('span', { class: 'no', text: String(startNo + i) }));
      if (q) {
        var from = 0, at;
        while ((at = low.indexOf(q, from)) !== -1) {
          if (at > from) row.appendChild(document.createTextNode(s.slice(from, at)));
          var hl = h('span', { class: 'hl', text: s.slice(at, at + q.length) });
          matches.push(hl);
          row.appendChild(hl);
          from = at + q.length;
        }
        row.appendChild(document.createTextNode(s.slice(from)));
      } else {
        row.appendChild(document.createTextNode(s));
      }
      wrap.appendChild(row);
    });

    wrap.__matches = matches;
    wrap.__matchIdx = 0;
    wrap.matchCount = matches.length;

    function jumpTo(idx) {
      if (!matches.length) return;
      matches.forEach(function (m) { m.classList.remove('active'); });
      wrap.__matchIdx = (idx + matches.length) % matches.length;
      var cur = matches[wrap.__matchIdx];
      cur.classList.add('active');
      if (typeof cur.scrollIntoView === 'function') {
        cur.scrollIntoView({ block: 'center', behavior: 'smooth' });
      }
      if (navBadge) navBadge.textContent = (wrap.__matchIdx + 1) + '/' + matches.length;
    }

    wrap.nextMatch = function () { jumpTo(wrap.__matchIdx + 1); };
    wrap.prevMatch = function () { jumpTo(wrap.__matchIdx - 1); };

    var navBadge = null;
    if (q && matches.length > 0) {
      navBadge = h('span', { class: 'log-nav-count', text: '1/' + matches.length });
      var prevBtn = h('button', {
        class: 'log-nav-btn', type: 'button', title: '上一个匹配项 (Shift+F3)',
        onClick: function (e) { e.preventDefault(); e.stopPropagation(); wrap.prevMatch(); }
      }, [ui.icon('triangle')]);
      var nextBtn = h('button', {
        class: 'log-nav-btn', type: 'button', title: '下一个匹配项 (F3)',
        onClick: function (e) { e.preventDefault(); e.stopPropagation(); wrap.nextMatch(); }
      }, [ui.icon('triangle-down')]);
      var navFloat = h('div', { class: 'log-search-nav' }, [
        h('span', { class: 'log-nav-lbl', text: '匹配' }),
        navBadge,
        prevBtn,
        nextBtn
      ]);
      wrap.appendChild(navFloat);
      // 默认聚焦第一个匹配
      setTimeout(function () { jumpTo(0); }, 10);
    }

    if (!(lines || []).length) {
      usingFallbackEmpty(wrap, opts.emptyText || '没有日志行。可能日志被清空了，或者过滤条件把全部都排除了。');
    }
    return wrap;
  };

  // 让空态能塞进固定高度的容器里
  function usingFallbackEmpty(host, msg) {
    host.appendChild(h('span', { class: 'ln', style: { color: 'var(--fg3)' }, text: msg }));
  }

  /* ══ 3. 事件总线 ════════════════════════════════════════════ */

  var bus = (function () {
    var map = {};
    return {
      on: function (evt, fn) {
        (map[evt] = map[evt] || []).push(fn);
        return function () { bus.off(evt, fn); };
      },
      off: function (evt, fn) {
        var a = map[evt]; if (!a) return;
        var i = a.indexOf(fn); if (i >= 0) a.splice(i, 1);
      },
      emit: function (evt, payload) {
        var a = map[evt]; if (!a) return;
        a.slice().forEach(function (fn) { try { fn(payload); } catch (e) { if (window.console) console.error(e); } });
      }
    };
  })();

  var state = { monitor: null, monitorError: null, page: null, lastPollAt: 0 };

  /* ══ 4. 轻提示 / 模态 ═══════════════════════════════════════ */

  function toast(msg, kind, ms) {
    var host = document.getElementById('toasts');
    if (!host) return;
    var el = h('div', { class: cls('toast', kind) }, h('span', { text: msg }));
    host.appendChild(el);
    setTimeout(function () {
      el.style.transition = 'opacity .2s';
      el.style.opacity = '0';
      setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 220);
    }, ms || (kind === 'bad' ? 6000 : 3000));
  }

  var dlgOpen = null;

  /* 模态。opts = {title, body(Node|string), okText, cancelText, danger}
     返回 Promise<boolean>；Esc 与点背景都算取消。

     同一时刻**只允许一个**模态：`dlgOpen` 原先只写不读（死状态），两个弹窗能叠在一起，
     各自挂一个 keydown 捕获监听 → 一次 Esc 把两个都关掉、两个 Promise 一起 resolve
     （复查轮 4 的 #5）。现在开新弹窗先把它当作"取消"收掉，语义上也更合理：
     新问题取代旧问题。 */
  function dialog(opts) {
    opts = opts || {};
    var isDanger = !!(opts.danger || opts.destructive);
    if (dlgOpen) {
      var previous = dlgOpen;
      dlgOpen = null;          // 先清，免得 close() 里再触发一次
      try { previous(false); } catch (e) { /* 旧弹窗收尾失败不该拦新弹窗 */ }
    }
    return new Promise(function (resolve) {
      var prevFocus = document.activeElement;
      var bd = h('div', { class: 'bd', id: 'modal' });
      var body = opts.body;
      if (typeof body === 'string') body = h('div', { text: body });
      var ok = ui.button(opts.okText || '确定', function () { close(true); }, isDanger ? 'danger' : 'pri');
      var cancel = ui.button(opts.cancelText || '取消', function () { close(false); });
      var dlg = h('div', {
        class: 'dlg',
        role: 'dialog',
        'aria-modal': 'true',
        'aria-label': opts.title || '确认'
      }, [
        h('div', { class: 'h', text: opts.title || '确认' }),
        h('div', { class: 'b' }, body || '确定要继续吗？'),
        h('div', { class: 'f2' }, [cancel, ok])
      ]);
      bd.appendChild(dlg);
      bd.addEventListener('mousedown', function (e) { if (e.target === bd) close(false); });

      function getFocusableElements() {
        var selector = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
        var list = Array.prototype.slice.call(dlg.querySelectorAll(selector));
        return list.filter(function (el) {
          if (el.disabled || el.getAttribute('aria-hidden') === 'true') return false;
          if (typeof window.getComputedStyle === 'function') {
            try {
              var cs = window.getComputedStyle(el);
              if (cs.display === 'none' || cs.visibility === 'hidden') return false;
            } catch (e) {}
          }
          return el.offsetParent !== null || el.offsetWidth > 0 || el.offsetHeight > 0 || el === ok || el === cancel;
        });
      }

      function onKey(e) {
        if (e.key === 'Escape') {
          e.preventDefault();
          close(false);
          return;
        }
        if (e.key === 'Tab') {
          var focusables = getFocusableElements();
          if (focusables.length === 0) {
            e.preventDefault();
            return;
          }
          var first = focusables[0];
          var last = focusables[focusables.length - 1];
          var cur = document.activeElement;
          if (e.shiftKey) {
            if (cur === first || !dlg.contains(cur)) {
              e.preventDefault();
              last.focus();
            }
          } else {
            if (cur === last || !dlg.contains(cur)) {
              e.preventDefault();
              first.focus();
            }
          }
          return;
        }
        if (e.key === 'Enter') {
          if (e.target && e.target.tagName === 'TEXTAREA') return;
          // 若焦点在弹窗内的自定义非确认/取消按钮上，回车应触发该按钮自身激活，不直接关闭弹窗
          if (e.target && e.target.tagName === 'BUTTON' && e.target !== ok && e.target !== cancel) return;
          e.preventDefault();
          if (isDanger) {
            if (document.activeElement === cancel) {
              close(false);
            } else if (document.activeElement === ok) {
              close(true);
            }
            // 危险模式下未聚焦确定按钮时，严禁直通执行 close(true)
          } else {
            if (document.activeElement === cancel) {
              close(false);
            } else {
              close(true);
            }
          }
        }
      }

      function close(answer) {
        document.removeEventListener('keydown', onKey, true);
        if (bd.parentNode) bd.parentNode.removeChild(bd);
        dlgOpen = null;
        if (prevFocus && typeof prevFocus.focus === 'function') {
          try { prevFocus.focus(); } catch (err) {}
        }
        resolve(answer);
      }

      dlgOpen = close;
      document.addEventListener('keydown', onKey, true);
      document.body.appendChild(bd);
      setTimeout(function () {
        if (isDanger) {
          cancel.focus();
        } else {
          // 若弹窗内部自带 input/textarea，优先聚焦输入框；否则聚焦确定按钮
          var firstInp = dlg.querySelector('input:not([disabled]), textarea:not([disabled])');
          if (firstInp && (!document.activeElement || !dlg.contains(document.activeElement) || document.activeElement === ok)) {
            firstInp.focus();
          } else {
            ok.focus();
          }
        }
      }, 0);
    });
  }

  /* ══ 5. 命令面板（Ctrl K） ══════════════════════════════════ */

  var PAL_KEY = 'prism.recent.pages';
  var cachedPaletteSources = null;

  function loadPaletteSources() {
    if (cachedPaletteSources && cachedPaletteSources.length) return Promise.resolve(cachedPaletteSources);
    if (state.monitor && Array.isArray(state.monitor.sources) && state.monitor.sources.length) {
      cachedPaletteSources = state.monitor.sources;
      return Promise.resolve(cachedPaletteSources);
    }
    return api.get('api/sources', null, { timeout: 3000 }).then(function (res) {
      cachedPaletteSources = Array.isArray(res) ? res : [];
      return cachedPaletteSources;
    }, function () {
      return [];
    });
  }

  function openPalette() {
    if (document.getElementById('palette')) return;
    var recent = [];
    try { recent = JSON.parse(localStorage.getItem(PAL_KEY) || '[]'); } catch (e) { recent = []; }

    var allItems = [];
    PAGES.forEach(function (p) {
      allItems.push({
        type: 'page',
        name: p.name,
        label: p.label,
        hash: p.hash,
        desc: p.desc || '页面导航',
        badge: '页面'
      });
    });

    if (cachedPaletteSources && cachedPaletteSources.length) {
      cachedPaletteSources.forEach(function (s) {
        allItems.push({
          type: 'source',
          id: s.id,
          label: s.label || s.id,
          hash: '#/home',
          sourceId: s.id,
          desc: (s.group ? s.group.toUpperCase() + ' · ' : '') + shortHost(s.base_url),
          badge: '来源'
        });
      });
    }

    var filtered = [];
    var cursor = 0;

    var prevFocus = document.activeElement;   // 关闭时把手柄还回打开前的元素

    var inp = h('input', {
      type: 'search',
      class: 'pal-inp',
      placeholder: '搜索页面或来源 (支持直达/过滤)...',
      autocomplete: 'off',
      spellcheck: 'false'
    });

    var list = h('div', { class: 'pal-list' });
    var bd = h('div', { class: 'bd', id: 'palette' }, [
      h('div', { class: 'pal' }, [
        h('div', { class: 'pal-head' }, [
          inp,
          h('span', { class: 'pal-kbd-hint', text: 'Esc 关闭 · ↑↓ 选择 · ↵ 跳转' })
        ]),
        list
      ])
    ]);

    function computeFiltered(q) {
      q = (q || '').trim().toLowerCase();
      if (!q) {
        var pagesSorted = allItems.filter(function (it) { return it.type === 'page'; }).sort(function (a, b) {
          var ia = recent.indexOf(a.name), ib = recent.indexOf(b.name);
          if (ia < 0) ia = 99; if (ib < 0) ib = 99;
          return ia - ib;
        });
        var sourcesSlice = allItems.filter(function (it) { return it.type === 'source'; }).slice(0, 10);
        filtered = pagesSorted.concat(sourcesSlice);
      } else {
        filtered = allItems.filter(function (it) {
          var text = (it.label + ' ' + (it.desc || '') + ' ' + (it.hash || '') + ' ' + (it.id || '')).toLowerCase();
          return text.indexOf(q) >= 0;
        });
      }
      if (cursor >= filtered.length) cursor = 0;
      if (cursor < 0) cursor = Math.max(0, filtered.length - 1);
      renderList();
    }

    function renderList() {
      clear(list);
      if (!filtered.length) {
        list.appendChild(h('div', { class: 'pal-empty', text: '未找到匹配的页面或来源' }));
        return;
      }
      filtered.forEach(function (it, i) {
        var row = h('div', { class: 'it' + (i === cursor ? ' on' : ''), data: { i: i } }, [
          h('span', { class: 'tag ' + (it.type === 'page' ? 'accent' : 'ok'), text: it.badge }),
          h('span', { class: 'pal-title', text: it.label }),
          h('span', { class: 'no', text: it.type === 'page' ? it.hash : it.desc })
        ]);
        row.addEventListener('mousedown', function (e) { e.preventDefault(); go(i); });
        list.appendChild(row);
      });
      var onEl = list.children[cursor];
      if (onEl && typeof onEl.scrollIntoView === 'function') {
        onEl.scrollIntoView({ block: 'nearest' });
      }
    }

    function mark() {
      Array.prototype.forEach.call(list.children, function (c, i) { c.classList.toggle('on', i === cursor); });
      var onEl = list.children[cursor];
      if (onEl && typeof onEl.scrollIntoView === 'function') {
        onEl.scrollIntoView({ block: 'nearest' });
      }
    }

    function go(i) {
      var it = filtered[i]; if (!it) return;
      close();
      if (it.type === 'page') {
        navigate(it.hash);
      } else if (it.type === 'source') {
        navigate('#/home');
        setTimeout(function () {
          bus.emit('highlight-source', it.sourceId);
        }, 150);
      }
    }

    inp.addEventListener('input', function () {
      cursor = 0;
      computeFiltered(inp.value);
    });

    function onKey(e) {
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key === 'Escape') { e.preventDefault(); close(); }
      else if (e.key === 'Tab') {
        // 面板里只有搜索框可聚焦：Tab 圈在面板内，别漏到遮罩后面的页面上
        e.preventDefault();
        inp.focus();
      }
      else if (e.key === 'ArrowDown') {
        if (!filtered.length) return;
        e.preventDefault(); cursor = (cursor + 1) % filtered.length; mark();
      }
      else if (e.key === 'ArrowUp') {
        if (!filtered.length) return;
        e.preventDefault(); cursor = (cursor - 1 + filtered.length) % filtered.length; mark();
      }
      else if (e.key === 'Enter') {
        if (filtered.length) { e.preventDefault(); go(cursor); }
      }
    }

    function close() {
      document.removeEventListener('keydown', onKey, true);
      if (bd.parentNode) bd.parentNode.removeChild(bd);
      if (prevFocus && typeof prevFocus.focus === 'function') {
        try { prevFocus.focus(); } catch (err) {}
      }
    }

    bd.addEventListener('mousedown', function (e) { if (e.target === bd) close(); });
    document.addEventListener('keydown', onKey, true);
    document.body.appendChild(bd);

    computeFiltered('');
    for (var ci = 0; ci < filtered.length; ci++) {
      if (filtered[ci].type === 'page' && filtered[ci].name !== state.page) { cursor = ci; break; }
    }
    mark();
    setTimeout(function () { inp.focus(); }, 20);

    loadPaletteSources().then(function (srcs) {
      if (!document.getElementById('palette')) return;
      if (!srcs || !srcs.length) return;
      var currentIds = {};
      allItems.forEach(function (x) { if (x.type === 'source') currentIds[x.sourceId] = true; });
      srcs.forEach(function (s) {
        if (!currentIds[s.id]) {
          allItems.push({
            type: 'source',
            id: s.id,
            label: s.label || s.id,
            hash: '#/home',
            sourceId: s.id,
            desc: (s.group ? s.group.toUpperCase() + ' · ' : '') + shortHost(s.base_url),
            badge: '来源'
          });
        }
      });
      computeFiltered(inp.value);
    });
  }

  function rememberPage(name) {
    try {
      var a = JSON.parse(localStorage.getItem(PAL_KEY) || '[]');
      a = a.filter(function (x) { return x !== name; });
      a.unshift(name);
      localStorage.setItem(PAL_KEY, JSON.stringify(a.slice(0, 5)));
    } catch (e) {}
  }

  /* ══ 6. 路由与页面加载 ══════════════════════════════════════ */

  var PAGES = [
    { name: 'home', label: '首页', hash: '#/home', file: 'pages/home.js',
      desc: '来源与模型拓扑配置' },
    { name: 'manage', label: '管理', hash: '#/manage', file: 'pages/manage.js',
      desc: '分组与来源' },
    { name: 'usage', label: '用量', hash: '#/usage', file: 'pages/usage.js',
      desc: '请求与用量历史' },
    { name: 'monitor', label: '监控', hash: '#/monitor', file: 'pages/monitor.js',
      desc: '网关健康与路由状态' },
    { name: 'logs', label: '日志', hash: '#/logs', file: 'pages/logs.js',
      desc: '网关实时运行日志' },
    { name: 'control', label: '控制', hash: '#/control', file: 'pages/control.js',
      desc: '网关与代理端' },
    { name: 'settings', label: '设置', hash: '#/settings', file: 'pages/settings.js',
      desc: '网关与应用参数设置' }
  ];

  /* 旧地址映射。首页原来是 #/config（那时它叫"配置"），改名的同时留一条别名：
     命令面板的最近记录存在 localStorage 里、用户也可能存过书签，直接 404 掉太糙。 */
  var PAGE_ALIAS = { config: 'home' };

  var registry = {};      // name -> page def
  var loading = {};       // name -> Promise<def>
  var current = null;     // {name, def, cleanup:[]}
  var mountToken = 0;

  function pageInfo(name) {
    for (var i = 0; i < PAGES.length; i++) if (PAGES[i].name === name) return PAGES[i];
    return null;
  }

  function parseHash() {
    var raw = (location.hash || '').replace(/^#\/?/, '');
    var name = raw.split(/[/?]/)[0];
    if (PAGE_ALIAS[name]) name = PAGE_ALIAS[name];
    return pageInfo(name) ? name : 'home';
  }

  var activeHash = location.hash || '#/home';
  var isConfirmingNav = false;
  var bypassDirtyGuard = false;

  function isCurrentDirty() {
    if (!current) return false;
    try {
      if (current.instance && typeof current.instance.isDirty === 'function') {
        return !!current.instance.isDirty();
      }
      if (current.def && typeof current.def.isDirty === 'function') {
        return !!current.def.isDirty();
      }
    } catch (e) {
      if (window.console) console.error('[isDirty check]', e);
    }
    return false;
  }

  function protectLeave(onProceed, onCancel) {
    if (bypassDirtyGuard || !isCurrentDirty()) {
      if (onProceed) onProceed();
      return;
    }
    if (isConfirmingNav) {
      if (onCancel) onCancel();
      return;
    }

    var instance = current && current.instance;
    var def = current && current.def;
    var flusher = null;
    if (instance && typeof instance.flushSave === 'function') {
      flusher = function () { return instance.flushSave(); };
    } else if (def && typeof def.flushSave === 'function') {
      flusher = function () { return def.flushSave(); };
    }

    if (flusher) {
      // 优先尝试调用 flushSave 自动落盘
      flusher().then(function () {
        toast('配置已自动保存', 'ok');
        bypassDirtyGuard = true;
        if (onProceed) onProceed();
      }).catch(function (err) {
        // 自动落盘失败或存在非法字段，弹出危险确认框拦截
        isConfirmingNav = true;
        var msg = (err && err.message) ? ('（' + err.message + '）') : '';
        dialog({
          title: '未保存的修改',
          body: '当前页面有未保存或校验未通过的修改' + msg + '，切换页面将放弃这些改动。确定要放弃修改并离开吗？',
          okText: '放弃修改并离开',
          cancelText: '留在此页',
          danger: true
        }).then(function (ok) {
          isConfirmingNav = false;
          if (ok) {
            if (instance && typeof instance.resetDirty === 'function') {
              instance.resetDirty();
            } else if (def && typeof def.resetDirty === 'function') {
              def.resetDirty();
            }
            bypassDirtyGuard = true;
            if (onProceed) onProceed();
          } else {
            if (onCancel) onCancel();
          }
        });
      });
      return;
    }

    // 默认弹窗确认
    isConfirmingNav = true;
    dialog({
      title: '未保存的修改',
      body: '当前修改尚未保存，离开将丢失改动。',
      okText: '放弃修改',
      cancelText: '取消',
      danger: true
    }).then(function (ok) {
      isConfirmingNav = false;
      if (ok) {
        bypassDirtyGuard = true;
        if (onProceed) onProceed();
      } else {
        if (onCancel) onCancel();
      }
    });
  }

  function confirmLeaveIfDirty(onConfirm, onCancel) {
    protectLeave(onConfirm, onCancel);
    return false;
  }

  function navigate(hash) {
    if (location.hash === hash) {
      if (isCurrentDirty()) {
        protectLeave(function () { route(true); });
        return;
      }
      route();
      return;
    }
    protectLeave(function () {
      location.hash = hash;
    });
  }

  function registerPage(name, def) {
    if (!name || !def) throw new Error('registerPage(name, def) 两个参数都要给');
    if (typeof def !== 'object') throw new Error('registerPage 的第二个参数应该是对象');
    if (typeof def.mount !== 'function' && typeof def.render !== 'function') {
      throw new Error('页面模块 ' + name + ' 必须提供 mount(root, ctx) 或 render(root, ctx)');
    }
    if (typeof def.mount !== 'function') def.mount = def.render;
    registry[name] = def;
    var pend = pending[name];
    if (pend) { pend.resolve(def); delete pending[name]; }
  }

  var pending = {};   // name -> {resolve, reject}

  /* 用 <script> 注入而不是 import()：file:// 下动态 import 会被 CORS 拦掉，
     而 <script src> 在 file:// 下能正常执行。 */
  var libLoading = {};
  function ensureLib(src, ready) {
    if (ready()) return Promise.resolve();
    if (libLoading[src]) return libLoading[src];
    libLoading[src] = new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = src;
      s.async = false;
      s.onload = function () { resolve(); };
      s.onerror = function () {
        delete libLoading[src];
        reject(new Error(src + ' 加载失败'));
      };
      document.head.appendChild(s);
    });
    return libLoading[src];
  }

  function loadPage(name) {
    if ((name === 'home' || name === 'manage') && !(window.PrismSourceForm)) {
      return ensureLib('pages/source-form.js', function () { return !!window.PrismSourceForm; })
        .then(function () { return loadPage(name); });
    }
    if (registry[name]) return Promise.resolve(registry[name]);
    if (window.PrismPages && window.PrismPages[name]) {
      var pre = window.PrismPages[name];
      if (typeof pre.mount !== 'function' && typeof pre.render === 'function') pre.mount = pre.render;
      registry[name] = pre;
      return Promise.resolve(pre);
    }
    if (loading[name]) return loading[name];
    var info = pageInfo(name);
    if (!info) return Promise.reject(new Error('未知页面：' + name));

    loading[name] = new Promise(function (resolve, reject) {
      pending[name] = { resolve: resolve, reject: reject };
      var s = document.createElement('script');
      s.src = info.file;
      s.async = false;
      s.onload = function () {
        // 两种注册方式都认：Prism.registerPage(...)（填了 registry）或 PrismPages[name]
        var def = registry[name] ||
          (window.PrismPages && window.PrismPages[name]) ||
          null;
        if (!def) {
          reject(new Error(info.file + ' 加载成功，但没有注册页面模块'));
          return;
        }
        if (typeof def.mount !== 'function' && typeof def.render === 'function') def.mount = def.render;
        registry[name] = def;
        resolve(def);
      };
      s.onerror = function () {
        reject(new Error(info.file + ' 加载失败'));
      };
      document.head.appendChild(s);
    }).then(function (def) {
      delete pending[name];
      return def;
    }, function (e) {
      delete pending[name];
      delete loading[name];   // 允许"重试"重新注入
      throw e;
    });
    return loading[name];
  }

  function missingPanel(info, err) {
    var box = ui.empty(
      '页面加载失败',
      '未能加载 ' + info.file + ' 模块。错误信息：' + api.shortErr(err),
      [ui.button('重试', function () { route(true); }, 'pri')]
    );
    return box;
  }

  function runCleanup() {
    if (!current) return;
    if (typeof current.def.unmount === 'function') {
      try { current.def.unmount(); } catch (e) { if (window.console) console.error(e); }
    }
    current.cleanup.forEach(function (fn) { try { fn(); } catch (e) { if (window.console) console.error(e); } });
    current = null;
  }

  function route(force) {
    var name = parseHash();
    var info = pageInfo(name);
    if (current && current.name === name && !force) return;
    if (!bypassDirtyGuard && isCurrentDirty() && current && current.name !== name) {
      confirmLeaveIfDirty(function () { route(force); });
      return;
    }
    bypassDirtyGuard = false;
    activeHash = location.hash || ('#/' + name);
    var token = ++mountToken;

    // 导航项用 data-nav 而不是 data-page：页面里有人的兜底挂载会 querySelector
    // ('[data-page="settings"]') 找容器，用 data-page 会让页面渲染进导航栏里
    Array.prototype.forEach.call(document.querySelectorAll('#tabs a'), function (a) {
      a.classList.toggle('on', a.dataset.nav === name);
    });
    document.title = 'Prism · ' + info.label;

    var view = document.getElementById('view') || document.querySelector('main.view') || document.querySelector('main');
    if (!view) {
      if (window.console && console.error) console.error('[route] #view 容器未就绪，路由暂缓执行');
      return;
    }
    runCleanup();
    state.page = name;

    // 若模块已在内存就绪，直接挂载，杜绝闪烁与布局抖动；若未就绪，延时 70ms 呈现轻量加载条
    var isCached = !!(registry[name] || (window.PrismPages && window.PrismPages[name]));
    var loadTimer = null;
    if (!isCached) {
      loadTimer = setTimeout(function () {
        if (token === mountToken && !current) {
          clear(view).appendChild(ui.loading('加载中…'));
        }
      }, 70);
    }

    loadPage(name).then(function (def) {
      if (loadTimer) clearTimeout(loadTimer);
      if (token !== mountToken) return;      // 已经切走了，这次加载的结果丢掉
      var cleanup = [];
      var ctx = {
        page: name,
        api: api,
        ui: ui,
        state: state,
        bus: bus,
        navigate: navigate,
        toast: toast,
        dialog: dialog,
        setThemeMode: setThemeMode,
        onUnmount: function (fn) { if (typeof fn === 'function') cleanup.push(fn); },
        reload: function () { route(true); }
      };
      current = { name: name, def: def, cleanup: cleanup, instance: null };
      var host = clear(view);
      var out;
      try { out = def.mount(host, ctx); }
      catch (e) { out = Promise.reject(e); }
      return Promise.resolve(out).then(function (inst) {
        if (inst && typeof inst === 'object') current.instance = inst;
        if (token !== mountToken) return;
        // mount 是异步的，期间可能什么都没画。给个兜底，绝不白屏
        if (!host.firstChild) {
          host.appendChild(ui.empty('页面未生成内容',
            '「' + info.label + '」页未返回有效渲染内容。',
            [ui.button('重试', function () { route(true); }, 'pri')]));
        }
      }, function (e) {
        if (token !== mountToken) return;
        if (window.console && console.error) console.error('[' + name + '] mount 失败', e);
        clear(view).appendChild(ui.error(e, function () { route(true); },
          '「' + info.label + '」页渲染出错。'));
      });
    }).catch(function (e) {
      if (token !== mountToken) return;
      if (window.console && console.error) console.error('[' + name + '] 模块加载失败', e);
      clear(view).appendChild(missingPanel(info, e));
    });

    rememberPage(name);
  }

  /* ══ 7. 状态灯（每 5 秒） ═══════════════════════════════════ */

  var POLL_MS = 5000;
  var pollTimer = null;
  var themeTimer = null;

  function setLight(id, state, titleText) {
    var el = document.getElementById(id);
    if (!el) return;
    var dot = el.querySelector('.dot');
    if (dot) dot.className = 'dot ' + state;
    if (titleText) el.title = titleText;
  }

  /* 顶栏那两个端口号。以前它们是 index.html 里的死文本，setLight 只改 dot 的
     class 和 title，从不碰数字——控制台跑在 8395、网关指向 65123 时顶栏还写着
     8317 / 8318，页脚却是对的。数字一律由这里按实际地址写进去。 */
  function setPort(id, value) {
    var el = document.getElementById(id);
    if (el) el.textContent = String(value);
  }

  // 契约里 gateway.version 是 "7.3.9"、latest 是 "v7.3.18"（一个带 v 一个不带），
  // 显示前统一成单前缀，免得出来 "vv7.3.18"
  function vtag(s) {
    if (!s) return '';
    return 'v' + String(s).replace(/^v/i, '');
  }

  /* 网关身份：端口在听 ≠ 在听的是本目录这套配置。实现与四态语义见
     app/core/identity.py，页面上怎么讲这件事见 static/pages/monitor.js。

     状态栏以前只看 g.running，于是监控卡片上写着"不是本目录的配置"、同一条状态栏
     亮着绿字"网关 在线 :8317"——一页两个说法。和托盘那次是同一个毛病，所以这里
     跟着卡片一起改：只有 identity 是 ok 才配说"在线"。

     后端没给这个字段（旧版后端）或不认识这个值时一律当 unknown，**不许当 ok**。 */
  function gwIdentity(g) {
    var raw = typeof g.identity === 'string' ? g.identity : '';
    return (raw === 'ok' || raw === 'foreign' || raw === 'unknown' || raw === 'down')
      ? raw : 'unknown';
  }

  function renderStatus(m, err) {
    var gw = document.getElementById('sbGw');
    var ctl = document.getElementById('sbCtl');
    var ver = document.getElementById('sbVer');
    var routeEl = document.getElementById('sbRoute');
    var modelEl = document.getElementById('sbModel');
    var timeEl = document.getElementById('sbTime');
    var setText = function (el, s) { if (el) { clear(el); el.appendChild(s); } };

    if (!m) {
      setLight('liveGw', 'idle', '拿不到监控数据，网关端口未知');
      setLight('liveCtl', 'off', err ? api.shortErr(err) : '控制台未响应');
      // 控制台端口不依赖监控接口：页面就是它端出来的
      setPort('liveCtlPort', consolePort());
      setText(gw, h('span', null, [h('span', { class: 'dot idle' }), ' 网关 未知']));
      setText(ctl, h('span', { class: 'bad' }, '控制台 未响应'));
      setText(ver, h('span', { text: '版本 —' }));
      setText(routeEl, h('span', { text: '数据不可用' }));
      setText(modelEl, h('span', { text: '—' }));
      setText(timeEl, h('span', { text: '刷新 ' + clock(Date.now()) }));
      if (gw) gw.title = err ? api.shortErr(err) : '';
      return;
    }

    var g = m.gateway || {};
    // 网关端口取监控数据里的（core\health.py 认 PRISM_GATEWAY_BASE），拿不到就写
    // "未知"，不写死 8317
    var gwPort = text(g.port, '未知');
    // 三种说法的措辞、灯色、字色一一对上。foreign / unknown 都走 warn（黄），
    // 不绿——绿的意思是"你看到的就是本目录这套的"。
    var ident = gwIdentity(g);
    var gwLabel, gwLight, gwClass;
    if (!g.running) {
      gwLabel = '未监听'; gwLight = 'off'; gwClass = 'bad';
    } else if (ident === 'ok') {
      gwLabel = '在线'; gwLight = 'ok'; gwClass = 'ok';
    } else if (ident === 'foreign') {
      gwLabel = '在线（配置不匹配）'; gwLight = 'warn'; gwClass = 'warn';
    } else {
      // unknown，以及"端口通、identity 却是 down"这种自相矛盾——都只敢说"未核实"
      gwLabel = '身份未核实'; gwLight = 'warn'; gwClass = 'warn';
    }
    setLight('liveGw', gwLight, '网关 ' + gwPort + '：' + gwLabel
      + (g.version ? ' · ' + vtag(g.version) : '')
      + (g.identity_note ? ' · ' + g.identity_note : ''));
    setPort('liveGwPort', gwPort);
    setLight('liveCtl', 'ok', '控制台在线 · ' + (location.host || '本机'));
    setPort('liveCtlPort', consolePort());
    setText(gw, h('span', null, [
      h('span', { class: 'dot ' + gwLight }),
      h('span', { class: gwClass, text: ' 网关 :' + gwPort })
    ]));
    setText(ctl, h('span', { class: 'ok', text: '控制台 :' + (location.port || consolePort()) }));

    if (ver) { clear(ver); }
    if (routeEl) { clear(routeEl); }
    if (modelEl) { clear(modelEl); }
    setText(timeEl, h('span', { title: '同步时间', text: clock(Date.now()) }));
  }

  function poll() {
    if (document.hidden) return;
    state.lastPollAt = Date.now();
    api.get('api/monitor', null, { timeout: 4000 }).then(function (m) {
      state.monitor = m; state.monitorError = null;
      renderStatus(m, null);
      bus.emit('monitor', m);
    }, function (e) {
      state.monitor = null; state.monitorError = e;
      renderStatus(null, e);
      bus.emit('monitor', null);
    });
  }

  /* ══ 7.4 外观主题（跟随系统 / 手动切换）════════════════════════════════
     权威在 Python 侧：server.py 服务 index.html 时已经把 data-theme 写进 <html>，
     首屏不会先白后黑闪烁。支持 [跟随系统 | 浅色 | 深色] 模式，并持久化到本地与联动后端。 */
  function applyTheme(t) {
    var mode = 'system';
    try { mode = localStorage.getItem('prism_theme_mode') || 'system'; } catch (e) {}
    var next;
    if (mode === 'dark') next = 'dark';
    else if (mode === 'light') next = 'light';
    else next = (t === 'dark') ? 'dark' : 'light';
    var root = document.documentElement;
    if (root.getAttribute('data-theme') !== next) root.setAttribute('data-theme', next);
  }

  function setThemeMode(mode) {
    if (mode !== 'dark' && mode !== 'light' && mode !== 'system') mode = 'system';
    try { localStorage.setItem('prism_theme_mode', mode); } catch (e) {}
    if (mode === 'system') pollTheme();
    else applyTheme(mode);
  }

  function pollTheme() {
    var mode = 'system';
    try { mode = localStorage.getItem('prism_theme_mode') || 'system'; } catch (e) {}
    if (mode === 'dark' || mode === 'light') {
      applyTheme(mode);
      return;
    }
    if (document.hidden) return;
    api.get('api/theme', null, { timeout: 4000 }).then(function (d) {
      if (d && d.theme) applyTheme(d.theme);
    }, function () { /* 主题不是关键路径：读不到就保持现状，不打扰用户 */ });
  }

  function startPolling() {
    poll();
    pollTheme();
    pollTimer = setInterval(poll, POLL_MS);
    themeTimer = setInterval(pollTheme, POLL_MS);
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) { poll(); pollTheme(); }
    });
    window.addEventListener('online', poll);
  }

  /* ══ 7.5 窗口按钮（自绘，替代被摘掉的原生标题栏）═════════════
     前提：main.py 的 hide_native_titlebar() 已经摘掉 WS_CAPTION，所以窗口没有
     系统画的最小化/最大化/关闭了。pywebview 没有 Electron 的 titleBarOverlay，
     这三个按钮和接线都在这一层。

     在浏览器里调试时 window.pywebview 不存在 —— 那时把整组藏起来，其余照跑，
     所以同一份前端在浏览器和 Prism 窗口里都能用。 */
  /* ══ 7.5 窗口按钮与无边框拖拽/缩放（自绘，替代被摘掉的原生标题栏）═════
     main.py 的 hide_native_titlebar() 摘掉了 WS_CAPTION，窗口物理区域由客户区全覆盖。
     窗口拖拽与八方向边缘缩放均基于 Pointer Events 的 setPointerCapture 全局指针捕获，
     配合原生物理光标追踪与 requestAnimationFrame 帧率节流，跨屏、高 DPI 均完美跟手。 */
  var _controlsBound = false;
  var _resizersCreated = false;

  function getWindowApi() {
    return (window.pywebview && window.pywebview.api) ? window.pywebview.api : null;
  }

  function ensureResizers() {
    if (_resizersCreated) return;
    _resizersCreated = true;

    var RESIZE_EDGES = [
      { cls: 'r-top', edge: 12, dir: 'top' },
      { cls: 'r-bottom', edge: 15, dir: 'bottom' },
      { cls: 'r-left', edge: 10, dir: 'left' },
      { cls: 'r-right', edge: 11, dir: 'right' },
      { cls: 'r-tl', edge: 13, dir: 'tl' },
      { cls: 'r-tr', edge: 14, dir: 'tr' },
      { cls: 'r-bl', edge: 16, dir: 'bl' },
      { cls: 'r-br', edge: 17, dir: 'br' }
    ];

    RESIZE_EDGES.forEach(function (cfg) {
      var el = document.createElement('div');
      el.className = 'win-resizer ' + cfg.cls;

      var isResizing = false;
      var rafPending = false;

      function onPointerMove(ev) {
        if (!isResizing) return;
        if (!rafPending) {
          rafPending = true;
          requestAnimationFrame(function () {
            rafPending = false;
            if (!isResizing) return;
            var api = getWindowApi();
            if (api && typeof api.resize_move === 'function') {
              try { api.resize_move(); } catch (err) {}
            }
          });
        }
      }

      function onPointerEnd(ev) {
        if (!isResizing) return;
        isResizing = false;
        try { el.releasePointerCapture(ev.pointerId); } catch (err) {}
        el.removeEventListener('pointermove', onPointerMove);
        el.removeEventListener('pointerup', onPointerEnd);
        el.removeEventListener('pointercancel', onPointerEnd);
        var api = getWindowApi();
        if (api && typeof api.resize_end === 'function') {
          try { api.resize_end(); } catch (err) {}
        }
      }

      el.addEventListener('pointerdown', function (e) {
        if (e.button !== 0) return;
        e.preventDefault();
        e.stopPropagation();

        var api = getWindowApi();
        if (api && typeof api.resize_start === 'function') {
          try { api.resize_start(cfg.dir); } catch (err) {}
        } else if (api && typeof api.start_resize === 'function') {
          try { api.start_resize(cfg.edge); } catch (err) {}
        }

        isResizing = true;
        try { el.setPointerCapture(e.pointerId); } catch (err) {}
        el.addEventListener('pointermove', onPointerMove);
        el.addEventListener('pointerup', onPointerEnd);
        el.addEventListener('pointercancel', onPointerEnd);
      });

      document.body.appendChild(el);
    });
  }

  function bindWindowControls() {
    ensureResizers();

    var box = document.getElementById('wctl');
    if (!box) return;
    var api = getWindowApi();
    if (!api) {
      box.style.display = 'none';
      return;
    }
    box.style.display = '';

    if (_controlsBound) return;
    _controlsBound = true;

    var btnMax = document.getElementById('wMax');

    function call(name) {
      return function () {
        var a = getWindowApi();
        if (a && typeof a[name] === 'function') {
          try { a[name](); } catch (e) { if (window.console) console.error('[wctl] ' + name, e); }
        }
      };
    }
    /* 关闭前走脏检查守卫：有未保存修改时先 flushSave / 弹确认，别让配置静默丢失 */
    var onMin = call('minimize'), onMax = call('toggle_maximize'), rawClose = call('close');
    var onClose = function () { protectLeave(rawClose); };
    var b;
    if ((b = document.getElementById('wMin'))) b.addEventListener('click', onMin);
    if (btnMax) btnMax.addEventListener('click', onMax);
    if ((b = document.getElementById('wClose'))) b.addEventListener('click', onClose);

    /* 最大化状态一律问系统的 IsZoomed，不自己记 —— 双击顶栏、拖到屏幕边缘贴边、
       Win+↑ 都会改变它，自己维护一个布尔值必然和真实状态漂移。 */
    function syncMax() {
      if (!btnMax) return;
      var a = getWindowApi();
      if (!a || typeof a.is_maximized !== 'function') return;
      Promise.resolve(a.is_maximized()).then(function (v) {
        var isMax = !!v;
        btnMax.classList.toggle('max', isMax);
        document.body.classList.toggle('is-maximized', isMax);
        btnMax.title = isMax ? '还原' : '最大化';
        btnMax.setAttribute('aria-label', isMax ? '还原' : '最大化');
      }, function () {});
    }
    syncMax();
    window.addEventListener('resize', syncMax);

    /* 按住顶栏空白 = 鼠标拖拽窗口（全局指针捕获 + Win32 绝对物理光标跟踪）。
       排除 a, button, input, select, textarea, .wctl, .tabs a, #kbdHint 等交互元素。 */
    var top = document.querySelector('.top');
    if (top) {
      var isDragging = false;
      var rafPending = false;

      function onDragMove(ev) {
        if (!isDragging) return;
        if (!rafPending) {
          rafPending = true;
          requestAnimationFrame(function () {
            rafPending = false;
            if (!isDragging) return;
            var a = getWindowApi();
            if (a && typeof a.drag_move === 'function') {
              try { a.drag_move(); } catch (err) {}
            }
          });
        }
      }

      function onDragEnd(ev) {
        if (!isDragging) return;
        isDragging = false;
        try { top.releasePointerCapture(ev.pointerId); } catch (err) {}
        top.removeEventListener('pointermove', onDragMove);
        top.removeEventListener('pointerup', onDragEnd);
        top.removeEventListener('pointercancel', onDragEnd);
        var a = getWindowApi();
        if (a && typeof a.drag_end === 'function') {
          try { a.drag_end(); } catch (err) {}
        }
      }

      top.addEventListener('pointerdown', function (e) {
        if (e.button !== 0) return;
        var t = e.target;
        if (t && t.closest && t.closest('a,button,input,select,textarea,.wctl,.tabs a,#kbdHint,.kbd')) return;
        e.preventDefault();

        var a = getWindowApi();
        if (a && typeof a.drag_start === 'function') {
          try { a.drag_start(); } catch (err) {}
        } else if (a && typeof a.drag === 'function') {
          try { a.drag(); } catch (err) {}
        }

        isDragging = true;
        try { top.setPointerCapture(e.pointerId); } catch (err) {}
        top.addEventListener('pointermove', onDragMove);
        top.addEventListener('pointerup', onDragEnd);
        top.addEventListener('pointercancel', onDragEnd);
      });

      top.addEventListener('dblclick', function (e) {
        var t = e.target;
        if (t && t.closest && t.closest('a,button,input,select,textarea,.tabs,.navr,.wctl')) return;
        onMax();
      });
    }
  }

  /* ══ 8. 启动 ════════════════════════════════════════════════ */

  function boot() {
    ensureResizers();
    var kbd = document.getElementById('kbdHint');
    if (kbd) { kbd.style.cursor = 'default'; kbd.addEventListener('click', openPalette); }

    // 控制台端口不等轮询：页面就是控制台端出来的，先把顶栏那两个数写成真的，
    // 免得监控接口不通时顶栏一直停在占位符上
    setPort('liveCtlPort', consolePort());

    // pywebview 的 api 是异步注入的：已经在就绑，不在就等 pywebviewready。
    // 再兜一次底——浏览器里调试时永远等不到那个事件，2 秒后把按钮组藏掉。
    if (window.pywebview && window.pywebview.api) {
      bindWindowControls();
    } else {
      window.addEventListener('pywebviewready', bindWindowControls);
      setTimeout(function () {
        if (!(window.pywebview && window.pywebview.api)) {
          var wc = document.getElementById('wctl');
          if (wc) wc.style.display = 'none';
        }
      }, 2000);
    }

    // 全局拦截默认 contextmenu 浏览器右键菜单，彻底杜绝在空白处、顶栏和按钮上弹出 Edge 原生右键菜单
    window.addEventListener('contextmenu', function (e) {
      e.preventDefault();
    });

    function triggerSilentRefresh() {
      var cur = current;
      var inst = cur && cur.instance;
      var def = cur && cur.def;
      var refreshed = false;

      // **有未保存的改动时绝不能就地重载。** 页面导出的 refresh()/reload() 会按服务端重置
      // 草稿（home.js 的 reload → applyState 重置 sel/draft/origin，表单 DOM 也重建），
      // 而 F5 在用户眼里只是"刷新数据" —— 那是静默丢弃改动（审查 N3-04）。
      // 能自动保存的页面（导出 flushSave，如设置页）先保存再刷新；不能保存的（首页表单）
      // 明确告知并**跳过刷新**，绝不静默丢。
      if (isCurrentDirty()) {
        var flushOwner = (inst && typeof inst.flushSave === 'function') ? inst
                       : (def && typeof def.flushSave === 'function') ? def : null;
        if (!flushOwner) {
          toast('当前有未保存的改动，请先保存', 'warn');
          return;
        }
        Promise.resolve(flushOwner.flushSave()).then(function () {
          // **必须重新判一次是否还脏。** `flushSave()` 有可能"noop 立即 resolve"——
          // 例如设置页在保存飞行中时 targets 为空、直接回 {noop:true} 而**不等**那次写。
          // 这时 isDirty() 仍为真（它把 saving 也算脏），无条件递归就变成**无限微任务
          // 循环**：浏览器再也排不上宏任务，正在飞的那次 POST 的完成回调也永远排不上 →
          // 整页永久卡死（复查轮 4 的 #1，实测复现）。
          // 这里加收敛条件：还脏就停手并如实说明，绝不递归。
          if (isCurrentDirty()) {
            toast('保存中，请稍后再试', 'warn');
            return;
          }
          toast('配置已自动保存', 'ok');
          triggerSilentRefresh();
        }).catch(function (err) {
          toast('自动保存失败：' + ((err && err.message) || err), 'bad');
        });
        return;
      }

      // 1. 若当前页面对象或实例导出了 refresh()，优先调用
      if (inst && typeof inst.refresh === 'function') {
        try {
          var r = inst.refresh();
          refreshed = true;
          Promise.resolve(r).catch(function () {});
        } catch (e) {}
      } else if (def && typeof def.refresh === 'function') {
        try {
          var r2 = def.refresh();
          refreshed = true;
          Promise.resolve(r2).catch(function () {});
        } catch (e) {}
      } else if (inst && typeof inst.reload === 'function') {
        // 2. 若当前页面实例导出了 reload()（例如 settings.js 的 reload）
        try {
          var r3 = inst.reload();
          refreshed = true;
          Promise.resolve(r3).catch(function () {});
        } catch (e) {}
      }

      // 3. 兜底重新触发路由 mount 当前页（静默重新获取页面组件）
      if (!refreshed) {
        route(true);
      }

      // 4. 同步触发状态灯轮询与主题轮询
      poll();
      pollTheme();
    }

    document.addEventListener('keydown', function (e) {
      // 0. 弹窗打开时拦截页面导航键（Ctrl+1..6 / Ctrl+, / Ctrl+K / F5），只放行
      //    Tab 与 Esc 交给弹窗自身的焦点循环与关闭逻辑
      if (dlgOpen && e.key !== 'Tab' && e.key !== 'Escape' &&
          ((e.ctrlKey || e.metaKey || e.altKey) || e.key === 'F5' || e.keyCode === 116)) {
        e.preventDefault();
        return;
      }

      // 1. Ctrl+K / Cmd+K 命令面板
      if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault();
        openPalette();
        return;
      }

      // 2. Ctrl+, / Cmd+, 快捷跳转至设置页
      if ((e.ctrlKey || e.metaKey) && (e.key === ',' || e.key === '，')) {
        e.preventDefault();
        navigate('#/settings');
        return;
      }

      // 3. Ctrl+1..6 / Cmd+1..6 快速切换 6 个主页面
      if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && /^[1-6]$/.test(e.key)) {
        var pageMap = {
          '1': '#/home',
          '2': '#/usage',
          '3': '#/monitor',
          '4': '#/logs',
          '5': '#/control',
          '6': '#/settings'
        };
        var targetHash = pageMap[e.key];
        if (targetHash) {
          e.preventDefault();
          navigate(targetHash);
          return;
        }
      }

      // 4. F5 与 Ctrl+R / Cmd+R 拦截浏览器默认刷新，就地静默数据刷新
      var isF5 = (e.key === 'F5' || e.keyCode === 116);
      var isCtrlR = (e.ctrlKey || e.metaKey) && (e.key === 'r' || e.key === 'R' || e.keyCode === 82);
      if (isF5 || isCtrlR) {
        e.preventDefault();
        triggerSilentRefresh();
        return;
      }
    });

    window.addEventListener('hashchange', function () {
      if (bypassDirtyGuard) {
        bypassDirtyGuard = false;
        activeHash = location.hash;
        route();
        return;
      }
      var targetHash = location.hash;
      var targetName = parseHash();
      if (current && current.name === targetName) {
        activeHash = targetHash;
        return;
      }
      if (isCurrentDirty()) {
        // **绝不在确认之前置 bypassDirtyGuard。** 之前先置 true 再调 confirmLeaveIfDirty，
        // 而 protectLeave 第一句就是 `if (bypassDirtyGuard || ...)` → 直接放行、弹窗一次
        // 都不弹，未保存的改动被静默丢弃并跳到目标页（审查 N3-03；触发路径：
        // 脏页面上按鼠标侧键 / Alt+← 后退）。
        //
        // 把 hash 退回原处这一步**不需要** bypass：hashchange 是异步的，等它再触发时
        // 页面名与 targetName 相同，会走上面的"同页"早退分支，既不递归也不弹窗。
        location.hash = activeHash;
        confirmLeaveIfDirty(function () {
          // 只在真正放行后才置 bypass，让下面这次赋值不被再拦一次
          bypassDirtyGuard = true;
          activeHash = targetHash;
          location.hash = targetHash;
        });
        return;
      }
      activeHash = targetHash;
      route();
    });

    document.addEventListener('click', function (e) {
      var a = e.target && e.target.closest && e.target.closest('#tabs a');
      if (!a) return;
      var targetHash = a.getAttribute('href');
      if (!targetHash) return;
      e.preventDefault();
      navigate(targetHash);
    });

    window.addEventListener('beforeunload', function (e) {
      if (isCurrentDirty()) {
        e.preventDefault();
        e.returnValue = '';
      }
    });

    // 全局兜底：任何没人接的错都不该让界面停摆；若当前视口为空则显示错误面板，杜绝白屏
    window.addEventListener('error', function (e) {
      if (window.console && console.error) console.error('[window.onerror]', e.message || e);
      var v = document.getElementById('view');
      if (v && (!v.firstChild || v.querySelector('#boot'))) {
        clear(v).appendChild(ui.error(e.error || e.message || '界面加载异常', function () { location.reload(); }, '可点击重试重新加载控制台'));
      }
    });
    window.addEventListener('unhandledrejection', function (e) {
      if (window.console && console.error) console.error('[unhandledrejection]', e.reason);
      var v = document.getElementById('view');
      if (v && (!v.firstChild || v.querySelector('#boot'))) {
        clear(v).appendChild(ui.error(e.reason || '后台任务异常', function () { location.reload(); }, '可点击重试重新加载控制台'));
      }
    });

    try {
      route();
    } catch (routeErr) {
      if (window.console && console.error) console.error('[route error]', routeErr);
      var viewEl = document.getElementById('view');
      if (viewEl) {
        clear(viewEl).appendChild(ui.error(routeErr, function () { location.reload(); }, '路由初始化异常'));
      }
    }
    startPolling();

    // 调试入口：控制台里 Prism.api.get('api/state').then(console.log)
    window.Prism = window.Prism || {};
    window.Prism.version = '0.1.0';
    window.Prism.pages = registry;
    window.Prism.registerPage = registerPage;
    window.Prism.api = api;
    window.Prism.ui = ui;
    window.Prism.h = h;
    window.Prism.clear = clear;
    window.Prism.append = append;
    window.Prism.state = state;
    window.Prism.bus = bus;
    window.Prism.route = route;
    window.Prism.setThemeMode = setThemeMode;
    window.Prism.pagesMeta = PAGES;
  }

  // 页面脚本可能先于本文件执行完（虽然 defer 有序，但别指望），
  // 所以注册接口和公共 UI 工具要在解析阶段就挂上
  window.PrismUI = ui;
  window.Prism = window.Prism || {};
  window.Prism.ui = ui;
  window.Prism.registerPage = registerPage;
  window.Prism.pages = registry;

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();

})();
