"""跑全部 Prism 自检。在项目根目录执行：

    python app\\tests\\run_all.py

每个用例都是独立进程，任何一个失败都会让退出码非 0。
所有用例都**不碰生产数据**：要么是纯函数，要么把 ROOT 重定向到临时目录后删掉。
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CASES = [
    ('渠道头数据模型', 'test_heads.py'),
    ('路由核心（build_config / regen_catalog）', 'test_multi_source.py'),
    ('保存路由端到端', 'test_multi_source_save.py'),
    ('Agent 适配器（5 个客户端）', 'test_agents.py'),
    ('加固（重定向 / 预检闸门 / auth 补偿）', 'test_hardening.py'),
]


def main() -> int:
    failed = []
    for label, name in CASES:
        path = HERE / name
        print('=' * 72)
        print('%s  ->  %s' % (label, name))
        print('=' * 72)
        proc = subprocess.run([sys.executable, str(path)], cwd=str(HERE.parents[1]))
        if proc.returncode != 0:
            failed.append(name)
        print()
    print('=' * 72)
    if failed:
        print('失败：%s' % '、'.join(failed))
        return 1
    print('全部通过（%d 个用例）' % len(CASES))
    return 0


if __name__ == '__main__':
    sys.exit(main())
