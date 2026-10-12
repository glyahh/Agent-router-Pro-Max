"""Windows Job Object 生命周期与隧道边界，无授权、无公网隧道。"""
import ctypes
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import cursor_tunnel

assert cursor_tunnel._url('https://test.ngrok-free.app')
assert not cursor_tunnel._url('https://test.ngrok-free.app/../../api')
assert not cursor_tunnel._url('https://paid.example.com')
assert not cursor_tunnel._url('http://test.ngrok-free.app')
if os.name == 'nt':
    outsider = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(120)'],
                                creationflags=subprocess.CREATE_NO_WINDOW)
    job = cursor_tunnel.Job()
    own = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(120)'],
                           creationflags=subprocess.CREATE_NO_WINDOW | 4)
    try:
        job.attach(own)
        assert own.poll() is None
        job.close()
        own.wait(timeout=5)
        assert outsider.poll() is None
    finally:
        job.close()
        if own.poll() is None: own.kill(); own.wait(timeout=5)
        outsider.terminate(); outsider.wait(timeout=5)
    print('PASS Job 关闭终止本次子进程，不误杀外部进程')
    with tempfile.TemporaryDirectory() as temp:
        script = Path(temp) / 'parent.py'
        script.write_text('import sys,subprocess,time\n'
                          'sys.path.insert(0,' + repr(str(Path(__file__).resolve().parents[1])) + ')\n'
                          'from core.cursor_tunnel import Job\n'
                          'job=Job()\n'
                          'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(120)"],'
                          'creationflags=subprocess.CREATE_NO_WINDOW|4)\n'
                          'job.attach(p)\nprint(p.pid,flush=True)\ntime.sleep(120)\n')
        parent = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = None
        try:
            child_pid = int(parent.stdout.readline())
            handle = kernel.OpenProcess(0x100000, False, child_pid)
            assert handle
            parent.kill(); parent.wait(timeout=5)
            assert kernel.WaitForSingleObject(handle, 5000) == 0
        finally:
            if parent.poll() is None: parent.kill(); parent.wait(timeout=5)
            parent.stdout.close()
            if handle: kernel.CloseHandle(handle)
        print('PASS 父进程异常退出后 Job 自动清理子进程')
print('PASS 固定 HTTPS 免费入口边界')
