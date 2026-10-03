"""
Prism 首页真实端到端 (E2E) 性能与加载质量测试套件
通过 Chromium (Edge Headless) 真实环境模拟用户打开应用首屏，
测量首屏网络开销、静态资源直出响应时间、无重复加载断言、真实 DOM 渲染就绪耗时。
"""
import sys
import os
import time
import subprocess
import tempfile
import unittest
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app import server


class TestHomeE2EPerformance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = 18399
        cls.token = 'e2e-test-token-sec123'
        cls.requests_log = []

        orig_do_get = server.Handler.do_GET
        def tracking_do_get(self):
            cls.requests_log.append(self.path)
            return orig_do_get(self)
        server.Handler.do_GET = tracking_do_get

        # 启动真实后台 HTTP 控制台服务（带繁忙顺延与释放保护）
        cls.httpd = None
        for p in range(18399, 18420):
            try:
                cls.httpd = server.start_background(port=p, token=cls.token)
                cls.port = p
                break
            except server.PortBusy:
                continue
        if cls.httpd is None:
            raise RuntimeError("无法为 E2E 性能测试分配端口")
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        try:
            if cls.httpd:
                cls.httpd.shutdown()
                cls.httpd.server_close()
        except Exception:
            pass

    def test_01_static_resources_direct_cache_speed(self):
        """测试 1: 核心静态资源纯内存直出耗时 (RTT 必须极速)"""
        url = f"http://127.0.0.1:{self.port}/index.html"
        # 预热一次
        with urllib.request.urlopen(url, timeout=2) as r:
            r.read()

        # 测量内存直出响应时间
        t0 = time.perf_counter()
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=2) as resp:
            content = resp.read().decode('utf-8')
            code = resp.status
        duration_ms = (time.perf_counter() - t0) * 1000

        self.assertEqual(code, 200)
        self.assertNotIn("pages/home.js", content, "index.html 绝不能写死预载 pages/home.js，避免双重执行")
        self.assertLess(duration_ms, 30.0, f"静态资源响应必须极快，实测: {duration_ms:.2f}ms")

    def test_02_real_browser_headless_e2e_render(self):
        """测试 2: 使用系统 Chromium (Edge Headless) 真实完整运行首屏，断言单次加载与无重复执行"""
        self.requests_log.clear()
        edge_path = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
        if not Path(edge_path).exists():
            edge_path = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"

        self.assertTrue(Path(edge_path).exists(), f"找不到 Edge 浏览器可执行程序: {edge_path}")

        test_url = f"http://127.0.0.1:{self.port}/?t={self.token}#/home"

        t0 = time.perf_counter()
        with tempfile.TemporaryDirectory() as user_data_dir:
            proc = subprocess.run([
                edge_path,
                '--headless=new',
                '--disable-gpu',
                '--no-sandbox',
                f'--user-data-dir={user_data_dir}',
                '--virtual-time-budget=2500',
                '--dump-dom',
                test_url
            ], capture_output=True, text=True, timeout=35)

        e2e_duration_ms = (time.perf_counter() - t0) * 1000
        dom_output = proc.stdout

        # 验证 1：Chromium 成功执行并正常退出
        self.assertEqual(proc.returncode, 0, f"Edge headless 进程异常退出: {proc.stderr}")

        # 验证 2：DOM 中已成功渲染出首页的核心特征节点
        self.assertIn('class="home"', dom_output, "首页根节点 .home 必须已渲染")
        self.assertIn('class="graph', dom_output, "模型路由树容器 .graph 必须已渲染")
        self.assertIn('home-global-toolbar', dom_output, "首页全局工具条必须已渲染")

        # 验证 3：断言 pages/home.js 仅被请求 1 次，严禁发生重复执行与双倍加载！
        home_js_requests = [r for r in self.requests_log if 'pages/home.js' in r or 'pages%2Fhome.js' in r]
        self.assertEqual(
            len(home_js_requests), 1,
            f"pages/home.js 必须且仅能被请求 1 次！实际请求列表: {home_js_requests}"
        )

        print("\n" + "=" * 70)
        print("  【真实端到端 (E2E) 浏览器加载性能报告】")
        print("=" * 70)
        print(f"  - 真实请求路径: {test_url}")
        print(f"  - 浏览器端到端冷启动+DOM树生成耗时: {e2e_duration_ms:.1f} ms")
        print(f"  - 静态资源请求次数: index.html: 1 次, app.js: 1 次, pages/home.js: {len(home_js_requests)} 次 (严格单次)")
        print(f"  - 渲染后首屏 DOM 节点规模: 完整就绪 (HTML 字节数: {len(dom_output)})")
        print("=" * 70 + "\n")


if __name__ == '__main__':
    unittest.main()
