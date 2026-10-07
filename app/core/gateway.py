"""网关进程的启停。

server.py 和 main.py 都调这里。server 不能 import main：main 一加载就会拉起
webview。退出 Prism 不调用 stop_gateway，网关继续跑。控制页的总开关才会停，
而且只停身份为 ok 的进程；foreign / unknown 只把 note 交回去，不结束进程。
"""
from __future__ import annotations

import ctypes
import logging
import socket
import subprocess
import time

from . import identity
from .bridge import RouteError

_log = logging.getLogger('prism')


def port_open(host, port, timeout: float = 0.5) -> bool:
    """探这个地址上有没有进程在听。网关没有 /health，只能用 socket。"""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def is_prisms_address(host, port) -> bool:
    """是不是 config.yaml 里那个地址。指到别的端口时只探不拉。"""
    return (host, int(port)) == (identity.DEFAULT_GATEWAY_HOST, identity.DEFAULT_GATEWAY_PORT)


def status() -> dict:
    """给控制页：身份、一句说明、端口是否在听。"""
    who = identity.gateway_identity()
    state = who.get('state') or identity.IDENTITY_UNKNOWN
    listening = state != identity.IDENTITY_DOWN and port_open(
        identity.GATEWAY_HOST, identity.GATEWAY_PORT, 0.3)
    return {
        'state': state,
        'note': who.get('note') or '',
        'listening': bool(listening),
    }


def ensure_gateway(wait: bool = True) -> str:
    """确保网关端口在监听。返回 'running' / 'started' / 'starting' / 'foreign:说明' / 'failed:原因'。

    已经在听就不动。foreign 如实返回，不另起一个进程盖掉别人的网关。
    """
    host = identity.GATEWAY_HOST
    port = identity.GATEWAY_PORT
    if port_open(host, port, 0.15):
        who = identity.gateway_identity()
        _log.info('网关身份：' + str(who.get('note') or ''))
        if who.get('state') == identity.IDENTITY_FOREIGN:
            return 'foreign:' + str(who.get('note') or '')
        return 'running'
    if not is_prisms_address(host, port):
        return ('failed:PRISM_GATEWAY_BASE 指着 %s:%d，Prism 不代管这个地址'
                '（只有 config.yaml 里的 %s:%d 才由 Prism 拉）'
                % (host, port, identity.DEFAULT_GATEWAY_HOST, identity.DEFAULT_GATEWAY_PORT))
    exe = identity.GATEWAY_EXE
    config = identity.GATEWAY_CONFIG
    if not exe.is_file():
        return 'failed:找不到网关程序 ' + str(exe)
    if not config.is_file():
        return 'failed:找不到网关配置 ' + str(config)
    try:
        subprocess.Popen(
            [str(exe), '-config', str(config)],
            cwd=str(identity.ROOT),
            # 隐藏窗口：桌面应用弹一个黑框很难看，而且用户会手动去关它
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        return 'failed:拉起网关失败 ' + str(exc)
    if not wait:
        return 'starting'
    # 网关是 Go 程序，冷启动通常 1~3 秒；给 20 秒余量
    deadline = time.time() + 20
    while time.time() < deadline:
        if port_open(host, port, 0.2):
            return 'started'
        time.sleep(0.15)
    return 'failed:网关已拉起但 %d 秒内没有监听 %d 端口，去看 %s' % (
        20, port, identity.ROOT / 'logs' / 'main.log')


def terminate_pid(pid: int) -> None:
    """结束这一个进程。调用方必须已经确认身份是 ok。"""
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel32.TerminateProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(0x0001, False, int(pid))
    if not handle:
        raise RouteError('打不开进程 %d，没有停止它（Win32 %d）'
                         % (pid, ctypes.get_last_error()))
    try:
        if not kernel32.TerminateProcess(handle, 1):
            raise RouteError('结束进程 %d 失败（Win32 %d）'
                             % (pid, ctypes.get_last_error()))
    finally:
        kernel32.CloseHandle(handle)


def stop_gateway() -> dict:
    """只停身份为 ok 的进程。foreign / unknown 拒绝，不结束任何进程。

    端口上本来就没人时直接返回现状，不算失败。
    """
    who = identity.gateway_identity()
    state = who.get('state')
    note = who.get('note') or ''
    if state == identity.IDENTITY_DOWN:
        return {'state': state, 'note': note, 'listening': False}
    if state != identity.IDENTITY_OK:
        raise RouteError(note or '这个端口上的进程不是本目录的网关，没有停止它')
    pid = who.get('pid')
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise RouteError(note or '没有读到可以停止的进程')
    terminate_pid(pid)
    return status()


def set_on(on: bool) -> dict:
    """控制页总开关。开：已在听就不动；关：只停 ok。"""
    if on:
        result = ensure_gateway(wait=True)
        if result.startswith('foreign:'):
            raise RouteError(result[len('foreign:'):])
        if result.startswith('failed:'):
            raise RouteError(result[len('failed:'):])
        current = status()
        if current['state'] != identity.IDENTITY_OK:
            raise RouteError(current['note'] or '网关身份不是本目录')
        return current
    return stop_gateway()
