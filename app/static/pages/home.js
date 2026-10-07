/* ============================================================================
 * Prism · 首页  —  static/pages/home.js
 *
 * 四级节点树：
 *
 *     Agent（Codex / Claude Code / Claude Code 桌面端 / opencode / Hermes）
 *       └ 分组（来自路由计划，不再写死）
 *           └ 来源（SRAPI、OpenAI 官方 …）        ← 点分组才展开
 *               └ 模型（srapi/gpt-5.6-sol …）      ← 点来源才展开
 *
 * 契约见 static/app.js 顶部的注释。本页要用 ctx.api / ctx.state / ctx.dialog /
 * ctx.onUnmount。
 *
 * ── 这一页的范围（别处重复了就删）─────────────────────────────────────
 * 去掉：原来那张 9 列的「所有来源」表。它的排序/统计能力在**监控页**已经有了
 *       （monitor.js 的 sourcesSection），首页不再摆第二份。
 * 保留：底部「＋ 添加来源」表单、节点树、测试、代理端接入预览。
 * 不在这页：来源的编辑、删除、改渠道头，以及分组的增删改（在管理页）。
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

  var LEVELS = ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'];
  var HEAD_RE = /^[a-z0-9][a-z0-9._-]{0,23}$/;
  var STYLE_ID = 'prism-home-style';
  // 旧键 prism.graph.open 里写过「默认展开第一个已接入」，那不是用户点的。换键后首次进来整棵树收起。
  var OPEN_KEY = 'prism.graph.open2';

  // ── 页内样式：只放树以外、本页独有的东西。树在 app.css。 ─────────────────
  var CSS = [
    // .form/.formh/.grid/.f/label/.btn/.tag/.statebox 全部继承 app.css，不重写
    '.home{color:var(--fg);font-family:var(--sans);padding:0 0 10px}',
    '.home *{box-sizing:border-box}',
    '.home .sechead{flex-wrap:wrap}',
    '.home .hr{display:none}',
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
    '.home .home-global-toolbar{display:flex;align-items:center;gap:12px;margin:0 0 16px 0;padding:8px 12px;background:var(--panel);border:1px solid var(--line);border-radius:var(--r-card);box-shadow:var(--shadow);flex-wrap:wrap}',
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
  // Claude Code 桌面端和 CLI 写同一个 settings.json，渠道头共用 claude-code 这一份。
  var HEAD_OWNER = { 'claude-code-desktop': 'claude-code' };
  function effectiveHead(agentId, p) {
    var owner = HEAD_OWNER[agentId] || agentId;
    var stored = S.state && S.state.agent_heads;
    var slot = stored && stored[owner];
    if (slot && Object.prototype.hasOwnProperty.call(slot, p.id)) {
      return String(slot[p.id] || '').trim();
    }
    return String((p && p.head) || '').trim();
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
  var $tree, $treeHint, $addsrc, $form, $acts, $stickyBar,
      $globalToolbar, $globalFilterInp, $filterBadge, $clearFilterBtn, $unreadableBox;

  // unmount 会把 $tree/$form/$acts 置 null，而多处**异步回调**直接调这些渲染函数
  // （saveRouting 的失败回调、pullModels、useSnapshot、previewAgent、editHead、applyToAgents 的
  // step…）。只守 renderAll 不够 —— 那些路径根本不经过它（复查轮 6 的 3-1）。
  // 所以三个入口各守一次：谁调都安全。
  function _unmounted() { return !S.wrap; }

  // ── 展开状态持久化。没点过的节点一律收起：先只看见代理端名单。
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
  function currentGroups() {
    var raw = S.state && S.state.groups;
    if (!Array.isArray(raw)) return [];
    return raw.filter(function (g) { return g && g.id; }).map(function (g) {
      return { id: g.id, name: g.name || g.id };
    });
  }
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

  // ── 渲染：动作条 + 消息 ──────────────────────────────────────────────────
  function updateStickyBar() {
    if (!$stickyBar) return;
    var dirty = isDirty();
    // 失败原因只留在这条吸底条。操作区再画一条横幅，会和这里叠成两句一样的报错。
    $stickyBar.style.display = (dirty || !!S.msg) ? 'flex' : 'none';
    if (!dirty && !S.msg) return;
    $stickyBar.className = 'home-sticky-bar' + (dirty ? ' dirty' : '');

    var on = 0, i;
    for (i = 0; i < currentGroups().length; i++) on += enabledCount(currentGroups()[i].id);
    var dirtyCount = 0;
    for (var gi = 0; gi < currentGroups().length; gi++) {
      var g = currentGroups()[gi].id;
      if ((S.sel[g] || []).join('\u0000') !== (S.selOrigin[g] || []).join('\u0000')) dirtyCount++;
    }
    for (var pid in S.draft) {
      if ((S.draft[pid] || []).join('\u0000') !== (S.origin[pid] || []).join('\u0000')) dirtyCount++;
    }
    if (isFormDirty()) dirtyCount++;

    var badgeText = '待保存';
    var saveText = S.busy ? '' : '保存';

    var badge = $stickyBar.querySelector('[data-role="sb-badge"]');
    var saveBtn = $stickyBar.querySelector('[data-role="sb-save"]');
    var refreshBtn = $stickyBar.querySelector('[data-role="sb-refresh"]');

    if (!badge) {
      clear($stickyBar);
      badge = h('span', { class: 'tag warn', text: badgeText, data: { role: 'sb-badge' } });
      saveBtn = h('button', {
        class: 'btn pri sm', type: 'button',
        disabled: !dirty || !!S.busy || !S.state,
        data: { role: 'sb-save' },
        onclick: saveRouting
      }, S.busy ? spin() : saveText);
      refreshBtn = h('button', {
        class: 'btn sm', type: 'button', disabled: !!S.busy,
        data: { role: 'sb-refresh' },
        onclick: function () { confirmDiscard(refreshFromGateway); }
      }, '刷新');

      $stickyBar.appendChild(h('div', { class: 'sb-left' }, [badge]));
      $stickyBar.appendChild(h('div', { class: 'sb-spacer' }));
      $stickyBar.appendChild(h('div', { class: 'sb-right' }, [refreshBtn, saveBtn]));
    } else {
      badge.className = 'tag warn';
      badge.textContent = badgeText;
      saveBtn.disabled = !dirty || !!S.busy || !S.state;
      refreshBtn.disabled = !!S.busy;
      clear(saveBtn);
      if (S.busy) saveBtn.appendChild(spin());
      else saveBtn.textContent = saveText;
    }

    var sbMsg = $stickyBar.querySelector('[data-role="sb-msg"]');
    if (!sbMsg) {
      sbMsg = h('span', { class: 'sb-msg', data: { role: 'sb-msg' } });
      $stickyBar.querySelector('.sb-left').appendChild(sbMsg);
    }
    sbMsg.className = 'sb-msg' + (S.msg ? ' ' + S.msg.kind : '');
    sbMsg.textContent = S.msg ? S.msg.text : '';
    sbMsg.title = S.msg ? S.msg.text : '';
    sbMsg.style.display = S.msg ? '' : 'none';
    badge.style.display = dirty ? '' : 'none';
  }

  // 「刷新」在脏状态下会把未保存的改动无声覆盖掉，先问一句再走
  function confirmDiscard(then) {
    if (!isDirty()) { then(); return; }
    if (!S.ctx || typeof S.ctx.dialog !== 'function') {
      flash('warn', '有未保存的路由改动，请先保存或撤销。');
      return;
    }
    S.ctx.dialog({
      title: '有未保存的路由改动',
      body: '刷新会用网关上的当前配置覆盖这些改动。',
      okText: '丢弃并刷新',
      cancelText: '继续编辑',
      danger: true
    }).then(function (ok) { if (ok) then(); });
  }

  function refreshFromGateway() { reload(true); }

  function renderActions() {
    if (_unmounted()) return;
    clear($acts);
    $acts.appendChild(h('div', { class: 'home-acts' },
      h('button', {
        class: 'btn pri', type: 'button', disabled: !!S.busy || !S.state, onclick: saveRouting
      }, S.busy ? spin() : '保存路由'),
      h('button', { class: 'btn', type: 'button', disabled: !!S.busy, onclick: function () { confirmDiscard(refreshFromGateway); } }, '刷新'),
      h('button', {
        class: 'btn', type: 'button', disabled: !!S.busy || !S.state,
        title: '把当前选择保存到网关，并写进各客户端的配置文件',
        onclick: applyToAgents
      }, '应用配置'),
      h('span', { class: 'hspacer' })
    ));
    updateStickyBar();
  }
  function flash(kind, text) { S.msg = { kind: kind, text: text }; renderActions(); }

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
    return !!(sourceForm && sourceForm.isDirty());
  }

  function isDirty() {
    if (isFormDirty()) return true;
    var i;
    for (i = 0; i < currentGroups().length; i++) {
      var g = currentGroups()[i].id;
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
    // 手风琴只显示一个代理端。全部写成展开的话，重画时会落到名单里的第一个，
    // 收起态下子节点还没挂上，只改 class 也看不到分组。
    var aId = getActiveAgentId();
    var k;
    for (k in S.open) {
      if (k.indexOf('a:') === 0) S.open[k] = false;
    }
    if (aId) S.open['a:' + aId] = true;
    (S.agents || []).forEach(function (ag) {
      for (var i = 0; i < currentGroups().length; i++) {
        S.open['g:' + ag.id + '/' + currentGroups()[i].id] = true;
      }
    });
    var provs = providerRows();
    for (var j = 0; j < provs.length; j++) {
      S.open['s:' + provs[j].id] = true;
    }
    saveOpen();
    renderTree();
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

    // 树默认收起时来源行还没挂上。搜索先展开当前代理端，分组仍收着，匹配项再由下面打开。
    if ($tree && !$tree.querySelector('.gnode.lv3')) {
      var aid = getActiveAgentId();
      if (aid && !isOpen('a:' + aid, false)) {
        S.open['a:' + aid] = true;
        renderTree();
      }
    }

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
      $filterBadge.textContent = totalMatches ? (totalMatches + ' 个匹配') : '无匹配';
    }
  }

  function renderGlobalToolbar() {
    $globalFilterInp = h('input', {
      type: 'text',
      class: 'home-global-filter',
      placeholder: '搜索模型...'
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
      title: '展开全部节点',
      onclick: function (e) {
        e.preventDefault();
        expandAllNodes();
      }
    }, '展开');

    var collapseAllBtn = h('button', {
      class: 'btn sm home-tool-btn',
      type: 'button',
      title: '折叠全部节点',
      onclick: function (e) {
        e.preventDefault();
        collapseAllNodes();
      }
    }, '折叠');

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
    var tag = a.connected ? h('span', { class: 'gtag ok', text: '已接入' })
      : (a.exists ? h('span', { class: 'gtag', text: '未接入' })
                  : h('span', { class: 'gtag warn', text: '未安装' }));
    var inner = [
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
      // 子树先不挂，但保留箭头。不要 clone 这一行，否则「接入预览」的点击会丢。
      n.classList.add('has-kids', 'clickable');
      row.setAttribute('role', 'button');
      row.setAttribute('tabindex', '0');
      row.setAttribute('aria-expanded', 'false');
      var doToggle = function (e) {
        if (e && e.target && e.target.closest && e.target.closest('input,.gbtn,.ghead')) return;
        for (var k in S.open) if (k.indexOf('a:') === 0) S.open[k] = false;
        S.open['a:' + a.id] = true;
        saveOpen();
        renderTree();
      };
      row.addEventListener('click', doToggle);
      row.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); doToggle(e); }
      });
    }
    return n;
  }

  function headOwnerConnected(agentId) {
    // 桌面端和 CLI 共用 claude-code 这一份渠道头，有一边已接入就算。
    var owner = agentId === 'claude-code-desktop' ? 'claude-code' : agentId;
    var list = S.agents || [];
    for (var i = 0; i < list.length; i++) {
      var id = list[i].id;
      var o = id === 'claude-code-desktop' ? 'claude-code' : id;
      if (o === owner && list[i].connected) return true;
    }
    return false;
  }

  function headWarnShown(agentId, gid) {
    if (!agentId || agentId === 'copilot-agent') return false;
    // 没接入的代理端不占干净模型 ID，不在这里预警。
    if (!headOwnerConnected(agentId)) return false;
    if (enabledCount(gid) <= 1) return false;
    var heads = groupProvs(gid).filter(function (p) {
      return isOn(p.id) && !effectiveHead(agentId, p);
    });
    return heads.length > 1;
  }

  function updateGroupStatus(gid) {
    if (!$tree) return;
    var gTags = $tree.querySelectorAll('[data-role="g-status"][data-gid="' + gid + '"]');
    var on = enabledCount(gid);
    for (var i = 0; i < gTags.length; i++) {
      gTags[i].className = 'gtag' + (on ? ' ok' : '');
      gTags[i].textContent = on ? (on + ' 启用') : '';
      gTags[i].style.display = on ? '' : 'none';
    }
    // 组级警告跟着一起就地切换：勾选来源只改行标签与计数、不重渲染整棵树，警告若挂在
    // 渲染树里就得等下一次重渲染才出现 —— 用户勾完直接点保存、被后端拒了却看不到预警。
    var warns = $tree.querySelectorAll('[data-role="g-headwarn"][data-gid="' + gid + '"]');
    for (i = 0; i < warns.length; i++) {
      var aid = warns[i].getAttribute('data-agent');
      warns[i].style.display = headWarnShown(aid, gid) ? '' : 'none';
    }
  }

  function updateTreeHint() {
    if (!$treeHint) return;
    if (!S.state) { $treeHint.textContent = S.loadErr ? '读取失败' : ''; return; }
    $treeHint.textContent = '';
  }

  function groupNodes(a) {
    var groups = currentGroups();
    if (!groups.length) {
      return [h('div', { class: 'gnote' }, [
        h('span', { text: '暂无分组' }),
        h('button', {
          class: 'gbtn', type: 'button',
          onclick: function (e) {
            e.stopPropagation();
            location.hash = '#/manage';
          }
        }, '创建分组')
      ])];
    }
    return groups.map(function (g) {
      var provs = groupProvs(g.id);
      var on = enabledCount(g.id);
      var gStatusTag = h('span', {
        class: 'gtag' + (on ? ' ok' : ''),
        text: on ? (on + ' 启用') : '',
        style: on ? null : { display: 'none' },
        data: { role: 'g-status', gid: g.id }
      });
      var inner = [
        h('span', { class: 'glabel', text: g.name }),
        gStatusTag,
        h('span', { class: 'gspacer' }),
        h('span', { class: 'gmeta', text: provs.length ? '' : '无来源' })
      ];
      var kids = provs.map(function (p) { return sourceNode(a, g, p); });
      if (!kids.length) {
        kids = [h('div', { class: 'gnote', text: '无来源' })];
      }
      // 警告节点常驻，显隐由 updateGroupStatus 就地切换（勾选来源不走整树重渲染）。
      kids = kids.concat([h('div', {
        class: 'gnote bad', text: '存在重复无渠道头来源，请设置渠道头',
        style: headWarnShown(a.id, g.id) ? null : { display: 'none' },
        data: { role: 'g-headwarn', gid: g.id, agent: a.id }
      })]);
      return node('lv2', 'g:' + a.id + '/' + g.id, false, inner, kids);
    });
  }

  function sourceNode(a, g, p) {
    var on = isOn(p.id);
    var head = effectiveHead(a.id, p);
    var usable = modelList(p);
    var exposed = (S.draft[p.id] || []).filter(function (x) { return usable.indexOf(x) >= 0; });
    var statusTag = h('span', {
      class: 'gtag ok',
      text: '已启用',
      style: on ? null : { display: 'none' }
    });
    var exposedTag = h('span', {
      class: 'gtag acc',
      text: String(exposed.length)
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
      statusTag.style.display = isNowOn ? '' : 'none';
      updateGroupStatus(g.id);
      updateTreeHint();
      renderActions();
    });

    var headEl = null;
    if (a.id !== 'copilot-agent') {
      // 渠道头在管理页改。这里只显示，点了不弹框。
      headEl = h('span', {
        class: 'ghead' + (head ? '' : ' none main'),
        title: head
          ? ('渠道头: ' + head + '（客户端 ID: ' + head + '/' + (usable[0] || '模型') + '）')
          : '主来源（保留原始模型 ID）',
        text: head || '主'
      });
    }

    var note = null;
    if (p.blocked) note = h('div', { class: 'gnote bad', text: '来源已阻断：' + p.blocked });
    else if (p.fetch_error) note = h('div', { class: 'gnote warn', text: '模型拉取失败' });

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
          exposedTag.textContent = String(curExp.length);
          exposedTag.style.display = curExp.length ? '' : 'none';
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
    if (!usable.length) {
      kids.push(h('div', { class: 'gnote', text: '无模型' }));
    } else {
      var filterInp = h('input', {
        class: 'model-search-inp',
        type: 'text',
        placeholder: '搜索模型...'
      });
      var filterBar = h('div', { class: 'model-filter-bar' }, filterInp);
      if (usable.length > 12) kids.push(filterBar);

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
      h('span', { class: 'glabel', text: p.label || p.id }),
      headEl,
      statusTag,
      exposedTag,
      h('span', { class: 'gspacer' }),
      h('span', { class: 'gmeta', text: shortHost(p.base_url) + (p.tag ? ' · ' + p.tag : '') }),
      h('span', { class: 'gacts' }, sourceButtons(a, p))
    ];
    var n = node('lv3', key, false, inner, kids, {
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
      class: 'gbtn', type: 'button', title: '向服务商拉一次 /models（不发推理请求）',
      disabled: !!S.testing[p.id],
      onclick: function (e) { e.stopPropagation(); runTest(p.id); }
    }, S.testing[p.id] ? '…' : '测试')];
    if (!isCustom(p)) {
      out.push(h('span', { class: 'gmeta', text: '内置' }));
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
        h('div', null, '来源地址：' + (p ? p.base_url : '-')),
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
      if (!pv.direct) {
        body.appendChild(h('div', { class: 'note', style: 'padding:0 0 10px' },
          '网关地址 ' + pv.gateway_base + ' · 会写进客户端配置的模型 ' + pv.models_count + ' 个'));
      }
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
  function writableAgents() {
    var seen = {}, list = [];
    (S.agents || []).forEach(function (a) {
      if (!a.implemented || !a.exists) return;
      var key = a.config_path || a.id;   // claude-code 与桌面端共用一份文件，只写一次
      if (seen[key]) return;
      seen[key] = 1;
      list.push(a);
    });
    return list;
  }

  // 先选目标（某一个客户端 / 全部）再执行。写入前仍要过一次变更预览，不减步骤。
  function pickAgents(list) {
    var picked = list.slice(), rows = [];
    function refresh() {
      rows.forEach(function (r) {
        var on = r.agent ? picked.indexOf(r.agent) >= 0 : picked.length === list.length;
        r.el.className = 'r pick' + (on ? ' on' : '');
      });
    }
    var allRow = { agent: null, el: h('div', { class: 'r pick on', role: 'button', tabindex: '0' },
      h('span', { class: 'n' }, h('span', { class: 't', text: '全部客户端（' + list.length + ' 个）' }))) };
    allRow.el.addEventListener('click', function () { picked = list.slice(); refresh(); });
    rows.push(allRow);
    list.forEach(function (a) {
      var row = { agent: a, el: h('div', { class: 'r pick', role: 'button', tabindex: '0' },
        h('span', { class: 'n' }, [
          h('span', { class: 't', text: a.label + '  ' }),
          h('span', { class: 'f', text: ' ' + (a.config_display || a.config_path || '') })
        ])) };
      row.el.addEventListener('click', function () { picked = [a]; refresh(); });
      rows.push(row);
    });
    rows.forEach(function (r) {
      r.el.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); r.el.click(); }
      });
    });
    var body = h('div', null,
      h('div', { class: 'note', style: 'padding:0 0 10px' },
        '选一个客户端或全部，下一步列出将要写入的改动。'),
      h('div', { class: 'pv' }, rows.map(function (r) { return r.el; })));
    return S.ctx.dialog({
      title: '应用到客户端配置', body: body, okText: '应用到所选', cancelText: '取消'
    }).then(function (yes) { return yes ? picked : null; });
  }

  // ── 应用配置：把当前选择真正落到各客户端的配置文件里 ────────────────────────
  //
  // 顺序不能反：客户端配置里的模型清单来自 routing-plan.json 的 expose，而 expose 由
  // 「保存路由」写入 —— 没落盘就同步不到客户端。脏的时候先保存，保存失败就停住（原因
  // 已经显示在吸底条上），不拿半旧的数据去改用户的客户端配置。
  function applyToAgents() {
    if (!S.state || S.busy) return;
    if (!S.ctx || typeof S.ctx.dialog !== 'function') return;
    var list = writableAgents();
    if (!list.length) { flash('warn', '没有可写入的客户端配置。'); return; }
    pickAgents(list).then(function (picked) {
      if (!picked || !picked.length) return null;
      var start = isDirty() ? saveRouting() : Promise.resolve(true);
      return start.then(function (saved) {
        if (!saved) return null;
        S.busy = true; renderActions();
        return previewAgents(picked);
      }).then(function (items) {
        if (!items) return null;
        return confirmApply(items).then(function (yes) {
          if (!yes) { S.busy = false; renderActions(); return null; }
          return runConnects(items);
        });
      }).then(function (results) {
        S.busy = false; renderActions();
        if (!results) return;
        var bad = results.filter(function (r) { return r.error; });
        if (bad.length) {
          flash('bad', '应用失败：' + bad.map(function (r) {
            return r.agent.label + '（' + r.error + '）';
          }).join('；'));
        }
        return reload();
      }).catch(function (e) {
        S.busy = false; S.agentBusy = null;
        flash('bad', '应用配置失败：' + errText(e));
      });
    });
  }

  function previewAgents(list) {
    var out = [], i = 0;
    function step() {
      if (i >= list.length) { S.agentBusy = null; renderTree(); return out; }
      var a = list[i++];
      S.agentBusy = a.id; renderTree();
      return api('/api/connect/preview', { method: 'POST', body: { agent: a.id } })
        .then(function (pv) { out.push({ agent: a, preview: pv, error: null }); return step(); })
        .catch(function (e) { out.push({ agent: a, preview: null, error: errText(e) }); return step(); });
    }
    return step();
  }

  function confirmApply(items) {
    var willWrite = 0;
    var box = h('div', { class: 'pv' });
    items.forEach(function (it) {
      var pv = it.preview, state, cls = '';
      if (it.error) { state = '读不到：' + it.error; cls = 'bad'; }
      else if (pv.blocked) { state = pv.blocked; cls = 'bad'; }
      else if (!(pv.changes || []).length) { state = '已是最新，无需改动'; }
      else { state = '写 ' + pv.changes.length + ' 处变更'; willWrite++; }
      box.appendChild(h('div', { class: 'r' }, h('span', { class: 'n' }, [
        h('span', { class: 't', text: it.agent.label + '  ' }),
        h('span', { class: 'f', text: ' ' + (it.agent.config_display || it.agent.config_path || '') + '  ' }),
        h('span', { class: 't' + (cls ? ' ' + cls : ''), text: state })
      ])));
    });
    var body = h('div', null,
      h('div', { class: 'note', style: 'padding:0 0 10px' },
        willWrite ? ('改写 ' + willWrite + ' 个客户端的配置文件，原文件先备份。') : '没有需要写入的改动。'),
      box,
      h('div', { class: 'note' }, '写入后需重启对应客户端生效。'));
    return S.ctx.dialog({
      title: '应用到客户端配置', body: body,
      okText: willWrite ? '确认应用' : '知道了', cancelText: '取消', danger: true
    });
  }

  function runConnects(items) {
    var todo = items.filter(function (it) {
      return !it.error && it.preview && !it.preview.blocked && (it.preview.changes || []).length;
    });
    var out = [], i = 0;
    function step() {
      if (i >= todo.length) { S.agentBusy = null; renderTree(); return out; }
      var it = todo[i++];
      S.agentBusy = it.agent.id; renderTree();
      // codex 走既有的 connect_client 路径（认 revision），其余适配器认 confirm —— 两个都带上。
      return api('/api/connect', {
        method: 'POST', timeout: 30000,
        body: { agent: it.agent.id, confirm: true, revision: S.state.revision }
      }).then(function () { out.push({ agent: it.agent, error: null }); return step(); })
        .catch(function (e) { out.push({ agent: it.agent, error: errText(e) }); return step(); });
    }
    return step();
  }

  // ── 保存路由 ─────────────────────────────────────────────────────────────
  function saveRouting() {
    if (!S.state || S.busy) return Promise.resolve(false);
    var selected = {}, i;
    for (i = 0; i < currentGroups().length; i++) {
      selected[currentGroups()[i].id] = (S.sel[currentGroups()[i].id] || []).slice();
    }
    var picks = {};
    for (var pid in S.draft) {
      if (!Object.prototype.hasOwnProperty.call(S.draft, pid)) continue;
      if ((S.draft[pid] || []).join('\u0000') !== (S.origin[pid] || []).join('\u0000')) {
        picks[pid] = (S.draft[pid] || []).slice();
      }
    }
    S.busy = true; renderActions();
    // 返回值给「应用配置」用：保存没成就别去改客户端配置。按钮 onclick 不看它，无副作用。
    return api('/api/select', { method: 'POST', timeout: 90000, body: { revision: S.state.revision, selected: selected, picks: picks } })
      .then(function (data) {
        applyState(data);
        S.busy = false;
        S.msg = null;
        return api('/api/sources').catch(function () { return null; });
      })
      .then(function (s) { if (Array.isArray(s)) S.sources = s; renderAll(); return true; })
      .catch(function (e) {
        S.busy = false;
        S.msg = { kind: 'bad', text: '保存失败：' + errText(e) };
        renderActions();
        return false;
      });
  }

  // ── 加载 ─────────────────────────────────────────────────────────────────
  function applyState(data) {
    if (!data || typeof data !== 'object') return;
    S.state = data;
    S.sel = {}; S.selOrigin = {};
    currentGroups().forEach(function (g) {
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
      api('/api/agents').catch(function () { return null; })
    ]).then(function (rs) {
      if (!S.wrap) return;
      applyState(rs[0]);
      if (!S.state) throw new Error('/api/state 返回的不是配置对象');
      S.sources = Array.isArray(rs[1]) ? rs[1] : null;
      S.agents = Array.isArray(rs[2]) ? rs[2] : null;
      S.busy = false; S.loadErr = null;
      if (showMsg) S.msg = null;
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


  var busUnbind = null;
  // 底部只添加来源。编辑、删除、改渠道头在管理页。没有分组时不渲染表单。
  var sourceForm = null;
  function ensureSourceForm() {
    if (sourceForm || !window.PrismSourceForm) return sourceForm;
    sourceForm = window.PrismSourceForm.create({
      h: h,
      api: api,
      errText: errText,
      codexUsable: codexUsable,
      groups: currentGroups,
      byId: byId,
      isCustom: isCustom,
      allowCreate: true,
      onBusy: function (v) { S.busy = !!v; if (typeof renderActions === 'function') renderActions(); },
      onSaved: function (warn) {
        S.msg = warn ? { kind: 'warn', text: warn } : null;
        return reload();
      },
      onError: function (text) {
        S.msg = { kind: 'bad', text: text };
        renderAll();
      }
    });
    return sourceForm;
  }
  function renderForm() {
    if (_unmounted()) return;
    if (!currentGroups().length) {
      if (sourceForm) sourceForm.close();
      else if ($form) clear($form);
      return;
    }
    var f = ensureSourceForm();
    if (f) f.renderInto($form);
  }

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
    var aid = getActiveAgentId();
    var k;
    // 树默认收起时来源行还没挂上。先打开当前代理端和它所在的分组，跳转才看得到。
    for (k in S.open) {
      if (k.indexOf('a:') === 0) S.open[k] = false;
    }
    if (aid) S.open['a:' + aid] = true;
    if (aid && gid) S.open['g:' + aid + '/' + gid] = true;
    if (p.id) S.open['s:' + p.id] = true;
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
        h('span', { class: 'stitle', text: '路由' }),
        h('span', { class: 'hr' }), $treeHint),
      $globalToolbar,
      $tree));

    wrap.appendChild(h('div', { class: 'sec' },
      ($form = h('div'))));

    wrap.appendChild($acts = h('div'));
    wrap.appendChild($stickyBar = h('div', { class: 'home-sticky-bar' }));
    S.root.appendChild(wrap);

    S.form = null;
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
    $tree = $treeHint = $addsrc = $form = $acts = $stickyBar =
      $globalToolbar = $globalFilterInp = $filterBadge = $clearFilterBtn = $unreadableBox = null;
    S.state = null; S.sources = null; S.agents = null;
    S.draft = {}; S.origin = {}; S.sel = {}; S.selOrigin = {};
    S.test = {}; S.testing = {}; S.form = null; S.msg = null;
    S.busy = false; S.loadErr = null; S.confirmDel = null; S.agentBusy = null;
    S.globalFilter = '';
    S.showTechDetails = false;
    S.ctx = null;
    sourceForm = null;
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
