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
test_calls = 0
rotate_calls = 0
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
    const exposed=document.getElementById('cursor-model-name');
    check('暴露模型名可编辑',exposed&&!exposed.readOnly&&exposed.value==='prism-cursor');
    change(model,JSON.stringify(['one','gpt-test']));
    check('默认 medium',level.value==='medium');
    check('保存中提示可见',!document.querySelector('.cursor-form [role="status"]').hidden);
    await wait(300);
    change(model,JSON.stringify(['two','gpt-test']));
    await wait(100);
    change(exposed,'my-cursor');
    change(level,'high');
    await wait(1300);
    check('旧响应不覆盖新选择',model.value===JSON.stringify(['two','gpt-test'])&&level.value==='high'&&exposed.value==='my-cursor');
    const current=await window.Prism.api.get('api/cursor');
    check('串行保存最后选择',current.source==='two'&&current.level==='high'&&current.model_name==='my-cursor');
    check('保存完成提示消失',document.querySelector('.cursor-form [role="status"]').hidden);
    change(model,JSON.stringify(['empty','gpt-test']));
    check('无声明禁用等级',level.disabled);
    await wait(700);
    change(model,JSON.stringify(['two','gpt-test']));
    await wait(700);
    check('切换模型保留暴露名',exposed.value==='my-cursor');
    exposed.focus();exposed.value='editing-name';
    await wait(2300);
    check('轮询不覆盖正在输入的名称',exposed.value==='editing-name');
    change(exposed,'my-cursor');exposed.blur();
    await wait(700);
    model.focus();check('键盘可聚焦',document.activeElement===model);
    check('最小窗口无横向溢出',document.documentElement.scrollWidth<=window.innerWidth);
    const names=['light','dark'];
    for (const theme of names) {
      document.documentElement.dataset.theme=theme;
      const colors=getComputedStyle(model);check(theme+'主题',colors.color!==colors.backgroundColor);
    }
    check('无成功态文案',!/已保存|保存成功|更新成功|ACTIVE|RUNNING/.test(document.getElementById('view').textContent));
    check('正常态不弹提示',!document.querySelector('#toasts .toast'));
    const testBtn=Array.from(document.querySelectorAll('button')).find(b=>b.textContent==='测试');
    check('未启用不可测试',testBtn.disabled);
    await fetch('/probe-starting');
    await wait(2300);
    check('启动中不可测试',testBtn.disabled);
    testBtn.click();
    await fetch('/probe-ready');
    await wait(2300);
    check('入口就绪可测试',!testBtn.disabled);
    testBtn.click();
    check('测试进行中可见',testBtn.textContent==='测试中…'&&testBtn.disabled);
    await wait(300);
    check('测试失败弹窗',(document.querySelector('#toasts .toast span')||{}).textContent==='本机入口 HTTP 503');
    check('测试错误不常驻页面',!document.getElementById('view').textContent.includes('HTTP 503'));
    check('失败后按钮恢复',testBtn.textContent==='测试'&&!testBtn.disabled);
    document.querySelector('#toasts .toast').remove();
    testBtn.click();
    await wait(300);
    check('测试成功按钮反馈',testBtn.textContent==='连接正常'&&!testBtn.disabled);
    check('测试成功不弹窗',!document.querySelector('#toasts .toast'));
    await wait(3200);
    check('成功反馈自动恢复',testBtn.textContent==='测试');
    await fetch('/probe-fail');
    change(model,JSON.stringify(['one','gpt-test']));
    await wait(700);
    check('失败弹红字提示',(document.querySelector('#toasts .toast span')||{}).textContent==='测试保存失败');
    const last=await window.Prism.api.get('api/cursor');
    check('失败不改变服务选择',last.source==='two');
    const rotate=Array.from(document.querySelectorAll('button')).find(b=>b.textContent==='轮换');
    rotate.click();await wait(100);
    check('轮换前告知失效和连接风险',document.querySelector('#modal').textContent.includes('立即失效')&&document.querySelector('#modal').textContent.includes('否则无法连接'));
    Array.from(document.querySelectorAll('#modal button')).find(b=>b.textContent==='取消').click();await wait(100);
    check('取消不调用更换接口',(await (await fetch('/probe-rotation-count')).json()).count===0);
    const previousURL=document.getElementById('cursor-url').value;
    rotate.click();await wait(100);
    Array.from(document.querySelectorAll('#modal button')).find(b=>b.textContent==='轮换').click();await wait(250);
    check('确认只调用一次',(await (await fetch('/probe-rotation-count')).json()).count===1);
    check('轮换后 URL 保持不变',document.getElementById('cursor-url').value===previousURL);
  } catch(e) { results.push({name:String(e),ok:false}); }
  const out=document.createElement('pre');out.id='cursor-results';out.textContent=JSON.stringify(results);document.body.appendChild(out);
})();
"""


class Fixture(server.Handler):
    def _serve_static(self, path):
        global failed
        if path == '/cursor-probe.js':
            return self._send(200, PROBE.encode(), 'application/javascript')
        if path == '/probe-rotation-count':
            return self._send(200, json.dumps({'count': rotate_calls}).encode(), 'application/json')
        if path == '/probe-fail':
            failed = True; return self._send(200, b'{}', 'application/json')
        if path == '/probe-starting':
            state.update(starting=True, enabled=False, running=False, url='')
            return self._send(200, b'{}', 'application/json')
        if path == '/probe-ready':
            state.update(starting=False, enabled=True, running=True, url='https://test.ngrok.dev')
            return self._send(200, b'{}', 'application/json')
        if path in ('/', '/index.html'):
            raw = (server.STATIC_DIR / 'index.html').read_text().replace('</body>', '<script src="/cursor-probe.js"></script></body>')
            return self._send(200, raw.encode(), 'text/html')
        return super()._serve_static(path)

    def _dispatch(self, method, path, query, body):
        global test_calls, rotate_calls
        if path == '/api/cursor/access-key':
            assert body == {'action': 'rotate', 'confirmed': True}, body
            rotate_calls += 1
            return self._ok({})
        if path == '/api/cursor/selftest':
            test_calls += 1
            return self._ok({'ok': test_calls > 1, 'summary': '本机入口 HTTP 503' if test_calls == 1 else '链路正常'})
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
                                 '--virtual-time-budget=26000', '--dump-dom',
                                 'http://127.0.0.1:%d/?t=fixture#/cursor' % httpd.server_port],
                                capture_output=True, text=True, encoding='utf-8', timeout=40)
    match = re.search(r'<pre id="cursor-results">(.*?)</pre>', result.stdout, re.S)
    assert match, result.stderr[-500:]
    import html
    checks = json.loads(html.unescape(match[1]))
    for check in checks: print(('PASS ' if check['ok'] else 'FAIL ') + check['name'])
    assert all(c['ok'] for c in checks), checks
    assert test_calls == 2, test_calls
    assert rotate_calls == 1, rotate_calls
    assert all(b['revision'] == i for i, b in enumerate(writes)), writes
finally:
    httpd.shutdown();httpd.server_close()
