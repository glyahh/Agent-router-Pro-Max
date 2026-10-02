"""Prism 前端渲染性能基准测试自动化运行器。
利用 WebView2 真实环境执行基准测试，并输出结构化量化指标。
"""
import sys
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
HTML_PATH = (ROOT / 'app' / 'static' / 'benchmark.html').resolve().as_uri() + '?auto=1'

import webview

benchmark_results = None


def runner(window):
    global benchmark_results
    for _ in range(150):  # 最多等待 30 秒
        time.sleep(0.2)
        try:
            res = window.evaluate_js("window.__BENCHMARK_RESULTS__")
            if res and isinstance(res, list) and len(res) >= 5:
                benchmark_results = res
                break
        except Exception:
            pass
    window.destroy()


def print_table(results, title="性能测试结果"):
    print("\n" + "=" * 80)
    print(f"  {title}")
    print("=" * 80)
    header = f"{'测试项':<32} | {'JS耗时':<10} | {'总渲染耗时':<12} | {'DOM节点数':<10} | {'长任务(>50ms)'}"
    print(header)
    print("-" * 80)
    for r in results:
        name = r.get('name', '')
        js_t = r.get('jsTime', '') + ' ms'
        render_t = r.get('renderTime', '') + ' ms'
        nodes = str(r.get('nodes', ''))
        lt = str(r.get('longTask', ''))
        print(f"{name:<30} | {js_t:<10} | {render_t:<12} | {nodes:<10} | {lt}")
    print("=" * 80 + "\n")


def main():
    window = webview.create_window(
        'Prism Performance Benchmark',
        url=HTML_PATH,
        width=1024,
        height=768
    )
    webview.start(runner, (window,))

    if not benchmark_results:
        print("未获取到基准测试结果！", file=sys.stderr)
        return 1

    print_table(benchmark_results, "【当前前端渲染性能基准数据】")
    out_file = ROOT / 'app' / 'tests' / 'benchmark_baseline.json'
    out_file.write_text(json.dumps(benchmark_results, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f"基准数据已保存至: {out_file}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
