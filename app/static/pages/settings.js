/* Prism · 设置页  (app\static\pages\settings.js)
 *
 * 契约（INTERFACES.md「GET /api/settings 返回」）：
 *   GET  /api/settings → { gateway:{debug, proxy_url, request_retry, request_log},
 *                          app:{autostart, close_to_tray, sample_interval_sec, retention_days} }
 *   POST /api/settings ← 同结构。整份回传：后端自己 diff，再逐项
 *                        PUT /v0/management/<key>，应用项写 app\settings.json。
 *
 * 这一页刻意不做的两件事：
 *   ① 绝不整份 PUT /v0/management/config —— 实测它返回 48 个键而文件只有 26 个顶层键，
 *      整份写回会丢 host / port / auth-dir / remote-management。逐项写是 server.py 的活。
 *   ② 不放「控制台端口」—— route_selector.Handler.check 拿模块级常量 PORT 校验 Host，
 *      改端口会让所有请求 409，所以这项从设置页拿掉而不是做进配置。
 *
 * 页面注册（app.js 未落地前先兼容多种约定）：
 *   window.PrismPages.settings = page
 *   同写 window.Pages.settings，并尝试 window.registerPage / window.Prism.registerPage
 * mount(容器或 {container|root|el}) 返回 {unmount}；容器找不到时回落到
 *   [data-page="settings"] / #page-settings / #view。
 */
