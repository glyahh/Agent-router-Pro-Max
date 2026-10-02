/* ============================================================================
 * Prism · 用量页  (static/pages/usage.js)
 *
 * 数据来源：GET /api/usage?days=N  →  {ok:true, data:{quota, counts, history}}
 * 静态稿：用户已确认的静态样板 B 下半部（用量页）。样板文件 app/design/ui-mock.html 已不在仓库里。
 *
 * 三块内容，缺任何一块都不能白屏：
 *   ① 上区配额   quota.available === false 时显示"尚无配额观测"，绝不把空当 0%
 *   ② 请求计数   10 分钟一桶的柱状图 + 来源明细表
 *   ③ 长期历史   按天/周聚合（来自 sampling.py 落的 usage-history.db）
 *
 * 契约与安全：
 *   - 不引框架，原生 JS。
 *   - 不把 source_key（形如 base_url|api_key）原样打到界面上，密钥一律掩码。
 *   - 与 app.js 的耦合降到最小：优先用 window.Prism.api.get()，没有就裸 fetch。
 *     页面靠 window.PrismPages.usage 暴露 {mount, unmount, refresh}；若宿主不认这个
 *     约定，模块自己认监听 hashchange 兜底挂载，保证单独打开也不白屏。
 *
 * 样式：全部在 static/app.css 的「用量页」小节里。不在这里注入 <style> 是照
 *   DEV-RULES A1（设计系统只有一份，页面不复制），**不是** CSP 挡的 ——
 *   server.py 的 style-src 是 'self' 'unsafe-inline'，注入 <style> 与内联样式都允许。
 *   尺寸仍一律走 CSS 变量 --w/--h/--okpct，由本文件用 CSSOM 设置。
 * ==========================================================================*/
