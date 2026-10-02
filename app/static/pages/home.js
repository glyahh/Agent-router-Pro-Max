/* ============================================================================
 * Prism · 首页  —  static/pages/home.js
 *
 * 四级节点树：
 *
 *     Agent（Codex / Claude Code / Claude Code 桌面端 / opencode / Hermes）
 *       └ 分组（GPT / DEEPSEEK / GLM）
 *           └ 来源（SRAPI、OpenAI 官方 …）        ← 默认只展到这一层
 *               └ 模型（srapi/gpt-5.6-sol …）      ← 点来源才展开
 *
 * 契约见 static/app.js 顶部的注释。本页要用 ctx.api / ctx.state / ctx.dialog /
 * ctx.onUnmount。
 *
 * ── 这一页的范围（别处重复了就删）─────────────────────────────────────
 * 去掉：原来那张 9 列的「所有来源」表。它的排序/统计能力在**监控页**已经有了
 *       （monitor.js 的 sourcesSection），首页不再摆第二份。
 * 保留：底部「＋ 添加来源」表单。
 * 新增：节点树、内联的来源动作（测试/编辑/删除/改渠道头）、Agent 接入。
 *
 * ── 三个容易改错的点（都写在 docs/DEV-RULES.md 里）────────────────────
 *  1. 样式：树本身的样式在 app.css 的「首页节点树」段，本页只注入**页面独有**的
 *     东西。详见 DEV-RULES A1/A2（页面 <style> 排在 app.css 之后，同权重时本页赢；
 *     一条裸元素选择器就能把复选框拉成整行宽的近黑药丸——上线坏过一次）。
 *  2. 渠道头：客户端可见 ID = head + '/' + 干净别名。plan 里的 expose / models
 *     /model_settings 三处**始终用干净别名**，加了头的是"显示与写入 config 时的
 *     形态"。混用会让勾选对不上（route_selector 的 picks∩available 求交会清零）。
 *  3. 勾选：`selected[group]` 现在是**数组**（一个分组可同时启用多家来源）。
 *     这里不再有"多个来源同时启用"的冲突态。
 * ========================================================================== */
