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
  // h/clear 用壳的单一实现（app.js），ME-10：私有副本修 bug 不传播。壳为超集。
  var h = window.Prism.h, clear = window.Prism.clear;
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
    timer: null,
    fingerprint: null
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

    r.appendChild(metricGrid(S.data.gateway || {}, S.data.routing || {}, S.data.sampling || S.data.sample_state || null, S.data.sources || []));
    var nb = noticesBox(S.data.gateway || {}, S.data.routing || {});
    if (nb) r.appendChild(nb);
    r.appendChild(routingSection(S.data.routing || {}));
    r.appendChild(sourcesSection(S.data.sources || []));
  }

  function toolbar() {
    var hint = S.err ? '读取失败' : '';
    var kids = [
      h('span', { class: 'stitle', text: '网关监控' }),
      hint ? h('span', { class: 'hint', text: hint }) : null,
      h('span', { class: 'spacer' }),
      button('刷新', function () { load(true); }, '', S.busy)
    ];
    if (!S.followed) kids.push(sw2('自动刷新', S.auto, function (v) { S.auto = v; S.failStreak = 0; schedule(); }));
    return h('div', { class: 'toolbar' }, kids);
  }

  // 开关：照 app.css 的 .sw2 结构（设置页那套），不用原生复选框
  function sw2(label, on, onchange) {
    var box = h('button', {
      class: 'sw2' + (on ? ' on' : ''), type: 'button', title: label
    }, [h('i'), h('b', { text: label })]);
    box.setAttribute('role', 'switch');
    box.setAttribute('aria-checked', on ? 'true' : 'false');
    box.addEventListener('click', function () {
      on = !on;
      box.classList.toggle('on', on);
      box.setAttribute('aria-checked', on ? 'true' : 'false');
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

  /* ── Codex 极简 4 连指标网格（去除所有副文本与装饰小标签） ──── */
  function metricGrid(g, rt, sp, srcs) {
    var port = (g.port === null || g.port === undefined) ? 8317 : g.port;

    var card1 = h('div', { class: 'metric-card' }, [
      h('div', { class: 'm-head' }, [h('span', { text: '网关核心' })]),
      h('div', { class: 'm-val', text: ':' + port })
    ]);

    var spRunning = sp && sp.running === true;
    var spInterval = (sp && typeof sp.interval_sec === 'number') ? Math.round(sp.interval_sec) : 600;
    var card2 = h('div', { class: 'metric-card' }, [
      h('div', { class: 'm-head' }, [h('span', { text: '数据采样' })]),
      h('div', { class: 'm-val', text: spRunning ? spInterval + 's' : '—' })
    ]);

    var activeCount = 0;
    for (var i = 0; i < srcs.length; i++) {
      if (srcs[i] && srcs[i].enabled === true) activeCount++;
    }
    var card3 = h('div', { class: 'metric-card' }, [
      h('div', { class: 'm-head' }, [h('span', { text: '活跃来源' })]),
      h('div', { class: 'm-val', text: activeCount + ' / ' + srcs.length })
    ]);

    var models = (rt && Array.isArray(rt.exposed_models)) ? rt.exposed_models : [];
    var card4 = h('div', { class: 'metric-card' }, [
      h('div', { class: 'm-head' }, [h('span', { text: '暴露模型' })]),
      h('div', { class: 'm-val', text: String(models.length) })
    ]);

    return h('div', { class: 'metric-grid' }, [card1, card2, card3, card4]);
  }

  function noticesBox(g, rt) {
    var running = g.running === true;
    var ident = identityOf(g);
    var note = (typeof g.identity_note === 'string' && g.identity_note.trim()) ? g.identity_note.trim() : null;
    var conf = (typeof g.identity_config === 'string' && g.identity_config.trim()) ? g.identity_config.trim() : null;
    var list = [];

    if (!running) {
      list.push(notice('bad', '网关未运行', ''));
    } else if (ident === 'foreign') {
      list.push(notice('warn', '网关配置来源不一致', note || (conf || '')));
    } else if (ident === 'unknown') {
      list.push(notice('warn', '网关身份未核实', note || ''));
    }
    if (g.version_stale && g.latest) {
      list.push(notice('warn', '网关版本可更新：' + g.version + ' → ' + g.latest, ''));
    }

    if (!list.length) return null;
    var wrap = h('div', { class: 'sec' });
    for (var i = 0; i < list.length; i++) wrap.appendChild(list[i]);
    return wrap;
  }

  function routeGroups(rt) {
    var raw = rt && rt.groups;
    if (!Array.isArray(raw)) return [];
    return raw.filter(function (g) { return g && g.id; }).map(function (g) {
      return [g.id, g.name || g.id];
    });
  }

  // 采样器（用量历史的后台线程）的现状。health.monitor_state 每轮都带回 sampling，
  // 老前端没显示：采样线程不跑了，用量页只会"不再更新"，用户看不出是为什么。
  function samplingRow(sp) {
    if (!sp || typeof sp !== 'object') return null;
    var running = sp.running === true;
    var err = sp.last_error ? String(sp.last_error) : '';
    var detail = err ? ('采样失败：' + err) : (!running ? '未运行' : '');
    return h('div', { class: 'toolbar' }, [
      chip(err ? '异常' : running ? '运行中' : '停止', err ? 'bad' : running ? 'ok' : 'warn'),
      detail ? h('span', { class: 'hint', text: detail }) : null
    ]);
  }

  function routingSection(rt) {
    var sel = (rt && rt.selected && typeof rt.selected === 'object') ? rt.selected : null;
    var models = (rt && Array.isArray(rt.exposed_models)) ? rt.exposed_models : null;
    var note = (rt && typeof rt.note === 'string' && rt.note.trim()) ? rt.note.trim() : null;
    var blind = note !== null || sel === null || models === null;
    var missing = [];
    if (sel === null) missing.push('selected');
    if (models === null) missing.push('exposed_models');
    var reason = note || (blind ? '后端返回的路由数据不完整（缺 ' + missing.join('、') + '），无法判定当前路由。' : null);
    if (models === null) models = [];

    var cols = h('div', { class: 'cols' }, routeGroups(rt).map(function (g) {
      var picked = sel ? sel[g[0]] : null;
      var unknown = (picked === null || picked === undefined) && blind;
      var state = picked === 'conflict' ? 'bad' : unknown ? 'warn' : picked ? 'ok' : '';
      
      // 来源胶囊展示：切分逗号，杜绝粗暴拼串
      var colbContent;
      if (picked === 'conflict') {
        colbContent = h('span', { class: 'sub', text: '多个来源同时启用' });
      } else if (unknown) {
        colbContent = h('span', { class: 'sub', text: '无法判定' });
      } else if (!picked) {
        colbContent = h('span', { class: 'sub', text: '未选择来源' });
      } else {
        var rawParts = String(picked).split(',');
        var chips = rawParts.map(function (p) {
          var trimmed = p.trim();
          return trimmed ? h('span', { class: 'route-chip', text: trimmed }) : null;
        }).filter(Boolean);
        colbContent = h('div', { class: 'route-chips' }, chips);
      }

      var headKids = [h('span', { class: 'nm', text: g[1] })];
      if (picked === 'conflict') headKids.push(chip('冲突', 'bad'));
      else if (unknown) headKids.push(chip('未知', 'warn', reason));

      return h('div', { class: 'col' }, [
        h('div', { class: 'colh' }, headKids),
        h('div', { class: 'colb' }, [colbContent])
      ]);
    }));

    var body = h('div', null, [cols]);
    if (!models.length) {
      body.appendChild(blind
        ? statebox('bad', '无法判定已暴露模型', reason)
        : statebox('empty', '暂无已暴露模型', '请在首页保存路由。'));
    } else {
      var MAX = 80;
      var vaultHead = h('div', { class: 'vault-head' }, [
        h('span', { text: '客户端已暴露模型目录' }),
        h('span', { class: 'vault-count', text: models.length + ' MODELS' })
      ]);
      var vaultBody = h('div', { class: 'vault-body' }, models.slice(0, MAX).map(function (m) {
        return h('span', { class: 'model-chip', text: String(m), title: String(m) });
      }).concat(models.length > MAX ? [h('span', { class: 'model-chip', text: '+' + (models.length - MAX) })] : []));
      body.appendChild(h('div', { class: 'model-vault' }, [vaultHead, vaultBody]));
    }

    var sec = section('路由', null, body);
    if (blind && reason) sec.insertBefore(notice('warn', '当前路由无法判定：', reason), sec.lastChild);
    return sec;
  }

  function sourcesSection(srcs) {
    var rows = srcs.slice().sort(function (a, b) {
      var ea = a.enabled === true ? 1 : 0, eb = b.enabled === true ? 1 : 0;
      if (ea !== eb) return eb - ea;
      return (b.success || 0) - (a.success || 0);
    }).map(function (s) {
      var oauth = s.kind === 'oauth';
      var enabled = s.enabled === true ? true : (s.enabled === false ? false : null);
      var succ = (typeof s.success === 'number') ? s.success : 0;
      var failed = (typeof s.failed === 'number') ? s.failed : 0;
      var total = succ + failed;
      var cd = cooldownText(s.cooldown);

      // 微型吞吐条与成功率
      var sparkNode;
      if (total > 0) {
        var okRate = Math.round((succ / total) * 100);
        var track = h('div', { class: 'sparkbar-track' }, [
          h('div', { class: 'sparkbar-fill-ok', style: 'width:' + okRate + '%' }),
          h('div', { class: 'sparkbar-fill-bad', style: 'width:' + (100 - okRate) + '%' })
        ]);
        sparkNode = h('div', { class: 'sparkbar-box' }, [
          track,
          h('span', { class: 'mono', style: 'font-size:10px;color:var(--fg3)', text: okRate + '%' })
        ]);
      } else {
        sparkNode = h('span', { class: 'mono', text: '—' });
      }

      return [
        { text: s.label || s.id || '（未命名）', cls: 'n' },
        { text: s.id || '—', cls: 'c mono' },
        { chip: oauth ? 'OAuth' : 'API 密钥', cls: 'c' },
        enabled === null ? { text: '—', cls: 'c' } : { chip: enabled ? 'ACTIVE' : 'DISABLED', kind: enabled ? 'ok' : '', cls: 'c' },
        { node: sparkNode, cls: 'c' },
        { text: num(s.success), cls: 'c mono' },
        { text: num(s.failed), cls: 'c mono' + (failed ? ' u-bad' : '') },
        cd ? { chip: cd, kind: 'bad', cls: 'c' } : { text: '—', cls: 'c' },
        { text: s.quota_hint || '—', cls: 'c' }
      ];
    });

    var empty = statebox('empty', '暂无来源', '请在首页添加来源。');
    var sec = section('来源', null,
      h('div', null, [
        table([
          { t: '来源' },
          { t: 'ID', cls: 'c' },
          { t: '类型', cls: 'c' },
          { t: '状态', cls: 'c' },
          { t: '质量', cls: 'c' },
          { t: '成功', cls: 'c' },
          { t: '失败', cls: 'c' },
          { t: '冷却', cls: 'c' },
          { t: '配额', cls: 'c' }
        ], rows, empty)
      ]));
    return sec;
  }

  /* ── 取数 ───────────────────────────────────────────────────── */
  function apiGet(path) {
    if (S.ctx && S.ctx.api && typeof S.ctx.api.get === 'function') return S.ctx.api.get(path);
    // 兜底直连：带 15s 超时，否则"端口通但不回包"会让刷新按钮永久卡在禁用态
    var ctl = (typeof AbortController === 'function') ? new AbortController() : null;
    var timer = null;
    var init = { headers: { 'Accept': 'application/json' }, cache: 'no-store' };
    if (ctl) { init.signal = ctl.signal; timer = setTimeout(function () { ctl.abort(); }, 15000); }
    function done() { if (timer) { clearTimeout(timer); timer = null; } }
    return fetch(path, init).then(function (r) {
      return r.text().then(function (t) {
        done();
        var body = null;
        try { body = t ? JSON.parse(t) : null; } catch (e) { body = null; }
        if (body && typeof body === 'object' && 'ok' in body) {
          if (body.ok) return body.data;
          throw new Error(body.error || ('请求失败（HTTP ' + r.status + '）'));
        }
        if (!r.ok) throw new Error('请求失败：HTTP ' + r.status);
        if (body === null) throw new Error('后端返回了无法解析的内容');
        return body;
      }, function (e) { done(); throw e; });
    }, function (e) {
      done();
      if (e && e.name === 'AbortError') throw new Error('控制台 15 秒没有响应，已中断。');
      throw new Error('连不上控制台服务。确认 8318 上的 Prism 服务在运行。');
    });
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
    // 轮询内容没变就不重建：否则每 5 秒一次 render 会拉回表格滚动位置、清掉悬停与选中
    var fp;
    try { fp = JSON.stringify(m === undefined ? null : m) + '\u0000' + (S.err || ''); }
    catch (e) { fp = null; }
    if (fp !== null && fp === S.fingerprint) return;
    S.fingerprint = fp;
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

  var page = {
    id: PAGE_ID, title: '监控', mount: mount, unmount: unmount, render: mount,
    refresh: function () { return load(false); }   // 壳的 F5 走就地刷新，不整页重挂载
  };
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
