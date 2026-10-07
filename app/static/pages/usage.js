/* ============================================================================
 * Prism · 用量页  (static/pages/usage.js)
 *
 * 数据来源：GET /api/usage?days=N  →  {ok:true, data:{quota, counts, history}}
 * 静态稿：用户已确认的静态样板 B 下半部（用量页）。样板文件 app/design/ui-mock.html 已不在仓库里。
 *
 * 两块内容，缺任何一块都不能白屏：
 *   ① 请求计数   10 分钟一桶的柱状图；有请求的来源各一行
 *   ② 长期历史   按天或按周的合计（来自 sampling.py 落的 usage-history.db）
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

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  function hhmm(d) { return pad2(d.getHours()) + ':' + pad2(d.getMinutes()); }
  function hhmmss(d) { return hhmm(d) + ':' + pad2(d.getSeconds()); }

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

  /* ────────────────────────── 数据归一 ────────────────────────── */
  /* 后端（core/health.py + server.py）的输出以 INTERFACES.md 为准，
     但它是并行开发的，字段可能先到齐、后到齐或换个名字。
     这里做一遍归一：认得几种常见形状，认不出就返回空数组走空态，
     绝不因为一个没见过的字段把整页炸掉。 */

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

    // 兜底直连：带 15s 超时，否则"端口通但不回包"会让「读取中…」永不恢复
    var ctl = (typeof AbortController === 'function') ? new AbortController() : null;
    var timer = null;
    var init = { headers: { 'Accept': 'application/json' }, cache: 'no-store' };
    if (ctl) { init.signal = ctl.signal; timer = setTimeout(function () { ctl.abort(); }, 15000); }
    function done() { if (timer) { clearTimeout(timer); timer = null; } }
    return fetch(url, init).then(function (res) {
      return res.text().then(function (txt) {
        done();
        var json = null;
        try { json = JSON.parse(txt); } catch (e) { /* 下面统一报错 */ }
        if (!json || typeof json !== 'object') {
          throw new Error('后端返回了非 JSON 内容（HTTP ' + res.status + '）。'
            );
        }
        if (json.ok === false) throw new Error(json.error || ('请求失败 HTTP ' + res.status));
        if (!res.ok) throw new Error(json.error || ('请求失败 HTTP ' + res.status));
        return Object.prototype.hasOwnProperty.call(json, 'data') ? json.data : json;
      }, function (e) { done(); throw e; });
    }, function (e) {
      done();
      if (e && e.name === 'AbortError') throw new Error('控制台 15 秒没有响应，已中断。');
      throw e;
    });
  }

  /* ────────────────────── 渲染：总览指标卡 ──────────────────────
     大号总数 + 右侧曲线 + 点阵底纹。全部由已有 history 推导，不新增取数。
     悬停热区沿用页面既有的 `<i data-time data-tot …>` 约定，bindTooltip 直接可用。 */

  function compactNum(n) {
    if (n === null || n === undefined || !isFinite(n)) return '—';
    var abs = Math.abs(n);
    if (abs >= 1000000) return (n / 1000000).toFixed(1).replace(/\.0$/, '') + 'M';
    if (abs >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
    return String(Math.round(n));
  }

  // history 是按「日期 × 来源」铺平的，总览要的是每天一行
  function dailyTotals(data, days) {
    var rows = normalizeHistory(data && data.history);
    var byDate = {};
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      var cell = byDate[r.date] || (byDate[r.date] = { date: r.date, success: 0, failed: 0 });
      cell.success += r.success;
      cell.failed += r.failed;
    }
    var dates = Object.keys(byDate).sort();
    if (days && dates.length > days) dates = dates.slice(-days);
    var out = [];
    for (var j = 0; j < dates.length; j++) {
      var d = byDate[dates[j]];
      d.total = d.success + d.failed;
      out.push(d);
    }
    return out;
  }

  function totalsStats(points) {
    var sum = 0, peak = 0, low = 0;
    for (var i = 0; i < points.length; i++) {
      var v = points[i].total;
      sum += v;
      if (i === 0 || v > peak) peak = v;
      if (i === 0 || v < low) low = v;
    }
    return { sum: sum, peak: peak, low: low, avg: points.length ? sum / points.length : 0 };
  }

  function totalsTrend(points) {
    if (!points.length) return { net: 0, step: 0, pct: 0 };
    var first = points[0].total;
    var last = points[points.length - 1].total;
    var prev = points.length > 1 ? points[points.length - 2].total : first;
    var net = last - first;
    return { net: net, step: last - prev, pct: first ? (net / first) * 100 : 0 };
  }

  var TREND_ARROW = {
    up: '<path d="M8 13V4M8 4 4.5 7.5M8 4l3.5 3.5"/>',
    down: '<path d="M8 3v9M8 12 4.5 8.5M8 12l3.5-3.5"/>',
    flat: '<path d="M3 8h10M13 8 10 5M13 8l-3 3"/>'
  };

  // viewBox 固定 300×120 + preserveAspectRatio=none：随卡片宽度自由拉伸，
  // 线宽靠 vector-effect 保持 1.5px 不跟着变形。
  function metricChartSVG(points, view) {
    var W = 300, H = 120, PAD = 10;
    var vals = [];
    for (var i = 0; i < points.length; i++) vals.push(points[i].total);
    var max = Math.max.apply(null, vals);
    var min = Math.min.apply(null, vals);
    var span = (max - min) || 1;
    var n = vals.length;
    var xOf = function (i) { return n > 1 ? PAD + i * ((W - PAD * 2) / (n - 1)) : W / 2; };
    var yOf = function (v) { return H - PAD - ((v - min) / span) * (H - PAD * 2); };

    var body = '';
    if (view === 'bar') {
      var barW = Math.max(2, ((W - PAD * 2) / n) * 0.55);
      for (var b = 0; b < n; b++) {
        var y = yOf(vals[b]);
        body += '<rect x="' + (xOf(b) - barW / 2).toFixed(1) + '" y="' + y.toFixed(1)
          + '" width="' + barW.toFixed(1) + '" height="' + Math.max(1, H - PAD - y).toFixed(1)
          + '" rx="1.5" fill="currentColor" opacity="0.85"/>';
      }
    } else {
      var line = 'M' + xOf(0).toFixed(1) + ',' + yOf(vals[0]).toFixed(1);
      for (var k = 1; k < n; k++) {
        // 中点法平滑：控制点取两点的水平中点，视觉上顺滑又不会过冲
        var cx = ((xOf(k - 1) + xOf(k)) / 2).toFixed(1);
        line += ' C' + cx + ',' + yOf(vals[k - 1]).toFixed(1)
          + ' ' + cx + ',' + yOf(vals[k]).toFixed(1)
          + ' ' + xOf(k).toFixed(1) + ',' + yOf(vals[k]).toFixed(1);
      }
      var area = line
        + ' L' + xOf(n - 1).toFixed(1) + ',' + (H - PAD)
        + ' L' + xOf(0).toFixed(1) + ',' + (H - PAD) + ' Z';
      body = '<path d="' + area + '" fill="currentColor" opacity="0.08"/>'
        + '<path d="' + line + '" fill="none" stroke="currentColor" stroke-width="1.5"'
        + ' vector-effect="non-scaling-stroke" stroke-linecap="round" stroke-linejoin="round"/>';
    }

    return '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" aria-hidden>'
      + body + '</svg>';
  }

  function metricHits(points) {
    var n = points.length;
    var w = n ? 100 / n : 100;
    var out = '';
    for (var i = 0; i < n; i++) {
      var p = points[i];
      var rate = p.total ? Math.round((p.success / p.total) * 100) : 100;
      out += '<i style="left:' + (i * w).toFixed(2) + '%;width:' + w.toFixed(2) + '%"'
        + ' data-time="' + esc(p.date) + '" data-tot="' + p.total + '"'
        + ' data-okcnt="' + p.success + '" data-badcnt="' + p.failed + '"'
        + ' data-rate="' + rate + '"></i>';
    }
    return out;
  }

  function renderOverview(data, opts) {
    var points = dailyTotals(data, (opts && opts.days) || 7);
    var view = (opts && opts.metricView) === 'bar' ? 'bar' : 'curve';

    var seg = '<span class="u-seg umetric-view" data-role="metricview">'
      + '<button type="button" data-v="curve" class="' + (view === 'curve' ? 'on' : '') + '">曲线</button>'
      + '<button type="button" data-v="bar" class="' + (view === 'bar' ? 'on' : '') + '">柱状</button>'
      + '</span>';
    var head = '<div class="umetric-head"><h3 class="umetric-title">请求总览</h3>' + seg + '</div>';

    if (!points.length) {
      return '<div class="sec u-lead"><div class="umetric" data-trend="flat">'
        + '<div class="umetric-body">' + head + emptySlotHTML('暂无数据', '') + '</div>'
        + '</div></div>';
    }

    var st = totalsStats(points);
    var tr = totalsTrend(points);
    var trend = Math.abs(tr.pct) < 0.5 ? 'flat' : (tr.net >= 0 ? 'up' : 'down');
    var delta = (tr.step >= 0 ? '+' : '−') + compactNum(Math.abs(tr.step));

    return '<div class="sec u-lead"><div class="umetric" data-trend="' + trend + '">'
      + '<div class="umetric-chart">'
      + '<div class="umetric-dots"></div>'
      + metricChartSVG(points, view)
      + '<div class="umetric-hit">' + metricHits(points) + '</div>'
      + '</div>'
      + '<div class="umetric-body">' + head
      + '<div class="umetric-value">' + compactNum(st.sum) + '</div>'
      + '</div>'
      + '<div class="umetric-foot">'
      + '<span class="umetric-trend">'
      + '<svg class="ui-icon" viewBox="0 0 16 16" aria-hidden>' + TREND_ARROW[trend] + '</svg>'
      + Math.abs(tr.pct).toFixed(1) + '% 较首日</span>'
      + '<span class="umetric-delta">' + delta + ' 较前一日</span>'
      + '<span class="umetric-stats">'
      + '峰值 <b>' + compactNum(st.peak) + '</b>'
      + '<span class="dot-sep">·</span>谷值 <b>' + compactNum(st.low) + '</b>'
      + '<span class="dot-sep">·</span>均值 <b>' + compactNum(Math.round(st.avg)) + '</b>'
      + '</span>'
      + '</div></div></div>';
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

  // 成功、失败写在行尾。失败为 0 也留着，两列才能对齐。
  function countNums(ok, bad) {
    return '<span class="nums"><span>成功 <b>' + ok + '</b></span>'
      + '<span' + (bad ? ' class="u-bad"' : '') + '>失败 <b>' + bad + '</b></span></span>';
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

    var head = '<div class="sechead">'
      + '<span class="stitle">请求分布</span><span class="hr"></span></div>';

    var html = '<div class="sec">' + head;

    if (!merged.length) {
      html += emptySlotHTML('暂无记录', '') + '</div>';
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

    // 窗口里有桶、但一个请求都没有。空桶 DOM 还在（flex 占位，时间轴不错位），
    // 样式上不画柱。.u-flat 把图区收矮，只留时间轴。
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
      + '</div>'
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
    // 没请求的来源不占行。上面的分布图已经覆盖了「这段时间没有量」。
    var rows = [];
    var i;
    for (i = 0; i < counts.length; i++) {
      if (counts[i].success || counts[i].failed) rows.push(counts[i]);
    }
    if (!rows.length) return '';

    rows.sort(function (a, b) { return (b.success + b.failed) - (a.success + a.failed); });

    // 同名来源补一段掩码尾巴，悬停仍可对上凭据，界面上不铺服务商域名。
    var labelCount = {};
    for (i = 0; i < rows.length; i++) labelCount[rows[i].label] = (labelCount[rows[i].label] || 0) + 1;

    var body = '';
    for (i = 0; i < rows.length; i++) {
      var c = rows[i];
      var shown = c.label + (labelCount[c.label] > 1 ? ' · ' + keyTail(c.source_key) : '');
      body += '<div class="u-line" title="' + esc(maskSourceKey(c.source_key)) + '">'
        + '<span class="nm">' + esc(shown) + '</span>'
        + countNums(c.success, c.failed) + '</div>';
    }

    return '<div class="sec"><div class="sechead">'
      + '<span class="stitle">来源明细</span><span class="hr"></span></div>'
      + '<div class="u-lines" data-role="srctable">' + body + '</div></div>';
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
      + '<button type="button" data-v="day" class="' + (byWeek ? '' : 'on') + '">按天</button>'
      + '<button type="button" data-v="week" class="' + (byWeek ? 'on' : '') + '">按周</button></span>';
    var rangeSeg = '<span class="u-seg" data-role="days">';
    var ranges = [7, 30, 90];
    for (var i = 0; i < ranges.length; i++) {
      rangeSeg += '<button type="button" data-v="' + ranges[i] + '" class="' + (ranges[i] === days ? 'on' : '') + '">'
        + ranges[i] + 'd</button>';
    }
    rangeSeg += '</span>';

    var head = '<div class="sechead">'
      + '<span class="stitle">长期历史</span><span class="hr"></span>'
      + seg + rangeSeg
      + '</div>';

    var html = '<div class="sec">' + head;
    if (!rows.length) {
      html += emptySlotHTML('暂无记录', '') + '</div>';
      return html;
    }

    // 一天或一周只留合计。按来源拆开、失败率、相对量条都不画。
    var groups = {}, order = [];
    for (var r = 0; r < rows.length; r++) {
      var row = rows[r];
      var period = byWeek ? mondayOf(row.date) : row.date;
      var g = groups[period];
      if (!g) { g = groups[period] = { period: period, ok: 0, bad: 0 }; order.push(period); }
      g.ok += row.success;
      g.bad += row.failed;
    }
    order.sort().reverse();

    var body = '';
    for (var k = 0; k < order.length; k++) {
      var g2 = groups[order[k]];
      if (!g2.ok && !g2.bad) continue;
      body += '<div class="u-line"><span class="nm">' + esc(g2.period) + '</span>'
        + countNums(g2.ok, g2.bad) + '</div>';
    }
    if (!body) {
      html += emptySlotHTML('暂无记录', '') + '</div>';
      return html;
    }

    html += '<div class="u-lines" data-role="histtable">' + body + '</div></div>';
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
    days: 7,
    historyMode: 'day',
    metricView: 'curve',
    timer: null,
    ticker: null,
    unmounted: true,
    refreshing: false,
    refreshPending: false
  };

  // 外壳的内部结构。拆成两层是因为宿主可能直接把外壳本身当容器传进来，
  // 那种情况下只能填内容，不能再套一层 .usage-page。
  function shellInnerHTML() {
    return '<div class="u-toolbar">'
      + '<span class="pagetag">用量</span>'
      + '<span class="u-spacer"></span>'
      + '<span class="u-updated" data-role="stamp"></span>'
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
      + renderOverview(d, state)
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

  function wire() {
    fillEmptySlots();
    applySizes();
  }

  function tick() {
    if (state.unmounted || !state.bodyEl) return;
    // 宿主换页时若只是把容器换掉、忘了调 unmount，这里自己收手，别让定时器空转
    if (document.documentElement.contains && !document.documentElement.contains(state.bodyEl)) {
      UsagePage.unmount();
    }
  }

  function refresh() {
    if (state.unmounted) return Promise.resolve();
    // 飞行中再点：记一笔，等当前请求收尾后补发，别把用户的区间切换吞掉
    if (state.refreshing) { state.refreshPending = true; return Promise.resolve(); }
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
        if (state.refreshPending) { state.refreshPending = false; refresh(); return; }
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
    } else if (segRole === 'metricview') {
      state.metricView = v === 'bar' ? 'bar' : 'curve';
      scheduleRender();
    } else if (segRole === 'days') {
      var n = Number(v);
      if (n !== state.days) {
        state.days = n;
        scheduleRender();   // 高亮先跟上，别等请求回来才动
        refresh();
      }
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

      // 用量页带着进场动画，computed transform 不是 none，fixed 的参照是
      // .usage-page 而不是视口。left/top 必须换成相对这一层的坐标，否则一滚动卡片就飘走。
      tooltip.style.opacity = '0';
      tooltip.style.display = 'block';
      var origin = tooltip.offsetParent && tooltip.offsetParent.getBoundingClientRect();
      var ox = origin ? origin.left : 0;
      var oy = origin ? origin.top : 0;
      tooltip.style.left = Math.round(targetX - ox) + 'px';
      tooltip.style.top = Math.round(targetY - oy) + 'px';
      tooltip.style.transform = 'translate(' + xAlign + ', ' + yAlign + ')';
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

      render();
      refresh();
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
      // 允许下次 mount 重新注册 onUnmount，否则二次进入后双保险失效、定时器泄漏
      UsagePage._cleanupBound = false;
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
