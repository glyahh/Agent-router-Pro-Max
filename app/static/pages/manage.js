/* Prism · 管理页
 *
 * 分组的添加、改名、删除，以及来源的编辑、删除、改渠道头。
 * 添加来源留在首页。成功不提示，失败留一句错误。
 */
(function (global) {
  'use strict';

  var PAGE_ID = 'manage';
  var STYLE_ID = 'prism-manage-style';
  var HEAD_RE = /^[a-z0-9][a-z0-9._-]{0,23}$/;
  var HEAD_OWNER = { 'claude-code-desktop': 'claude-code' };

  function h() {
    var fn = global.Prism && global.Prism.h;
    if (!fn) return null;
    return fn.apply(null, arguments);
  }

  var CSS = [
    '.mg-err{margin:0 0 12px;font-size:12.5px;line-height:1.5;color:var(--bad)}',
    '.mg-row{display:flex;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid var(--line)}',
    '.mg-row:last-child{border-bottom:0}',
    '.mg-row input,.mg-row select{font:inherit;font-size:13px;color:var(--fg);background:var(--panel);',
    '  border:1px solid var(--line2);border-radius:var(--r-ctl);padding:6px 10px;min-width:0}',
    '.mg-row input:focus,.mg-row select:focus{outline:none;border-color:var(--fg);box-shadow:0 0 0 1px var(--fg)}',
    '.mg-name{flex:1;min-width:0}',
    '.mg-meta{flex:1;min-width:0;font-size:12.5px;color:var(--fg2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '.mg-acts{display:flex;gap:8px;flex-shrink:0}',
    '.mg-add{display:flex;gap:8px;padding:10px 14px}',
    '.mg-add input{flex:1}',
    '.mg-list{border:1px solid var(--line);border-radius:var(--r-card);background:var(--panel);overflow:hidden}',
    '.mg-load{display:flex;align-items:center;justify-content:center;gap:8px;',
    '  padding:36px 4px;font-size:13px;color:var(--fg3)}'
  ].join('\n');

  var S = {
    root: null, ctx: null, state: null, sources: null, agents: [],
    err: '', busy: false, names: {}
  };
  var form = null;
  var $err, $groups, $sources, $form, $srcSec;

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var el = document.createElement('style');
    el.id = STYLE_ID;
    el.textContent = CSS;
    document.head.appendChild(el);
  }

  function api(path, opts) {
    var P = (S.ctx && S.ctx.api) ? S.ctx.api
      : (global.Prism && global.Prism.api ? global.Prism.api : null);
    opts = opts || {};
    if (!P) return Promise.reject(new Error('接口还没就绪'));
    var method = (opts.method || 'GET').toUpperCase();
    var timeout = opts.timeout || 15000;
    if (method === 'GET') return P.get(path, null, { timeout: timeout });
    if (method === 'POST') return P.post(path, opts.body || {}, { timeout: timeout });
    if (method === 'PUT') return P.put(path, opts.body || {}, { timeout: timeout });
    if (method === 'DELETE') return P.del(path, null, { timeout: timeout });
    return Promise.reject(new Error('不支持的方法'));
  }

  function errText(e) {
    var P = S.ctx && S.ctx.api;
    if (P && typeof P.shortErr === 'function') return P.shortErr(e);
    return (e && e.message) ? e.message : '失败';
  }

  function groups() {
    var raw = S.state && S.state.groups;
    if (!Array.isArray(raw)) return [];
    return raw.filter(function (g) { return g && g.id; });
  }

  function groupName(id) {
    var list = groups(), i;
    for (i = 0; i < list.length; i++) if (list[i].id === id) return list[i].name || id;
    return id || '';
  }

  function sources() {
    if (Array.isArray(S.sources) && S.sources.length) return S.sources;
    return (S.state && S.state.providers) || [];
  }

  function byId(id) {
    var list = sources(), i;
    for (i = 0; i < list.length; i++) if (list[i] && list[i].id === id) return list[i];
    var ps = (S.state && S.state.providers) || [];
    for (i = 0; i < ps.length; i++) if (ps[i] && ps[i].id === id) return ps[i];
    return null;
  }

  function isCustom(p) { return !!(p && (p.custom === true || p.editable === true)); }

  function clear(el) { if (el) el.textContent = ''; }

  function render() {
    if (!$groups) return;
    clear($err);
    if (S.err) $err.appendChild(h('p', { class: 'mg-err', text: S.err }));
    // 首屏还没有状态时，和首页一样转圈，不先画出空列表。
    if (!S.state) {
      clear($groups);
      clear($sources);
      if ($srcSec) $srcSec.style.display = 'none';
      if (!S.err) {
        $groups.appendChild(h('div', { class: 'mg-load' }, [
          h('span', { class: 'spin' }),
          '正在加载状态…'
        ]));
      }
      return;
    }
    if ($srcSec) $srcSec.style.display = '';
    clear($groups);
    var list = h('div', { class: 'mg-list' });
    groups().forEach(function (g) {
      if (S.names[g.id] === undefined) S.names[g.id] = g.name || '';
      var inp = h('input', { class: 'mg-name', value: S.names[g.id] });
      inp.addEventListener('input', function () { S.names[g.id] = inp.value; });
      list.appendChild(h('div', { class: 'mg-row' }, [
        inp,
        h('div', { class: 'mg-acts' }, [
          h('button', { class: 'btn sm', type: 'button', disabled: !!S.busy,
            onclick: function () { rename(g.id); } }, '改名'),
          h('button', { class: 'btn sm', type: 'button', disabled: !!S.busy,
            onclick: function () { removeGroup(g); } }, '删除')
        ])
      ]));
    });
    var addInp = h('input', { id: 'mg-new-name', placeholder: '分组名称' });
    list.appendChild(h('div', { class: 'mg-add' }, [
      addInp,
      h('button', { class: 'btn pri sm', type: 'button', disabled: !!S.busy,
        onclick: function () { createGroup(addInp.value); } }, '添加')
    ]));
    $groups.appendChild(list);

    clear($sources);
    var rows = h('div', { class: 'mg-list' });
    var items = sources();
    if (!items.length) {
      rows.appendChild(h('div', { class: 'mg-row' }, h('span', { class: 'mg-meta', text: '暂无来源' })));
    }
    items.forEach(function (p) {
      var acts = [];
      if (isCustom(p)) {
        acts.push(h('button', { class: 'btn sm', type: 'button', disabled: !!S.busy,
          onclick: function () { editSource(p); } }, '编辑'));
      }
      if (p.section !== 'auth-file') {
        acts.push(h('button', { class: 'btn sm', type: 'button', disabled: !!S.busy,
          onclick: function () { removeSource(p); } }, '删除'));
      }
      acts.push(h('button', { class: 'btn sm', type: 'button', disabled: !!S.busy,
        onclick: function () { editHead(p); } }, '渠道头'));
      rows.appendChild(h('div', { class: 'mg-row' }, [
        h('span', { class: 'mg-meta', text: (p.label || p.id) + ' · ' + groupName(p.group) }),
        h('div', { class: 'mg-acts' }, acts)
      ]));
    });
    $sources.appendChild(rows);
    if (form) form.renderInto($form);
  }

  function ensureForm() {
    if (form || !global.PrismSourceForm) return form;
    form = global.PrismSourceForm.create({
      h: h,
      api: api,
      errText: errText,
      groups: groups,
      byId: byId,
      isCustom: isCustom,
      allowCreate: false,
      onBusy: function (v) { S.busy = !!v; render(); },
      onSaved: function () { S.err = ''; return reload(); },
      onError: function (text) { S.err = text; render(); }
    });
    return form;
  }

  function reload() {
    return Promise.all([
      api('/api/state', { timeout: 30000 }),
      api('/api/sources').catch(function () { return null; }),
      api('/api/agents').catch(function () { return null; })
    ]).then(function (rs) {
      S.state = rs[0];
      S.sources = Array.isArray(rs[1]) ? rs[1] : null;
      S.agents = Array.isArray(rs[2]) ? rs[2] : [];
      var keep = {};
      groups().forEach(function (g) {
        keep[g.id] = Object.prototype.hasOwnProperty.call(S.names, g.id) ? S.names[g.id] : (g.name || '');
      });
      S.names = keep;
      render();
    }).catch(function (e) {
      S.err = errText(e);
      render();
    });
  }

  function createGroup(name) {
    S.err = '';
    S.busy = true; render();
    api('/api/groups', { method: 'POST', body: { name: name } }).then(function () {
      S.busy = false;
      S.err = '';
      return reload();
    }).catch(function (e) {
      S.busy = false;
      S.err = errText(e);
      render();
    });
  }

  function rename(id) {
    S.err = '';
    S.busy = true; render();
    api('/api/groups/' + encodeURIComponent(id), { method: 'PUT', body: { name: S.names[id] } }).then(function () {
      S.busy = false;
      return reload();
    }).catch(function (e) {
      S.busy = false;
      S.err = errText(e);
      render();
    });
  }

  function removeGroup(g) {
    var dlg = S.ctx && S.ctx.dialog;
    var ask = dlg ? dlg({ title: '删除分组', body: '删除「' + (g.name || g.id) + '」', okText: '删除', danger: true })
      : Promise.resolve(global.confirm('删除「' + (g.name || g.id) + '」'));
    ask.then(function (yes) {
      if (!yes) return;
      S.busy = true; render();
      api('/api/groups/' + encodeURIComponent(g.id), { method: 'DELETE' }).then(function () {
        S.busy = false;
        S.err = '';
        return reload();
      }).catch(function (e) {
        S.busy = false;
        S.err = errText(e);
        render();
      });
    });
  }

  function editSource(p) {
    var full = byId(p.id) || p;
    ensureForm();
    if (form) form.openEdit(full);
  }

  function removeSource(p) {
    var dlg = S.ctx && S.ctx.dialog;
    var ask = dlg ? dlg({ title: '删除来源', body: '删除「' + (p.label || p.id) + '」', okText: '删除', danger: true })
      : Promise.resolve(global.confirm('删除「' + (p.label || p.id) + '」'));
    ask.then(function (yes) {
      if (!yes) return;
      S.busy = true; render();
      api('/api/sources/' + encodeURIComponent(p.id), { method: 'DELETE' }).then(function () {
        S.busy = false;
        S.err = '';
        return reload();
      }).catch(function (e) {
        S.busy = false;
        S.err = errText(e);
        render();
      });
    });
  }

  function headAgents() {
    var list = (S.agents || []).filter(function (a) { return a && a.id && a.id !== 'copilot-agent'; });
    if (list.length) return list;
    return [
      { id: 'codex', label: 'Codex' },
      { id: 'claude-code', label: 'Claude Code' },
      { id: 'opencode', label: 'opencode' },
      { id: 'hermes', label: 'Hermes' }
    ];
  }

  function effectiveHead(agentId, p) {
    var owner = HEAD_OWNER[agentId] || agentId;
    var stored = S.state && S.state.agent_heads;
    var slot = stored && stored[owner];
    if (slot && Object.prototype.hasOwnProperty.call(slot, p.id)) return String(slot[p.id] || '').trim();
    return String((p && p.head) || '').trim();
  }

  function editHead(p) {
    var dlg = S.ctx && S.ctx.dialog;
    if (!dlg) return;
    var agents = headAgents();
    var sel = h('select', null, agents.map(function (a) {
      return h('option', { value: a.id, text: a.label || a.id });
    }));
    var inp = h('input', { value: effectiveHead(sel.value || agents[0].id, p), placeholder: '留空 = 主来源' });
    sel.addEventListener('change', function () { inp.value = effectiveHead(sel.value, p); });
    var body = h('div', null,
      h('div', { class: 'f' }, h('label', null, '代理端'), sel),
      h('div', { class: 'f' }, h('label', null, '渠道头'), inp));
    dlg({ title: p.label || p.id, body: body, okText: '保存' }).then(function (yes) {
      if (!yes) return;
      var want = String(inp.value || '').trim();
      var agent = sel.value;
      if (want && !HEAD_RE.test(want)) {
        S.err = '渠道头不合法';
        render();
        return;
      }
      S.busy = true; render();
      api('/api/sources/' + encodeURIComponent(p.id) + '/head', {
        method: 'PUT', body: { head: want, agent: agent }
      }).then(function () {
        S.busy = false;
        S.err = '';
        return reload();
      }).catch(function (e) {
        S.busy = false;
        S.err = errText(e);
        render();
      });
    });
  }

  function mount(root, ctx) {
    ensureStyle();
    S.root = root;
    S.ctx = ctx || {};
    S.err = '';
    S.busy = false;
    S.names = {};
    form = null;
    var wrap = h('div', { class: 'home' });
    $err = h('div');
    $groups = h('div');
    $sources = h('div');
    $form = h('div');
    wrap.appendChild($err);
    wrap.appendChild(h('div', { class: 'sec' }, [
      h('div', { class: 'sechead' }, [h('span', { class: 'stitle', text: '分组' }), h('span', { class: 'hr' })]),
      $groups
    ]));
    $srcSec = h('div', { class: 'sec' }, [
      h('div', { class: 'sechead' }, [h('span', { class: 'stitle', text: '来源' }), h('span', { class: 'hr' })]),
      $sources,
      $form
    ]);
    wrap.appendChild($srcSec);
    root.appendChild(wrap);
    ensureForm();
    render();
    reload();
    return {
      id: PAGE_ID,
      unmount: unmount,
      isDirty: function () { return !!(form && form.isDirty()); },
      refresh: function () { return reload(); }
    };
  }

  function unmount() {
    if (S.root) S.root.textContent = '';
    S.root = null;
    S.ctx = null;
    S.state = null;
    form = null;
    $err = $groups = $sources = $form = $srcSec = null;
  }

  global.PrismPages = global.PrismPages || {};
  global.PrismPages[PAGE_ID] = {
    id: PAGE_ID, title: '管理', mount: mount, unmount: unmount,
    isDirty: function () { return !!(form && form.isDirty()); }
  };
})(window);
