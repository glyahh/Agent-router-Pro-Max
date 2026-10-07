/* Prism · 控制页
 *
 * 总开关启停本目录的网关。下面是各代理端的开关。
 * Copilot 编辑和 Copilot Agent 不走网关，开关仍是接入 / 断开。
 *
 * 开某个代理端：先拉接入预览，确认后才写。
 * 关某个代理端：断开确认后，只撤回仍是我们写过的字段。
 * 页面上只有开关；失败时留一句错误，成功不提示。
 */
(function (global) {
  'use strict';

  var PAGE_ID = 'control';
  var STYLE_ID = 'pc-control-style';
  function h() {
    var fn = global.Prism && global.Prism.h;
    if (!fn) return null;
    return fn.apply(null, arguments);
  }

  /* 顺序固定。标签以接口返回为准，这里只是还没读到时的占位。 */
  var AGENTS = [
    { id: 'codex', label: 'Codex' },
    { id: 'claude-code', label: 'Claude Code' },
    { id: 'claude-code-desktop', label: 'Claude Code 桌面端' },
    { id: 'opencode', label: 'opencode' },
    { id: 'hermes', label: 'Hermes' },
    { id: 'copilot', label: 'Copilot 编辑' },
    { id: 'copilot-agent', label: 'Copilot Agent' }
  ];

  var CSS = [
    '.pc-list{border:1px solid var(--line);border-radius:var(--r-card);background:var(--panel);overflow:hidden}',
    '.pc-row{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:14px 18px;border-bottom:1px solid var(--line)}',
    '.pc-row:last-child{border-bottom:0}',
    '.pc-name{font-size:13.5px;color:var(--fg)}',
    '.pc-sw{display:inline-flex;align-items:center;margin:0;cursor:pointer}',
    '.pc-sw input{appearance:none;-webkit-appearance:none;width:38px;height:22px;margin:0;border-radius:var(--r-pill);background:var(--line2);border:1px solid transparent;position:relative;cursor:pointer;display:block}',
    '.pc-sw input::after{content:"";position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;background:var(--panel);box-shadow:var(--knob-shadow);transition:transform .16s cubic-bezier(.16,1,.3,1)}',
    '.pc-sw input:checked{background:var(--accent);border-color:var(--accent)}',
    '.pc-sw input:checked::after{transform:translateX(16px);background:var(--onaccent)}',
    '.pc-sw input:focus-visible{outline:none;box-shadow:0 0 0 1px var(--fg)}',
    '.pc-sw input:disabled{opacity:.45;cursor:not-allowed}',
    '.pc-err{margin:0 0 12px;font-size:12.5px;line-height:1.5;color:var(--bad)}',
    '.pc-pv{margin:0;white-space:pre-wrap;font:12px/1.55 var(--mono);color:var(--fg)}'
  ];

  var S = {
    root: null,
    ctx: null,
    gw: null,
    agents: [],
    err: '',
    busy: false
  };

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var el = document.createElement('style');
    el.id = STYLE_ID;
    el.textContent = CSS.join('\n');
    document.head.appendChild(el);
  }

  function api() {
    return S.ctx && S.ctx.api;
  }

  function short(e) {
    var a = api();
    if (a && typeof a.shortErr === 'function') return a.shortErr(e);
    return (e && e.message) ? e.message : '失败';
  }

  function byId(id) {
    var list = S.agents || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i] && list[i].id === id) return list[i];
    }
    return null;
  }

  function render() {
    if (!S.root || !h) return;
    var root = S.root;
    while (root.firstChild) root.removeChild(root.firstChild);
    if (S.err) root.appendChild(h('div', { class: 'pc-err', text: S.err }));
    var list = h('div', { class: 'pc-list' });
    list.appendChild(row({ gateway: true, label: '网关' }, gatewayOn()));
    AGENTS.forEach(function (spec) {
      var live = byId(spec.id);
      list.appendChild(row({
        id: spec.id,
        label: (live && live.label) || spec.label
      }, !!(live && live.connected)));
    });
    root.appendChild(list);
  }

  function gatewayOn() {
    return !!(S.gw && S.gw.state === 'ok');
  }

  function row(spec, on) {
    var input = h('input', { type: 'checkbox', 'aria-label': spec.label });
    input.checked = !!on;
    input.disabled = !!S.busy;
    input.addEventListener('change', function () {
      var want = input.checked;
      input.checked = !want;
      if (S.busy) return;
      if (spec.gateway) toggleGateway(want);
      else if (want) turnOn(spec);
      else turnOff(spec);
    });
    return h('div', { class: 'pc-row' }, [
      h('span', { class: 'pc-name', text: spec.label }),
      h('label', { class: 'pc-sw' }, [input])
    ]);
  }

  function noteFromGateway(gw) {
    if (!gw) return '';
    if (gw.state === 'foreign' || gw.state === 'unknown') return gw.note || '';
    return '';
  }

  function refresh(keepErr) {
    var a = api();
    if (!a) return Promise.resolve();
    return Promise.all([a.get('api/gateway'), a.get('api/agents')]).then(function (pair) {
      S.gw = pair[0] || {};
      S.agents = Array.isArray(pair[1]) ? pair[1] : [];
      if (!keepErr) S.err = noteFromGateway(S.gw);
      S.busy = false;
      render();
    }, function (e) {
      S.busy = false;
      S.err = short(e);
      render();
    });
  }

  function fail(e) {
    S.err = short(e);
    S.busy = false;
    return refresh(true);
  }

  function toggleGateway(on) {
    var a = api();
    if (!a) return;
    S.busy = true;
    render();
    a.post('api/gateway', { on: !!on }).then(function () {
      S.err = '';
      return refresh(false);
    }, fail);
  }

  function previewBody(pv) {
    var lines = (pv.changes || []).map(function (c) {
      var from = (c.from === null || c.from === undefined) ? '' : String(c.from);
      var to = (c.to === null || c.to === undefined) ? '' : String(c.to);
      if (!from && !to) return c.key || '';
      return (c.key || '') + '  ' + from + ' → ' + to;
    }).join('\n');
    return h('pre', { class: 'pc-pv', text: lines });
  }

  function turnOn(spec) {
    var a = api();
    var dialog = S.ctx && S.ctx.dialog;
    if (!a || typeof dialog !== 'function') return;
    S.busy = true;
    render();
    var rev = spec.id === 'codex'
      ? a.get('api/state').then(function (s) { return s && s.revision; }, function () { return null; })
      : Promise.resolve(null);
    a.post('api/connect/preview', { agent: spec.id }).then(function (pv) {
      S.busy = false;
      render();
      if (pv && pv.blocked) {
        S.err = pv.blocked;
        render();
        return null;
      }
      return dialog({
        title: spec.label,
        body: previewBody(pv || {}),
        okText: '接入',
        cancelText: '取消',
        danger: true
      }).then(function (yes) {
        if (!yes) return null;
        S.busy = true;
        render();
        return rev.then(function (revision) {
          var body = { agent: spec.id, confirm: true };
          if (revision) body.revision = revision;
          return a.post('api/connect', body);
        }).then(function () {
          S.err = '';
          return refresh(false);
        }, fail);
      });
    }, fail);
  }

  function turnOff(spec) {
    var a = api();
    var dialog = S.ctx && S.ctx.dialog;
    if (!a || typeof dialog !== 'function') return;
    dialog({
      title: '断开 ' + spec.label,
      okText: '断开',
      cancelText: '取消',
      danger: true
    }).then(function (yes) {
      if (!yes) return null;
      S.busy = true;
      render();
      return a.post('api/connect', {
        agent: spec.id,
        confirm: true,
        disconnect: true
      }).then(function () {
        S.err = '';
        return refresh(false);
      }, fail);
    });
  }

  function mount(root, ctx) {
    if (!root) return;
    ensureStyle();
    S.root = root;
    S.ctx = ctx || {};
    S.err = '';
    S.busy = false;
    root.classList.add('pc');
    render();
    refresh(false);
    if (S.ctx.onUnmount) S.ctx.onUnmount(unmount);
    return { unmount: unmount, refresh: function () { return refresh(false); } };
  }

  function unmount() {
    var r = S.root;
    if (r) {
      while (r.firstChild) r.removeChild(r.firstChild);
      r.classList.remove('pc');
    }
    S.root = null;
    S.ctx = null;
    S.busy = false;
  }

  var page = { id: PAGE_ID, title: '控制', mount: mount, unmount: unmount, refresh: function () { return refresh(false); } };
  global.PrismPages = global.PrismPages || {};
  global.PrismPages[PAGE_ID] = page;
  try {
    if (global.Prism && typeof global.Prism.registerPage === 'function') global.Prism.registerPage(PAGE_ID, page);
  } catch (e) { /* 壳还没挂上注册口时，PrismPages 仍然够用 */ }
})(window);
