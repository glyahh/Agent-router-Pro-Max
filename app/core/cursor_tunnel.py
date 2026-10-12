"""只管理本次创建的 ngrok。授权仅经环境传递，诊断只保留错误分类。"""
from __future__ import annotations

import ctypes
import hashlib
import io
import json
import os
import re
import socket
import subprocess
import threading
import urllib.request
import zipfile
from ctypes import wintypes
from pathlib import Path

from . import bridge

DOWNLOAD = 'https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-windows-amd64.zip'
VERSION = '3.39.11'
# 只收录确认过含义的错误码：106 = 拿的是 ngrok v1 的旧版 token，v3 agent 不认
# （官方错误页 err_ngrok_106）。其余编号保持原样，用户拿着去查也不会被误导。
NGROK_HINTS = {
    'ERR_NGROK_106': 'token 是 ngrok v1 旧格式，去 dashboard 重新复制',
}
SHA256 = 'd339bcbd0713233337e860163f5249eea679cf26750a5700510dbc241d201748'
_lock = threading.RLock()
_worker = None
_cancel = threading.Event()
_process = None
_job = None
_error = ''
_codes = []
_starting = threading.Event()


def error():
    return _error


def starting():
    return _starting.is_set()


def _verify(path):
    if hashlib.sha256(path.read_bytes()).hexdigest() != SHA256:
        raise bridge.RouteError('ngrok 版本指纹不匹配')
    # 固定 SHA256 先验，再核对 Windows 信任链和发行者；校验前不执行下载程序。
    escaped = str(path).replace("'", "''")
    command = (r'Import-Module "$PSHOME\Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1" -Force;'
               "$s=Get-AuthenticodeSignature -LiteralPath '" + escaped + "';"
               "[pscustomobject]@{status=[string]$s.Status;subject=$s.SignerCertificate.Subject}"
               '|ConvertTo-Json -Compress')
    try:
        environment = dict(os.environ)
        environment.pop('PSModulePath', None)  # 不继承 PowerShell 7 的不兼容模块路径。
        powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
                                env=environment)
        signer = json.loads(result.stdout.decode('utf-8-sig'))
        if signer.get('status') != 'Valid' or 'O="ngrok, Inc."' not in signer.get('subject', ''):
            raise ValueError()
        version = subprocess.run([str(path), 'version'], capture_output=True, timeout=10,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        if version.returncode or version.stdout.decode().strip() != 'ngrok version ' + VERSION:
            raise ValueError()
    except (ValueError, OSError, subprocess.SubprocessError):
        raise bridge.RouteError('ngrok 签名或版本校验失败') from None


def prepare():
    if os.name != 'nt':
        raise bridge.RouteError('ngrok 隧道仅支持 Windows')
    directory = bridge.ROOT / '.cursor-runtime'
    directory.mkdir(exist_ok=True)
    path = directory / 'ngrok.exe'
    if not path.exists():
        try:
            with urllib.request.urlopen(DOWNLOAD, timeout=45) as response:
                if not response.geturl().startswith('https://bin.equinox.io/'):
                    raise ValueError()
                archive = response.read(32 * 1024 * 1024 + 1)
            if len(archive) > 32 * 1024 * 1024:
                raise ValueError()
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                info = bundle.getinfo('ngrok.exe')
                if info.file_size > 64 * 1024 * 1024:
                    raise ValueError()
                raw = bundle.read(info)
            if hashlib.sha256(raw).hexdigest() != SHA256:
                raise bridge.RouteError('ngrok 下载版本已变化，需更新版本锁')
            temporary = path.with_suffix('.tmp')
            temporary.write_bytes(raw)
            temporary.replace(path)
        except bridge.RouteError:
            raise
        except (OSError, ValueError, zipfile.BadZipFile, KeyError):
            raise bridge.RouteError('ngrok 下载失败') from None
    _verify(path)
    return path


class Job:
    """KILL_ON_JOB_CLOSE；挂起创建、先加入 Job 再运行，消除启动时的遗留窗口。"""
    def __init__(self):
        class BASIC(ctypes.Structure):
            _fields_ = [('per_process', ctypes.c_int64), ('per_job', ctypes.c_int64),
                        ('flags', wintypes.DWORD), ('min_working', ctypes.c_size_t),
                        ('max_working', ctypes.c_size_t), ('active_limit', wintypes.DWORD),
                        ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                        ('scheduling', wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]
        class EXTENDED(ctypes.Structure):
            _fields_ = [('basic', BASIC), ('io', IO), ('process_memory', ctypes.c_size_t),
                        ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t),
                        ('peak_job', ctypes.c_size_t)]
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        self.api.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        self.api.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.handle = self.api.CreateJobObjectW(None, None)
        limits = EXTENDED()
        limits.basic.flags = 0x2000
        if not self.handle or not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise bridge.RouteError('无法保护隧道进程生命周期')

    def attach(self, process):
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise bridge.RouteError('无法绑定隧道进程生命周期')
        resume = ctypes.WinDLL('ntdll').NtResumeProcess
        resume.argtypes = (wintypes.HANDLE,)
        resume.restype = ctypes.c_long
        if resume(int(process._handle)) != 0:
            raise bridge.RouteError('无法启动隧道进程')

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def _url(value):
    return isinstance(value, str) and bool(re.fullmatch(r'https://[a-z0-9-]+\.(?:ngrok-free\.app|ngrok-free\.dev|ngrok\.app|ngrok\.dev)', value))


def _launch(executable, token, fixed_url):
    global _process, _job, _codes
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    config = bridge.ROOT / '.cursor-runtime' / 'ngrok.json'
    bridge._write_text_atomic(config, json.dumps({'version': '3', 'agent': {
        'web_addr': '127.0.0.1:%d' % port, 'console_ui': False,
        'update_check': False, 'log': 'stdout', 'log_format': 'json'}}))
    args = [str(executable), 'http', 'http://127.0.0.1:8319', '--config', str(config), '--inspect=false']
    if fixed_url:
        if not _url(fixed_url):
            raise bridge.RouteError('固定入口无效')
        args.extend(['--url', fixed_url])
    env = {k: v for k, v in os.environ.items() if not k.startswith('NGROK_')}
    env['NGROK_AUTHTOKEN'] = token
    with _lock:
        if _cancel.is_set():
            return None
        _job = Job()
        try:
            _process = subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        creationflags=subprocess.CREATE_NO_WINDOW | 4)
            _job.attach(_process)
        except Exception:
            _terminate()
            raise
        process = _process
    errors = []
    _codes = errors
    def read_errors():
        try:
            for line in iter(process.stdout.readline, b''):
                # 只提取公开错误编号，不保留原始日志（可能带授权内容）。
                code = re.search(rb'ERR_NGROK_\d+', line)
                if code:
                    errors.append(code[0].decode())
                    del errors[:-1]
        except (OSError, ValueError):
            pass
    threading.Thread(target=read_errors, name='prism-ngrok-log', daemon=True).start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(120):
        if _cancel.wait(.25):
            return None
        if errors:
            code = errors[0]
            hint = NGROK_HINTS.get(code)
            raise bridge.RouteError('ngrok 授权、额度或入口错误（' + code + '）'
                                    + ('：' + hint if hint else ''))
        if process.poll() is not None:
            raise bridge.RouteError('ngrok 启动失败')
        try:
            with opener.open('http://127.0.0.1:%d/api/tunnels' % port, timeout=.5) as response:
                tunnels = json.load(response).get('tunnels', [])
            for tunnel in tunnels:
                url = tunnel.get('public_url')
                if _url(url) and tunnel.get('config', {}).get('addr') == 'http://127.0.0.1:8319':
                    if fixed_url and url != fixed_url:
                        raise bridge.RouteError('ngrok 固定入口不匹配')
                    return url
        except (OSError, ValueError):
            pass
    raise bridge.RouteError('ngrok 连接超时')


