/* Cursor：独立选择，串行即时保存。密钥只在显式复制时取回。 */
(function (global) {
  'use strict';
  function mount(root, ctx) {
    var h = global.Prism.h, api = ctx.api;
    var state = null, draft = null, version = 0, savedVersion = 0;
    var timer = null, polling = null, pending = null, alive = true, error = '', busy = false;
    var model = h('select', { id: 'cursor-model', 'aria-label': '模型' });
    var level = h('select', { id: 'cursor-level', 'aria-label': '推理等级' });
    var url = h('input', { id: 'cursor-url', readonly: true, 'aria-label': 'URL' });
    var note = h('div', { class: 'sub bad', style: { gridColumn: 'span 2' }, role: 'alert', hidden: true });
    var toggle = h('button', { type: 'button', class: 'sw2', role: 'switch', 'aria-label': '启用' },
      h('i', { 'aria-hidden': true }));
    function field(label, control, id) {
      return h('div', { class: 'f' }, h('label', { for: id, text: label }), control);
    }
    function button(label, run) {
      var b = ctx.ui.button(label, function () {
        if (b.disabled) return;
        b.disabled = true;
        Promise.resolve().then(run).catch(fail).finally(function () { if (alive) b.disabled = false; });
      });
      return b;
    }
    function copy(text) {
      return navigator.clipboard.writeText(text);
    }
    var fixed = h('input', { readonly: true, value: 'prism-cursor', 'aria-label': '模型名' });
    var form = h('div', { class: 'grid c2' }, [
      note,
      field('模型', model, 'cursor-model'),
      field('推理等级', level, 'cursor-level'),
      field('启用', toggle),
      field('URL', [url, button('复制 URL', function () {
        if (!state || !state.url) throw new Error('入口未就绪');
        return copy(state.url + '/v1');
      })], 'cursor-url'),
      field('凭据', [button('复制凭据', function () {
        return api.post('api/cursor/access-key', { action: 'copy' }).then(function (data) {
          return copy(data.access_key);
        });
      }), button('轮换凭据', function () {
        return ctx.dialog({ title: '轮换 Cursor 凭据', body: '旧凭据立即失效。',
          okText: '轮换', cancelText: '取消', danger: true }).then(function (yes) {
          if (yes) return api.post('api/cursor/access-key', { action: 'rotate' }).then(function () {});
        });
      })]),
      field('模型名', fixed)
    ]);
    root.appendChild(ctx.ui.cardbox(form, { pad0: true }));
    function key(item) { return JSON.stringify([item.source, item.model]); }
    function selected() {
      return state && state.candidates.find(function (item) {
        return item.source === draft.source && item.model === draft.model;
      });
    }
    function errors() { note.textContent = error || ''; note.hidden = !error; }
    function fail(e) { if (alive) { error = api.shortErr(e); errors(); } }
    function render() {
      if (!alive || !state || !draft) return;
      model.replaceChildren();
      model.appendChild(h('option', { value: '', text: '选择模型' }));
      var groups = {};
      state.candidates.forEach(function (item) {
        if (!groups[item.source]) {
          groups[item.source] = h('optgroup', { label: item.label });
          model.appendChild(groups[item.source]);
        }
        groups[item.source].appendChild(h('option', { value: key(item), text: item.model }));
      });
      model.value = selected() ? key(draft) : '';
      var item = selected();
      level.replaceChildren();
      (item ? item.levels : []).forEach(function (value) {
        level.appendChild(h('option', { value: value, text: value }));
      });
      level.disabled = !item || !item.levels.length;
      level.value = draft.level || '';
      url.value = state.url ? state.url + '/v1' : '';
      toggle.classList.toggle('on', !!(state.enabled || state.starting));
      toggle.setAttribute('aria-checked', String(!!(state.enabled || state.starting)));
      toggle.disabled = busy;
      errors();
    }
    function changed() {
      version++;
      clearTimeout(timer);
      timer = setTimeout(function () { flush().catch(fail); }, 250);
      render();
    }
    model.addEventListener('change', function () {
      if (!state || !model.value) return;
      var parts = JSON.parse(model.value);
      var item = state.candidates.find(function (m) { return m.source === parts[0] && m.model === parts[1]; });
      if (!item) return;
      draft = { source: item.source, model: item.model, level: item.default };
      changed();
    });
    level.addEventListener('change', function () {
      if (draft) { draft.level = level.value || null; changed(); }
    });
    function flush() {
      clearTimeout(timer);
      if (pending) return pending.then(flush);
      if (!alive || version === savedVersion) return Promise.resolve();
      var sent = version;
      var body = Object.assign({}, draft, { revision: state.revision });
      pending = api.put('api/cursor', body).then(function (data) {
        if (!alive) return;
        state = data;
        savedVersion = sent;
        // 已发送的选择在服务端成功；较新的用户草稿不被旧响应覆盖。
        if (version === sent) draft = { source: data.source, model: data.model, level: data.level };
        error = '';
        render();
      }).catch(function (e) {
        fail(e);
        throw e;
      }).finally(function () { pending = null; });
      return pending.then(function () { if (alive && version !== savedVersion) return flush(); });
    }
    function refresh(force) {
      var observed = version;
      if (pending || (!force && version !== savedVersion)) return Promise.resolve();
      return api.get('api/cursor').then(function (data) {
        if (!alive || pending || observed !== version) return;
        state = data;
        if (version === savedVersion) {
          draft = { source: data.source, model: data.model, level: data.level };
          error = data.error || '';
        }
        render();
      }).catch(fail);
    }
    toggle.addEventListener('click', function () {
      if (busy || !state) return;
      var on = !(state.enabled || state.starting);
      var authorization = h('input', { type: 'password', autocomplete: 'off', 'aria-label': 'ngrok 授权信息' });
      var ask = on && (!state.url || (state.error || '').indexOf('授权') >= 0) ? ctx.dialog({ title: 'ngrok 授权',
        body: field('授权信息', authorization), okText: '启用', cancelText: '取消' }) : Promise.resolve(true);
      ask.then(function (yes) {
        if (!yes || !alive) return;
        busy = true; render();
        return flush().then(function () {
          var body = { on: on };
          if (authorization.value) body.authtoken = authorization.value;
          authorization.value = '';
          return api.post('api/cursor/tunnel', body);
        }).then(function () { return refresh(); }).catch(fail).finally(function () {
          busy = false; render();
        });
      }).catch(fail);
    });
    function unmount() {
      alive = false;
      clearTimeout(timer); clearInterval(polling);
    }
    refresh();
    polling = setInterval(refresh, 2000);
    ctx.onUnmount(unmount);
    return { unmount: unmount, refresh: function () { return refresh(true); }, isDirty: function () { return version !== savedVersion; },
      flushSave: flush };
  }
  global.PrismPages = global.PrismPages || {};
  global.PrismPages.cursor = { id: 'cursor', title: 'Cursor', mount: mount };
})(window);
