"""真实 Edge 日志明细：刷新焦点、缺失计量、清空与安全渲染。"""
import html
import json
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from ux_render_probe import browser_path

rows = [dict(timestamp='2026-10-13T12:34:56Z', requested_model='route/test-model',
             upstream_model='A/test-model', response_model='test-model-2026', reasoning_level='high',
             failed=False, duration_ms=1200, ttft_ms=200, tokens_per_second=100),
        dict(timestamp='2026-10-13T12:34:55Z', requested_model='<img src=x onerror=alert(1)>',
             upstream_model='fallback-model', failed=True, status=503, duration_ms=800,
             ttft_ms=None, tokens_per_second=None)]
error = ''
PROBE = r"""
(async function () {
  const results=[], wait=ms=>new Promise(r=>setTimeout(r,ms));
  function check(name,ok){results.push({name,ok:!!ok});}
  try {
    for(let i=0;i<100&&!document.querySelector('#pm-request-details tbody tr');i++) await wait(50);
    const page=window.PrismPages.logs, text=()=>document.querySelector('#pm-request-details').textContent;
    check('模型映射与计量',text().includes('route/test-model')&&text().includes('A/test-model')&&text().includes('1.20 s')&&text().includes('0.20 s')&&text().includes('100.00'));
    const timeCell=document.querySelector('#pm-request-details tbody tr td span');
    check('时间列按完成时刻展示且保留起止提示',timeCell&&timeCell.textContent.endsWith(':34:57')&&timeCell.title.includes('开始：')&&timeCell.title.includes('完成：'));
    check('缺失计量和失败状态',text().includes('—')&&text().includes('503'));
    check('模型名按文本渲染',!document.querySelector('#pm-request-details img')&&text().includes('<img'));
    const devBtn=document.querySelector('#pm-dev-logs-toggle'), devBody=document.querySelector('#pm-dev-logs-body');
    check('专业开发者日志默认折叠',devBtn&&devBtn.textContent.includes('专业开发者日志')&&devBtn.getAttribute('aria-expanded')==='false'&&devBody&&devBody.hidden===true);
    devBtn.click();
    check('点击展开专业开发者日志',devBtn.getAttribute('aria-expanded')==='true'&&devBody.hidden===false&&document.querySelector('#pm-logview').textContent.includes('ordinary log'));
    devBtn.click();
    check('再次点击收起专业开发者日志',devBtn.getAttribute('aria-expanded')==='false'&&devBody.hidden===true);
    await fetch('/probe-many');await page.refresh();
    const wrap=document.querySelector('#pm-request-details .tw');wrap.scrollTop=100;
    await page.refresh();
    check('明细限高且轮询保留滚动',document.querySelector('#pm-request-details .tw').clientHeight<=320&&document.querySelector('#pm-request-details .tw').scrollTop===100);
    await fetch('/probe-few');await page.refresh();
    const input=document.querySelector('input[type=search]');input.focus();
    input.value='test-model';input.dispatchEvent(new Event('input',{bubbles:true}));await wait(120);
    check('搜索模型明细',document.querySelectorAll('#pm-request-details tbody tr').length===1);
    await page.refresh();
    check('轮询保留搜索焦点',document.activeElement===input&&input.value==='test-model');
    input.value='not-found';input.dispatchEvent(new Event('input',{bubbles:true}));await wait(120);
    await page.refresh();
    check('日志空态刷新不丢焦点',document.activeElement===input&&document.querySelectorAll('#pm-request-details tbody tr').length===0);
    const refreshBtn=document.querySelector('#pm-btn-refresh');refreshBtn.click();await wait(120);
    check('空态手动刷新后按钮恢复可用',refreshBtn&&!refreshBtn.disabled);
    input.value='';input.dispatchEvent(new Event('input',{bubbles:true}));await wait(120);
    await fetch('/probe-error');await page.refresh();await page.refresh();
    check('采集错误只弹一次',document.querySelectorAll('#toasts .toast').length===1);
    check('错误不常驻明细',!text().includes('测试采集失败'));
    for(const theme of ['light','dark']){document.documentElement.dataset.theme=theme;check(theme+'无整页横向溢出',document.documentElement.scrollWidth<=window.innerWidth);}
    const clear=Array.from(document.querySelectorAll('button')).find(b=>b.textContent==='清空日志');clear.click();await wait(100);
    const ok=Array.from(document.querySelectorAll('#modal button')).find(b=>b.textContent==='清空');if(ok)ok.click();else Array.from(document.querySelectorAll('button')).find(b=>b.textContent.includes('确认清空')).click();
    await wait(250);
    check('清空包含请求明细',document.querySelectorAll('#pm-request-details tbody tr').length===0);
  }catch(e){check('exception: '+e.message,false);}
  const out=document.createElement('pre');out.id='logs-results';out.textContent=JSON.stringify(results);document.body.appendChild(out);
})();
"""


class Fixture(server.Handler):
    def _serve_static(self, path):
        global error
        if path == '/logs-probe.js': return self._send(200, PROBE.encode(), 'application/javascript')
        if path == '/probe-error':
            error = '测试采集失败'; return self._send(200, b'{}', 'application/json')
        if path == '/probe-many':
            rows.extend(dict(rows[0]) for _ in range(50))
            return self._send(200, b'{}', 'application/json')
        if path == '/probe-few':
            del rows[2:]
            return self._send(200, b'{}', 'application/json')
        if path in ('/', '/index.html'):
            raw = (server.STATIC_DIR / 'index.html').read_text(encoding='utf-8').replace('</body>', '<script src="/logs-probe.js"></script></body>')
            return self._send(200, raw.encode(), 'text/html')
        return super()._serve_static(path)

    def _dispatch(self, method, path, query, body):
        if path == '/api/logs':
            if method == 'DELETE': rows.clear(); return self._ok({'cleared': True})
            return self._ok(dict(lines=['[fixture] ordinary log'], line_count=1, truncated=False, requests=rows, request_error=error))
        if path == '/api/error-logs': return self._ok({'files': []})
        if path == '/api/theme': return self._ok({'theme': 'light', 'system': 'light'})
        if path == '/api/sources': return self._ok([])
        if path == '/api/monitor': return self._ok({'gateway': {'state': 'down'}})
        return self._ok({})


assert browser_path(), '需要 Edge'
httpd = server.ExclusiveServer(('127.0.0.1', 0), Fixture)
httpd.console_port = httpd.server_port; httpd.console_token = 'fixture'
threading.Thread(target=httpd.serve_forever, daemon=True).start()
try:
    with tempfile.TemporaryDirectory() as profile:
        result = subprocess.run([browser_path(), '--headless=new', '--disable-gpu', '--no-sandbox',
                                 '--window-size=900,620', '--user-data-dir=' + profile,
                                 '--virtual-time-budget=8000', '--dump-dom',
                                 'http://127.0.0.1:%d/?t=fixture#/logs' % httpd.server_port],
                                capture_output=True, text=True, encoding='utf-8', timeout=35)
    match = re.search(r'<pre id="logs-results">(.*?)</pre>', result.stdout, re.S)
    assert match, result.stderr[-500:]
    checks = json.loads(html.unescape(match[1]))
    for check in checks: print(('PASS ' if check['ok'] else 'FAIL ') + check['name'])
    assert all(c['ok'] for c in checks), checks
finally:
    httpd.shutdown(); httpd.server_close()