def _terminate():
    global _job, _process
    if _job:
        _job.close()
        _job = None
    if _process:
        if _process.poll() is None:
            _process.terminate()  # 仅本次 Popen 的进程，不按名称批量结束。
        try:
            _process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _process.kill()
            _process.wait(timeout=5)
        if _process.stdout:
            _process.stdout.close()
        _process = None


def _run():
    global _error
    from . import cursor
    try:
        token = cursor._read('.local-secrets.json').get('cursor_ngrok_authtoken')
        if not token:
            raise bridge.RouteError('需要 ngrok 授权信息')
        models = cursor.candidates()
        state = cursor._state()
        if not any(m['source'] == state['source'] and m['model'] == state['model'] for m in models):
            raise bridge.RouteError('先选择 GPT 模型')
        executable = prepare()
        if _cancel.is_set():
            return
        cursor.install_rules()
        cursor.access_key({'action': 'copy'})
        with cursor._lock:
            if _cancel.is_set():
                return
            cursor.start_service()
        for attempt in range(3):
            try:
                url = _launch(executable, token, state['url'])
                if url is None:
                    return
                with cursor._lock, bridge._named_mutex():
                    if _cancel.is_set():
                        return
                    state = cursor._state()
                    state.update(enabled=True, url=url, revision=state['revision'] + 1)
                    cursor._write('cursor-settings.json', state)
                _error = ''
                _starting.clear()
                while not _cancel.wait(.5):
                    if _codes:
                        raise bridge.RouteError('ngrok 授权或额度错误（' + _codes[0] + '）')
                    if _process is None or _process.poll() is not None:
                        raise bridge.RouteError('ngrok 隧道中断')
                return
            except bridge.RouteError as exc:
                with _lock:
                    _terminate()
                _error = str(exc)
                if 'ERR_NGROK_' in _error or attempt == 2:
                    raise
                if _cancel.wait(2 * (attempt + 1)):
                    return
    except bridge.RouteError as exc:
        _error = str(exc)
    except Exception:
        _error = 'Cursor 隧道启动失败'
    finally:
        _starting.clear()
        with _lock:
            _terminate()
        cursor.stop(wait=False)


def start():
    global _worker, _error
    with _lock:
        if _worker and _worker.is_alive():
            if _cancel.is_set():
                raise bridge.RouteError('隧道正在停止')
            return
        _cancel.clear()
        _starting.set()
        _error = ''
        _worker = threading.Thread(target=_run, name='prism-cursor-tunnel', daemon=True)
        _worker.start()


def stop(wait=True):
    _cancel.set()
    _starting.clear()
    with _lock:
        _terminate()
    worker = _worker
    if wait and worker and worker is not threading.current_thread():
        worker.join(timeout=50)