(function () {
  'use strict';

  var GROUPS = [
    { id: 'gpt', name: 'GPT 中转站' },
    { id: 'deepseek', name: 'DEEPSEEK' },
    { id: 'glm', name: 'GLM' }
  ];
  var LEVELS = ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'];
  var HEAD_RE = /^[a-z0-9][a-z0-9._-]{0,23}$/;
  var STYLE_ID = 'prism-home-style';
  var OPEN_KEY = 'prism.graph.open';

  // ── 页内样式：只放树以外、本页独有的东西。树在 app.css。 ─────────────────
  var CSS = [
    // .form/.formh/.grid/.f/label/.btn/.tag/.statebox 全部继承 app.css，不重写
    '.home{color:var(--fg);font-family:var(--sans);padding:0 0 10px}',
    '.home *{box-sizing:border-box}',
    '.home .sechead{flex-wrap:wrap}',
    '.home .hr{min-width:14px;flex:1;height:1px;background:var(--line)}',
    // 输入控件基线。**必须排除 checkbox**（DEV-RULES A2，踩过一次）
    '.home select,.home input:not([type="checkbox"]),.home textarea{font-family:inherit;',
    '  font-size:13.5px;color:var(--fg);background:var(--panel);border:1px solid var(--line2);',
    '  border-radius:var(--r-ctl);padding:8px 11px;width:100%;min-width:0;',
    '  transition:border-color .14s cubic-bezier(.16,1,.3,1),box-shadow .14s cubic-bezier(.16,1,.3,1)}',
    '.home input:not([type="checkbox"]):focus,.home select:focus,.home textarea:focus{outline:none;',
    '  border-color:var(--fg);box-shadow:0 0 0 1px var(--fg)}',
    '.home textarea{resize:vertical;line-height:1.5}',
    '@media (max-width:900px){.home .grid{grid-template-columns:1fr}.home .f.span2,',
    '  .home .f.span3{grid-column:span 1}}',
    '.home .modelrow{grid-template-columns:1.1fr 1.1fr .7fr 2.3fr auto;gap:12px;align-items:start}',
    '@media (max-width:900px){.home .modelrow{grid-template-columns:1fr 1fr}}',
    '.home .lv{cursor:pointer;user-select:none;transition:all .14s cubic-bezier(.16,1,.3,1)}',
    '.home .lv.def{box-shadow:inset 0 -2px 0 var(--fg3)}',
    '.home .dfl{display:flex;align-items:center;gap:6px;margin-top:6px;flex-wrap:wrap}',
    '.home .dfl select{width:auto;flex:0 0 auto;padding:5px 22px 5px 9px;font-size:12px}',
    '.home .rowbtns{display:flex;gap:8px;align-items:center;margin-top:3px;flex-wrap:wrap}',
    '.home .note{font-size:12px;color:var(--fg3);line-height:1.6;padding:8px 2px 0}',
    '.home .note.bad{color:var(--bad)}',
    '.home .note.warn{color:var(--warn)}',
    '.home .note.ok{color:var(--ok)}',
    '.home .empty{font-size:13px;color:var(--fg3);padding:16px 4px;text-align:center}',
    '.home .msg{display:flex;gap:9px;align-items:center;font-size:12.5px;line-height:1.6;',
    '  border:1px solid var(--line2);border-left-width:3px;border-radius:var(--r-ctl);',
    '  padding:10px 13px;margin-bottom:14px;background:var(--panel);box-shadow:var(--shadow)}',
    '.home .msg .txt{flex:1;min-width:0}',
    '.home .msg.ok{border-left-color:var(--ok)}',
    '.home .msg.bad{border-left-color:var(--bad);color:var(--bad);background:var(--badbg)}',
    '.home .msg.warn{border-left-color:var(--warn);color:var(--warn);background:var(--warnbg)}',
    '.home .msg.info{border-left-color:var(--line3);color:var(--fg2)}',
    '.home .spin{display:inline-block;width:10px;height:10px;border:1.5px solid var(--line3);',
    '  border-top-color:var(--fg2);border-radius:50%;animation:homespin .7s linear infinite;',
    '  vertical-align:-1px}',
    '@keyframes homespin{to{transform:rotate(360deg)}}',
    // 接入预览的行：等宽给数据（键名/值都是数据）
    '.home .pv{border:1px solid var(--line);border-radius:var(--r-card);overflow:hidden;box-shadow:var(--shadow)}',
    '.home .pv .r{display:flex;gap:10px;padding:7px 10px;border-top:1px solid var(--line);',
    '  font-family:var(--mono);font-size:11.5px;align-items:baseline}',
    '.home .pv .r:first-child{border-top:0}',
    '.home .pv .k{flex:0 0 auto;font-size:10.5px;padding:2px 6px;border-radius:var(--r-pill);',
    '  border:1px solid var(--line2);background:var(--panel2);color:var(--fg2);min-width:52px;',
    '  text-align:center}',
    '.home .pv .k.create{color:var(--ok);background:var(--okbg);border-color:var(--okdim)}',
    '.home .pv .k.replace{color:var(--warn);background:var(--warnbg);border-color:var(--warndim)}',
    '.home .pv .k.append{color:var(--ok);background:var(--okbg);border-color:var(--okdim)}',
    '.home .pv .k.keep{color:var(--fg3)}',
    '.home .pv .n{flex:1;min-width:0;word-break:break-all;color:var(--fg2)}',
    '.home .pv .f{color:var(--fg3)}',
    '.home .pv .t{color:var(--fg)}',
    // 全局模型搜索与一键控制工具条
    '.home .home-global-toolbar{display:flex;align-items:center;gap:12px;margin:0 0 14px 0;padding:10px 14px;background:var(--panel2);border:1px solid var(--line);border-radius:var(--r-card);box-shadow:var(--shadow);flex-wrap:wrap}',
    '.home .home-search-box{display:flex;align-items:center;gap:8px;flex:1;min-width:240px;position:relative}',
    '.home .search-ic{color:var(--fg3);display:inline-flex;align-items:center;flex-shrink:0}',
    '.home .search-ic .ui-icon{width:15px;height:15px}',
    '.home .home-global-filter{background:var(--panel);border:1px solid var(--line2);border-radius:var(--r-ctl);padding:7px 11px;font-size:13px;color:var(--fg);width:100%;outline:none;transition:border-color .14s cubic-bezier(.16,1,.3,1),box-shadow .14s cubic-bezier(.16,1,.3,1)}',
    '.home .home-global-filter:focus{border-color:var(--fg);box-shadow:0 0 0 1px var(--fg)}',
    '.home .home-filter-badge{font-size:12px;padding:3px 8px;border-radius:var(--r-pill);background:var(--accentdim);color:var(--accent);font-weight:500;white-space:nowrap}',
    '.home .home-clear-btn{border:none;background:transparent;color:var(--fg3);cursor:pointer;padding:2px 4px;border-radius:4px;display:inline-flex;align-items:center;justify-content:center;transition:all .12s cubic-bezier(.16,1,.3,1)}',
    '.home .home-clear-btn .ui-icon{width:13px;height:13px}',
    '.home .home-clear-btn:hover{color:var(--fg);background:var(--hover)}',
    '.home .home-toolbar-actions{display:flex;align-items:center;gap:8px;flex-shrink:0}',
    '.home .home-tool-btn{white-space:nowrap}',
    '.home .kw-mark{background:var(--accentdim);color:var(--accent);border-radius:2px;padding:0 2px;font-weight:600}',
    '.home .source-dim{opacity:.45;filter:grayscale(.2)}',
    '.home .graph.hide-tech .lv1 > .grow > .gmeta,',
    '.home .graph.hide-tech .lv1 > .grow > .gacts,',
    '.home .graph.hide-tech .lv3 > .grow > .gmeta{display:none!important}'
  ].join('\n');

  function injectStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var s = document.createElement('style');
    s.id = STYLE_ID;
    s.textContent = CSS;
    (document.head || document.documentElement).appendChild(s);
  }

  // ── DOM 小工具（一律 textContent，不拼 HTML 串）───────────────────────────
  // ⚠ 这里的 h() 必须与壳的 h()（app.js 第 5 节）**支持同一批键**。少一个分支不会报错，
  // 只会静默把对象变成 "[object Object]" —— 实测吃过两次：
  //   · 缺 `style` 分支 → `style:{display:'none'}` 变成无效 CSS，筛选徽章与"✕"清空按钮
  //     **一打开首页就露出来**；
  //   · 缺 `data` 分支 → `data:{role:'x'}` 变成属性 `data="[object Object]"`，
  //     按 `[data-role=…]` 的查询永远空集 → 分组"N / M 启用"计数不刷新、跳转高亮静默失效。
  // 长远的修法是页面统一用 ctx.ui.h（见 HANDOFF §6.9 的 ME-10），本轮先把两个分支补齐。
  // h/clear 用壳的单一实现（app.js）；原先各页一份私有副本，修 bug 不传播（审查 ME-10）。
  // 壳的 h 是这里的超集：value/checked/disabled 对新建元素语义等效，style 对象/字符串都收。
  var h = window.Prism.h, clear = window.Prism.clear, append = window.Prism.append;
  function option(value, label) { return h('option', { value: value, text: label }); }
  function spin() { return h('span', { class: 'spin' }); }
  function shortHost(url) {
    if (!url) return '—';
    try { return new URL(url).host; } catch (e) {
      return String(url).replace(/^[a-z]+:\/\//i, '').split('/')[0] || '—';
    }
  }

  // ── 渠道头（与 app/core/sources.py 的 HEAD_SEP 必须一致）──────────────────
  var HEAD_SEP = '/';
  function clientId(head, alias) {
    var x = (head || '').trim();
    return x ? x + HEAD_SEP + alias : alias;
  }

  // ── API 封装（与 config.js 同口径：壳的 api 优先，其次 window.Prism.api）──
  function api(path, opts) {
    var P = (S.ctx && S.ctx.api) ? S.ctx.api
      : (window.Prism && window.Prism.api ? window.Prism.api : null);
    if (!P || typeof P.get !== 'function') {
      return Promise.reject(new Error('页面拿不到壳的 api 封装，无法访问后台接口。'));
    }
    var method = (opts && opts.method) || 'GET';
    var body = opts ? opts.body : undefined;
    var p = String(path).replace(/^\//, '');
    var slow = (opts && opts.timeout) ? { timeout: opts.timeout } : undefined;
    if (method === 'GET') return Promise.resolve(P.get(p, null, slow));
    if (method === 'POST') return Promise.resolve(P.post(p, body === undefined ? {} : body, slow));
    if (method === 'PUT') return Promise.resolve(P.put(p, body === undefined ? {} : body, slow));
    if (method === 'DELETE') return Promise.resolve(P.del(p, null, slow));
    return Promise.reject(new Error('不支持的方法：' + method));
  }
  function enc(s) { return encodeURIComponent(String(s)); }
  function errText(e) {
    if (!e) return '未知错误';
    var m = e.message || String(e);
    if ((e.status === 404 || e.status === 405) && m.indexOf('接口') < 0) m += '（后端未提供该接口）';
    return m;
  }

  // ── 状态 ─────────────────────────────────────────────────────────────────
  var S = {
    root: null, wrap: null, ctx: null,
    state: null,        // GET /api/state
    sources: null,      // GET /api/sources（后端未就绪时 null，此时从 state.providers 推）
    agents: null,       // GET /api/agents
    sel: {},            // group -> [pid]
    draft: {},          // pid -> [干净别名]
    origin: {},         // pid -> [干净别名]（进入页面时的基线）
    selOrigin: {},      // group -> [pid] 基线
    open: {},           // 节点 key -> true
    test: {}, testing: {}, confirmDel: null,
    form: null, msg: null, busy: false, loadErr: null,
    agentBusy: null,
    globalFilter: '',
    showTechDetails: false
  };
  var $tree, $treeHint, $addsrc, $form, $acts, $msgBox, $stickyBar,
      $globalToolbar, $globalFilterInp, $filterBadge, $clearFilterBtn, $unreadableBox;

  // unmount 会把 $tree/$form/$acts 置 null，而多处**异步回调**直接调这些渲染函数
  // （saveRouting 的失败回调、pullModels、useSnapshot、previewAgent、editHead、testAll 的
  // step…）。只守 renderAll 不够 —— 那些路径根本不经过它（复查轮 6 的 3-1）。
  // 所以三个入口各守一次：谁调都安全。
  function _unmounted() { return !S.wrap; }

  // ── 展开状态持久化（默认：只展开第一个 agent；分组与来源也默认展开，
  //     于是"不点击的时候"看到的正好是 Agent + 分组 + 来源三层，模型层收起）──
  function loadOpen() {
    try {
      var raw = JSON.parse(localStorage.getItem(OPEN_KEY) || '{}');
      S.open = (raw && typeof raw === 'object') ? raw : {};
    } catch (e) { S.open = {}; }
  }
  function saveOpen() {
    try { localStorage.setItem(OPEN_KEY, JSON.stringify(S.open)); } catch (e) {}
  }
  function isOpen(key, dflt) {
    var v = S.open[key];
    return v === undefined ? !!dflt : !!v;
  }
  function setOpen(key, on) {
    S.open[key] = !!on;
    saveOpen();
  }

  // ── 派生数据 ─────────────────────────────────────────────────────────────
  function providers() { return (S.state && S.state.providers) || []; }
  function byId(id) {
    var ps = providers(), i;
    for (i = 0; i < ps.length; i++) if (ps[i].id === id) return ps[i];
    if (S.sources) for (i = 0; i < S.sources.length; i++) if (S.sources[i].id === id) return S.sources[i];
    return null;
  }
  function groupProvs(g) { return providers().filter(function (p) { return p.group === g; }); }
  function isCustom(p) { return !!(p && (p.custom === true || p.editable === true)); }
  function isOn(pid) {
    for (var k in S.sel) {
      if (Object.prototype.hasOwnProperty.call(S.sel, k) && (S.sel[k] || []).indexOf(pid) >= 0) return true;
    }
    return false;
  }
  function enabledCount(g) { return (S.sel[g] || []).length; }
  function gName(id) {
    for (var i = 0; i < GROUPS.length; i++) if (GROUPS[i].id === id) return GROUPS[i].name;
    return id || '—';
  }
  function codexUsable(alias) {
    var low = String(alias).toLowerCase();
    return low.indexOf('image') < 0 && low.indexOf('-expires-on-') < 0;
  }
  // 一个来源可勾选的干净别名集合（口径与 route_selector 的 picks∩available 一致）
  function modelList(p) {
    var draft = S.draft[p.id] || [];
    var on = {};
    draft.forEach(function (a) { on[a] = 1; });
    var names = {}, list = [];
    function add(a) { if (typeof a === 'string' && a && !names[a]) { names[a] = 1; list.push(a); } }
    (p.available || []).forEach(function (m) { if (m && m.alias) add(m.alias); });
    (p.expose || []).forEach(add);
    // A/ 是给老任务留的隐藏别名（legacy_models），不当勾选项；只把已保存的那几个显示出来
    (p.models || []).forEach(function (m) {
      if (m && m.alias && (on[m.alias] || String(m.alias).slice(0, 2) !== 'A/')) add(m.alias);
    });
    list.sort();
    return list;
  }
  function levelsOf(p, alias) {
    var ms = p && p.model_settings;
    if (ms && typeof ms === 'object' && ms[alias] && typeof ms[alias] === 'object') {
      return { levels: ms[alias].levels || [], dflt: ms[alias]['default'] || '' };
    }
    return null;
  }
  function blockedCode(text) {
    var m = /\bHTTP\s*(\d{3})\b/.exec(String(text || ''));
    return m ? 'HTTP ' + m[1] : null;
  }
  function shortRev(rev) { return rev ? String(rev).slice(0, 8) : '—'; }

  // ── 渲染：动作条 + 消息 ──────────────────────────────────────────────────
  function updateStickyBar() {
    if (!$stickyBar) return;
    var dirty = isDirty();
    $stickyBar.style.display = dirty ? 'flex' : 'none';
    if (!dirty) return;
    $stickyBar.className = 'home-sticky-bar dirty';

    var on = 0, i;
    for (i = 0; i < GROUPS.length; i++) on += enabledCount(GROUPS[i].id);
    var dirtyCount = 0;
    for (var gi = 0; gi < GROUPS.length; gi++) {
      var g = GROUPS[gi].id;
      if ((S.sel[g] || []).join('\u0000') !== (S.selOrigin[g] || []).join('\u0000')) dirtyCount++;
    }
    for (var pid in S.draft) {
      if ((S.draft[pid] || []).join('\u0000') !== (S.origin[pid] || []).join('\u0000')) dirtyCount++;
    }
    if (isFormDirty()) dirtyCount++;

    var badgeText = dirty ? ('待保存：' + dirtyCount + ' 处改动') : '配置已同步';
    var noteText = '已启用 ' + on + ' 来源 · ' + clientIds().length + ' 模型';
    var saveText = S.busy ? '' : (dirty ? ('保存待修改项 (' + dirtyCount + ')') : '保存路由');

    var badge = $stickyBar.querySelector('[data-role="sb-badge"]');
    var note = $stickyBar.querySelector('[data-role="sb-note"]');
    var saveBtn = $stickyBar.querySelector('[data-role="sb-save"]');
    var refreshBtn = $stickyBar.querySelector('[data-role="sb-refresh"]');

    if (!badge) {
      clear($stickyBar);
      badge = h('span', { class: dirty ? 'tag warn' : 'tag', text: badgeText, data: { role: 'sb-badge' } });
      note = h('span', { class: 'sb-note', text: noteText, data: { role: 'sb-note' } });
      saveBtn = h('button', {
        class: 'btn pri sm', type: 'button',
        disabled: !dirty || !!S.busy || !S.state,
        data: { role: 'sb-save' },
        onclick: saveRouting
      }, S.busy ? spin() : saveText);
      refreshBtn = h('button', {
        class: 'btn sm', type: 'button', disabled: !!S.busy,
        data: { role: 'sb-refresh' },
        onclick: function () { reload(true); }
      }, '刷新');

      $stickyBar.appendChild(h('div', { class: 'sb-left' }, [badge, note]));
      $stickyBar.appendChild(h('div', { class: 'sb-spacer' }));
      $stickyBar.appendChild(h('div', { class: 'sb-right' }, [refreshBtn, saveBtn]));
    } else {
      badge.className = dirty ? 'tag warn' : 'tag';
      badge.textContent = badgeText;
      note.textContent = noteText;
      saveBtn.disabled = !dirty || !!S.busy || !S.state;
      refreshBtn.disabled = !!S.busy;
      clear(saveBtn);
      if (S.busy) saveBtn.appendChild(spin());
      else saveBtn.textContent = saveText;
    }
  }

  function renderActions() {
    if (_unmounted()) return;
    clear($acts);
    if ($msgBox && $msgBox.parentNode) $msgBox.parentNode.removeChild($msgBox);
    $msgBox = null;
    if (S.msg) {
      $msgBox = h('div', { class: 'msg ' + S.msg.kind },
        h('span', { class: 'txt', text: S.msg.text }),
        h('button', { class: 'btn sm', type: 'button', onclick: function () { S.msg = null; renderActions(); } }, '关闭'));
    }
    var dirty = isDirty();
    $acts.appendChild(h('div', { class: 'home-acts' },
      h('button', {
        class: 'btn pri', type: 'button', disabled: !!S.busy || !S.state, onclick: saveRouting
      }, S.busy ? spin() : '保存路由'),
      h('button', { class: 'btn', type: 'button', disabled: !!S.busy, onclick: function () { reload(true); } }, '刷新'),
      h('button', {
        class: 'btn', type: 'button', disabled: !!S.busy || !providerRows().length, onclick: testAll
      }, '测试全部来源'),
      h('span', { class: 'hspacer' })
    ));
    if ($msgBox) $acts.insertBefore($msgBox, $acts.firstChild);
    updateStickyBar();
  }
  function flash(kind, text) { S.msg = { kind: kind, text: text }; renderActions(); }

  // 所有来源产出的客户端可见 ID（含未启用的，口径同 regen_catalog）
  function clientIds() {
    var out = [], i, j;
    var ps = providerRows();
    for (i = 0; i < ps.length; i++) {
      var p = ps[i], ids = p.client_ids;
      if (!ids) {
        ids = [];
        (p.expose || []).forEach(function (a) {
          if (typeof a === 'string' && a && a.slice(0, 2) !== 'A/') ids.push(clientId(p.head, a));
        });
      }
      for (j = 0; j < ids.length; j++) if (out.indexOf(ids[j]) < 0) out.push(ids[j]);
    }
    return out.sort();
  }
  function providerRows() {
    if (S.sources && S.sources.length) return S.sources;
    return providers().map(function (p) {
      return { id: p.id, label: p.label, group: p.group, base_url: p.base_url, section: p.section,
        tag: p.tag, head: p.head || '', custom: isCustom(p), expose: p.expose || [],
        models: p.models || [], model_settings: p.model_settings, blocked: p.blocked,
        warning: p.warning, available: p.available, fetch_error: p.fetch_error,
        client_ids: p.client_ids };
    });
  }
  function isFormDirty() {
    if (!S.form) return false;
    var f = S.form;
    return !!((f.base_url && f.base_url.trim()) ||
              (f.api_key && f.api_key.trim()) ||
              (f.label && f.label.trim()) ||
              (f.head && f.head.trim()) ||
              (f.models && f.models.some(function (m) { return m && m.alias && m.alias.trim(); })));
  }

  function isDirty() {
    if (isFormDirty()) return true;
    var i;
    for (i = 0; i < GROUPS.length; i++) {
      var g = GROUPS[i].id;
      if ((S.sel[g] || []).join('\u0000') !== (S.selOrigin[g] || []).join('\u0000')) return true;
    }
    for (var pid in S.draft) {
      if (!Object.prototype.hasOwnProperty.call(S.draft, pid)) continue;
      if ((S.draft[pid] || []).join('\u0000') !== (S.origin[pid] || []).join('\u0000')) return true;
    }
    return false;
  }

  // ── 全局模型搜索与一键全部展开/折叠 ─────────────────────────────────────────
  function getActiveAgentId() {
    for (var i = 0; i < (S.agents || []).length; i++) {
      if (isOpen('a:' + S.agents[i].id, false)) return S.agents[i].id;
    }
    for (var j = 0; j < (S.agents || []).length; j++) {
      if (S.agents[j].connected) return S.agents[j].id;
    }
    return (S.agents && S.agents[0] && S.agents[0].id) || 'codex';
  }

  function expandAllNodes() {
    (S.agents || []).forEach(function (ag) {
      S.open['a:' + ag.id] = true;
      for (var i = 0; i < GROUPS.length; i++) {
        S.open['g:' + ag.id + '/' + GROUPS[i].id] = true;
      }
    });
    var aId = getActiveAgentId();
    if (aId) S.open['a:' + aId] = true;
    var provs = providerRows();
    for (var j = 0; j < provs.length; j++) {
      S.open['s:' + provs[j].id] = true;
    }
    saveOpen();

    if ($tree) {
      var nodes = $tree.querySelectorAll('.gnode');
      for (var k = 0; k < nodes.length; k++) {
        var el = nodes[k];
        el.classList.add('open');
        var grow = el.firstElementChild;
        if (grow && grow.classList.contains('grow')) {
          grow.setAttribute('aria-expanded', 'true');
        }
        if (typeof el._mountKids === 'function') {
          el._mountKids();
        }
      }
    } else {
      renderTree();
    }
  }

  function collapseAllNodes() {
    for (var k in S.open) {
      if (k.indexOf('g:') === 0 || k.indexOf('s:') === 0) {
        S.open[k] = false;
      }
    }
    saveOpen();

    if ($tree) {
      var openNodes = $tree.querySelectorAll('.gnode.lv2.open, .gnode.lv3.open');
      for (var i = 0; i < openNodes.length; i++) {
        var el = openNodes[i];
        el.classList.remove('open');
        var grow = el.firstElementChild;
        if (grow && grow.classList.contains('grow')) {
          grow.setAttribute('aria-expanded', 'false');
        }
      }
    } else {
      renderTree();
    }
  }

  function highlightLabel(head, alias, query) {
    var txt = clientId(head, alias);
    var q = String(query || '').trim().toLowerCase();
    if (!q) return h('span', { class: 'glabel mono', text: txt, title: txt });
    var lower = txt.toLowerCase();
    var idx = lower.indexOf(q);
    if (idx < 0) return h('span', { class: 'glabel mono', text: txt, title: txt });
    var span = h('span', { class: 'glabel mono', title: txt });
    var before = txt.slice(0, idx);
    var match = txt.slice(idx, idx + q.length);
    var after = txt.slice(idx + q.length);
    if (before) span.appendChild(document.createTextNode(before));
    span.appendChild(h('mark', { class: 'kw-mark', text: match }));
    if (after) span.appendChild(document.createTextNode(after));
    return span;
  }

  function applyGlobalFilter() {
    var q = String(S.globalFilter || '').trim().toLowerCase();
    if (!q) {
      if ($filterBadge) $filterBadge.style.display = 'none';
      if ($clearFilterBtn) $clearFilterBtn.style.display = 'none';
      if ($tree) {
        var allSourceNodes = $tree.querySelectorAll('.gnode.lv3');
        for (var si = 0; si < allSourceNodes.length; si++) {
          var sn = allSourceNodes[si];
          sn.classList.remove('source-dim');
          var key = sn.dataset.key;
          var wasOpen = isOpen(key, false);
          sn.classList.toggle('open', wasOpen);
          var wrap = sn.querySelector('.model-list-wrap');
          if (wrap) {
            var items = wrap.children;
            for (var ii = 0; ii < items.length; ii++) {
              items[ii].style.display = '';
            }
          }
        }
      } else {
        renderTree();
      }
      return;
    }

    if ($clearFilterBtn) $clearFilterBtn.style.display = '';

    var provs = providerRows();
    var totalMatches = 0;
    var matchedProvs = 0;

    if ($tree) {
      for (var i = 0; i < provs.length; i++) {
        var p = provs[i];
        var sNode = $tree.querySelector('[data-key="s:' + p.id + '"]');
        if (!sNode) continue;
        if (typeof sNode._mountKids === 'function') {
          sNode._mountKids();
        }
        var wrap = sNode.querySelector('.model-list-wrap');
        var pMatches = 0;
        var pLabelMatch = p.label && p.label.toLowerCase().indexOf(q) >= 0;

        if (wrap) {
          var mNodes = wrap.children;
          for (var j = 0; j < mNodes.length; j++) {
            var mEl = mNodes[j];
            var kw = mEl.dataset.kw || '';
            var matches = pLabelMatch || kw.indexOf(q) >= 0;
            if (matches) {
              mEl.style.display = '';
              pMatches++;
            } else {
              mEl.style.display = 'none';
            }
          }
        }

        if (pMatches > 0 || pLabelMatch) {
          totalMatches += pMatches;
          matchedProvs++;
          sNode.classList.remove('source-dim');
          sNode.classList.add('open');
          var parentGroup = sNode.closest('.gnode.lv2');
          if (parentGroup) parentGroup.classList.add('open');
        } else {
          sNode.classList.remove('open');
          sNode.classList.add('source-dim');
        }
      }
    } else {
      renderTree();
    }

    if ($filterBadge) {
      $filterBadge.style.display = '';
      $filterBadge.textContent = '匹配 ' + totalMatches + ' 个模型 · ' + matchedProvs + ' 个来源';
    }
  }

  function renderGlobalToolbar() {
    $globalFilterInp = h('input', {
      type: 'text',
      class: 'home-global-filter',
      placeholder: '搜索模型名称或 ID...'
    });
    if (S.globalFilter) $globalFilterInp.value = S.globalFilter;

    var closeIconNode = (window.PrismUI && typeof window.PrismUI.icon === 'function')
      ? window.PrismUI.icon('close')
      : null;
    $filterBadge = h('span', { class: 'home-filter-badge', style: { display: 'none' } });
    $clearFilterBtn = h('button', {
      type: 'button',
      class: 'home-clear-btn',
      style: { display: 'none' },
      title: '清空搜索'
    }, [closeIconNode]);

    var debounceTimer = null;
    $globalFilterInp.addEventListener('input', function () {
      if (debounceTimer) clearTimeout(debounceTimer);
      debounceTimer = setTimeout(function () {
        // 切页会 unmount 并把 $globalFilterInp 置 null；输入后 70ms 内切页就会摸到 null，
        // 回调里抛 TypeError（审查 NC-6）。这里先挡一下，定时器本身在 unmount 里也会清。
        if (!$globalFilterInp) return;
        S.globalFilter = String($globalFilterInp.value || '').trim();
        applyGlobalFilter();
      }, 70);
    });

    $globalFilterInp.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        e.preventDefault();
        $globalFilterInp.value = '';
        S.globalFilter = '';
        applyGlobalFilter();
        $globalFilterInp.blur();
      }
    });

    $clearFilterBtn.addEventListener('click', function () {
      $globalFilterInp.value = '';
      S.globalFilter = '';
      applyGlobalFilter();
      $globalFilterInp.focus();
    });

    var expandAllBtn = h('button', {
      class: 'btn sm home-tool-btn',
      type: 'button',
      title: '全部展开',
      onclick: function (e) {
        e.preventDefault();
        expandAllNodes();
      }
    }, '全部展开');

    var collapseAllBtn = h('button', {
      class: 'btn sm home-tool-btn',
      type: 'button',
      title: '全部折叠',
      onclick: function (e) {
        e.preventDefault();
        collapseAllNodes();
      }
    }, '全部折叠');

    var searchIconNode = (window.PrismUI && typeof window.PrismUI.icon === 'function')
      ? window.PrismUI.icon('search')
      : null;

    var searchBox = h('div', { class: 'home-search-box' }, [
      h('span', { class: 'search-ic' }, [searchIconNode]),
      $globalFilterInp,
      $filterBadge,
      $clearFilterBtn
    ]);

    var actionsBox = h('div', { class: 'home-toolbar-actions' }, [
      expandAllBtn,
      collapseAllBtn
    ]);

    return h('div', { class: 'home-global-toolbar' }, [
      searchBox,
      actionsBox
    ]);
  }

  // ── 渲染：节点树 ─────────────────────────────────────────────────────────
  function toggle(key, dflt, rerender) {
    setOpen(key, !isOpen(key, dflt));
    (rerender || renderTree)();
  }

  // 用户有没有对 agent 层做过选择。没做过才套用"默认展开第一个接入的"，
  // 否则把最后一个 agent 收起之后会被默认逻辑立刻又摊开，收不起来。
  function hasAgentChoice() {
    for (var k in S.open) if (k.indexOf('a:') === 0) return true;
    return false;
  }

  function node(cls, key, dflt, inner, kids, opts) {
    opts = opts || {};
    var open = isOpen(key, dflt);
    var box = h('div', {
      class: 'gnode ' + cls + (open ? ' open' : '') +
        (kids && kids.length ? ' has-kids' : '') + (opts.clickable ? ' clickable' : '') +
        (opts.pinned ? ' pinned' : ''),
      data: { key: key }
    });
    var row = h('div', { class: 'grow' });
    if (kids && kids.length) {
      row.setAttribute('role', 'button');
      row.setAttribute('tabindex', '0');
      row.setAttribute('aria-expanded', open ? 'true' : 'false');
      var doToggle = function (e) {
        if (e && e.target && e.target.closest &&
            e.target.closest('input,.gbtn,.ghead')) return;
        var nowOpen = !box.classList.contains('open');
        box.classList.toggle('open', nowOpen);
        row.setAttribute('aria-expanded', nowOpen ? 'true' : 'false');
        setOpen(key, nowOpen);
        if (nowOpen && typeof opts.onExpand === 'function') {
          opts.onExpand();
        }
        updateTreeHint();
      };
      row.addEventListener('click', doToggle);
      row.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); doToggle(e); }
      });
    }
    row.appendChild(h('span', { class: 'gchev' }));
    append(row, inner);
    box.appendChild(row);
    if (kids && kids.length) {
      box.appendChild(h('div', { class: 'gkids' }, h('div', { class: 'ginner' },
        h('div', { class: 'gtree' }, kids))));
    }
    return box;
  }

  function paintTree() {
    if (_unmounted()) return;
    if ($tree) $tree.classList.toggle('hide-tech', !S.showTechDetails);
    clear($tree);
    if (!S.state) {
      $tree.appendChild(h('div', { class: 'empty' },
        S.loadErr ? '配置加载失败，请检查网关状态并刷新。'
                  : '正在加载状态…'));
      return;
    }
    if (!S.agents) {
      $tree.appendChild(h('div', { class: 'empty' }, '正在加载 Agent 列表…'));
      return;
    }
    // L1：agent 做成手风琴——展开一个就收起别的。五个 agent 共享同一套网关路由，
    // 全部摊开会把同一份分组/来源重复画五遍，反而看不出层级。
    var openAgent = null, i;
    for (i = 0; i < S.agents.length; i++) {
      if (isOpen('a:' + S.agents[i].id, false)) { openAgent = S.agents[i].id; break; }
    }
    if (!openAgent && !hasAgentChoice()) {
      // 默认展开第一个"已接入"的；都没有就展开第一个存在的
      var pick = null;
      for (i = 0; i < S.agents.length; i++) {
        if (S.agents[i].connected) { pick = S.agents[i].id; break; }
      }
      if (!pick) for (i = 0; i < S.agents.length; i++) if (S.agents[i].exists) { pick = S.agents[i].id; break; }
      openAgent = pick || (S.agents[0] && S.agents[0].id);
      if (openAgent) { S.open['a:' + openAgent] = true; saveOpen(); }
    }
    var frag = document.createDocumentFragment();
    for (i = 0; i < S.agents.length; i++) {
      frag.appendChild(agentNode(S.agents[i], S.agents[i].id === openAgent));
    }
    $tree.appendChild(frag);
  }

  // 树 + 提示行一起刷。提示行依赖 S.state / S.open / 勾选草稿，三者都在这条路径上变。
  function renderTree() {
    if (_unmounted()) return;
    paintTree();
    if (!$treeHint) return;
    $treeHint.textContent = '';
  }

  function agentNode(a, expanded) {
    var dot = a.connected ? 'ok' : (a.exists ? 'warn' : '');
    var tag = a.connected ? h('span', { class: 'gtag ok', text: '已接入' })
      : (a.exists ? h('span', { class: 'gtag', text: '未接入' })
                  : h('span', { class: 'gtag warn', text: '未安装' }));
    var inner = [
      h('span', { class: 'gdot ' + dot }),
      h('span', { class: 'glabel', text: a.label }),
      tag,
      a.implemented ? null : h('span', { class: 'gtag warn', text: '只读' }),
      h('span', { class: 'gspacer' }),
      h('span', { class: 'gmeta', text: a.config_display || '' }),
      h('span', { class: 'gacts' }, [
        h('button', {
          class: 'gbtn', type: 'button', title: '预览接入配置变更',
          disabled: !a.implemented || !!S.agentBusy,
          onclick: function (e) { e.stopPropagation(); previewAgent(a.id); }
        }, S.agentBusy === a.id ? '…' : '接入预览')
      ])
    ];
    var kids = expanded ? groupNodes(a) : null;
    var n = node('lv1', 'a:' + a.id, false, inner, kids);
    // L1 手风琴：展开自己时收起别的 agent（同一套路由，摊开五份没意义）
    var row = n.firstChild;
    if (expanded) {
      n.classList.add('open');
    } else {
      // 覆盖默认的 toggle：这里要用互斥语义
      var clone = row.cloneNode(true);
      row.parentNode.replaceChild(clone, row);
      var doToggle = function (e) {
        if (e && e.target && e.target.closest && e.target.closest('input,.gbtn,.ghead')) return;
        for (var k in S.open) if (k.indexOf('a:') === 0) S.open[k] = false;
        S.open['a:' + a.id] = true;
        saveOpen();
        renderTree();
      };
      clone.addEventListener('click', doToggle);
      clone.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); doToggle(e); }
      });
    }
    return n;
  }

  function updateGroupStatus(gid) {
    if (!$tree) return;
    var gTags = $tree.querySelectorAll('[data-role="g-status"][data-gid="' + gid + '"]');
    var provs = groupProvs(gid);
    var on = enabledCount(gid);
    for (var i = 0; i < gTags.length; i++) {
      gTags[i].className = 'gtag' + (on ? ' acc' : '');
      gTags[i].textContent = on + ' / ' + provs.length + ' 启用';
    }
  }

  function updateTreeHint() {
    if (!$treeHint) return;
    if (!S.state) { $treeHint.textContent = S.loadErr ? '读取失败' : '读取中…'; return; }
    var on = 0, i;
    for (i = 0; i < GROUPS.length; i++) on += enabledCount(GROUPS[i].id);
    $treeHint.textContent = providerRows().length + ' 来源 · 已启用 ' + on +
      ' · ' + clientIds().length + ' 个模型在客户端目录里' +
      (isDirty() ? ' · 有未保存改动' : '');
  }

  function groupNodes(a) {
    return GROUPS.map(function (g) {
      var provs = groupProvs(g.id);
      var on = enabledCount(g.id);
      var gStatusTag = h('span', {
        class: 'gtag' + (on ? ' acc' : ''),
        text: on + ' / ' + provs.length + ' 启用',
        data: { role: 'g-status', gid: g.id }
      });
      var inner = [
        h('span', { class: 'gdot' }),
        h('span', { class: 'glabel', text: g.name }),
        gStatusTag,
        h('span', { class: 'gspacer' }),
        h('span', { class: 'gmeta', text: provs.length ? '' : '该分组暂无来源' })
      ];
      var kids = provs.map(function (p) { return sourceNode(a, g, p); });
      if (!kids.length) {
        kids = [h('div', { class: 'gnote', text: '该分组暂无来源' })];
      } else if (on > 1) {
        var heads = provs.filter(function (p) { return isOn(p.id) && !(p.head || '').trim(); });
        if (heads.length > 1) {
          kids = kids.concat([h('div', { class: 'gnote bad' },
            '这个分组同时启用了 ' + heads.length + ' 个没有渠道头的来源（' +
            heads.map(function (p) { return p.label || p.id; }).join('、') +
            '）。它们会暴露同名的模型 ID，保存会被拒绝——请给其中一个设置渠道头。')]);
        }
      }
      return node('lv2', 'g:' + a.id + '/' + g.id, true, inner, kids);
    });
  }

  function sourceNode(a, g, p) {
    var on = isOn(p.id);
    var head = (p.head || '').trim();
    var usable = modelList(p);
    var exposed = (S.draft[p.id] || []).filter(function (x) { return usable.indexOf(x) >= 0; });
    var statusTag = h('span', {
      class: 'gtag' + (on ? ' ok' : ''),
      text: on ? '启用' : '停用'
    });
    var exposedTag = h('span', {
      class: 'gtag acc',
      text: exposed.length + ' 暴露'
    });
    if (!exposed.length) exposedTag.style.display = 'none';

    var cb = h('input', { type: 'checkbox', class: 'gchk', checked: on,
      title: '在这个分组里启用 / 停用该来源（可多选）' });
    cb.addEventListener('change', function () {
      var arr = (S.sel[g.id] || []).slice(), at = arr.indexOf(p.id);
      if (cb.checked && at < 0) arr.push(p.id);
      if (!cb.checked && at >= 0) arr.splice(at, 1);
      S.sel[g.id] = arr;
      var isNowOn = cb.checked;
      statusTag.className = 'gtag' + (isNowOn ? ' ok' : '');
      statusTag.textContent = isNowOn ? '启用' : '停用';
      updateGroupStatus(g.id);
      updateTreeHint();
      renderActions();
    });

    var headEl = h('span', {
      // 无头 = 该分组的主来源，它保留干净的模型 ID。给它明确的「无头·主」标记
      class: 'ghead' + (head ? '' : ' none main'),
      title: head
        ? ('渠道头: ' + head + '（客户端 ID: ' + head + '/' + (usable[0] || '模型') + '）')
        : '主来源（保留原始模型 ID）',
      text: head || '无头·主'
    });
    headEl.addEventListener('click', function (e) { e.stopPropagation(); editHead(a, p); });

    var note = null;
    if (p.blocked) note = h('div', { class: 'gnote bad', text: '该来源已被标记阻断：' + p.blocked });
    else if (p.fetch_error) {
      note = h('div', { class: 'gnote warn' },
        '上游模型拉取失败: ' + p.fetch_error);
    } else if (p.warning) {
      note = h('div', { class: 'gnote' }, '备注：' + String(p.warning).slice(0, 140));
    }

    var testEl = testNote(p.id);
    var kids = [];

    // 本函数原先声明在下面的 else 块里。本文件是 'use strict'（第 31 行），严格模式下
    // 块内函数声明**不外提**到函数作用域，于是 _mountKids / onExpand 闭包里
    // `typeof mountModels` 恒为 'undefined' —— 「全部展开」与点来源行都静默挂不出模型行
    // （并行会话引入懒挂载时踩的，诊断见 HANDOFF §6.13）。挪到函数作用域即修复。
    function mountModels() {
      if (modelWrap._mounted || !usable.length) return;
      modelWrap._mounted = true;
      var frag = document.createDocumentFragment();
      usable.forEach(function (alias) {
        var mEl = modelNode(a, g, p, alias, head, function () {
          var curExp = (S.draft[p.id] || []).filter(function (x) { return usable.indexOf(x) >= 0; });
          exposedTag.textContent = curExp.length + ' 暴露';
          exposedTag.style.display = curExp.length ? '' : 'none';
          if (summaryNote) {
            summaryNote.textContent = '已选 ' + curExp.length + ' / ' + usable.length + ' 个模型' +
              (head ? '（渠道头: ' + head + '）' : '');
          }
          updateTreeHint();
          renderActions();
        });
        var fullId = clientId(head, alias);
        var kwStr = (alias + ' ' + fullId).toLowerCase();
        mEl.dataset.kw = kwStr;
        var matchesGlobal = !gq || kwStr.indexOf(gq) >= 0 || (p.label && p.label.toLowerCase().indexOf(gq) >= 0);
        if (matchesGlobal && gq) hasMatchedModel = true;
        if (gq && !matchesGlobal) {
          mEl.style.display = 'none';
        }
        modelItems.push({ kw: kwStr, el: mEl });
        frag.appendChild(mEl);
      });
      modelWrap.appendChild(frag);
    }
    var summaryNote = null;
    if (!usable.length) {
      kids.push(h('div', { class: 'gnote', text: '暂无可勾选模型' }));
    } else {
      summaryNote = h('div', { class: 'gnote' },
        '已选 ' + exposed.length + ' / ' + usable.length + ' 个模型' +
        (head ? '（渠道头: ' + head + '）' : ''));
      kids.push(summaryNote);

      var filterInp = h('input', {
        class: 'model-search-inp',
        type: 'text',
        placeholder: '过滤模型 (' + usable.length + ')...'
      });
      var filterBar = h('div', { class: 'model-filter-bar' }, filterInp);
      kids.push(filterBar);

      var gq = String(S.globalFilter || '').trim().toLowerCase();
      var modelWrap = h('div', { class: 'model-list-wrap' });
      var modelItems = [];
      var key = 's:' + p.id;
      var hasMatchedModel = false;

      var initiallyOpen = isOpen(key, false) || (gq && usable.some(function (alias) {
        var fullId = clientId(head, alias);
        return alias.toLowerCase().indexOf(gq) >= 0 || fullId.toLowerCase().indexOf(gq) >= 0 || (p.label && p.label.toLowerCase().indexOf(gq) >= 0);
      }));

      if (initiallyOpen) {
        mountModels();
      }

      var filterDebounceTimer = null;
      filterInp.addEventListener('input', function () {
        if (filterDebounceTimer) clearTimeout(filterDebounceTimer);
        filterDebounceTimer = setTimeout(function () {
          mountModels();
          var kw = String(filterInp.value || '').trim().toLowerCase();
          for (var mi = 0; mi < modelItems.length; mi++) {
            var item = modelItems[mi];
            item.el.style.display = (!kw || item.kw.indexOf(kw) >= 0) ? '' : 'none';
          }
        }, 50);
      });
      kids.push(modelWrap);
    }
    if (note) kids.push(note);
    if (testEl) kids.push(testEl);

    var inner = [
      cb,
      h('span', { class: 'gdot' }),
      h('span', { class: 'glabel', text: p.label || p.id }),
      headEl,
      statusTag,
      exposedTag,
      h('span', { class: 'gspacer' }),
      h('span', { class: 'gmeta', text: shortHost(p.base_url) + (p.tag ? ' · ' + p.tag : '') }),
      h('span', { class: 'gacts' }, sourceButtons(a, p))
    ];
    var n = node('lv3', key, false, inner, kids, {
      pinned: S.confirmDel === p.id,
      onExpand: function () {
        if (typeof n._mountKids === 'function') n._mountKids();
      }
    });

    n._mountKids = function () {
      if (typeof mountModels === 'function') mountModels();
    };

    if (initiallyOpen) {
      n.classList.add('open');
    } else if (gq) {
      n.classList.remove('open');
      n.classList.add('source-dim');
    }
    return n;
  }

  function sourceButtons(a, p) {
    var out = [h('button', {
      class: 'gbtn', type: 'button', title: '向上游拉一次 /models（不发推理请求）',
      disabled: !!S.testing[p.id],
      onclick: function (e) { e.stopPropagation(); runTest(p.id); }
    }, S.testing[p.id] ? '…' : '测试')];
    if (isCustom(p)) {
      out.push(h('button', {
        class: 'gbtn', type: 'button', title: '编辑来源',
        onclick: function (e) { e.stopPropagation(); openForm('edit', p.id); }
      }, '编辑'));
      if (S.confirmDel === p.id) {
        out.push(h('button', {
          class: 'gbtn danger', type: 'button', title: '确认删除此来源',
          onclick: function (e) { e.stopPropagation(); removeSource(p.id); }
        }, '确认删除'));
        out.push(h('button', {
          class: 'gbtn', type: 'button',
          onclick: function (e) { e.stopPropagation(); S.confirmDel = null; renderTree(); }
        }, '取消'));
      } else {
        out.push(h('button', {
          class: 'gbtn danger', type: 'button',
          title: '删除来源',
          onclick: function (e) { e.stopPropagation(); S.confirmDel = p.id; renderTree(); }
        }, '删除'));
      }
    } else {
      out.push(h('span', { class: 'gmeta', title: '内置来源，支持连通性测试与配置渠道头', text: '内置' }));
    }
    return out;
  }

  function modelNode(a, g, p, alias, head, onChange) {
    var draft = S.draft[p.id] || [];
    var on = draft.indexOf(alias) >= 0;
    var isLegacy = alias.slice(0, 2) === 'A/';
    var usable = codexUsable(alias);
    var lv = levelsOf(p, alias);
    var cb = h('input', { type: 'checkbox', class: 'gchk', checked: on, disabled: !usable,
      title: usable ? '暴露给客户端' : '已停用别名' });
    cb.addEventListener('change', function () {
      var arr = (S.draft[p.id] || []).slice(), at = arr.indexOf(alias);
      if (cb.checked && at < 0) arr.push(alias);
      if (!cb.checked && at >= 0) arr.splice(at, 1);
      S.draft[p.id] = arr;
      if (onChange) onChange();
      else { renderTree(); renderActions(); }
    });
    var inner = [
      cb,
      highlightLabel(head, alias, S.globalFilter),
      head && !isLegacy ? h('span', { class: 'ghead', text: head, title: '流量走 ' + p.id }) : null,
      isLegacy ? h('span', { class: 'gtag', text: '老任务别名', title: '兼容历史会话别名，已在客户端隐藏' }) : null,
      !usable ? h('span', { class: 'gtag', text: '跳过',
        title: '已停用别名' }) : null,
      lv && lv.levels.length ? h('span', { class: 'glv',
        title: '推理等级：' + lv.levels.join(' / ') + (lv.dflt ? '，默认 ' + lv.dflt : ''),
        text: lv.levels.length + ' LV' }) : null,
      h('span', { class: 'gspacer' }),
      h('span', { class: 'gmeta', text: p.id })
    ];
    return node('lv4', 'm:' + p.id + '/' + alias, false, inner, null);
  }

  function testNote(pid) {
    var t = S.test[pid];
    if (!t) return null;
    var ok = !!t.ok;
    var code = (typeof t.status === 'number' && t.status >= 100) ? ('HTTP ' + t.status)
      : (blockedCode(t.message) || '');
    var bits = [];
    if (ok && typeof t.model_count === 'number') bits.push(t.model_count + ' 个模型');
    if (typeof t.elapsed_ms === 'number' && t.elapsed_ms > 0) bits.push(t.elapsed_ms + ' ms');

    var textSpan = h('span', { class: 'txt' },
      (ok ? '✓ 连通' : '✗ 失败') + (code ? ' · ' + code : '') +
      (bits.length ? ' · ' + bits.join(' · ') : '') + (t.message ? ' — ' + t.message : ''));

    var els = [textSpan];
    if (!ok) {
      var detailBtn = h('button', {
        class: 'btn xs',
        type: 'button',
        text: '报错详情',
        title: '查看详细报错与响应内容',
        onclick: function (e) {
          e.stopPropagation();
          showTestDetail(pid, t);
        }
      });
      els.push(detailBtn);
    }
    return h('div', { class: 'gnote ' + (ok ? 'ok' : 'bad'), title: t.message || '' }, els);
  }

  function showTestDetail(pid, t) {
    if (!S.ctx || !S.ctx.dialog) return;
    var p = null;
    var ps = providerRows();
    for (var i = 0; i < ps.length; i++) {
      if (ps[i].id === pid) { p = ps[i]; break; }
    }
    var fullReport = {
      provider_id: pid,
      label: p ? p.label : pid,
      base_url: p ? p.base_url : '',
      ok: t.ok,
      status: t.status,
      elapsed_ms: t.elapsed_ms,
      message: t.message,
      detail: t.detail || t.error || t.message || '未知错误'
    };
    var rawJson = JSON.stringify(fullReport, null, 2);

    var copyBtn = h('button', { class: 'btn sm', type: 'button', text: '复制详情' });
    copyBtn.addEventListener('click', function () {
      function showSuccess() {
        copyBtn.textContent = '已复制 ✓';
        setTimeout(function () { copyBtn.textContent = '复制详情'; }, 1800);
      }
      function fallbackCopy() {
        try {
          var ta = document.createElement('textarea');
          ta.value = rawJson;
          ta.style.position = 'fixed';
          ta.style.opacity = '0';
          document.body.appendChild(ta);
          ta.focus();
          ta.select();
          var ok = document.execCommand('copy');
          document.body.removeChild(ta);
          if (ok) showSuccess();
          else copyBtn.textContent = '复制失败';
        } catch (e) {
          copyBtn.textContent = '复制失败';
        }
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(rawJson).then(showSuccess).catch(function () {
          fallbackCopy();
        });
      } else {
        fallbackCopy();
      }
    });

    var body = h('div', { class: 'test-detail-box' }, [
      h('div', { class: 'note' }, [
        h('div', null, '来源 ID：' + pid + (p && p.label ? ' (' + p.label + ')' : '')),
        h('div', null, '上游地址：' + (p ? p.base_url : '-')),
        h('div', null, '状态码：' + (t.status || '无') + ' · 耗时：' + (t.elapsed_ms ? t.elapsed_ms + ' ms' : '无'))
      ]),
      h('pre', { class: 'test-detail-pre', text: rawJson }),
      h('div', { class: 'home-acts' }, [copyBtn])
    ]);

    S.ctx.dialog({
      title: '连通性测试报告 · ' + pid,
      body: body,
      okText: '关闭',
      cancelText: null
    });
  }

  // ── 接入：预览 → 确认 → 写 ───────────────────────────────────────────────
  function previewAgent(aid) {
    if (!S.ctx || !S.ctx.dialog) return;
    S.agentBusy = aid; renderTree();
    api('/api/connect/preview', { method: 'POST', body: { agent: aid } }).then(function (pv) {
      S.agentBusy = null; renderTree();
      var body = h('div', null);
      if (pv.blocked) {
        body.appendChild(h('div', { class: 'msg bad' }, h('span', { class: 'txt', text: pv.blocked })));
        
        return S.ctx.dialog({ title: '接入 ' + pv.label, body: body, okText: '知道了', cancelText: '关闭' });
      }
      body.appendChild(h('div', { class: 'note', style: 'padding:0 0 10px' },
        '网关地址 ' + pv.gateway_base + ' · 会写进客户端配置的模型 ' + pv.models_count + ' 个'));
      if (pv.reformat_note) {
        body.appendChild(h('div', { class: 'msg warn' }, h('span', { class: 'txt', text: pv.reformat_note })));
      }
      var pvBox = h('div', { class: 'pv' });
      (pv.changes || []).forEach(function (c) {
        pvBox.appendChild(h('div', { class: 'r' }, [
          h('span', { class: 'k ' + c.kind, text: c.kind }),
          h('span', { class: 'n' }, [
            h('span', { class: 't', text: c.key }),
            h('span', { class: 'f', text: '  ' + (c.from === null || c.from === undefined ? '（无）' : String(c.from)) + ' → ' }),
            h('span', { class: 't', text: String(c.to) })
          ])
        ]));
      });
      body.appendChild(pvBox);
      body.appendChild(h('div', { class: 'note' },
        '原配置将自动备份，修改后需重启客户端生效。'));
      return S.ctx.dialog({
        title: '配置变更预览 · ' + pv.label,
        body: body, okText: '确认接入', cancelText: '取消'
      }).then(function (yes) {
        if (!yes) return null;
        S.agentBusy = aid; renderTree();
        return api('/api/connect', { method: 'POST', body: { agent: aid, confirm: true } })
          .then(function (r) {
            S.agentBusy = null;
            flash('ok', (r.label || aid) + ' 已接入，需重启客户端生效。');
            return reload();
          });
      });
    }).catch(function (e) {
      S.agentBusy = null; renderTree();
      flash('bad', '接入预览失败：' + errText(e));
    });
  }

  // ── 渠道头：点一下就改（走 PUT /api/sources/{id}/head，纯 plan 字段）──────
  function editHead(a, p) {
    if (!S.ctx || !S.ctx.dialog) return;
    var cur = (p.head || '').trim();
    var inp = h('input', { value: cur, placeholder: '例如 srapi（留空 = 无头主来源）' });
    var body = h('div', null,
      h('div', { class: 'note', style: 'padding:0 0 10px' },
        '客户端模型 ID: ' + (cur ? cur + '/' : '') + (modelList(p)[0] || '模型')),
      h('div', { class: 'f' }, h('label', null, '渠道头'), inp),
      h('div', { class: 'note' },
        '同一分组仅允许一个无头主来源。保存路由后生效。'));
    setTimeout(function () { try { inp.focus(); inp.select(); } catch (e) {} }, 0);
    S.ctx.dialog({ title: '渠道头 · ' + (p.label || p.id), body: body, okText: '保存' })
      .then(function (yes) {
        if (!yes) return null;
        var want = String(inp.value || '').trim();
        if (want === cur) { flash('info', '渠道头未修改'); return null; }
        if (want && !HEAD_RE.test(want)) {
          flash('bad', '渠道头「' + want + '」不合法：只能用 24 个字符以内的小写字母、数字、'
            + '点、下划线、连字符，不能含斜杠或空格。');
          return null;
        }
        S.busy = true; renderActions();
        return api('/api/sources/' + enc(p.id) + '/head', { method: 'PUT', body: { head: want } })
          .then(function (r) {
            S.busy = false;
            flash('ok', (r.changed ? '渠道头已更新为「' + (r.head || '无头') + '」' : '渠道头未修改') + '，保存路由后生效。');
            return reload();
          }).catch(function (e) {
            S.busy = false;
            flash('bad', '改渠道头失败：' + errText(e));
          });
      });
  }

  // ── 测试 ─────────────────────────────────────────────────────────────────
  function runTest(pid) {
    S.testing[pid] = true; renderTree();
    return api('/api/sources/' + enc(pid) + '/test', { method: 'POST', timeout: 60000 })
      .then(function (r) { S.test[pid] = r || { ok: false, message: '后端没返回测试结果' }; })
      .catch(function (e) { S.test[pid] = { ok: false, status: e.status || 0, message: errText(e) }; })
      .then(function () {
        if (!S.wrap) return;                     // 已 unmount（同上）
        S.testing[pid] = false; renderTree();
      });
  }
  function testAll() {
    var rows = providerRows().slice();
    if (!rows.length) return;
    S.busy = true; renderActions();
    var i = 0;
    (function step() {
      if (i >= rows.length) {
        S.busy = false;
        var ok = rows.filter(function (r) { return S.test[r.id] && S.test[r.id].ok; }).length;
        flash(ok === rows.length ? 'ok' : 'warn',
          '测试完毕：' + ok + '/' + rows.length + ' 个来源连通。');
        return;
      }
      return runTest(rows[i++].id).then(step);
    })();
  }

  // ── 保存路由 ─────────────────────────────────────────────────────────────
  function saveRouting() {
    if (!S.state || S.busy) return;
    var selected = {}, i;
    for (i = 0; i < GROUPS.length; i++) {
      selected[GROUPS[i].id] = (S.sel[GROUPS[i].id] || []).slice();
    }
    var picks = {};
    for (var pid in S.draft) {
      if (!Object.prototype.hasOwnProperty.call(S.draft, pid)) continue;
      if ((S.draft[pid] || []).join('\u0000') !== (S.origin[pid] || []).join('\u0000')) {
        picks[pid] = (S.draft[pid] || []).slice();
      }
    }
    S.busy = true; renderActions();
    api('/api/select', { method: 'POST', timeout: 90000, body: { revision: S.state.revision, selected: selected, picks: picks } })
      .then(function (data) {
        applyState(data);
        S.busy = false;
        var n = 0;
        for (i = 0; i < GROUPS.length; i++) n += (selected[GROUPS[i].id] || []).length;
        S.msg = { kind: 'ok', text: '路由已保存（' + n + ' 个来源启用）' };
        return api('/api/sources').catch(function () { return null; });
      })
      .then(function (s) { if (Array.isArray(s)) S.sources = s; renderAll(); })
      .catch(function (e) {
        S.busy = false;
        S.msg = { kind: 'bad', text: '保存失败：' + errText(e) };
        renderActions();
      });
  }

  // ── 加载 ─────────────────────────────────────────────────────────────────
  function applyState(data) {
    if (!data || typeof data !== 'object') return;
    S.state = data;
    S.sel = {}; S.selOrigin = {};
    GROUPS.forEach(function (g) {
      var v = data.selected ? data.selected[g.id] : null;
      var arr = Array.isArray(v) ? v.slice() : (typeof v === 'string' && v ? [v] : []);
      S.sel[g.id] = arr.slice();
      S.selOrigin[g.id] = arr.slice();
    });
    S.draft = {}; S.origin = {};
    providers().forEach(function (p) {
      var arr = (p.expose || []).filter(function (x) { return typeof x === 'string'; });
      S.draft[p.id] = arr.slice();
      S.origin[p.id] = arr.slice();
    });
  }

  function reload(showMsg) {
    S.busy = true; S.loadErr = null;
    renderAll();
    return Promise.all([
      api('/api/state', { timeout: 30000 }),
      api('/api/sources').catch(function () { return null; }),
      api('/api/agents').catch(function () { return null; }),
      api('/api/settings').catch(function () { return null; })
    ]).then(function (rs) {
      // 挂载期间切页 → unmount 会清掉 S.wrap/$tree 并置 S.state=null，而这里的
      // 异步回调没有守卫，会 TypeError 并产生未捕获 rejection（其余四页都有守卫，
      // 首页是唯一的例外 —— 复查轮 5 的 #B）。
      if (!S.wrap) return;
      var cfg = rs[3];
      if (cfg && cfg.app && typeof cfg.app.show_tech_details === 'boolean') {
        S.showTechDetails = cfg.app.show_tech_details;
        try { localStorage.setItem('prism.show_tech_details', S.showTechDetails ? '1' : '0'); } catch (e) {}
      }
      applyState(rs[0]);
      if (!S.state) throw new Error('/api/state 返回的不是配置对象');
      S.sources = Array.isArray(rs[1]) ? rs[1] : null;
      S.agents = Array.isArray(rs[2]) ? rs[2] : null;
      S.busy = false; S.loadErr = null;
      if (showMsg) S.msg = { kind: 'ok', text: '已刷新' };
      renderAll();
    }).catch(function (e) {
      if (!S.wrap) return;                       // 已 unmount：别碰 DOM（同上）
      S.busy = false;
      S.loadErr = errText(e);
      S.msg = { kind: 'bad', text: '读取配置失败：' + errText(e) };
      renderAll();
    });
  }

  // 统一的 unmount 守卫：四个 render* 都会 getElementById/appendChild 到已被移除的节点上，
  // 挂载期切页后由异步回调再进来就会 TypeError。这里一次挡住（复查轮 5 的 #B）。
  function renderAll() {
    if (!S.wrap) return;
    renderUnreadable(); renderTree(); renderForm(); renderActions();
  }

  // 有来源**读不出状态**时把它说出来。
  // `/api/state` 的 unreadable 收的是"这一行算不出启用状态"的来源（config 里那条条目
  // 缺失或重复、行缺 id、auth 文件读不出来……）。**必须显示** —— 不显示的话界面会把它
  // 画成"未启用"，用户分不清"读不出"和"没选"，那 API 层那句"不静默"就白写了。
  function renderUnreadable() {
    if (!$unreadableBox) return;
    clear($unreadableBox);
    var list = (S.state && Array.isArray(S.state.unreadable)) ? S.state.unreadable : [];
    if (!list.length) return;
    $unreadableBox.appendChild(h('div', { class: 'notice warn' },
      h('span', { class: 'k', text: list.length + ' 个来源状态异常' }),
      h('span', { class: 'nlist' }, list.map(function (u) {
        return h('span', {
          class: 'pth',
          text: (u && (u.label || u.id) || '（未知来源）') + '：' + ((u && u.error) || '未知原因')
        });
      }))));
  }

  // ── 底部：添加来源（保留。原来那张「所有来源」9 列表已去掉，
  //     来源的排序/统计在监控页）─────────────────────────────────────────
  function blankModel() { return { name: '', alias: '', cw: '', levels: [], dflt: '' }; }
  function blankForm() {
    return { mode: 'create', id: null, label: '', group: 'gpt', head: '', base_url: '',
      api_key: '', dirText: '', models: [blankModel()], pulled: null, err: null };
  }
  function openForm(mode, id) {
    if (mode === 'create') {
      S.form = blankForm();
    } else {
      var p = byId(id);
      if (!p) { flash('bad', '找不到来源 ' + id + '，先点「刷新」。'); return; }
      var ms = (p.model_settings && typeof p.model_settings === 'object') ? p.model_settings : {};
      var models = (p.models || []).filter(function (m) { return m && m.alias; }).map(function (m) {
        var s = ms[m.alias] && typeof ms[m.alias] === 'object' ? ms[m.alias] : {};
        return { name: m.name || m.alias, alias: m.alias,
          cw: (typeof m.context_length === 'number' ? String(m.context_length) : ''),
          levels: Array.isArray(s.levels) ? s.levels.slice() : [], dflt: s['default'] || '' };
      });
      S.form = { mode: 'edit', id: id, label: p.label || '', group: p.group || 'gpt',
        head: p.head || '', base_url: p.base_url || '', api_key: '', dirText: '',
        models: models.length ? models : [blankModel()], pulled: null,
        err: isCustom(p) ? null : '内置来源的端点与密钥受保护；渠道头可在行上直接调整。' };
    }
    S.confirmDel = null;
    renderForm();
    if ($form && $form.scrollIntoView) { try { $form.scrollIntoView({ block: 'nearest' }); } catch (e) {} }
  }

  function renderForm() {
    if (_unmounted()) return;
    clear($form);
    var f = S.form;
    if (!f) {
      $form.appendChild(h('div', { class: 'addsrc' },
        h('div', { class: 'as-open' },
          h('button', { class: 'btn pri', type: 'button', onclick: function () { openForm('create'); } }, '＋ 添加来源'),
          h('span', { class: 'as-line' }),
          null)));
      return;
    }
    var isCreate = f.mode === 'create';
    var box = h('div', { class: 'form' });
    box.appendChild(h('div', { class: 'formh' },
      h('span', { class: 't', text: isCreate ? '+ 添加来源' : '编辑来源 · ' + f.id }),
      h('span', { class: 'hr' }),
      h('span', { class: 'tag', text: isCreate ? 'ID 自动生成' : (isCustom(byId(f.id)) ? '自定义来源' : '内置来源') })));

    if (f.err) box.appendChild(h('div', { class: 'msg warn' }, h('span', { class: 'txt', text: f.err })));

    var groupSel = h('select', null, GROUPS.map(function (g) { return option(g.id, g.name); }));
    groupSel.value = f.group;
    groupSel.addEventListener('change', function () { f.group = groupSel.value; });

    var labelIn = h('input', { value: f.label, placeholder: '例如 Hub 大号' });
    labelIn.addEventListener('input', function () { f.label = labelIn.value; });

    var headIn = h('input', { value: f.head, placeholder: '留空 = 该分组的主来源' });
    headIn.addEventListener('input', function () { f.head = headIn.value.trim(); });

    var urlIn = h('input', { value: f.base_url, placeholder: 'https://example.com/v1' });
    urlIn.addEventListener('input', function () { f.base_url = urlIn.value.trim(); });

    var keyIn = h('input', { type: 'password', value: f.api_key,
      placeholder: isCreate ? 'sk-…' : '留空表示不修改密钥' });
    keyIn.addEventListener('input', function () { f.api_key = keyIn.value.trim(); });

    var dirIn = h('textarea', { rows: 3, placeholder: '点「拉取」从上游获取，或按行手填，每行一个模型 ID' });
    dirIn.value = f.dirText;
    dirIn.addEventListener('input', function () { f.dirText = dirIn.value; });

    box.appendChild(h('div', { class: 'grid' },
      h('div', { class: 'f' }, h('label', null, '分组'), groupSel),
      h('div', { class: 'f' }, h('label', null, '显示名称'), labelIn),
      h('div', { class: 'f' }, h('label', null, '渠道头'),
        headIn),
      h('div', { class: 'f span2' }, h('label', null, '端点'), urlIn),
      h('div', { class: 'f' }, h('label', null, 'API 密钥'), keyIn),
      h('div', { class: 'f span3' }, h('label', null, '模型目录'), dirIn,
        h('div', { class: 'rowbtns' },
          h('button', { class: 'btn sm', type: 'button', disabled: !!S.busy, onclick: pullModels },
            S.busy ? spin() : '拉取'),
          h('button', { class: 'btn sm', type: 'button', onclick: applyDirText }, '按行解析'),
          h('span', { class: 'sub', text: pullHint() })))));

    f.models.forEach(function (m, i) { box.appendChild(modelRow(m, i)); });

    box.appendChild(h('div', { class: 'formh', style: 'border-top:1px solid var(--line);border-bottom:0' },
      h('button', { class: 'btn', type: 'button', onclick: function () { f.models.push(blankModel()); renderForm(); } }, '+ 添加模型'),
      h('span', { class: 'spacer' }),
      h('button', { class: 'btn', type: 'button', onclick: function () { S.form = null; renderForm(); } }, '取消'),
      h('button', { class: 'btn pri', type: 'button', disabled: !!S.busy, onclick: submitForm },
        S.busy ? spin() : (isCreate ? '创建来源' : '保存修改'))));

    $form.appendChild(box);
  }

  function pullHint() {
    var f = S.form;
    if (!f.pulled) return '从上游拉取可用模型';
    if (!f.pulled.ok) return '拉取失败：' + f.pulled.text;
    return '已拉取 ' + f.pulled.total + ' 个可用模型 · 已选 ' +
      f.models.filter(function (m) { return m.alias; }).length + ' 个';
  }

  function modelRow(m, i) {
    var f = S.form;
    var idIn = h('input', { value: m.alias, placeholder: 'gpt-5.6-sol' });
    var oldAlias = m.alias;
    idIn.addEventListener('input', function () {
      m.alias = idIn.value.trim();
      if (!m.name || m.name === oldAlias) m.name = m.alias;
      oldAlias = m.alias;
    });
    var nameIn = h('input', { value: m.name, placeholder: '显示名称' });
    nameIn.addEventListener('input', function () { m.name = nameIn.value; });
    var cwIn = h('input', { value: m.cw, placeholder: '272000' });
    cwIn.addEventListener('input', function () { m.cw = cwIn.value.trim(); });

    var chips = h('div', { class: 'lvpick' });
    LEVELS.forEach(function (lv) {
      var on = m.levels.indexOf(lv) >= 0;
      chips.appendChild(h('span', {
        class: 'lv' + (on ? ' on' : '') + (m.dflt === lv ? ' def' : ''),
        title: on ? (lv + '（已选' + (m.dflt === lv ? '，默认' : '') + '）') : (lv + '（点一下选中）'),
        onclick: function () {
          var at = m.levels.indexOf(lv);
          if (at >= 0) m.levels.splice(at, 1); else m.levels.push(lv);
          m.levels = LEVELS.filter(function (x) { return m.levels.indexOf(x) >= 0; });
          if (m.dflt && m.levels.indexOf(m.dflt) < 0) m.dflt = '';
          renderForm();
        }
      }, lv));
    });

    var dfltSel = h('select', { title: '默认推理等级（必须是上面勾选的档位之一）' });
    dfltSel.appendChild(option('', m.levels.length ? '（不指定默认）' : '（先勾选档位）'));
    m.levels.forEach(function (lv) { dfltSel.appendChild(option(lv, lv)); });
    dfltSel.value = m.dflt || '';
    dfltSel.disabled = !m.levels.length;
    dfltSel.addEventListener('change', function () { m.dflt = dfltSel.value; renderForm(); });

    return h('div', { class: 'modelrow' },
      h('div', { class: 'f' }, h('label', null, '模型 ID'), idIn),
      h('div', { class: 'f' }, h('label', null, '显示名称'), nameIn),
      h('div', { class: 'f' }, h('label', null, '上下文窗口'), cwIn),
      h('div', { class: 'f' }, h('label', null, '推理等级 · 默认等级'), chips,
        h('div', { class: 'dfl' }, h('span', { class: 'sub', text: '默认' }), dfltSel)),
      h('div', { class: 'f' }, h('button', {
        class: 'btn', type: 'button', title: '移除该行',
        onclick: function () { f.models.splice(i, 1); if (!f.models.length) f.models.push(blankModel()); renderForm(); }
      }, '移除')));
  }

  // ── 拉模型 / 解析目录 / 提交来源 ─────────────────────────────────────────
  function pullModels() {
    var f = S.form;
    if (!f || S.busy) return;
    if (!f.base_url) { f.err = '请先填写端点地址。'; renderForm(); return; }
    f.err = null; f.pulled = null; S.busy = true; renderForm();
    if (!f.api_key && f.mode === 'edit' && f.id) {
      S.busy = false;
      useSnapshot('使用上次快照的模型列表');
      return;
    }
    api('/api/sources/preview', { method: 'POST', timeout: 60000, body: { base_url: f.base_url, api_key: f.api_key || undefined } })
      .then(function (r) {
        S.busy = false;
        var models = normalizeModels(r);
        if (r && r.ok === false && !models.length) {
          f.pulled = { ok: false, text: (r.message || '上游没返回模型') + (r.status ? '（HTTP ' + r.status + '）' : '') };
          f.err = '拉取失败：' + f.pulled.text; renderForm(); return;
        }
        if (!models.length) { f.pulled = { ok: false, text: (r && r.message) || '上游返回了 0 个模型' }; renderForm(); return; }
        fillPulled(models, (r && r.message) || '');
      })
      .catch(function (e) {
        if (e && (e.status === 404 || e.status === 405)) {
          S.busy = false; useSnapshot('使用上次快照的模型列表'); return;
        }
        S.busy = false; f.pulled = { ok: false, text: errText(e) }; f.err = '拉取失败：' + errText(e); renderForm();
      });
  }
  function useSnapshot(note) {
    var f = S.form;
    if (f.mode !== 'edit' || !f.id) {
      f.pulled = { ok: false, text: '获取模型列表失败，请手动填写模型 ID' };
      renderForm(); return Promise.resolve();
    }
    return api('/api/state', { timeout: 30000 }).then(function (st) {
      var ps = (st && st.providers) || [], p = null, i;
      for (i = 0; i < ps.length; i++) if (ps[i].id === f.id) p = ps[i];
      if (!p) { f.pulled = { ok: false, text: '已保存的来源里找不到 ' + f.id }; renderForm(); return; }
      if (p.fetch_error) { f.pulled = { ok: false, text: p.fetch_error }; renderForm(); return; }
      fillPulled((p.available || []).map(function (m) {
        return { alias: m.alias, name: m.name, context_length: m.context_length };
      }), note);
    }).catch(function (e) { f.pulled = { ok: false, text: errText(e) }; renderForm(); });
  }
  function normalizeModels(r) {
    var raw = (r && (r.models || r.available)) || [];
    if (!Array.isArray(raw)) return [];
    return raw.map(function (m) {
      if (typeof m === 'string') return { alias: m, name: m, context_length: null };
      return { alias: m.alias || m.id || m.name, name: m.name || m.alias || m.id,
        context_length: m.context_length || m.context_window || null };
    }).filter(function (m) { return m.alias; });
  }
  function tidyModels() {
    var f = S.form;
    var real = f.models.filter(function (m) { return !!m.alias; });
    f.models = real.length ? real : [blankModel()];
  }
  function fillPulled(list, note) {
    var f = S.form;
    var usable = list.filter(function (m) { return codexUsable(m.alias); });
    var have = {};
    var kept = f.models.filter(function (m) { return m.alias; });
    kept.forEach(function (m) { have[m.alias] = 1; });
    var src = byId(f.id);
    var ms = (src && src.model_settings && typeof src.model_settings === 'object') ? src.model_settings : {};
    usable.forEach(function (m) {
      if (have[m.alias]) return;
      have[m.alias] = 1;
      var s = ms[m.alias] && typeof ms[m.alias] === 'object' ? ms[m.alias] : {};
      f.models.push({ alias: m.alias, name: m.name || m.alias,
        cw: m.context_length ? String(m.context_length) : '',
        levels: Array.isArray(s.levels) ? s.levels.slice() : [], dflt: s['default'] || '' });
    });
    if (!f.models.length) f.models.push(blankModel());
    tidyModels();
    f.pulled = { ok: true, total: usable.length, note: note };
    renderForm();
  }
  function applyDirText() {
    var f = S.form;
    if (!f) return;
    var lines = String(f.dirText || '').split(/[\r\n]+/).map(function (s) { return s.trim(); }).filter(Boolean);
    if (!lines.length) { f.err = '模型目录为空，请粘贴模型 ID 或点「拉取」。'; renderForm(); return; }
    var have = {};
    f.models.forEach(function (m) { if (m.alias) have[m.alias] = 1; });
    var added = 0;
    lines.forEach(function (id) {
      if (have[id]) return;
      have[id] = 1; added++;
      f.models.push({ alias: id, name: id, cw: '', levels: [], dflt: '' });
    });
    f.models = f.models.filter(function (m) { return !!m.alias; });
    if (!f.models.length) f.models.push(blankModel());
    tidyModels();
    f.err = added ? (added + ' 个模型已添加') : '模型已存在';
    f.dirText = '';
    renderForm();
  }
  function buildSpec() {
    var f = S.form;
    var spec = { label: (f.label || '').trim(), group: f.group, head: (f.head || '').trim(),
      base_url: (f.base_url || '').trim(), models: [] };
    if (!spec.label) return { err: '显示名称不能为空。' };
    if (!spec.head && spec.head !== '') { /* 空串合法 = 无头 */ }
    if (spec.head && !HEAD_RE.test(spec.head)) {
      return { err: '渠道头不合法：只能用 24 个字符以内的小写字母、数字、点、下划线、连字符。' };
    }
    if (!spec.base_url) return { err: '端点不能为空。' };
    if (f.mode === 'create' && !f.api_key) return { err: '新增来源必须填 API 密钥。' };
    if (f.api_key) spec.api_key = f.api_key;
    var seen = {}, ms = {}, cws = [], bad = null;
    f.models.forEach(function (m) {
      var alias = (m.alias || '').trim();
      if (!alias) return;
      if (seen[alias]) { bad = bad || ('模型 ID 重复：' + alias); return; }
      seen[alias] = 1;
      spec.models.push({ name: (m.name || alias).trim() || alias, alias: alias });
      if (m.levels && m.levels.length) {
        var d = m.levels.indexOf(m.dflt) >= 0 ? m.dflt : m.levels[0];
        ms[alias] = { levels: m.levels.slice(), 'default': d };
      }
      if (m.cw !== '' && m.cw !== null && m.cw !== undefined) {
        var n = Number(m.cw);
        if (!isFinite(n) || n <= 0) { bad = bad || ('上下文窗口不是正整数：' + m.cw); return; }
        if (cws.indexOf(n) < 0) cws.push(n);
      }
    });
    if (bad) return { err: bad };
    if (!spec.models.length) return { err: '至少填一个模型 ID。' };
    if (Object.keys(ms).length) spec.model_settings = ms;
    spec.context_window = cws.length ? cws[0] : null;
    if (cws.length > 1) {
      return { spec: spec, warn: '上下文窗口是来源级设置，多行填了不同的值（' + cws.join(' / ') + '），已采用 ' + cws[0] + '。' };
    }
    return { spec: spec };
  }
  function submitForm() {
    var f = S.form;
    if (!f || S.busy) return;
    f.err = null;
    var built = buildSpec();
    if (built.err) { f.err = built.err; renderForm(); return; }
    var isCreate = f.mode === 'create';
    S.busy = true; renderForm(); renderActions();
    var req = isCreate
      ? api('/api/sources', { method: 'POST', body: built.spec })
      : api('/api/sources/' + enc(f.id), { method: 'PUT', body: built.spec });
    req.then(function (r) {
      var id = (r && r.id) || f.id;
      S.busy = false; S.form = null;
      S.msg = { kind: built.warn ? 'warn' : 'ok',
        text: (isCreate ? '来源已创建：' : '来源已更新：') + id +
          (built.warn ? '（' + built.warn + '）' : '') };
      return reload();
    }).catch(function (e) {
      S.busy = false;
      if (S.form) S.form.err = (isCreate ? '创建失败：' : '保存失败：') + errText(e);
      S.msg = { kind: 'bad', text: (isCreate ? '创建失败：' : '保存失败：') + errText(e) };
      renderAll();
    });
  }
  function removeSource(id) {
    var wasOn = isOn(id);
    S.confirmDel = null; S.busy = true;
    renderTree(); renderActions();
    api('/api/sources/' + enc(id), { method: 'DELETE' }).then(function () {
      S.busy = false;
      S.msg = { kind: 'ok', text: '已删除来源 ' + id };
      return reload();
    }).catch(function (e) {
      S.busy = false;
      S.msg = { kind: 'bad', text: '删除失败：' + errText(e) };
      renderAll();
    });
  }

  var busUnbind = null;
  function highlightSource(sourceId) {
    if (!sourceId) return;
    var p = byId(sourceId);
    if (!p) {
      setTimeout(function () {
        var p2 = byId(sourceId);
        if (p2) doHighlight(p2);
      }, 500);
      return;
    }
    doHighlight(p);
  }

  function doHighlight(p) {
    var gid = p.group;
    for (var k in S.open) {
      if (k.indexOf('g:') === 0 && k.indexOf('/' + gid) > 0) S.open[k] = true;
    }
    S.open['s:' + p.id] = true;
    saveOpen();
    renderTree();

    setTimeout(function () {
      if (!$tree) return;
      var el = $tree.querySelector('[data-key="s:' + p.id + '"]');
      if (el) {
        if (typeof el.scrollIntoView === 'function') {
          el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
        el.classList.add('highlight-pulse');
        setTimeout(function () { el.classList.remove('highlight-pulse'); }, 2200);
      }
    }, 120);
  }

  // ── mount / unmount ──────────────────────────────────────────────────────
  function mount(root, ctx) {
    injectStyle();
    if (S.wrap && S.wrap.parentNode) S.wrap.parentNode.removeChild(S.wrap);
    S.root = root || document.body;
    S.ctx = ctx || null;
    S.form = null; S.msg = null; S.test = {}; S.testing = {}; S.confirmDel = null;
    S.loadErr = null; S.agentBusy = null;
    loadOpen();
    try {
      S.showTechDetails = localStorage.getItem('prism.show_tech_details') === '1';
    } catch (e) {
      S.showTechDetails = false;
    }

    if (ctx && ctx.bus && typeof ctx.bus.on === 'function') {
      if (busUnbind) busUnbind();
      busUnbind = ctx.bus.on('highlight-source', highlightSource);
    }

    var wrap = S.wrap = h('div', { class: 'home' });

    $treeHint = h('span', { class: 'hint', text: '' });
    $tree = h('div', { class: 'graph' + (S.showTechDetails ? '' : ' hide-tech') });
    $tree.appendChild(h('div', { class: 'empty', text: '正在加载路由状态…' }));
    $globalToolbar = renderGlobalToolbar();
    wrap.appendChild($unreadableBox = h('div'));
    wrap.appendChild(h('div', { class: 'sec' },
       h('div', { class: 'sechead' },
        h('span', { class: 'cmt', text: '//' }), h('span', { class: 'stitle', text: '路由' }),
        h('span', { class: 'hr' }), $treeHint),
      $globalToolbar,
      $tree));

    wrap.appendChild(h('div', { class: 'sec' },
      h('div', { class: 'sechead' },
        h('span', { class: 'cmt', text: '//' }), h('span', { class: 'stitle', text: '添加来源' }),
        h('span', { class: 'hr' })
      ),
      ($form = h('div'))));

    wrap.appendChild($acts = h('div'));
    wrap.appendChild($stickyBar = h('div', { class: 'home-sticky-bar' }));
    S.root.appendChild(wrap);

    S.form = null;
    renderAll();
    reload();
    return {
      id: 'home',
      unmount: unmount,
      isDirty: isDirty,
      refresh: function () { return reload(true); }
    };
  }

  function unmount() {
    if (busUnbind) { busUnbind(); busUnbind = null; }
    if (S.wrap && S.wrap.parentNode) S.wrap.parentNode.removeChild(S.wrap);
    S.wrap = null;
    $tree = $treeHint = $addsrc = $form = $acts = $msgBox = $stickyBar =
      $globalToolbar = $globalFilterInp = $filterBadge = $clearFilterBtn = $unreadableBox = null;
    S.state = null; S.sources = null; S.agents = null;
    S.draft = {}; S.origin = {}; S.sel = {}; S.selOrigin = {};
    S.test = {}; S.testing = {}; S.form = null; S.msg = null;
    S.busy = false; S.loadErr = null; S.confirmDel = null; S.agentBusy = null;
    S.globalFilter = '';
    S.showTechDetails = false;
    S.ctx = null;
    return Promise.resolve();
  }

  window.PrismPages = window.PrismPages || {};
  window.PrismPages.home = {
    id: 'home', title: '首页',
    mount: mount, unmount: unmount,
    isDirty: isDirty,
    refresh: function () { return reload(true); }
  };
  window.PrismHomePage = window.PrismPages.home;
})();