(function () {
  'use strict';

  var PAGE_ID = 'page-usage';
  var REFRESH_MS = 30000;   // 定时刷新间隔：本机接口，30s 足够，也不至于把后端问出火
  var TICK_MS = 1000;       // 倒计时走秒

  /* ────────────────────────── 小工具 ────────────────────────── */

  function renderIconHTML(name, extraClass) {
    if (window.PrismUI && typeof window.PrismUI.iconHTML === 'function') {
      return window.PrismUI.iconHTML(name, extraClass);
    }
    var raw = '<path d="M8 1.8l6.2 11.2c.3.5-.1 1-.7 1H2.5c-.6 0-1-.5-.7-1L8 1.8z" stroke="currentColor" stroke-width="1.2" fill="none" stroke-linejoin="round"/><line x1="8" y1="6" x2="8" y2="9.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/><circle cx="8" cy="11.8" r=".8" fill="currentColor"/>';
    return '<svg class="ui-icon' + (extraClass ? ' ' + extraClass : '') + '" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">' + raw + '</svg>';
  }

  function esc(v) {
    return String(v === null || v === undefined ? '' : v)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // 只认「真的是个数」；字符串数字也收，null/undefined/''/NaN 一律 null，
  // 免得把「没读数」画成 0
  function num(v) {
    if (v === null || v === undefined || v === '' || typeof v === 'boolean') return null;
    var n = Number(v);
    return isFinite(n) ? n : null;
  }

  function pctText(v) {
    if (v === null) return null;
    var r = Math.round(v * 10) / 10;
    return (Math.abs(r - Math.round(r)) < 0.05 ? String(Math.round(r)) : r.toFixed(1));
  }

  function clampPct(v) {
    if (v === null) return 0;
    return Math.max(0, Math.min(100, v));
  }

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  function hhmm(d) { return pad2(d.getHours()) + ':' + pad2(d.getMinutes()); }
  function hhmmss(d) { return hhmm(d) + ':' + pad2(d.getSeconds()); }

  // 倒计时：样板里是 "2h10m 后重置" / "3d4h 后重置"
  function fmtAgo(sec) {
    if (sec === null) return '—';
    if (sec <= 0) return '即将重置';
    if (sec < 60) return '<1m 后重置';
    var m = Math.floor(sec / 60), h = Math.floor(m / 60), d = Math.floor(h / 24);
    if (d > 0) return d + 'd' + (h % 24) + 'h 后重置';
    if (h > 0) return h + 'h' + pad2(m % 60) + 'm 后重置';
    return m + 'm 后重置';
  }

  // 窗口长度 → "5h 窗口" / "7d 窗口" / "200m 窗口"
  function windowLabel(minutes) {
    var m = num(minutes);
    if (m === null || m <= 0) return null;
    if (m % 1440 === 0) return (m / 1440) + 'd 窗口';
    if (m % 60 === 0) return (m / 60) + 'h 窗口';
    return Math.round(m) + 'm 窗口';
  }

  function parseHHMM(s) {
    var m = /^(\d{1,2}):(\d{2})/.exec(String(s || ''));
    if (!m) return null;
    return Number(m[1]) * 60 + Number(m[2]);
  }

  // "https://api.commandcode.ai/provider/v1|user_2Zb..." → "api.commandcode.ai"
  function hostOf(sourceKey) {
    var base = String(sourceKey || '').split('|')[0];
    if (!base) return '';
    try { return new URL(base).host; } catch (e) { /* 不是合法 URL 就退回手抠 */ }
    var m = /^[a-zA-Z][\w+.-]*:\/\/([^/?#]+)/.exec(base);
    return m ? m[1] : base;
  }

  // 后端给的是脱敏后的短哈希（形如 sk-a1b2c3d4e5f6），没有 `|`，也就是没有可掩的
  // 凭据了——原样返回即可。真拿到 `base|凭据` 形状（老后端 / 裸 fetch）时照旧掩码。
  function maskSourceKey(sourceKey) {
    var parts = String(sourceKey || '').split('|');
    var cred = parts.slice(1).join('|');
    var masked = !cred ? '' : (cred.length > 14 ? cred.slice(0, 6) + '…' + cred.slice(-6) : '…');
    return parts[0] + (masked ? '  |  ' + masked : '');
  }

  // 只取尾巴。同一服务商挂两条来源时用它把行区分开。短哈希没有 `|`，取它自己的尾段
  // ——哈希是脱敏值，尾巴也带不出原文。
  function keyTail(sourceKey) {
    var s = String(sourceKey || '');
    var cred = s.split('|').slice(1).join('|');
    if (!cred) return s ? '…' + s.slice(-6) : '';
    return cred.length > 6 ? '…' + cred.slice(-6) : '…';
  }

  // 样板把长邮箱截成 "user…@163.com"
  function shortAccount(a) {
    var s = String(a || '');
    var at = s.indexOf('@');
    if (at > 8) return s.slice(0, 8) + '…' + s.slice(at);
    return s;
  }

  /* ────────────────────────── 数据归一 ────────────────────────── */
  /* 后端（core/health.py + server.py）的输出以 INTERFACES.md 为准，
     但它是并行开发的，字段可能先到齐、后到齐或换个名字。
     这里做一遍归一：认得几种常见形状，认不出就返回空数组走空态，
     绝不因为一个没见过的字段把整页炸掉。 */

  // 一条配额信号 → {label, used_percent, window_minutes, reset_at}
  function normalizeSignal(label, sig) {
    if (sig === null || typeof sig !== 'object') return null;
    var used = num(sig.used_percent != null ? sig.used_percent : sig.usedPercent);
    var win = num(sig.window_minutes != null ? sig.window_minutes : sig.windowMinutes);
    var resetAt = num(sig.reset_at != null ? sig.reset_at : sig.resetAt);
    if (resetAt === null) {
      // 网关原始字段是 reset_after_seconds；后端应转成 reset_at，没转就在这儿补
      var after = num(sig.reset_after_seconds != null ? sig.reset_after_seconds : sig.resetAfterSeconds);
      if (after !== null) resetAt = Math.floor(Date.now() / 1000) + after;
    }
    if (used === null && win === null && resetAt === null) return null;
    return {
      label: String(label == null || label === '' ? '窗口' : label),
      used_percent: used,
      window_minutes: win,
      reset_at: resetAt,
      limit_reached: sig.limit_reached === true || sig.limitReached === true
    };
  }

  // quota.windows / quota.model_quotas[x] 的多种形状 → 统一的窗口数组
  function normalizeWindows(raw) {
    var out = [];
    if (!raw) return out;

    if (Array.isArray(raw)) {
      for (var i = 0; i < raw.length; i++) {
        var it = raw[i];
        if (!it || typeof it !== 'object') continue;
        var one = normalizeSignal(it.label || it.name || it.window || it.key || ('窗口 ' + (i + 1)), it);
        if (one) out.push(one);
      }
      return out;
    }

    if (typeof raw === 'object') {
      // {signals:{'5h':{...}}} 或 {windows:{...}} 再套一层
      var inner = raw.signals || raw.windows || raw.window;
      if (inner && typeof inner === 'object' && !('used_percent' in raw) && !('usedPercent' in raw)) {
        return normalizeWindows(inner);
      }
      // {used_percent:..} 直接就是一条读数
      if ('used_percent' in raw || 'usedPercent' in raw || 'reset_at' in raw || 'reset_after_seconds' in raw) {
        var solo = normalizeSignal(raw.label || raw.name || '窗口', raw);
        return solo ? [solo] : [];
      }
      // {'5h':{...}, '7d':{...}} 字典
      var keys = Object.keys(raw);
      for (var j = 0; j < keys.length; j++) {
        var s2 = normalizeSignal(keys[j], raw[keys[j]]);
        if (s2) out.push(s2);
      }
    }
    return out;
  }

  // quota.model_quotas → [{model, windows}]；支持 dict 与 list 两种形状
  function normalizeModelQuotas(raw) {
    var out = [];
    if (!raw) return out;
    var push = function (name, val) {
      var wins = normalizeWindows(val);
      if (wins.length) out.push({ model: String(name), windows: wins });
    };
    if (Array.isArray(raw)) {
      for (var i = 0; i < raw.length; i++) {
        var it = raw[i];
        if (!it || typeof it !== 'object') continue;
        var nm = it.model || it.name || it.label || it.id;
        if (!nm) continue;
        push(nm, it.windows || it.signals || it.quota || it);
      }
      return out;
    }
    if (typeof raw === 'object') {
      var keys = Object.keys(raw).sort();
      for (var j = 0; j < keys.length; j++) {
        var v = raw[keys[j]];
        if (v && typeof v === 'object' && v.available === false) continue; // 该模型没有反馈
        push(keys[j], v);
      }
    }
    return out;
  }

  function normalizeCounts(raw) {
    var out = [];
    if (!Array.isArray(raw)) return out;
    for (var i = 0; i < raw.length; i++) {
      var c = raw[i];
      if (!c || typeof c !== 'object') continue;
      var buckets = [];
      if (Array.isArray(c.buckets)) {
        for (var j = 0; j < c.buckets.length; j++) {
          var b = c.buckets[j];
          if (!b || typeof b !== 'object') continue;
          buckets.push({
            time: String(b.time || b.label || b.bucket || ''),
            success: num(b.success) || 0,
            failed: num(b.failed != null ? b.failed : b.fail) || 0
          });
        }
      }
      var key = String(c.source_key || c.sourceKey || '');
      out.push({
        source_key: key,
        label: String(c.label || c.name || hostOf(key) || key || '未知来源'),
        vendor: String(c.vendor || hostOf(key) || ''),
        status: c.status || c.warning || null,
        blocked: c.blocked || null,
        success: num(c.success) || 0,
        failed: num(c.failed != null ? c.failed : c.fail) || 0,
        buckets: buckets
      });
    }
    return out;
  }

  function normalizeHistory(raw) {
    var out = [];
    if (!Array.isArray(raw)) return out;
    for (var i = 0; i < raw.length; i++) {
      var r = raw[i];
      if (!r || typeof r !== 'object') continue;
      var date = String(r.date || r.day || '');
      if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) continue;
      out.push({
        date: date,
        label: String(r.label || r.source || hostOf(r.source_key) || '未知来源'),
        success: num(r.success) || 0,
        failed: num(r.failed != null ? r.failed : r.fail) || 0
      });
    }
    return out;
  }

  /* ────────────────────────── 取数 ────────────────────────── */

  // 取数。优先走宿主的 Prism.api（它已经处理了 {ok,data}/超时/非 JSON/Host 校验提示），
  // 没有宿主时裸 fetch 兜底，形状保持一致：成功给 data，失败抛带中文 message 的 Error。
  function apiGet(path, params) {
    var P = window.Prism;
    if (P && P.api && typeof P.api.get === 'function') return P.api.get(path, params);
    if (P && typeof P.get === 'function') return P.get(path);

    var qs = [];
    if (params) {
      for (var k in params) {
        if (Object.prototype.hasOwnProperty.call(params, k) && params[k] !== null && params[k] !== undefined) {
          qs.push(encodeURIComponent(k) + '=' + encodeURIComponent(params[k]));
        }
      }
    }
    var url = qs.length ? path + (path.indexOf('?') >= 0 ? '&' : '?') + qs.join('&') : path;

    return fetch(url, { headers: { 'Accept': 'application/json' }, cache: 'no-store' }).then(function (res) {
      return res.text().then(function (txt) {
        var json = null;
        try { json = JSON.parse(txt); } catch (e) { /* 下面统一报错 */ }
        if (!json || typeof json !== 'object') {
          throw new Error('后端返回了非 JSON 内容（HTTP ' + res.status + '）。'
            );
        }
        if (json.ok === false) throw new Error(json.error || ('请求失败 HTTP ' + res.status));
        if (!res.ok) throw new Error(json.error || ('请求失败 HTTP ' + res.status));
        return Object.prototype.hasOwnProperty.call(json, 'data') ? json.data : json;
      });
    });
  }

  /* ────────────────────────── 渲染：配额 ────────────────────────── */

  function quotaRowHTML(win) {
    var p = pctText(win.used_percent);
    var hot = win.used_percent !== null && win.used_percent >= 75;
    // used_percent 缺失 ≠ 0%：写 "—" 并把条留空
    var valHTML = p === null
      ? '<span class="v na">—</span>'
      : '<span class="v">' + p + '<small>%</small></span>';
    var wPct = (p === null ? 0 : clampPct(win.used_percent));
    return '<div class="qrow">'
      + '<span class="k" title="' + esc(win.label) + '">' + esc(win.label) + '</span>'
      + '<div class="bar' + (hot || win.limit_reached ? ' hot' : '') + '">'
      + '<i data-w="' + wPct + '" style="--w:' + wPct + '%"></i></div>'
      + valHTML
      + '</div>';
  }

  function quotaFootHTML(windows) {
    var parts = [];
    var hottest = null;
    for (var i = 0; i < windows.length; i++) {
      var w = windows[i];
      var tail = '';
      var cls = 'rt';
      if (w.reset_at !== null) {
        var left = w.reset_at - Math.floor(Date.now() / 1000);
        if (left <= 3600) cls = 'rt near';
        tail = '<span class="' + cls + '" data-reset-at="' + w.reset_at + '">' + fmtAgo(left) + '</span>';
      } else {
        tail = '<span class="rt">重置时间未知</span>';
      }
      var wl = windowLabel(w.window_minutes);
      parts.push('<span>' + esc(wl || w.label) + ' · ' + tail + '</span>');
      if (hottest === null || (w.used_percent || 0) > (hottest.used_percent || 0)) hottest = w;
    }
    if (hottest && hottest.used_percent !== null && hottest.used_percent >= 75) {
      parts.push('<span class="alarm">' + renderIconHTML('warn') + ' ' + esc(hottest.label) + '已用 ' + pctText(hottest.used_percent) + '%</span>');
    }
    return '<div class="qfoot">' + parts.join('') + '</div>';
  }

  function quotaEmptyHTML() {
    return emptySlotHTML('尚无配额观测', '');
  }

  function renderQuota(data) {
    var quota = data && data.quota && typeof data.quota === 'object' ? data.quota : null;
    var windows = quota ? normalizeWindows(quota.windows || quota.signals) : [];
    var models = quota ? normalizeModelQuotas(quota.model_quotas || quota.modelQuotas) : [];

    var head = '<div class="sechead"><span class="cmt">//</span>'
      + '<span class="stitle">配额</span><span class="hr"></span></div>';

    if (windows.length === 0 && models.length === 0) {
      return '<div class="sec">' + head
        + quotaEmptyHTML() + '</div>';
    }

    var html = '<div class="sec">' + head;
    if (windows.length) {
      var rows = '';
      for (var i = 0; i < windows.length; i++) rows += quotaRowHTML(windows[i]);
      html += '<div class="qbox">' + rows + quotaFootHTML(windows) + '</div>';
    }
    if (models.length) {
      // 同一账号不同模型的读数能差很多（实测并存过 3% 与 75%），必须分组看
      html += '<div class="u-mgrid">';
      for (var m = 0; m < models.length; m++) {
        var g = models[m];
        var mrows = '';
        for (var n = 0; n < g.windows.length; n++) mrows += quotaRowHTML(g.windows[n]);
        html += '<div class="qbox"><div class="u-mhead">'
          + '<span class="nm mono" title="' + esc(g.model) + '">' + esc(g.model) + '</span>'
          + '<span class="tag">MODEL</span></div>'
          + mrows + quotaFootHTML(g.windows) + '</div>';
      }
      html += '</div>';
    }
    return html + '</div>';
  }

  /* ────────────────────────── 渲染：请求计数 ────────────────────────── */

  // 各来源的桶合并成一条时间轴。桶标签 "14:10-14:20" 不含日期，跨零点时要靠
  // 「最后一个桶=现在」这个事实把顺序摆正：算出每个标签相对最新标签的分钟差，
  // 取模 1440 后升序排，差值 0 的（最新桶）排最后。
  // 某个 HHMM 桶"距今多少分钟"（按 1440 取模）。用来挑真正的最新桶 ——
  // 不能取数值最大的 HHMM：跨零点时 23:5x 比 00:0x 大，而最新的是 00:0x，
  // 于是整个柱状图的时间轴会在零点后颠倒（复查轮 7 的 #1）。
  function ageFromNow(hm) {
    var now = new Date();
    return (now.getHours() * 60 + now.getMinutes() - hm + 1440) % 1440;
  }

  function mergeBuckets(counts) {
    var newest = null, labels = {}, i, j, b;
    for (i = 0; i < counts.length; i++) {
      var bs = counts[i].buckets;
      for (j = 0; j < bs.length; j++) {
        if (bs[j].time) labels[bs[j].time] = true;
      }
      // 每个来源的数组按时间升序，末位就是它的最新桶；取所有来源里最靠后的那个
      if (bs.length && bs[bs.length - 1].time) {
        var mins = parseHHMM(bs[bs.length - 1].time);
        if (mins !== null && (newest === null || ageFromNow(mins) < ageFromNow(newest))) newest = mins;
      }
    }
    var list = Object.keys(labels);
    if (newest === null) return [];

    var decorated = [];
    for (i = 0; i < list.length; i++) {
      var hm = parseHHMM(list[i]);
      if (hm === null) continue;
      // age = 这个桶比最新桶早了多少分钟（跨零点按 1440 取模回绕）。
      // 最新桶 age=0，越老越大；按 age 降序排，老的在左、新的在右。
      decorated.push({ time: list[i], age: (newest - hm + 1440) % 1440 });
    }
    decorated.sort(function (a, b2) { return b2.age - a.age; });

    for (i = 0; i < decorated.length; i++) { decorated[i].success = 0; decorated[i].failed = 0; }
    var index = {};
    for (i = 0; i < decorated.length; i++) index[decorated[i].time] = decorated[i];

    for (i = 0; i < counts.length; i++) {
      var bs2 = counts[i].buckets;
      for (j = 0; j < bs2.length; j++) {
        b = index[bs2[j].time];
        if (!b) continue;
        b.success += bs2[j].success;
        b.failed += bs2[j].failed;
      }
    }
    return decorated;
  }

  function axisTicks(merged) {
    if (!merged.length) return [];
    var n = merged.length;
    var idx = [0, Math.round((n - 1) * 0.25), Math.round((n - 1) * 0.5), Math.round((n - 1) * 0.75), n - 1];
    var out = [], seen = {};
    for (var i = 0; i < idx.length; i++) {
      var k = idx[i];
      if (seen[k]) continue;
      seen[k] = true;
      var t = merged[k].time;
      out.push(String(t).split('-')[0] || t);
    }
    return out;
  }

  function lastActiveBucket(b) {
    for (var i = b.length - 1; i >= 0; i--) {
      if (b[i].success || b[i].failed) return b[i];
    }
    return null;
  }

  function statusChip(count) {
    // 后端若给了 status / warning / blocked，直接用（能表达 HTTP 403 这类上游错误）
    var raw = count.blocked ? (count.status || 'BLOCKED') : count.status;
    if (raw) {
      var s = String(raw);
      var low = s.toLowerCase();
      var kind = (low.indexOf('403') >= 0 || low.indexOf('410') >= 0 || low.indexOf('401') >= 0
        || low.indexOf('error') >= 0 || low.indexOf('block') >= 0 || low.indexOf('fail') >= 0)
        ? 'bad' : (low.indexOf('idle') >= 0 || low.indexOf('off') >= 0 ? '' : 'ok');
      return '<span class="tag ' + kind + '">' + esc(s) + '</span>';
    }
    if (count.failed > 0 && count.success === 0) return '<span class="tag bad">失败 ' + count.failed + '</span>';
    if (count.failed > 0) return '<span class="tag warn">部分失败</span>';
    if (count.success > 0) return '<span class="tag ok">ACTIVE</span>';
    return '<span class="tag">IDLE</span>';
  }

  function renderCounts(data) {
    var counts = normalizeCounts(data && data.counts);
    var since = (data && (data.counters_since || data.counts_since)) || '网关启动以来';
    var merged = mergeBuckets(counts);
    var totalOK = 0, totalBad = 0, idle = 0, i;

    for (i = 0; i < merged.length; i++) {
      totalOK += merged[i].success;
      totalBad += merged[i].failed;
      if (!merged[i].success && !merged[i].failed) idle++;
    }

    var hours = merged.length ? (merged.length * 10 / 60) : 0;
    var days = 0, seen = {};
    var hist = normalizeHistory(data && data.history);
    for (i = 0; i < hist.length; i++) { if (!seen[hist[i].date]) { seen[hist[i].date] = 1; days++; } }

    var head = '<div class="sechead"><span class="cmt">//</span>'
      + '<span class="stitle">请求计数 · 按来源</span><span class="hr"></span></div>';

    var html = '<div class="sec">' + head;

    if (!merged.length) {
      html += emptySlotHTML('暂无请求计数',
        '暂无经过网关的请求记录。') + '</div>';
      return html;
    }

    var max = 0;
    for (i = 0; i < merged.length; i++) {
      var t = merged[i].success + merged[i].failed;
      if (t > max) max = t;
    }
    if (max <= 0) max = 1;

    var bars = '';
    for (i = 0; i < merged.length; i++) {
      var b = merged[i], tot = b.success + b.failed;
      var hgt = tot ? Math.max(2, Math.round(tot / max * 100)) : 0;
      // 一根柱子一个桶。尺寸走 CSS 变量，数据属性供 Tooltip 读取
      var okPct = tot ? Math.round(b.success / tot * 100) : 100;
      bars += '<i class="' + (tot ? '' : 's0') + '" data-h="' + hgt + '"'
        + ' style="--h:' + hgt + '%;' + (tot && b.failed ? '--okpct:' + okPct + '%;' : '') + '"'
        + ' data-time="' + esc(b.time) + '"'
        + ' data-tot="' + tot + '"'
        + ' data-okcnt="' + b.success + '"'
        + ' data-badcnt="' + b.failed + '"'
        + ' data-rate="' + okPct + '"'
        + (tot && b.failed ? ' data-split="1" data-ok="' + okPct + '"' : '')
        + '></i>';
    }

    var ticks = axisTicks(merged);
    var axis = '';
    for (i = 0; i < ticks.length; i++) axis += '<span>' + esc(ticks[i]) + '</span>';

    // 窗口里有桶、但桶里一个请求都没有。这时按 96px 画一块空图就是一片黑，
    // 光留一条时间轴，看着像加载失败。收成一条基线（.u-flat）并说清为什么空。
    var flat = (totalOK === 0 && totalBad === 0);

    var yAxisHtml = '<div class="chart-y-axis">'
      + '<span>' + max + '</span>'
      + '<span>' + (max > 1 ? Math.round(max / 2) : '') + '</span>'
      + '<span>0</span>'
      + '</div>';
    var gridHtml = '<div class="chart-grid">'
      + '<div class="chart-grid-line"></div>'
      + '<div class="chart-grid-line"></div>'
      + '<div class="chart-grid-line"></div>'
      + '</div>';

    html += '<div class="chart' + (flat ? ' u-flat' : '') + '">'
      + '<div class="legend">'
      + '<span><i class="sw ok"></i>成功 <b>' + totalOK + '</b></span>'
      + '<span><i class="sw bad"></i>失败 <b>' + totalBad + '</b></span>'
      + '<span><i class="sw idle"></i>空闲桶 <b>' + idle + '</b></span>'
      + '</div>'
      + (flat ? '<div class="u-note">当前时间窗口内暂无请求数据。</div>' : '')
      + '<div class="chart-wrap">'
      + yAxisHtml
      + gridHtml
      + '<div class="bars with-axis" data-role="usage-bars">' + bars + '</div>'
      + '<div class="axis with-axis">' + axis + '</div>'
      + '</div>'
      + '</div></div>';
    return html;
  }

  function renderSourceTable(data) {
    var counts = normalizeCounts(data && data.counts);

    var head = '<div class="sechead"><span class="cmt">//</span>'
      + '<span class="stitle">来源明细</span><span class="hr"></span></div>';

    var html = '<div class="sec">' + head;
    if (!counts.length) {
      html += emptySlotHTML('暂无来源数据',
        '暂无各来源调用统计。') + '</div>';
      return html;
    }

    // 全零的来源排到后面，有流量的按成功数降序——一屏先看到真正在干活的
    var rows = counts.slice().sort(function (a, b) {
      var ta = a.success + a.failed, tb = b.success + b.failed;
      if (!ta && tb) return 1;
      if (ta && !tb) return -1;
      return tb - ta;
    });

    // 后端给的 label 可能撞车（同一服务商挂两条来源，或后端还没做映射）。
    // 撞了的行补一段掩码凭据尾巴，保证每行能对上号。
    var labelCount = {}, i2;
    for (i2 = 0; i2 < rows.length; i2++) labelCount[rows[i2].label] = (labelCount[rows[i2].label] || 0) + 1;

    var body = '';
    for (var i = 0; i < rows.length; i++) {
      var c = rows[i];
      var recent = lastActiveBucket(c.buckets);
      var recentText = recent ? (recent.success + recent.failed) : 0;
      var shown = c.label + (labelCount[c.label] > 1 ? ' · ' + keyTail(c.source_key) : '');
      body += '<tr>'
        + '<td class="n" title="' + esc(maskSourceKey(c.source_key)) + '">' + esc(shown) + '</td>'
        + '<td title="' + esc(maskSourceKey(c.source_key)) + '">' + esc(c.vendor || '—') + '</td>'
        + '<td>' + statusChip(c) + '</td>'
        + '<td class="r n">' + c.success + '</td>'
        + '<td class="r' + (c.failed ? ' u-bad' : '') + '">' + c.failed + '</td>'
        + '<td class="r"' + (recent ? ' title="' + esc(recent.time) + '"' : '') + '>'
        + recentText + '</td>'
        + '</tr>';
    }

    html += '<div class="chart tight"><table class="t" data-role="srctable">'
      + '<tr><th>来源</th><th>服务商</th><th>状态</th><th class="r">成功</th><th class="r">失败</th>'
      + '<th class="r">最近 10 分钟</th></tr>'
      + body + '</table></div></div>';
    return html;
  }

  /* ────────────────────────── 渲染：长期历史 ────────────────────────── */

  function mondayOf(dateStr) {
    var d = new Date(dateStr + 'T00:00:00');
    if (isNaN(d.getTime())) return dateStr;
    var dow = (d.getDay() + 6) % 7;          // 周一 = 0
    d.setDate(d.getDate() - dow);
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
  }

  function renderHistory(data, opts) {
    var rows = normalizeHistory(data && data.history);
    var byWeek = opts && opts.historyMode === 'week';
    var days = (opts && opts.days) || 7;
    var seg = '<span class="u-seg" data-role="histmode">'
      + '<span data-v="day" class="' + (byWeek ? '' : 'on') + '">按天</span>'
      + '<span data-v="week" class="' + (byWeek ? 'on' : '') + '">按周</span></span>';
    var rangeSeg = '<span class="u-seg" data-role="days">';
    var ranges = [7, 30, 90];
    for (var i = 0; i < ranges.length; i++) {
      rangeSeg += '<span data-v="' + ranges[i] + '" class="' + (ranges[i] === days ? 'on' : '') + '">'
        + ranges[i] + 'd</span>';
    }
    rangeSeg += '</span>';

    var head = '<div class="sechead"><span class="cmt">//</span>'
      + '<span class="stitle">长期历史</span><span class="hr"></span>'
      + seg + rangeSeg
      + '</div>';

    var html = '<div class="sec">' + head;
    if (!rows.length) {
      html += emptySlotHTML('暂无历史记录',
        '尚未采集到历史采样数据。') + '</div>';
      return html;
    }

    // 按周期归组
    var groups = {}, order = [];
    for (var r = 0; r < rows.length; r++) {
      var row = rows[r];
      var period = byWeek ? mondayOf(row.date) : row.date;
      var g = groups[period];
      if (!g) { g = groups[period] = { period: period, items: {}, order: [], ok: 0, bad: 0, max: 0 }; order.push(period); }
      var it = g.items[row.label];
      if (!it) { it = g.items[row.label] = { label: row.label, success: 0, failed: 0 }; g.order.push(row.label); }
      it.success += row.success;
      it.failed += row.failed;
      g.ok += row.success;
      g.bad += row.failed;
    }
    order.sort().reverse();  // 最近的在最上面

    var peak = 0;
    for (var q = 0; q < order.length; q++) { if (groups[order[q]].ok > peak) peak = groups[order[q]].ok; }
    if (peak <= 0) peak = 1;

    var body = '';
    for (var k = 0; k < order.length; k++) {
      var g2 = groups[order[k]];
      var names = g2.order.slice().sort();
      for (var n = 0; n < names.length; n++) {
        var it2 = g2.items[names[n]];
        var tot = it2.success + it2.failed;
        var failRate = tot ? Math.round(it2.failed / tot * 1000) / 10 : 0;
        var cell = byWeek
          ? (n === 0 ? g2.period + ' 起一周' : '')
          : (n === 0 ? g2.period : '');
        body += '<tr>'
          + '<td class="n">' + esc(cell) + '</td>'
          + '<td>' + esc(it2.label) + '</td>'
          + '<td class="r n">' + it2.success + '</td>'
          + '<td class="r' + (it2.failed ? ' u-bad' : '') + '">' + it2.failed + '</td>'
          + '<td class="r">' + (tot ? failRate + '%' : '—') + '</td>'
          + '<td><span class="u-mini' + (failRate >= 10 ? ' hot' : '') + '"><i data-w="'
          + clampPct(Math.round(it2.success / peak * 100)) + '" style="--w:' + clampPct(Math.round(it2.success / peak * 100)) + '%"></i></span></td>'
          + '</tr>';
      }
      if (names.length > 1) {
        body += '<tr class="u-sum"><td>' + esc(byWeek ? g2.period + ' 起一周' : g2.period) + '</td>'
          + '<td>合计 ' + names.length + ' 来源</td>'
          + '<td class="r">' + g2.ok + '</td>'
          + '<td class="r' + (g2.bad ? ' u-bad' : '') + '">' + g2.bad + '</td>'
          + '<td class="r">—</td><td></td></tr>';
      }
    }

    html += '<div class="chart tight"><div class="u-scroll"><table class="t" data-role="histtable">'
      + '<tr><th>' + (byWeek ? '周（周一起）' : '日期') + '</th><th>来源</th>'
      + '<th class="r">成功</th><th class="r">失败</th><th class="r">失败率</th><th>相对量</th></tr>'
      + body + '</table></div></div></div>';
    return html;
  }

  /* ────────────────────────── 页面装配 ────────────────────────── */

  var state = {
    root: null,
    ctx: null,
    bodyEl: null,
    stampEl: null,
    data: null,
    stampText: '',
    pendingError: null,
    expiredSeen: false,
    days: 7,
    historyMode: 'day',
    timer: null,
    ticker: null,
    unmounted: true,
    refreshing: false
  };

  // 外壳的内部结构。拆成两层是因为宿主可能直接把外壳本身当容器传进来，
  // 那种情况下只能填内容，不能再套一层 .usage-page。
  function shellInnerHTML() {
    return '<div class="u-toolbar">'
      + '<span class="pagetag">用量</span>'
      + '<span class="u-spacer"></span>'
      + '<span class="u-updated" data-role="stamp">尚未取数</span>'
      + '<span class="btn" data-role="refresh">刷新</span>'
      + '</div>'
      + '<div class="u-body" data-role="body"></div>'
      + '<div class="u-tooltip" data-role="chart-tooltip"></div>';
  }

  function shellHTML() {
    return '<div class="usage-page" id="' + PAGE_ID + '">' + shellInnerHTML() + '</div>';
  }

  function loadingHTML() {
    return '<div class="sec"><div class="u-empty"><div class="t">正在加载用量数据…</div></div></div>';
  }

  function errorHTML(msg) {
    return '<div class="sec"><div class="u-error">'
      + '<div class="t">用量数据读取失败</div>'
      + '<div class="d">' + esc(msg) + '</div>'
      + '<span class="btn pri" data-role="retry">重试</span>'
      + '</div></div>';
  }

  function bannerHTML(msg) {
    // 有旧数据时不清屏，但必须把「这次没读到」说出来，否则用户会把旧数字当成实时值
    return '<div class="sec u-lead"><div class="u-error slim">'
      + '<div class="t">刷新失败，显示缓存数据</div>'
      + '<div class="d">' + esc(msg) + '</div>'
      + '<span class="btn" data-role="retry">重试</span>'
      + '</div></div>';
  }

  var renderRafId = null;
  function scheduleRender() {
    if (state.unmounted) return;
    if (renderRafId) return;
    renderRafId = requestAnimationFrame(function () {
      renderRafId = null;
      render();
    });
  }

  function render() {
    if (!state.bodyEl) return;
    var stamp = state.stampEl;
    if (!state.data) {
      state.bodyEl.innerHTML = state.pendingError ? errorHTML(state.pendingError) : loadingHTML();
      if (stamp) stamp.textContent = state.pendingError ? '取数失败' : '正在取数…';
      wire();
      return;
    }
    var d = state.data;
    state.bodyEl.innerHTML = (state.pendingError ? bannerHTML(state.pendingError) : '')
      + renderQuota(d)
      + renderCounts(d)
      + renderSourceTable(d)
      + renderHistory(d, state);
    if (stamp) {
      stamp.textContent = state.pendingError
        ? (state.stampText || '') + ' · 刷新失败'
        : (state.stampText || '');
    }
    wire();
  }

  /* 空态用占位符，等 innerHTML 落地后再换成真的组件：
     宿主给了 ctx.ui.empty 就用它的（五页空态长得一样），没有就用自己的 .u-empty。
     字符串渲染器没法直接插 DOM 节点，所以走这一步后处理。 */
  function emptySlotHTML(title, detail) {
    return '<div data-role="emptyslot" data-title="' + esc(title) + '" data-detail="' + esc(detail) + '"></div>';
  }

  function fillEmptySlots() {
    if (!state.bodyEl) return;
    var slots = state.bodyEl.querySelectorAll('[data-role="emptyslot"]');
    for (var i = 0; i < slots.length; i++) {
      var s = slots[i];
      if (s.getAttribute('data-filled') === '1') continue;   // wire() 每秒都跑，别重复填
      s.setAttribute('data-filled', '1');
      var title = s.getAttribute('data-title') || '';
      var detail = s.getAttribute('data-detail') || '';
      var cui = state.ctx && state.ctx.ui;
      if (cui && typeof cui.empty === 'function') {
        try {
          var box = cui.empty(title, detail);
          // 整页级的 statebox 上下各留 34px。这三处（QUOTA / 请求计数 / 按来源）
          // 是区块内的空态，撑那么高就是一片空白——加个 u-slim 收窄，见 app.css。
          if (box && box.classList) box.classList.add('u-slim');
          s.appendChild(box); continue;
        } catch (e) { /* 退回自带样式 */ }
      }
      s.innerHTML = '<div class="u-empty"><div class="t">' + esc(title) + '</div>'
        + '<div class="d">' + esc(detail) + '</div></div>';
    }
  }

  /* 尺寸用 CSSOM 设进自定义属性。这里**不**是因为 CSP 拦内联 style ——
     server.py 的 style-src 带 'unsafe-inline'，内联与注入都是允许的（复查轮 6/7 的注释
     与事实不符，已改正）；用 CSSOM 只是为了少拼字符串、少一次 HTML 解析。 */
  function applySizes() {
    if (!state.bodyEl) return;
    var i, els = state.bodyEl.querySelectorAll('[data-w]:not([style*="--w"])');
    for (i = 0; i < els.length; i++) els[i].style.setProperty('--w', els[i].getAttribute('data-w') + '%');
    els = state.bodyEl.querySelectorAll('[data-h]:not([style*="--h"])');
    for (i = 0; i < els.length; i++) {
      els[i].style.setProperty('--h', els[i].getAttribute('data-h') + '%');
      var ok = els[i].getAttribute('data-ok');
      if (ok !== null) els[i].style.setProperty('--okpct', ok + '%');
    }
  }

  // 渲染后跑一次：填空态、把尺寸写进 CSS 变量、把倒计时文本刷一遍
  function wire() {
    fillEmptySlots();
    applySizes();
    tickCountdowns();
  }

  // 走秒只改文本节点，不重排整页
  function tickCountdowns() {
    if (!state.bodyEl) return;
    var nodes = state.bodyEl.querySelectorAll('[data-reset-at]');
    var now = Math.floor(Date.now() / 1000);
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var at = Number(el.getAttribute('data-reset-at'));
      if (!isFinite(at)) continue;
      var left = at - now;
      el.textContent = fmtAgo(left);
      if (left <= 3600) el.className = 'rt near';
      if (left <= 0) state.expiredSeen = true;
    }
  }

  function tick() {
    if (state.unmounted || !state.bodyEl) return;
    // 宿主换页时若只是把容器换掉、忘了调 unmount，这里自己收手，别让定时器空转
    if (document.documentElement.contains && !document.documentElement.contains(state.bodyEl)) {
      UsagePage.unmount();
      return;
    }
    tickCountdowns();
    // 倒计时归零说明窗口翻篇了，主动补一次数据（一次归零只补一次，避免死循环）
    if (state.expiredSeen && !state.refreshing) {
      state.expiredSeen = false;
      refresh();
    }
  }

  function refresh() {
    if (state.unmounted || state.refreshing) return Promise.resolve();
    state.refreshing = true;
    var btn = state.root && state.root.querySelector('[data-role="refresh"]');
    if (btn) { btn.textContent = '读取中…'; btn.setAttribute('disabled', '1'); }
    return apiGet('api/usage', { days: state.days })
      .then(function (data) {
        state.data = data || {};
        state.pendingError = null;
        state.stampText = '实测 ' + hhmmss(new Date());
      })
      .catch(function (err) {
        state.pendingError = (err && err.message) ? err.message : String(err);
        // 有旧数据就留着，只多挂一条「本次刷新失败」的横幅；没数据才整页报错
      })
      .then(function () {
        state.refreshing = false;
        if (btn) { btn.textContent = '刷新'; btn.removeAttribute('disabled'); }
        if (state.unmounted) return;
        scheduleRender();
      });
  }

  function onClick(ev) {
    var t = ev.target;
    if (!t || !t.getAttribute) return;
    var role = t.getAttribute('data-role');
    if (role === 'refresh' || role === 'retry') { refresh(); return; }

    var seg = t.closest ? t.closest('.u-seg') : null;
    if (!seg) return;
    var v = t.getAttribute('data-v');
    if (!v) return;
    var segRole = seg.getAttribute('data-role');
    if (segRole === 'histmode') {
      state.historyMode = v === 'week' ? 'week' : 'day';
      scheduleRender();
    } else if (segRole === 'days') {
      var n = Number(v);
      if (n !== state.days) { state.days = n; refresh(); }
    }
  }

  function bindTooltip(pageEl) {
    if (!pageEl) return;
    var tooltip = pageEl.querySelector('[data-role="chart-tooltip"]');
    if (!tooltip) return;

    function hideTooltip() {
      tooltip.style.display = 'none';
      tooltip.style.opacity = '0';
    }

    function onMouseOver(ev) {
      var t = ev.target;
      if (!t || t.tagName !== 'I' || !t.hasAttribute('data-time')) return;
      var time = t.getAttribute('data-time') || '';
      var tot = Number(t.getAttribute('data-tot') || 0);
      var okcnt = Number(t.getAttribute('data-okcnt') || 0);
      var badcnt = Number(t.getAttribute('data-badcnt') || 0);
      var rate = t.getAttribute('data-rate') || '100';

      tooltip.innerHTML = '<div class="tt-time">' + esc(time) + '</div>'
        + '<div class="tt-row"><span class="tt-ok">成功</span><span>' + okcnt + '</span></div>'
        + '<div class="tt-row"><span class="tt-bad">失败</span><span>' + badcnt + '</span></div>'
        + '<div class="tt-row tt-rate"><span>成功率</span><span>' + (tot ? rate + '%' : '—') + '</span></div>';

      var rect = t.getBoundingClientRect();
      var cx = Math.round(rect.left + rect.width / 2);
      var cy = Math.round(rect.top - 8);
      var winW = window.innerWidth || (document.documentElement && document.documentElement.clientWidth) || 800;

      var xAlign = '-50%';
      var targetX = cx;
      if (cx < 90) {
        targetX = Math.max(10, Math.round(rect.left));
        xAlign = '0%';
      } else if (cx > winW - 90) {
        targetX = Math.min(winW - 10, Math.round(rect.right));
        xAlign = '-100%';
      }

      var yAlign = '-115%';
      var targetY = cy;
      if (cy < 80) {
        targetY = Math.round(rect.bottom + 8);
        yAlign = '15%';
      }

      tooltip.style.left = targetX + 'px';
      tooltip.style.top = targetY + 'px';
      tooltip.style.transform = 'translate(' + xAlign + ', ' + yAlign + ')';
      tooltip.style.display = 'block';
      tooltip.style.opacity = '1';
    }

    function onMouseOut(ev) {
      var t = ev.target;
      if (t && t.tagName === 'I' && t.hasAttribute('data-time')) {
        hideTooltip();
      }
    }

    // 这两个 handler 是**本次调用新建的闭包**，所以原来"先 remove 再 add"里的 remove
    // 一个都摘不掉（函数标识不同）—— 写了防重复绑定却从未生效（审查 N3-08）。
    // 把上一次那对存下来再摘，语义才对得上。
    if (UsagePage._tipHandlers) {
      pageEl.removeEventListener('mouseover', UsagePage._tipHandlers.over);
      pageEl.removeEventListener('mouseout', UsagePage._tipHandlers.out);
    }
    UsagePage._tipHandlers = { over: onMouseOver, out: onMouseOut };
    pageEl.addEventListener('mouseover', onMouseOver);
    pageEl.addEventListener('mouseout', onMouseOut);

    window.removeEventListener('scroll', hideTooltip, true);
    window.addEventListener('scroll', hideTooltip, true);
    UsagePage._hideTooltip = hideTooltip;
  }

  var UsagePage = {
    id: 'usage',
    title: '用量',

    // 宿主（app.js 的路由）调这个：mount(root, ctx)。
    // root 可以是容器元素或选择器；ctx 里有 ui/api/onUnmount，缺了也能跑。
    mount: function (root, ctx) {
      state.ctx = ctx || null;
      var host = null;
      if (root && root.nodeType === 1) host = root;
      else if (typeof root === 'string') host = document.querySelector(root);
      if (!host) host = document.getElementById(PAGE_ID) || document.querySelector('[data-page="usage"]')
        || document.querySelector('main') || document.body;

      // 宿主的容器有三种可能：① 就是本页外壳本身（路由把 #page-usage 当容器）；
      // ② 是外层容器，里面已经有外壳；③ 空的通用容器。三种都要能挂，且不能套壳。
      var page;
      if (host.classList && host.classList.contains('usage-page')) {
        page = host;
        if (!page.querySelector('[data-role="body"]')) page.innerHTML = shellInnerHTML();
      } else {
        page = host.querySelector('.usage-page');
        if (!page) {
          host.innerHTML = shellHTML();
          page = host.querySelector('.usage-page');
        }
      }
      state.root = page;
      state.bodyEl = page.querySelector('[data-role="body"]');
      state.stampEl = page.querySelector('[data-role="stamp"]');
      state.unmounted = false;

      page.removeEventListener('click', onClick);
      page.addEventListener('click', onClick);
      bindTooltip(page);

      if (state.timer) clearInterval(state.timer);
      if (state.ticker) clearInterval(state.ticker);
      state.timer = setInterval(function () { if (!state.unmounted) refresh(); }, REFRESH_MS);
      state.ticker = setInterval(tick, TICK_MS);

      // 宿主给了清理钩子就挂上，双保险：路由即使不走 unmount()，定时器也会被收掉
      if (ctx && typeof ctx.onUnmount === 'function' && !UsagePage._cleanupBound) {
        UsagePage._cleanupBound = true;
        ctx.onUnmount(function () { UsagePage.unmount(); });
      }

      if (state.data) render(); else { render(); refresh(); }
      return UsagePage;
    },

    unmount: function () {
      state.unmounted = true;
      if (renderRafId) { cancelAnimationFrame(renderRafId); renderRafId = null; }
      if (state.timer) { clearInterval(state.timer); state.timer = null; }
      if (state.ticker) { clearInterval(state.ticker); state.ticker = null; }
      if (typeof UsagePage._hideTooltip === 'function') {
        window.removeEventListener('scroll', UsagePage._hideTooltip, true);
        UsagePage._hideTooltip();
      }
      if (state.root) {
        state.root.removeEventListener('click', onClick);
        var tt = state.root.querySelector('[data-role="chart-tooltip"]');
        if (tt) { tt.style.display = 'none'; tt.style.opacity = '0'; }
      }
      return UsagePage;
    },

    refresh: function () { return refresh(); },

    // 自测用：直接塞一份数据，不发请求
    _setData: function (data) {
      state.data = data || {};
      state.pendingError = null;
      state.stampText = '实测 ' + hhmmss(new Date()) + '（注入）';
      if (!state.bodyEl) UsagePage.mount();
      render();
      return UsagePage;
    },
    _state: state
  };

  /* 注册。宿主认哪个用哪个，都不认就自己兜底挂载。 */
  window.PrismPages = window.PrismPages || {};
  window.PrismPages.usage = UsagePage;
  window.UsagePage = UsagePage;              // 兼容按名字直接取的写法

  var hosted = false;
  if (window.Prism && typeof window.Prism.registerPage === 'function') {
    window.Prism.registerPage('usage', UsagePage);
    hosted = true;
  }
  if (typeof window.registerPage === 'function') {
    window.registerPage('usage', UsagePage);
    hosted = true;
  }

  // 兜底：宿主没接管时，自己认 hash。只在 hash 指向用量页（或容器已存在）时挂载，
  // 免得在配置页上抢渲染。
  function isUsageHash() {
    var h = (location.hash || '').replace(/^#\/?/, '').split('?')[0];
    return h === 'usage' || h === '用量';
  }
  function autoMount() {
    if (hosted) return;
    if (state.root && !state.unmounted) return;
    if (!isUsageHash() && !document.querySelector('[data-page="usage"]')) return;
    UsagePage.mount();
  }
  if (!hosted) {
    window.addEventListener('hashchange', autoMount);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', autoMount);
    else setTimeout(autoMount, 0);
  }
})();
