"""真实 Edge 渲染 Cursor 页面；临时控制台夹具，不读写生产状态。"""
import copy
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from ux_render_probe import browser_path

assert browser_path(), '需要 Edge'
models = [{'source': s, 'label': s, 'model': 'gpt-test', 'levels': levels, 'default': default}
          for s, levels, default in [('one', ['medium', 'high'], 'medium'), ('two', ['high'], 'high'), ('empty', [], None)]]
state = dict(source=None, model=None, level=None, enabled=False, url='', revision=0,
             candidates=models, model_name='prism-cursor', error='')
failed = False
writes = []
lock = threading.Lock()
PROBE = r"""
(async function () {
  const results = [], wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  function check(name, value) { results.push({name:name, ok:!!value}); }
  function change(el, value) { el.value=value; el.dispatchEvent(new Event('change',{bubbles:true})); }
  try {
    for(let i=0;i<100&&!document.querySelector('#cursor-model option[value]:not([value=""])');i++)await wait(50);
    const model=document.getElementById('cursor-model'), level=document.getElementById('cursor-level');
    check('页面与按来源分组', model&&model.querySelectorAll('optgroup').length===3);
    check('导航位于首页之后', document.querySelectorAll('[data-nav]')[1].dataset.nav==='cursor');
    change(model,JSON.stringify(['one','gpt-test']));
    check('默认 medium',level.value==='medium');
    await wait(300);
    change(model,JSON.stringify(['two','gpt-test']));
    await wait(100);
    change(level,'high');
    await wait(1300);
    check('旧响应不覆盖新选择',model.value===JSON.stringify(['two','gpt-test'])&&level.value==='high');
    const current=await window.Prism.api.get('api/cursor');
    check('串行保存最后选择',current.source==='two'&&current.level==='high');
    change(model,JSON.stringify(['empty','gpt-test']));
    check('无声明禁用等级',level.disabled);
    await wait(700);
    change(model,JSON.stringify(['two','gpt-test']));
    await wait(700);
    model.focus();check('键盘可聚焦',document.activeElement===model);
    check('最小窗口无横向溢出',document.documentElement.scrollWidth<=window.innerWidth);
    const names=['light','dark'];
    for (const theme of names) {
      document.documentElement.dataset.theme=theme;
      const colors=getComputedStyle(model);check(theme+'主题',colors.color!==colors.backgroundColor);
    }
    check('无成功态文案',!/已保存|保存成功|更新成功|ACTIVE|RUNNING/.test(document.getElementById('view').textContent));
    check('正常态不占错误行',document.querySelector('[role="alert"]').getClientRects().length===0);
    await fetch('/probe-fail');
    change(model,JSON.stringify(['one','gpt-test']));
    await wait(700);
    check('失败只显示错误',document.querySelector('[role="alert"]').textContent==='测试保存失败');
    const last=await window.Prism.api.get('api/cursor');
    check('失败不改变服务选择',last.source==='two');
  } catch(e) { results.push({name:String(e),ok:false}); }
  const out=document.createElement('pre');out.id='cursor-results';out.textContent=JSON.stringify(results);document.body.appendChild(out);
})();
"""


class Fixture(server.Handler):
    def _serve_static(self, path):
        global failed
        if path == '/cursor-probe.js':
            return self._send(200, PROBE.encode(), 'application/javascript')
        if path == '/probe-fail':
            failed = True; return self._send(200, b'{}', 'application/json')
        if path in ('/', '/index.html'):
            raw = (server.STATIC_DIR / 'index.html').read_text().replace('</body>', '<script src="/cursor-probe.js"></script></body>')
            return self._send(200, raw.encode(), 'text/html')
        return super()._serve_static(path)

    def _dispatch(self, method, path, query, body):
        if path == '/api/cursor':
            if method == 'PUT':
                time.sleep(.4)
                if failed:
                    return self._fail(409, '测试保存失败')
                with lock:
                    if body['revision'] != state['revision']:
                        return self._fail(409, 'revision 冲突')
                    writes.append(copy.deepcopy(body));state.update(body);state['revision'] += 1
            return self._ok(copy.deepcopy(state))
        if path == '/api/theme': return self._ok({'theme': 'light', 'system': 'light'})
        if path == '/api/sources': return self._ok([])
        if path == '/api/monitor': return self._ok({'gateway': {'state': 'down'}})
        return self._ok({})


httpd = server.ExclusiveServer(('127.0.0.1', 0), Fixture)
httpd.console_port = httpd.server_port;httpd.console_token = 'fixture'
threading.Thread(target=httpd.serve_forever, daemon=True).start()
try:
    with tempfile.TemporaryDirectory() as profile:
        result = subprocess.run([browser_path(), '--headless=new', '--disable-gpu', '--no-sandbox',
                                 '--window-size=900,620', '--user-data-dir=' + profile,
                                 '--virtual-time-budget=10000', '--dump-dom',
                                 'http://127.0.0.1:%d/?t=fixture#/cursor' % httpd.server_port],
                                capture_output=True, text=True, encoding='utf-8', timeout=40)
    match = re.search(r'<pre id="cursor-results">(.*?)</pre>', result.stdout, re.S)
    assert match, result.stderr[-500:]
    import html
    checks = json.loads(html.unescape(match[1]))
    for check in checks: print(('PASS ' if check['ok'] else 'FAIL ') + check['name'])
    assert all(c['ok'] for c in checks), checks
    assert all(b['revision'] == i for i, b in enumerate(writes)), writes
finally:
    httpd.shutdown();httpd.server_close()
