/* Cursor：独立选择，串行即时保存。密钥只在显式复制时取回。 */
(function (global) {
  'use strict';
  function mount(root, ctx) {
    var h = global.Prism.h, api = ctx.api;
    var state = null, draft = null, version = 0, savedVersion = 0;
    var polling = null, pending = null, alive = true, error = '', busy = false, testing = false;
    var testFeedbackTimer = null;
    var model = h('select', { id: 'cursor-model', 'aria-label': '模型' });
    var level = h('select', { id: 'cursor-level', 'aria-label': '推理等级' });
    var url = h('input', { id: 'cursor-url', readonly: true, 'aria-label': 'URL' });
    // URL 和 API Key 都是 Prism 生成给你往 Cursor 里填的，只展示 + 复制，不让人在这儿改。
    // 值直接摆在框里，光放个空框谁也看不出该复制什么。
    var apiKey = h('input', { id: 'cursor-key', readonly: true, 'aria-label': 'API Key' });
    var toggle = h('button', { type: 'button', class: 'sw2', role: 'switch', 'aria-label': '启用' },
      h('i', { 'aria-hidden': true }));
    // 只有入口就绪才测试；按钮短暂显示结果，错误一次性弹窗。
    var testBtn = h('button', { type: 'button', class: 'btn sm', text: '测试' });
    function field(label, control, id, extra) {
      return h('div', { class: extra ? 'f ' + extra : 'f' },
        h('label', { for: id, text: label }), control);
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
    var exposedName = h('input', { id: 'cursor-model-name', maxlength: 128, autocomplete: 'off', 'aria-label': '模型名' });
    var saveNote = h('span', { class: 'sub', hidden: true, role: 'status', text: '待保存' });
    var form = h('div', { class: 'grid c2 cursor-form' }, [
      field('模型', model, 'cursor-model'),
      field('推理等级', level, 'cursor-level'),
      field('URL', h('div', { class: 'row' }, [url, button('复制 URL', function () {
        if (!state || !state.url) throw new Error('入口未就绪');
        return copy(state.url + '/v1');
      })]), 'cursor-url'),
      field('模型名', h('div', { class: 'row' }, [exposedName, saveNote]), 'cursor-model-name'),
      field('API Key', h('div', { class: 'row' }, [apiKey, button('复制', function () {
        return api.post('api/cursor/access-key', { action: 'copy' }).then(function (data) {
          return copy(data.access_key);
        });
      }), button('轮换', function () {
        return ctx.dialog({ title: '轮换 API Key', body: '旧 API Key 将立即失效。请同步更新 Cursor，否则无法连接。URL 不变。',
          okText: '轮换', cancelText: '取消', danger: true }).then(function (yes) {
          if (yes && alive) return api.post('api/cursor/access-key', { action: 'rotate', confirmed: true }).then(function () { return refresh(true); });
        });
      })])),
      field('启用', h('div', { class: 'row' }, [toggle, testBtn]), null, 'cursor-switch')
    ]);
    root.appendChild(ctx.ui.cardbox(form, { pad0: true }));

    // 使用教学：右下角常驻入口，展开是非模态的分步说明——不遮罩、不抢焦点，
    // 点面板外或 Esc 收起，收起后焦点回到按钮。
    var NGROK_TOKEN_URL = 'https://dashboard.ngrok.com/get-started/your-authtoken';
    // 外链要走 pywebview 的桥：WebView2 里 <a target="_blank"> 唤不起系统浏览器。
    // Python 侧只放行白名单域名，所以这里传什么都不会被无条件打开。
    function openExternal(href) {
      var api = window.pywebview && window.pywebview.api;
      if (api && typeof api.open_url === 'function') {
        try { api.open_url(href); return; } catch (e) { /* 落到下面的兜底 */ }
      }
      window.open(href, '_blank', 'noopener');   // 浏览器里调试时才走这里
    }
    var helpPanel = h('div', { class: 'help-panel', id: 'cursor-help-panel', role: 'dialog',
      'aria-label': '使用教学', hidden: true }, [
      h('p', { class: 'help-note' }, 'Cursor 的服务器在云端，够不到你电脑上的本地地址，'
        + '所以要借 ngrok 搭一条临时通道，把请求转进来。'),
      h('p', { class: 'help-note' }, 'ngrok 免费。进去后用 GitHub 或 Google 一键登录，'
        + '打开左栏的「Your Authtoken」，把那串长字符串复制下来——'
        + '回来打开「启用」，粘进弹窗即可。'),
      h('a', { class: 'btn sm', href: NGROK_TOKEN_URL, target: '_blank', rel: 'noopener',
        onclick: function (e) { e.preventDefault(); openExternal(this.href); } },
        '去 ngrok 获取授权信息')
    ]);
    var helpBtn = h('button', { type: 'button', class: 'btn sm help-btn',
      'aria-expanded': 'false', 'aria-controls': 'cursor-help-panel', text: '使用教学' });
    // 挂在 body 上而不是页面容器里：.view 会滚动，容器内的浮动元素会跟着内容走。
    // 页面卸载时随 unmount 一起摘掉，不会留在别的页面上。
    var helpRoot = h('div', { class: 'cursor-help' },
      h('div', { class: 'help-wrap' }, [helpBtn, helpPanel]));
    document.body.appendChild(helpRoot);
    function setHelp(open) {
      helpPanel.hidden = !open;
      helpBtn.setAttribute('aria-expanded', String(open));
    }
    helpBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      setHelp(helpPanel.hidden);
    });
    function onDocClick(e) {
      if (!helpPanel.hidden && !helpPanel.contains(e.target)) setHelp(false);
    }
    function onDocKey(e) {
      if (e.key === 'Escape' && !helpPanel.hidden) { setHelp(false); helpBtn.focus(); }
    }
    document.addEventListener('click', onDocClick);
    document.addEventListener('keydown', onDocKey);
    function key(item) { return JSON.stringify([item.source, item.model]); }
    function selected() {
      return state && state.candidates.find(function (item) {
        return item.source === draft.source && item.model === draft.model;
      });
    }
    // 错误只弹一次，不在页面上常驻。render() 每 2 秒跑一轮，按内容去重，
    // 否则同一个错会反复弹。
    var shownError = '';
    function errors() {
      if (error === shownError) return;
      shownError = error;
      if (error) ctx.toast(error, 'bad');
    }
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
      if (document.activeElement !== exposedName) exposedName.value = draft.model_name || '';
      saveNote.hidden = version === savedVersion;
      url.value = state.url ? state.url + '/v1' : '';
      url.placeholder = state.enabled ? '正在生成…' : '打开「启用」后生成';
      apiKey.value = state.access_key_masked || '';
      apiKey.placeholder = '点「复制」时生成';
      toggle.classList.toggle('on', !!(state.enabled || state.starting));
      toggle.setAttribute('aria-checked', String(!!(state.enabled || state.starting)));
      toggle.disabled = busy;
      testBtn.disabled = testing || busy || state.starting || !state.enabled || !state.running || !state.url;
      errors();
    }
    function changed() {
      version++;
      flush().catch(fail);
      render();
    }
    model.addEventListener('change', function () {
      if (!state || !model.value) return;
      var parts = JSON.parse(model.value);
      var item = state.candidates.find(function (m) { return m.source === parts[0] && m.model === parts[1]; });
      if (!item) return;
      draft = { source: item.source, model: item.model, level: item.default, model_name: draft.model_name };
      changed();
    });
    level.addEventListener('change', function () {
      if (draft) { draft.level = level.value || null; changed(); }
    });
    exposedName.addEventListener('change', function () {
      if (draft) { draft.model_name = exposedName.value.trim(); changed(); }
    });
    exposedName.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); exposedName.blur(); }
    });
    function flush() {
      if (pending) return pending.then(flush);
      if (!alive || version === savedVersion) return Promise.resolve();
      var sent = version;
      var body = Object.assign({}, draft, { revision: state.revision });
      pending = api.put('api/cursor', body).then(function (data) {
        if (!alive) return;
        state = data;
        savedVersion = sent;
        // 已发送的选择在服务端成功；较新的用户草稿不被旧响应覆盖。
        if (version === sent) draft = { source: data.source, model: data.model, level: data.level, model_name: data.model_name };
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
          draft = { source: data.source, model: data.model, level: data.level, model_name: data.model_name };
          error = data.error || '';
        }
        render();
      }).catch(fail);
    }
    // 点击脉冲：一瞬的扩散环。先摘 class 再强制回流，连点也能重新触发；
    // 动画结束由 animationend 收尾，不靠定时器。
    function pulse(el) {
      el.classList.remove('pulse');
      void el.offsetWidth;
      el.classList.add('pulse');
    }
    testBtn.addEventListener('click', function () {
      if (testBtn.disabled || testing) return;
      testing = true;
      testBtn.disabled = true;
      clearTimeout(testFeedbackTimer);
      testBtn.textContent = '测试中…';
      api.post('api/cursor/selftest', {}).then(function (data) {
        if (!alive) return;
        if (data.ok) {
          testBtn.textContent = '连接正常';
          testFeedbackTimer = setTimeout(function () {
            if (alive) testBtn.textContent = '测试';
          }, 3000);
        } else {
          ctx.toast(data.summary || '测试失败', 'bad');
        }
      }).catch(fail).finally(function () {
        testing = false;
        if (alive) {
          if (testBtn.textContent === '测试中…') testBtn.textContent = '测试';
          render();
        }
      });
    });
    toggle.addEventListener('animationend', function () { toggle.classList.remove('pulse'); });
    toggle.addEventListener('click', function () {
      pulse(toggle);
      if (busy || !state) return;
      var on = !(state.enabled || state.starting);
      var authorization = h('input', { type: 'password', autocomplete: 'off', 'aria-label': 'ngrok 授权信息' });
      // 这台机器的 WebView2 里 Ctrl+V 粘不进来（JS 拦截去掉后仍然如此），所以给弹窗
      // 留一条绕过浏览器剪贴板栈的入口：让 Python 直接读系统剪贴板填进来。
      var pasteBtn = h('button', {
        type: 'button', class: 'btn sm', text: '粘贴',
        onclick: function () {
          var api = window.pywebview && window.pywebview.api;
          if (!api || typeof api.read_clipboard !== 'function') return;
          Promise.resolve(api.read_clipboard()).then(function (text) {
            var value = String(text || '').trim();
            if (value) authorization.value = value;
          }).catch(function () {});
        }
      });
      // 授权信息后端一直存在 .local-secrets.json 里，存过就不该再问。只有从没存过、
      // 或这次失败正好是授权相关的（要换一个），才弹出来。
      var authError = (state.error || '').indexOf('授权') >= 0;
      var ask = on && (!state.has_authtoken || authError) ? ctx.dialog({ title: 'ngrok 授权',
        body: field('授权信息', h('div', { class: 'row' }, [authorization, pasteBtn])),
        okText: '启用', cancelText: '取消' }) : Promise.resolve(true);
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
      clearInterval(polling);
      clearTimeout(testFeedbackTimer);
      document.removeEventListener('click', onDocClick);
      document.removeEventListener('keydown', onDocKey);
      helpRoot.remove();
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
