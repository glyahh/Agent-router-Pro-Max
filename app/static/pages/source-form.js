/* 来源表单。首页只用「添加」，管理页只用「编辑」。两边共用这一份，不各写一套模型行。 */
(function (global) {
  'use strict';

  var LEVELS = ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'];
  var HEAD_RE = /^[a-z0-9][a-z0-9._-]{0,23}$/;
  var STYLE_ID = 'prism-source-form-style';

  var CSS = [
    '.srcform select,.srcform input:not([type="checkbox"]),.srcform textarea{font-family:inherit;',
    '  font-size:13.5px;color:var(--fg);background:var(--panel);border:1px solid var(--line2);',
    '  border-radius:var(--r-ctl);padding:8px 11px;width:100%;min-width:0;',
    '  transition:border-color .14s cubic-bezier(.16,1,.3,1),box-shadow .14s cubic-bezier(.16,1,.3,1)}',
    '.srcform input:not([type="checkbox"]):focus,.srcform select:focus,.srcform textarea:focus{outline:none;',
    '  border-color:var(--fg);box-shadow:0 0 0 1px var(--fg)}',
    '.srcform textarea{resize:vertical;line-height:1.5}',
    '.srcform .modelrow{grid-template-columns:1.1fr 1.1fr .7fr 2.3fr auto;gap:12px;align-items:start}',
    '@media (max-width:900px){.srcform .modelrow{grid-template-columns:1fr 1fr}}',
    '.srcform .lv{cursor:pointer;user-select:none}',
    '.srcform .lv.def{box-shadow:inset 0 -2px 0 var(--fg3)}',
    '.srcform .dfl{display:flex;align-items:center;gap:6px;margin-top:6px;flex-wrap:wrap}',
    '.srcform .dfl select{width:auto;flex:0 0 auto;padding:5px 22px 5px 9px;font-size:12px}',
    '.srcform .rowbtns{display:flex;gap:8px;align-items:center;margin-top:3px;flex-wrap:wrap}',
    '.srcform .note{font-size:12px;color:var(--fg3);line-height:1.6;padding:8px 2px 0}',
    '.srcform .msg{display:flex;gap:9px;align-items:center;font-size:12.5px;line-height:1.6;',
    '  border:1px solid var(--line2);border-left-width:3px;border-radius:var(--r-ctl);',
    '  padding:10px 13px;margin-bottom:14px;background:var(--panel)}',
    '.srcform .msg .txt{flex:1;min-width:0}',
    '.srcform .msg.warn{border-left-color:var(--warn);color:var(--warn);background:var(--warnbg)}',
    '.srcform .msg.bad{border-left-color:var(--bad);color:var(--bad);background:var(--badbg)}'
  ].join('\n');

  function injectStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var el = document.createElement('style');
    el.id = STYLE_ID;
    el.textContent = CSS;
    document.head.appendChild(el);
  }

  function blankModel() { return { name: '', alias: '', cw: '', levels: [], dflt: '' }; }

  function create(deps) {
    var form = null;
    var busy = false;
    var host = null;
    deps = deps || {};

    function h() { return deps.h.apply(null, arguments); }
    function api(path, opts) { return deps.api(path, opts); }
    function errText(e) { return deps.errText ? deps.errText(e) : ((e && e.message) || '失败'); }
    function groups() {
      var list = deps.groups ? deps.groups() : [];
      return Array.isArray(list) ? list : [];
    }
    function option(value, label) { return h('option', { value: value, text: label }); }
    function spin() { return h('span', { class: 'spin' }); }
    function enc(s) { return encodeURIComponent(String(s)); }

    function setBusy(v) {
      busy = !!v;
      if (typeof deps.onBusy === 'function') deps.onBusy(busy);
      render();
    }

    function render() {
      injectStyle();
      if (!host) return;
      host.textContent = '';
      if (!form) {
        if (!deps.allowCreate) return;
        if (!groups().length) return;
        host.appendChild(h('div', { class: 'addsrc' },
          h('div', { class: 'as-open' },
            h('button', { class: 'btn pri', type: 'button', onclick: openCreate }, '＋ 添加来源'),
            h('span', { class: 'as-line' }))));
        return;
      }
      host.appendChild(buildBox());
    }

    function buildBox() {
      var f = form;
      var isCreate = f.mode === 'create';
      var gs = groups();
      var box = h('div', { class: 'form srcform' });
      box.appendChild(h('div', { class: 'formh' },
        h('span', { class: 't', text: isCreate ? '+ 添加来源' : '编辑来源 · ' + f.id }),
        h('span', { class: 'hr' }),
        h('span', { class: 'tag', text: isCreate ? 'ID 自动生成' : '自定义来源' })));
      if (f.err) box.appendChild(h('div', { class: 'msg warn' }, h('span', { class: 'txt', text: f.err })));

      var groupSel = h('select', null, gs.map(function (g) { return option(g.id, g.name || g.id); }));
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
      var dirIn = h('textarea', { rows: 3, placeholder: '点「拉取」从服务商获取，或按行手填，每行一个模型 ID' });
      dirIn.value = f.dirText;
      dirIn.addEventListener('input', function () { f.dirText = dirIn.value; });

      box.appendChild(h('div', { class: 'grid' },
        h('div', { class: 'f' }, h('label', null, '分组'), groupSel),
        h('div', { class: 'f' }, h('label', null, '显示名称'), labelIn),
        h('div', { class: 'f' }, h('label', null, '渠道头'), headIn),
        h('div', { class: 'f span2' }, h('label', null, '端点'), urlIn),
        h('div', { class: 'f' }, h('label', null, 'API 密钥'), keyIn),
        h('div', { class: 'f span3' }, h('label', null, '模型目录'), dirIn,
          h('div', { class: 'rowbtns' },
            h('button', { class: 'btn sm', type: 'button', disabled: !!busy, onclick: pullModels },
              busy ? spin() : '拉取'),
            h('button', { class: 'btn sm', type: 'button', onclick: applyDirText }, '按行解析'),
            h('span', { class: 'sub', text: pullHint() })))));

      f.models.forEach(function (m, i) { box.appendChild(modelRow(m, i)); });
      box.appendChild(h('div', { class: 'formh', style: 'border-top:1px solid var(--line);border-bottom:0' },
        h('button', { class: 'btn', type: 'button', onclick: function () { f.models.push(blankModel()); render(); } }, '+ 添加模型'),
        h('span', { class: 'spacer' }),
        h('button', { class: 'btn', type: 'button', onclick: function () { form = null; render(); } }, '取消'),
        h('button', { class: 'btn pri', type: 'button', disabled: !!busy, onclick: submitForm },
          busy ? spin() : (isCreate ? '创建来源' : '保存修改'))));
      return box;
    }

    function modelRow(m, i) {
      var f = form;
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
      var dfltSel = h('select', null);
      var chipEls = {};
      function syncLevels() {
        LEVELS.forEach(function (lv) {
          var el = chipEls[lv];
          if (!el) return;
          var on = m.levels.indexOf(lv) >= 0;
          el.className = 'lv' + (on ? ' on' : '') + (m.dflt === lv ? ' def' : '');
        });
        dfltSel.textContent = '';
        dfltSel.appendChild(option('', m.levels.length ? '（不指定默认）' : '（先勾选档位）'));
        m.levels.forEach(function (lv) { dfltSel.appendChild(option(lv, lv)); });
        dfltSel.value = m.dflt || '';
        dfltSel.disabled = !m.levels.length;
      }
      LEVELS.forEach(function (lv) {
        chips.appendChild(h('button', {
          type: 'button', class: 'lv',
          onclick: function () {
            var at = m.levels.indexOf(lv);
            if (at >= 0) m.levels.splice(at, 1); else m.levels.push(lv);
            m.levels = LEVELS.filter(function (x) { return m.levels.indexOf(x) >= 0; });
            if (m.dflt && m.levels.indexOf(m.dflt) < 0) m.dflt = '';
            syncLevels();
          }
        }, lv));
        chipEls[lv] = chips.lastChild;
      });
      dfltSel.addEventListener('change', function () { m.dflt = dfltSel.value; syncLevels(); });
      syncLevels();
      return h('div', { class: 'modelrow' },
        h('div', { class: 'f' }, h('label', null, '模型 ID'), idIn),
        h('div', { class: 'f' }, h('label', null, '显示名称'), nameIn),
        h('div', { class: 'f' }, h('label', null, '上下文窗口'), cwIn),
        h('div', { class: 'f' }, h('label', null, '推理等级 · 默认等级'), chips,
          h('div', { class: 'dfl' }, h('span', { class: 'sub', text: '默认' }), dfltSel)),
        h('div', { class: 'f' }, h('button', {
          class: 'btn', type: 'button', title: '移除该行',
          onclick: function () {
            f.models.splice(i, 1);
            if (!f.models.length) f.models.push(blankModel());
            render();
          }
        }, '移除')));
    }

    function pullHint() {
      var f = form;
      if (!f || !f.pulled) return '';
      if (!f.pulled.ok) return '拉取失败：' + f.pulled.text;
      return '已拉取 ' + f.pulled.total + ' 个模型';
    }

    function pullModels() {
      var f = form;
      if (!f || busy) return;
      if (!f.base_url) { f.err = '请先填写端点地址。'; render(); return; }
      f.err = null; f.pulled = null;
      if (!f.api_key && f.mode === 'edit' && f.id) {
        useSnapshot('使用上次快照的模型列表');
        return;
      }
      setBusy(true);
      api('/api/sources/preview', { method: 'POST', timeout: 60000, body: { base_url: f.base_url, api_key: f.api_key || undefined } })
        .then(function (r) {
          busy = false;
          if (typeof deps.onBusy === 'function') deps.onBusy(false);
          var models = normalizeModels(r);
          if (r && r.ok === false && !models.length) {
            f.pulled = { ok: false, text: (r.message || '服务商没返回模型') + (r.status ? '（HTTP ' + r.status + '）' : '') };
            f.err = '拉取失败：' + f.pulled.text; render(); return;
          }
          if (!models.length) { f.pulled = { ok: false, text: (r && r.message) || '服务商返回了 0 个模型' }; render(); return; }
          fillPulled(models, (r && r.message) || '');
        })
        .catch(function (e) {
          if (e && (e.status === 404 || e.status === 405)) {
            busy = false;
            if (typeof deps.onBusy === 'function') deps.onBusy(false);
            useSnapshot('使用上次快照的模型列表');
            return;
          }
          busy = false;
          if (typeof deps.onBusy === 'function') deps.onBusy(false);
          f.pulled = { ok: false, text: errText(e) };
          f.err = '拉取失败：' + errText(e);
          render();
        });
    }

    function useSnapshot(note) {
      var f = form;
      if (!f || f.mode !== 'edit' || !f.id) {
        if (f) f.pulled = { ok: false, text: '获取模型列表失败，请手动填写模型 ID' };
        render();
        return;
      }
      api('/api/state', { timeout: 30000 }).then(function (st) {
        var ps = (st && st.providers) || [], p = null, i;
        for (i = 0; i < ps.length; i++) if (ps[i].id === f.id) p = ps[i];
        if (!p) { f.pulled = { ok: false, text: '已保存的来源里找不到 ' + f.id }; render(); return; }
        if (p.fetch_error) { f.pulled = { ok: false, text: p.fetch_error }; render(); return; }
        fillPulled((p.available || []).map(function (m) {
          return { alias: m.alias, name: m.name, context_length: m.context_length };
        }), note);
      }).catch(function (e) { f.pulled = { ok: false, text: errText(e) }; render(); });
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
      var real = form.models.filter(function (m) { return !!m.alias; });
      form.models = real.length ? real : [blankModel()];
    }

    function fillPulled(list, note) {
      var f = form;
      var usable = list.filter(function (m) {
        return deps.codexUsable ? deps.codexUsable(m.alias) : true;
      });
      var have = {};
      f.models.filter(function (m) { return m.alias; }).forEach(function (m) { have[m.alias] = 1; });
      var src = deps.byId ? deps.byId(f.id) : null;
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
      render();
    }

    function applyDirText() {
      var f = form;
      if (!f) return;
      var lines = String(f.dirText || '').split(/[\r\n]+/).map(function (s) { return s.trim(); }).filter(Boolean);
      if (!lines.length) { f.err = '模型目录为空，请粘贴模型 ID 或点「拉取」。'; render(); return; }
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
      render();
    }

    function buildSpec() {
      var f = form;
      var spec = { label: (f.label || '').trim(), group: f.group, head: (f.head || '').trim(),
        base_url: (f.base_url || '').trim(), models: [] };
      if (!spec.label) return { err: '显示名称不能为空。' };
      if (!spec.group) return { err: '先创建分组。' };
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
      var f = form;
      if (!f || busy) return;
      f.err = null;
      var built = buildSpec();
      if (built.err) { f.err = built.err; render(); return; }
      var isCreate = f.mode === 'create';
      var warn = built.warn || '';
      setBusy(true);
      var req = isCreate
        ? api('/api/sources', { method: 'POST', body: built.spec })
        : api('/api/sources/' + enc(f.id), { method: 'PUT', body: built.spec });
      req.then(function () {
        busy = false;
        if (typeof deps.onBusy === 'function') deps.onBusy(false);
        form = null;
        render();
        if (typeof deps.onSaved === 'function') return deps.onSaved(warn);
      }).catch(function (e) {
        busy = false;
        if (typeof deps.onBusy === 'function') deps.onBusy(false);
        if (form) form.err = (isCreate ? '创建失败：' : '保存失败：') + errText(e);
        if (typeof deps.onError === 'function') deps.onError((isCreate ? '创建失败：' : '保存失败：') + errText(e));
        render();
      });
    }

    function openCreate() {
      var gs = groups();
      form = { mode: 'create', id: null, label: '', group: (gs[0] && gs[0].id) || '',
        head: '', base_url: '', api_key: '', dirText: '', models: [blankModel()], pulled: null, err: null };
      render();
    }

    function openEdit(source) {
      if (!source) return;
      var ms = (source.model_settings && typeof source.model_settings === 'object') ? source.model_settings : {};
      var models = (source.models || []).filter(function (m) { return m && m.alias; }).map(function (m) {
        var s = ms[m.alias] && typeof ms[m.alias] === 'object' ? ms[m.alias] : {};
        return { name: m.name || m.alias, alias: m.alias,
          cw: (typeof m.context_length === 'number' ? String(m.context_length) : ''),
          levels: Array.isArray(s.levels) ? s.levels.slice() : [], dflt: s['default'] || '' };
      });
      form = { mode: 'edit', id: source.id, label: source.label || '', group: source.group || '',
        head: source.head || '', base_url: source.base_url || '', api_key: '', dirText: '',
        models: models.length ? models : [blankModel()], pulled: null, err: null };
      render();
    }

    return {
      renderInto: function (el) { host = el; render(); },
      openCreate: openCreate,
      openEdit: openEdit,
      close: function () { form = null; if (host) render(); },
      isOpen: function () { return !!form; },
      isDirty: function () {
        if (!form) return false;
        var f = form;
        return !!((f.base_url && f.base_url.trim()) ||
          (f.api_key && f.api_key.trim()) ||
          (f.label && f.label.trim()) ||
          (f.head && f.head.trim()) ||
          (f.models && f.models.some(function (m) { return m && m.alias && m.alias.trim(); })));
      }
    };
  }

  global.PrismSourceForm = { create: create, LEVELS: LEVELS, HEAD_RE: HEAD_RE };
})(window);