(function (global) {
  'use strict';

  var PAGE_ID = 'settings';
  var STYLE_ID = 'ps-settings-style';

  /* ── 字段定义 ──────────────────────────────────────────────
   * scope 决定 POST 时挂到 gateway 还是 app 下——这个分法跟服务器端
   * 「网关项走管理 API、应用项走 settings.json」一一对应，不能混。 */
  var FIELDS = [
    {
      id: 'g-debug', scope: 'gateway', key: 'debug', kind: 'bool',
      label: 'Debug 模式',
      hint: '记录网关内部调试日志'
    },
    {
      id: 'g-proxy', scope: 'gateway', key: 'proxy_url', kind: 'text', mono: true,
      placeholder: 'http://127.0.0.1:7897',
      label: '上游代理',
      hint: '留空表示直连'
    },
    {
      id: 'g-retry', scope: 'gateway', key: 'request_retry', kind: 'int', min: 0, max: 99, unit: '次',
      label: '请求重试次数',
      hint: '0 表示不重试'
    },
    {
      id: 'g-reqlog', scope: 'gateway', key: 'request_log', kind: 'bool',
      label: '请求日志',
      hint: '落盘记录每笔请求内容'
    },
    {
      id: 'a-auto', scope: 'app', key: 'autostart', kind: 'bool',
      label: '开机自启',
      hint: '系统登录后自动启动'
    },
    {
      id: 'a-close', scope: 'app', key: 'close_to_tray', kind: 'seg',
      label: '关闭窗口时',
      hint: '窗口关闭后保持后台运行',
      options: [{ v: true, t: '到托盘' }, { v: false, t: '直接退出' }]
    },
    {
      id: 'a-theme', scope: 'app', key: 'theme_mode', kind: 'seg',
      label: '外观主题',
      hint: '',
      options: [
        { v: 'system', t: '跟随系统' },
        { v: 'light', t: '浅色' },
        { v: 'dark', t: '深色' }
      ]
    },
    {
      id: 'a-interval', scope: 'app', key: 'sample_interval_sec', kind: 'int', min: 10, max: 86400, unit: '秒',
      label: '用量采样间隔',
      hint: '默认 600 秒'
    },
    {
      id: 'a-retention', scope: 'app', key: 'retention_days', kind: 'int', min: 1, max: 3650, unit: '天',
      label: '历史保留上限',
      hint: '超期采样记录自动清理'
    }
  ];

  var ADVANCED_URL = 'http://127.0.0.1:8317/management.html';

  /* ── 小工具 ─────────────────────────────────────────────── */

  function h(tag, attrs, kids) {
    var el = document.createElement(tag);
    if (attrs) {
      for (var k in attrs) {
        if (!Object.prototype.hasOwnProperty.call(attrs, k)) continue;
        var v = attrs[k];
        if (v === null || v === undefined) continue;
        if (k === 'text') el.textContent = String(v);
        else if (k === 'class') el.className = v;
        else if (k.indexOf('on') === 0 && typeof v === 'function') el.addEventListener(k.slice(2), v);
        else el.setAttribute(k, String(v));
      }
    }
    if (kids) {
      for (var i = 0; i < kids.length; i++) {
        if (kids[i] === null || kids[i] === undefined) continue;
        el.appendChild(typeof kids[i] === 'string' ? document.createTextNode(kids[i]) : kids[i]);
      }
    }
    return el;
  }

  function clock() {
    var d = new Date();
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  /* 兼容 app.js 的 fetch 封装：它的 get/post 可能返回 {ok,data}，也可能直接返回 data。 */
  function unwrap(r) {
    if (r && typeof r === 'object' && Object.prototype.hasOwnProperty.call(r, 'ok')) {
      if (r.ok !== true) throw new Error(r.error || '请求失败');
      return r.data;
    }
    return r;
  }

  var transport = {
    get: function (path) {
      var api = global.Prism && global.Prism.api;
      if (api && typeof api.get === 'function') {
        return Promise.resolve(api.get(path)).then(unwrap);
      }
      return raw('GET', path, undefined);
    },
    post: function (path, body) {
      var api = global.Prism && global.Prism.api;
      if (api && typeof api.post === 'function') {
        return Promise.resolve(api.post(path, body)).then(unwrap);
      }
      return raw('POST', path, body);
    }
  };

  function raw(method, path, body) {
    return fetch(path, {
      method: method,
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body)
    }).then(function (res) {
      return res.text().then(function (txt) {
        var payload = null;
        try { payload = txt ? JSON.parse(txt) : null; } catch (e) { payload = null; }
        if (payload === null) {
          // 后端挂掉时 WebView2 可能直接给出非 JSON 页面，别把它当成“保存成功”。
          throw new Error('控制台没有返回 JSON（HTTP ' + res.status + '），后端可能已退出');
        }
        if (payload.ok !== true) throw new Error(payload.error || ('请求失败 HTTP ' + res.status));
        return payload.data;
      });
    }, function () {
      throw new Error('连不上 Prism 控制台，确认后端还在跑');
    });
  }

  /* ── 样式（只加 ps* 前缀的新类，色板全部走 app.css 的变量，不写兜底色）── */
  var CSS = [
    '.ps-set{display:block}',
    '.ps-set .ps-list{border:1px solid var(--line);border-radius:var(--r-card);background:var(--panel);overflow:hidden}',
    // 列顺序：标签(吃掉剩余空间) | 状态(定宽，免得控件跟着状态文字长度左右跳) | 控件(auto，贴右缘)
    // 与 buildRow() 里 c.row 的子元素顺序一一对应，改一个就得改另一个。
    '.ps-set .ps-row{display:grid;grid-template-columns:minmax(0,1fr) 140px auto;gap:18px;align-items:center;padding:15px 18px;border-bottom:1px solid var(--line)}',
    '.ps-set .ps-row:last-child{border-bottom:0}',
    '.ps-set .ps-row:hover{background:var(--hover)}',
    '.ps-set .ps-lbl{font-size:13.5px;color:var(--fg);min-width:0}',
    '.ps-set .ps-hint{font-size:12px;color:var(--fg3);margin-top:5px;line-height:1.55}',
    '.ps-set .ps-ctl{justify-self:end;display:flex;align-items:center;gap:10px;min-width:0}',
    '.ps-set .ps-in{background:var(--panel);border:1px solid var(--line2);color:var(--fg);font-family:inherit;font-size:13.5px;padding:8px 11px;border-radius:var(--r-ctl);outline:none}',
    '.ps-set .ps-in:focus{border-color:var(--fg3);box-shadow:0 0 0 3px var(--accentdim)}',
    '.ps-set .ps-in.bad{border-color:var(--bad)}',
    '.ps-set .ps-in::placeholder{color:var(--fg3)}',
    '.ps-set .ps-in.ps-num{width:90px;text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums}',
    '.ps-set .ps-in.ps-url{width:260px;font-family:var(--mono);font-size:12.5px}',
    '.ps-set .ps-unit{font-size:12px;color:var(--fg3)}',
    '.ps-set .ps-sw{display:inline-flex;align-items:center;gap:9px;cursor:pointer}',
    '.ps-set .ps-sw input{appearance:none;-webkit-appearance:none;width:36px;height:20px;margin:0;border-radius:var(--r-pill);background:var(--line2);border:1px solid transparent;position:relative;cursor:pointer;transition:background .14s,border-color .14s}',
    '.ps-set .ps-sw input::after{content:"";position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:var(--panel);box-shadow:var(--knob-shadow);transition:transform .14s}',
    '.ps-set .ps-sw input:checked{background:var(--accent);border-color:var(--accent)}',
    '.ps-set .ps-sw input:checked::after{transform:translateX(16px)}',
    '.ps-set .ps-sw input:focus-visible{outline:2px solid var(--fg3);outline-offset:2px}',
    '.ps-set .ps-sw b{font-size:12.5px;font-weight:400;color:var(--fg3);min-width:22px}',
    '.ps-set .ps-sw input:checked ~ b{color:var(--fg)}',
    '.ps-set .ps-seg{display:inline-flex;border:1px solid var(--line2);border-radius:var(--r-ctl);overflow:hidden;background:var(--panel2)}',
    '.ps-set .ps-opt{font-size:12.5px;padding:7px 12px;background:transparent;color:var(--fg2);border:0;border-right:1px solid var(--line2);cursor:pointer}',
    '.ps-set .ps-opt:last-child{border-right:0}',
    '.ps-set .ps-opt:hover{color:var(--fg)}',
    '.ps-set .ps-opt.on{background:var(--panel);color:var(--fg);font-weight:500}',
    '.ps-set .ps-stat{font-size:12px;color:var(--fg3);text-align:right;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    '.ps-set .ps-stat.dirty{color:var(--fg2)}',
    '.ps-set .ps-stat.ok{color:var(--ok)}',
    '.ps-set .ps-stat.err{color:var(--bad)}',
    '.ps-set .ps-banner{border:1px solid var(--baddim);background:var(--badbg);border-radius:var(--r-card);padding:14px 16px;margin-bottom:18px}',
    '.ps-set .ps-banner .t{font-size:13.5px;font-weight:600;color:var(--bad)}',
    '.ps-set .ps-banner .m{font-size:12.5px;color:var(--fg2);margin-top:6px;line-height:1.6;word-break:break-all}',
    '.ps-set .ps-banner .a{margin-top:12px;display:flex;gap:9px}',
    '.ps-set .ps-empty{border:1px dashed var(--line2);border-radius:var(--r-card);padding:28px 18px;text-align:center;font-size:13px;color:var(--fg3)}',
    '.ps-set .ps-note{font-size:12px;color:var(--fg3);line-height:1.7;margin-top:12px}',
    '.ps-set a.ps-btn{text-decoration:none;display:inline-block}'
  ].join('\n');

  function injectStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var st = h('style', { id: STYLE_ID, text: CSS });
    (document.head || document.documentElement).appendChild(st);
  }

  /* ── 取值 / 校验 ────────────────────────────────────────── */

  function readField(def, ctl) {
    if (def.kind === 'bool') return { value: !!ctl.input.checked };
    if (def.kind === 'seg') return { value: ctl.value };
    var s = String(ctl.input.value || '').trim();
    if (def.kind === 'text') {
      if (s === '') return { value: '' };              // 留空 = 关掉代理，是合法值
      if (!/^https?:\/\/\S+$/i.test(s)) {
        return { error: '代理地址要以 http:// 或 https:// 开头，中间不能有空格', value: s };
      }
      return { value: s };
    }
    // int
    if (s === '') return { error: '这一项不能留空', value: s };
    if (!/^-?\d+$/.test(s)) return { error: '只能填整数', value: s };
    var n = parseInt(s, 10);
    if (n < def.min || n > def.max) {
      return { error: '取值范围 ' + def.min + ' – ' + def.max, value: s };
    }
    return { value: n };
  }

  /* ── 降级提示 ──────────────────────────────────────────────
   * 后端 notes 每条都点名了是哪个 key 读不到（"proxy_url 读不到（…），…"），
   * 按 key 名认领到 scope。认不出来的（比如"自启以注册表为准"这种同步提示）不算降级：
   * 那不是读取失败，不该把整段标成"部分项读不到"。 */
  var CACHE_CLAIM = /，下面是上次保存的值\s*$/;

  function notedKeys(note, scope) {
    var s = String(note || '');
    return FIELDS.filter(function (d) {
      return d.scope === scope && s.indexOf(d.key) >= 0;
    }).map(function (d) { return d.key; });
  }

  function noteScope(note) {
    var s = String(note || '');
    if (notedKeys(s, 'gateway').length) return 'gateway';
    if (notedKeys(s, 'app').length) return 'app';
    if (/网关|gateway/i.test(s)) return 'gateway';   // 点不出 key 名时的兜底
    return null;
  }

  /* "下面是上次保存的值"是后端拼的，只在本地 settings.json 真存过这一项时才成立。
   * 首次运行 + 网关读不到时缓存是空的，控件停的是 HTML 默认值，照抄这句话等于声称
   * 展示了一份不存在的缓存。判据前端手上就有：响应里这个 key 有没有值。 */
  function degradeLine(note, data) {
    var s = String(note || '');
    if (!CACHE_CLAIM.test(s)) return s;
    var head = s.replace(CACHE_CLAIM, '');
    var gw = (data && data.gateway) || {};
    var missing = notedKeys(s, 'gateway').filter(function (k) {
      return gw[k] === undefined || gw[k] === null;
    });
    if (missing.length) {
      return head + '；本地也没有可回落的缓存（app\\settings.json 里没有 ' +
             missing.join('、') + '），控件停在默认值';
    }
    return head + '；控件里显示的是上次保存的值';
  }

  /* ── 一处 mount 一个实例 ────────────────────────────────── */

  function mount(arg) {
    injectStyle();

    var container = arg;
    if (container && container.nodeType !== 1) {
      container = container.container || container.root || container.el || null;
    }
    if (!container) {
      container = document.querySelector('[data-page="settings"]') ||
                  document.getElementById('page-settings') ||
                  document.getElementById('page-settings-view');
    }
    if (!container) return null;

    var ctrls = {};        // id -> {input?, value?, opts?, row, stat}
    var status = {};       // id -> {state, msg}
    var debounceTimers = {}; // id -> timerId
    var lastLoadedData = null; // 上次成功读取/保存的快照，供 resetDirty 回滚
    var alive = true;
    var pending = Promise.resolve();
    var loaded = false;
    var loadFailed = false;
    var loadEmpty = false;

    function clearDebounce(id) {
      if (debounceTimers[id]) {
        clearTimeout(debounceTimers[id]);
        delete debounceTimers[id];
      }
    }

    function clearAllDebounce() {
      for (var id in debounceTimers) {
        if (Object.prototype.hasOwnProperty.call(debounceTimers, id)) {
          clearTimeout(debounceTimers[id]);
        }
      }
      debounceTimers = {};
    }

    function hasActiveDebounce() {
      for (var id in debounceTimers) {
        if (Object.prototype.hasOwnProperty.call(debounceTimers, id)) return true;
      }
      return false;
    }
    // 后端返回 200 但内容是降级值时（网关读不到），data.notes 里带中文原因。
    // 不显示它，用户会以为"网关设置被清空了"并且"已经保存"——比不显示更糟。
    //
    // 降级按 scope 分开记，不是一个全局开关：网关四项读不到时 Application 四项
    // 明明都读到了，却被一起标成"部分项读不到"，那是假警报。
    var degradedScope = { gateway: false, app: false };
    var degradeNotes = [];
    var banner = null;
    var listHost = {};

    container.textContent = '';
    var root = h('section', { class: 'ps-set', 'data-page': PAGE_ID });
    container.appendChild(root);

    function setStatus(id, state, msg, full) {
      status[id] = { state: state, msg: msg || '' };
      var c = ctrls[id];
      if (!c) return;
      var txt = '';
      if (state === 'saving') txt = '保存中…';
      else if (state === 'ok') txt = '已保存 ' + clock();
      else if (state === 'dirty') txt = '待保存';
      else if (state === 'err') txt = msg || '保存失败';
      c.stat.textContent = txt;
      c.stat.className = 'ps-stat' + (state === 'idle' ? '' : ' ' + state);
      // 芯片那一列窄，长原因放不下就只留结论，全文挂 title 并由横幅完整展示。
      c.stat.title = full || txt;
      refreshSummary();
    }

    function setStatusAll(state, msg) {
      FIELDS.forEach(function (d) { setStatus(d.id, state, msg); });
    }

    function refreshSummary() {
      ['gateway', 'app'].forEach(function (scope) {
        var host = listHost[scope];
        if (!host) return;
        var bad = 0, dirty = 0, busy = 0;
        FIELDS.forEach(function (d) {
          if (d.scope !== scope) return;
          var st = status[d.id] && status[d.id].state;
          if (st === 'err') bad++;
          else if (st === 'dirty') dirty++;
          else if (st === 'saving') busy++;
        });
        var tail;
        if (loadFailed) tail = '读取失败';
        else if (loadEmpty) tail = '无可读设置';
        else if (!loaded) tail = '读取中…';
        else if (busy) tail = '保存中';
        else if (bad) tail = bad + ' 项保存失败';
        else if (dirty) tail = dirty + ' 项待保存';
        else if (degradedScope[scope]) tail = '部分项读不到';
        else tail = '全部已保存';
        host.textContent = tail;
        host.style.color = (bad || loadFailed || loadEmpty) ? 'var(--bad,#EF4444)'
          : (degradedScope[scope] ? 'var(--warn,#EAB308)'
          : (dirty || busy ? 'var(--accent2,#6366F1)' : ''));
      });
    }

    function collect() {
      var out = { gateway: {}, app: {} };
      FIELDS.forEach(function (d) {
        var r = readField(d, ctrls[d.id]);
        if (!('error' in r)) out[d.scope][d.key] = r.value;
      });
      return out;
    }

    function invalidFields() {
      return FIELDS.filter(function (d) {
        return 'error' in readField(d, ctrls[d.id]);
      });
    }

    /* 焦点是不是还在这个控件上。bool / text 直接比 activeElement；seg 是一组
     * <button>，焦点落在刚点的那一个上，所以要看焦点在不在整组里面。 */
    function isFocused(c) {
      var a = document.activeElement;
      if (!a || !c.input) return false;
      return a === c.input || (typeof c.input.contains === 'function' && c.input.contains(a));
    }

    // 应用服务器返回值：只覆盖不在编辑中的控件，别把用户正在敲的字擦掉。
    //
    // only = 本次提交（targets）的那几项。响应里回显的是**整份**结构——要么是
    // collect() 发出去的整份 payload，要么是后端 settings_payload() 的整份回读。
    // 拿整份回写会把用户在这次请求飞行期间改的、还在排队里的值静默擦掉：
    // 控件自己弹回旧值，随后那次保存又把旧值当成"用户想改成的值"发出去，
    // 芯片还显示"已保存"。所以这里只认 targets，其它字段一个字都不碰。
    function applyValues(vals, only) {
      if (!vals) return;
      (only && only.length ? only : FIELDS).forEach(function (d) {
        var c = ctrls[d.id];
        if (!c) return;
        // targets 里已经被用户又改了一次的（dirty / err），本地值比响应里的新，
        // 保留本地值，交给排队中那次保存。
        if (only && only.length) {
          var st = status[d.id] && status[d.id].state;
          if (st !== 'saving') return;
        }
        var v = vals[d.scope] ? vals[d.scope][d.key] : undefined;
        if (v === undefined || v === null) return;
        if (d.kind === 'bool') {
          if (!isFocused(c)) {
            c.input.checked = !!v;
            syncSwitch(d, c);
          }
        } else if (d.kind === 'seg') {
          if (!isFocused(c)) {
            c.value = v;
            paintSeg(c, v);
          }
        } else {
          if (!isFocused(c)) {
            c.input.value = String(v);
            c.input.className = 'ps-in' + (d.kind === 'int' ? ' ps-num' : (d.mono ? ' ps-url' : ''));
          }
        }
      });
    }

    function clearBanner() {
      if (banner && banner.parentNode) banner.parentNode.removeChild(banner);
      banner = null;
    }

    function showBanner(title, message, actions) {
      clearBanner();
      banner = h('div', { class: 'ps-banner' }, [
        h('div', { class: 't', text: title }),
        h('div', { class: 'm', text: message })
      ]);
      if (actions && actions.length) {
        banner.appendChild(h('div', { class: 'a' }, actions));
      }
      root.insertBefore(banner, root.firstChild);
    }

    function doSave(reasonId) {
      if (!alive) return Promise.resolve();
      var bad = invalidFields();
      if (bad.length) {
        // 有一项不合法就不发请求：后端收到半份数据只会更难收拾。
        bad.forEach(function (d) {
          var r = readField(d, ctrls[d.id]);
          ctrls[d.id].input.classList.add('bad');
          setStatus(d.id, 'err', r.error);
        });
        return Promise.resolve();
      }
      var targets = FIELDS.filter(function (d) {
        var st = status[d.id] && status[d.id].state;
        return st === 'dirty' || st === 'err';
      });
      if (!targets.length) return Promise.resolve();
      targets.forEach(function (d) { setStatus(d.id, 'saving'); });
      clearBanner();
      var payload = collect();
      return transport.post('/api/settings', payload).then(function (data) {
        if (!alive) return { ok: false };
        // 完整结构才拿来当回显；否则用本地 payload（发出去的是什么就是什么）。
        var vals = data && data.gateway && data.app ? data : payload;
        // 回显只作用于本次 targets（见 applyValues 的注释）。顺序要紧：先回显再改状态，
        // 否则 applyValues 里那条 "不是 saving 就不回写" 会把整批都挡掉。
        applyValues(vals, targets);
        try { lastLoadedData = JSON.parse(JSON.stringify(collect())); } catch (e) {}
        targets.forEach(function (d) {
          // 飞行期间又被改过的项，排队里还有一次保存；这里标"已保存"就是撒谎。
          var st = status[d.id] && status[d.id].state;
          if (st === 'saving') setStatus(d.id, 'ok');
        });
        return { ok: true, data: vals };
      }, function (err) {
        if (!alive) return { ok: false };
        var msg = (err && err.message) || String(err);
        targets.forEach(function (d) { setStatus(d.id, 'err', '保存失败', '保存失败：' + msg); });
        showBanner('保存失败', msg, [
          h('button', {
            class: 'btn', type: 'button', text: '重试保存',
            onclick: function () { queueSave().catch(function () {}); }
          })
        ]);
        throw err;
      });
    }

    function queueSave() {
      // `pending` 这条链**必须永远保持 fulfilled**。原先 `pending = pending.then(doSave)`
      // 一旦某次 doSave 抛错，链就变成 rejected，而 `.then(fn)` 在 rejected 的基上会
      // **跳过 fn** —— 之后每次保存都不再发请求，连"重试保存"也没用，直到离开页面重挂载
      // （复查轮 5 的 #A，HIGH）。
      // 现在：本次调用的结果（含失败）单独回给调用方；排队链只负责串行化，永不 rejected。
      var task = pending
        .then(null, function () { /* 前一次失败不该拦住后一次 */ })
        .then(function () { return doSave(); });
      pending = task.then(function () {}, function () {});
      return task;
    }

    function onChange(d) {
      var c = ctrls[d.id];
      var r = readField(d, c);
      if ('error' in r) {
        c.input.classList.add('bad');
        setStatus(d.id, 'err', r.error);
        return;
      }
      c.input.classList.remove('bad');
      setStatus(d.id, 'dirty');
      if (d.key === 'theme_mode') {
        var mode = r.value || 'system';
        try { localStorage.setItem('prism_theme_mode', mode); } catch (e) {}
        if (global.Prism && typeof global.Prism.setThemeMode === 'function') {
          global.Prism.setThemeMode(mode);
        } else {
          var next = (mode === 'dark') ? 'dark' : (mode === 'light' ? 'light' : null);
          if (next) document.documentElement.setAttribute('data-theme', next);
          else {
            fetch('/api/theme').then(function (res) { return res.json(); }).then(function (res) {
              if (res && res.data && res.data.theme) document.documentElement.setAttribute('data-theme', res.data.theme);
            }).catch(function () {});
          }
        }
      }
      queueSave().catch(function () {});   // 失败已由字段状态显示，这里只防未捕获 rejection
    }

    function syncSwitch(d, c) {
      if (c.onoff) c.onoff.textContent = c.input.checked ? '开' : '关';
    }

    /* 分段控件的选中态要同时落到 class 和 aria 上，否则键盘/读屏用户看不出选了哪个。 */
    function paintSeg(c, val) {
      c.opts.forEach(function (o) {
        var on = o.v === val;
        o.node.className = 'ps-opt' + (on ? ' on' : '');
        o.node.setAttribute('aria-checked', on ? 'true' : 'false');
      });
    }

    /* ── 建行 ───────────────────────────────────────────── */
    function buildRow(d) {
      var ctl = h('div', { class: 'ps-ctl' });
      var c = { row: null, stat: null };

      if (d.kind === 'bool') {
        var input = h('input', { type: 'checkbox' });
        var label = h('label', { class: 'ps-sw' }, [input, h('b', { text: '关' })]);
        input.addEventListener('change', function () { syncSwitch(d, c); onChange(d); });
        c.input = input;
        c.onoff = label.querySelector('b');
        ctl.appendChild(label);
      } else if (d.kind === 'seg') {
        c.value = (d.options && d.options[0]) ? d.options[0].v : false;
        c.opts = [];
        var seg = h('div', { class: 'ps-seg', role: 'radiogroup', 'aria-label': d.label });
        d.options.forEach(function (o) {
          var btn = h('button', {
            type: 'button', class: 'ps-opt', text: o.t, role: 'radio'
          });
          btn.addEventListener('click', function () {
            if (c.value === o.v) return;
            c.value = o.v;
            paintSeg(c, o.v);
            onChange(d);
          });
          c.opts.push({ v: o.v, node: btn });
          seg.appendChild(btn);
        });
        paintSeg(c, c.value);
        c.input = seg;   // 只为 .bad / activeElement 判断而存在
        ctl.appendChild(seg);
      } else {
        var cls = 'ps-in' + (d.kind === 'int' ? ' ps-num' : (d.mono ? ' ps-url' : ''));
        var ti = h('input', {
          type: 'text', class: cls, placeholder: d.placeholder || '',
          inputmode: d.kind === 'int' ? 'numeric' : null,
          spellcheck: 'false', autocomplete: 'off'
        });
        ti.addEventListener('input', function () {
          var r = readField(d, c);
          if ('error' in r) {
            ti.classList.add('bad');
            setStatus(d.id, 'err', r.error);
            clearDebounce(d.id);
            return;
          }
          ti.classList.remove('bad');
          setStatus(d.id, 'dirty');
          clearDebounce(d.id);
          debounceTimers[d.id] = setTimeout(function () {
            delete debounceTimers[d.id];
            if (!alive) return;
            var r2 = readField(d, c);
            if ('error' in r2) {
              ti.classList.add('bad');
              setStatus(d.id, 'err', r2.error);
              return;
            }
            onChange(d);
          }, 800);
        });
        ti.addEventListener('change', function () {
          clearDebounce(d.id);
          onChange(d);
        });
        ti.addEventListener('blur', function () {
          var r = readField(d, c);
          ti.classList.toggle('bad', 'error' in r);
          if ('error' in r) {
            clearDebounce(d.id);
            setStatus(d.id, 'err', r.error);
          } else {
            // 失焦时若尚有防抖定时器等待中，立即触发落盘
            if (debounceTimers[d.id]) {
              clearDebounce(d.id);
              onChange(d);
            }
          }
        });
        c.input = ti;
        ctl.appendChild(ti);
        if (d.unit) ctl.appendChild(h('span', { class: 'ps-unit', text: d.unit }));
      }

      var stat = h('span', { class: 'ps-stat' });
      c.stat = stat;
      // ⚠️ 子元素顺序必须与下面 .ps-row 的 grid 列顺序一致：标签 | 状态 | 控件。
      // 控件必须是**最后一列**且列宽 auto，它才贴得住卡片右缘。
      // 踩过的坑：原来是 [标签, 控件, 状态] 配 `1fr auto 150px` —— 控件被 auto 挤在中间，
      // 最右边那 150px 留给几乎永远为空的状态列，于是控件右边永远挂着一大片空白。
      // 改这里的顺序时，同步改 .ps-row 的 grid-template-columns。
      c.row = h('div', { class: 'ps-row' }, [
        h('div', {}, [
          h('div', { class: 'ps-lbl', text: d.label }),
          d.hint ? h('div', { class: 'ps-hint', text: d.hint }) : null
        ]),
        stat,
        ctl
      ]);
      ctrls[d.id] = c;
      status[d.id] = { state: 'idle', msg: '' };
      return c.row;
    }

    function buildSection(comment, title, scope, fields, extraHint) {
      var hint = h('span', { class: 'hint' });
      listHost[scope] = hint;
      var list = h('div', { class: 'ps-list' });
      fields.forEach(function (d) { list.appendChild(buildRow(d)); });
      var sec = h('div', { class: 'sec' }, [
        h('div', { class: 'sechead' }, [
          h('span', { class: 'cmt', text: '//' }),
          h('span', { class: 'stitle', text: title }),
          h('span', { class: 'hr' }),
          hint
        ]),
        list
      ]);
      if (extraHint) sec.appendChild(extraHint);
      return sec;
    }

    function byScope(scope) {
      return FIELDS.filter(function (d) { return d.scope === scope; });
    }

    /* 高级设置单独一行，跟其它行同构，但右边不是控件而是跳转。 */
    function advancedRow() {
      var link = h('a', {
        class: 'btn ps-btn', href: ADVANCED_URL, target: '_blank', rel: 'noopener noreferrer',
        text: '打开官方面板'
      });
      // pywebview 里 target=_blank 交给系统浏览器；万一被拦，退一步用 window.open。
      // 但这里必须先 preventDefault：<a target=_blank> 自己的默认跳转不会因为
      // 调了 window.open 就取消，两个一起走就是一次点击开两个标签（实测过）。
      link.addEventListener('click', function (ev) {
        if (!(link.target === '_blank' && global.open)) return;   // 没兜底手段，让 a 自己跳
        ev.preventDefault();
        try { global.open(ADVANCED_URL, '_blank'); } catch (e) { /* 打不开就算了 */ }
      });
      return h('div', { class: 'ps-row' }, [
        h('div', {}, [
          h('div', { class: 'ps-lbl', text: '高级设置' }),
          h('div', {
            class: 'ps-hint',
            text: '网关底层管理面板'
          })
        ]),
        h('div', { class: 'ps-ctl' }, [
          h('span', { class: 'ps-unit', text: '127.0.0.1:8317' })
        ]),
        h('span', {}, [link])
      ]);
    }

    root.appendChild(buildSection('//', '网关', 'gateway', byScope('gateway')));

    root.appendChild(buildSection('//', '应用', 'app', byScope('app')));

    var advSec = h('div', { class: 'sec' }, [
      h('div', { class: 'sechead' }, [
        h('span', { class: 'cmt', text: '//' }),
        h('span', { class: 'stitle', text: '高级配置' }),
        h('span', { class: 'hr' }),
        null
      ]),
      h('div', { class: 'ps-list' }, [advancedRow()])
    ]);
    root.appendChild(advSec);

    /* ── 载入 ───────────────────────────────────────────── */
    function load() {
      loaded = false;
      loadFailed = false;
      loadEmpty = false;
      degradedScope = { gateway: false, app: false };
      degradeNotes = [];
      clearBanner();
      setStatusAll('idle', '');
      refreshSummary();
      return transport.get('/api/settings').then(function (data) {
        if (!alive) return;
        loaded = true;
        if (!data || (!data.gateway && !data.app)) {
          loaded = false;
          loadEmpty = true;
          setStatusAll('idle', '');
          showBanner('设置项为空', '未读取到有效设置数据。', [
            h('button', { class: 'btn', type: 'button', text: '重新读取', onclick: load })
          ]);
          refreshSummary();
          return;
        }
        applyValues(data);
        try { lastLoadedData = JSON.parse(JSON.stringify(collect())); } catch (e) {}
        var notes = (Array.isArray(data.notes) ? data.notes : []).filter(Boolean);
        if (notes.length) {
          degradeNotes = notes;
          degradedScope = { gateway: false, app: false };
          notes.forEach(function (n) {
            var sc = noteScope(n);
            if (sc) degradedScope[sc] = true;
          });
          showBanner('部分设置读不到',
            notes.map(function (n) { return degradeLine(n, data); }).join('　'), [
            h('button', { class: 'btn', type: 'button', text: '重新读取', onclick: load }),
            h('a', { class: 'btn ps-btn', href: ADVANCED_URL, target: '_blank', rel: 'noopener noreferrer', text: '打开官方面板' })
          ]);
        }
        setStatusAll('idle', '');
        refreshSummary();
      }, function (err) {
        if (!alive) return;
        var msg = (err && err.message) || String(err);
        loaded = false;
        loadFailed = true;
        showBanner('读不到设置', msg, [
          h('button', { class: 'btn', type: 'button', text: '重新读取', onclick: load }),
          h('a', { class: 'btn ps-btn', href: ADVANCED_URL, target: '_blank', rel: 'noopener noreferrer', text: '打开官方面板' })
        ]);
        refreshSummary();
      });
    }

    // 手动保存入口：失败重试、或 user 想确认都靠它。
    var saveBtn = h('button', { class: 'btn', type: 'button', text: '保存改动', onclick: function () { queueSave().catch(function () {}); } });
    var refreshBtn = h('button', { class: 'btn', type: 'button', text: '重新读取', onclick: function () { load(); } });
    var acts = h('div', { class: 'acts' }, [saveBtn, refreshBtn]);
    root.appendChild(acts);

    function isDirty() {
      if (hasActiveDebounce()) return true;
      return FIELDS.some(function (d) {
        var st = status[d.id] && status[d.id].state;
        return st === 'dirty' || st === 'saving' || st === 'err';
      });
    }

    function flushSave() {
      if (!alive) return Promise.resolve({ ok: true });
      clearAllDebounce();
      var bad = invalidFields();
      if (bad.length) {
        bad.forEach(function (d) {
          var r = readField(d, ctrls[d.id]);
          ctrls[d.id].input.classList.add('bad');
          setStatus(d.id, 'err', r.error);
        });
        var firstErr = readField(bad[0], ctrls[bad[0].id]).error;
        return Promise.reject(new Error(bad[0].label + '：' + firstErr));
      }
      var targets = FIELDS.filter(function (d) {
        var st = status[d.id] && status[d.id].state;
        return st === 'dirty' || st === 'err';
      });
      if (!targets.length) {
        // 有字段正**在飞**（saving）时不能立刻 noop-resolve：调用方（F5/Ctrl+R 的自动保存）
        // 会以为"已经保存完了"而立刻刷新，看到的是旧数据；而 isDirty() 仍为真（它把
        // saving 也算脏），调用方可能据此再次触发自动保存 → 循环（复查轮 4 的 #1）。
        // 正确语义：等那笔写落定再回话。
        var saving = FIELDS.some(function (d) {
          var st = status[d.id] && status[d.id].state;
          return st === 'saving';
        });
        if (saving) {
          return pending.then(function () { return { ok: true, waited: true }; });
        }
        return Promise.resolve({ ok: true, noop: true });
      }
      return queueSave().then(function () {
        var stillBad = FIELDS.some(function (d) {
          var st = status[d.id] && status[d.id].state;
          return st === 'err';
        });
        if (stillBad) {
          throw new Error('部分配置项保存失败，请检查控制台连接');
        }
        return { ok: true };
      });
    }

    function resetDirty() {
      if (!alive) return;
      clearAllDebounce();
      if (lastLoadedData) {
        applyValues(lastLoadedData, FIELDS);
      }
      setStatusAll('idle', '');
      FIELDS.forEach(function (d) {
        var c = ctrls[d.id];
        if (c && c.input && c.input.classList) {
          c.input.classList.remove('bad');
        }
      });
      refreshSummary();
    }

    load();

    var inst = {
      id: PAGE_ID,
      root: root,
      reload: load,
      refresh: load,
      isDirty: isDirty,
      flushSave: flushSave,
      resetDirty: resetDirty,
      save: function () { return queueSave(); },
      unmount: function () {
        alive = false;
        clearAllDebounce();
        container.textContent = '';
      }
    };
    return inst;
  }

  /* ── 注册 ───────────────────────────────────────────────── */
  var page = {
    id: PAGE_ID,
    title: '设置',
    order: 5,
    mount: mount,
    render: function (ctx) { return mount(ctx); },
    refresh: function () {
      return page._instance && typeof page._instance.reload === 'function' ? page._instance.reload() : Promise.resolve();
    },
    reload: function () {
      return page._instance && typeof page._instance.reload === 'function' ? page._instance.reload() : Promise.resolve();
    },
    isDirty: function () {
      return page._instance && typeof page._instance.isDirty === 'function' ? page._instance.isDirty() : false;
    },
    flushSave: function () {
      return page._instance && typeof page._instance.flushSave === 'function' ? page._instance.flushSave() : Promise.resolve({ ok: true });
    },
    resetDirty: function () {
      if (page._instance && typeof page._instance.resetDirty === 'function') {
        page._instance.resetDirty();
      }
    },
    unmount: function () {
      var cur = page._instance;
      if (cur && cur.unmount) cur.unmount();
      page._instance = null;
    }
  };

  // mount 结果记在 page 上，便于 app.js 的 destroy 钩子直接调用。
  var rawMount = page.mount;
  page.mount = function (arg) {
    page._instance = rawMount(arg);
    return page._instance;
  };
  page.render = function (ctx) { return page.mount(ctx); };

  function register() {
    if (typeof global === 'undefined' || !global) return;
    global.PrismPages = global.PrismPages || {};
    global.PrismPages[PAGE_ID] = page;
    if (!global.Pages) global.Pages = {};
    if (!global.Pages[PAGE_ID]) global.Pages[PAGE_ID] = page;
    global.PrismSettings = page;            // 便于自测与调试直接拿到实例
    var fns = [
      global.registerPage,
      global.register,
      global.Prism && global.Prism.registerPage,
      global.Prism && global.Prism.register
    ];
    for (var i = 0; i < fns.length; i++) {
      if (typeof fns[i] === 'function') {
        try { fns[i](PAGE_ID, page); } catch (e) { /* 别人的注册表坏了不该拖死这一页 */ }
      }
    }
  }

  register();

  // app.js 还没写完时也能单独挂载：DOM 就绪后，容器若还空着就自己上。
  function autoboot() {
    if (page._instance) return;
    var el = document.querySelector('[data-page="settings"]') ||
             document.getElementById('page-settings');
    if (!el || el.getAttribute('data-prism-autoboot') === 'off') return;
    if (el.childElementCount > 0) return;
    page.mount(el);
  }

  if (global && global.document) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', autoboot);
    } else {
      autoboot();
    }
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = page;
})(typeof window !== 'undefined' ? window : this);
