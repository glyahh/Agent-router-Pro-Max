r"""网关身份核对：这个端口上跑的，到底是不是本目录这套网关。

**端口在监听 ≠ 这个端口上跑的是本目录这套配置。** CLIProxyAPI 的网关是独立进程，
谁坐着 8317 跟 Prism 装在哪没关系。实测本机 8317 上坐的就是另一个部署：

    D:\My_Agent_Proxy\cli-proxy-api.exe -config D:\My_Agent_Proxy\config.yaml

那时候监控页显示的来源、auth-files 全是**那边**的；点「清空日志」删的也是那边的
日志，本目录一个字节不动。所以端口探通之后必须再核一次身份，核不上就得说出来。

身份只认命令行里的 `-config`：那才是网关实际读的那份配置。exe 路径只能当退路
——同一个 exe 可以被拷到别处、配着另一份 config.yaml 跑。

实现全是纯 ctypes，**只依赖标准库**：

    GetExtendedTcpTable  → 端口占用 PID
    OpenProcess + QueryFullProcessImageNameW → exe 路径
    NtQueryInformationProcess → PEB → RTL_USER_PROCESS_PARAMETERS → CommandLine

不用 WMI / PowerShell（要起进程，约 1 秒，还会闪窗口），不用 psutil（.venv 里没装，
为一个诊断功能加依赖还得改 build.bat）。

**这个模块不许 import webview / pystray**：托盘那边（main.py）用它，控制台的
监控页（core/health.py）也用它，后者跑在 WebView 之外，拖进 GUI 依赖会当场炸。
"""

from __future__ import annotations

import ctypes
import os
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core import bridge  # noqa: E402

ROOT = bridge.ROOT                          # D:\MY_DESIGN\Agent-router-Pro-Max
GATEWAY_EXE = ROOT / 'cli-proxy-api.exe'
GATEWAY_CONFIG = ROOT / 'config.yaml'

# 网关地址认 core/* 共用的那个开关。main.py 与 health.py 各算各的那份解析从
# 2026-09-28 起收敛到这里，两边算出同一个 host:port，不会再出现"页面上写着网关
# 在线、托盘标题写着未运行"的同机两说。解析方式与原 health.py:38-40 逐字一致。
DEFAULT_GATEWAY_HOST = '127.0.0.1'
DEFAULT_GATEWAY_PORT = 8317
_gateway_base = urlparse(os.environ.get(
    'PRISM_GATEWAY_BASE',
    'http://%s:%d' % (DEFAULT_GATEWAY_HOST, DEFAULT_GATEWAY_PORT)))
GATEWAY_HOST = _gateway_base.hostname or DEFAULT_GATEWAY_HOST
GATEWAY_PORT = _gateway_base.port or DEFAULT_GATEWAY_PORT

AF_INET = 2
TCP_TABLE_OWNER_PID_LISTENER = 3
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_VM_READ = 0x0010

IDENTITY_OK = 'ok'              # 就是本目录的配置
IDENTITY_FOREIGN = 'foreign'    # 端口在听，但跑的是别人的配置
IDENTITY_UNKNOWN = 'unknown'    # 端口在听，身份读不出来——不许当成本目录的
IDENTITY_DOWN = 'down'          # 端口没人听

_CONFIG_FLAGS = ('-config', '--config')


class _MibTcpRowOwnerPid(ctypes.Structure):
    """MIB_TCPROW_OWNER_PID（iphlpapi.h）。24 字节，字段全是 DWORD。"""

    _fields_ = [('dwState', ctypes.c_uint32),
                ('dwLocalAddr', ctypes.c_uint32),
                ('dwLocalPort', ctypes.c_uint32),
                ('dwRemoteAddr', ctypes.c_uint32),
                ('dwRemotePort', ctypes.c_uint32),
                ('dwOwningPid', ctypes.c_uint32)]


class _UnicodeString(ctypes.Structure):
    _fields_ = [('Length', ctypes.c_uint16),
                ('MaximumLength', ctypes.c_uint16),
                ('Buffer', ctypes.c_void_p)]


class _ProcessBasicInformation(ctypes.Structure):
    _fields_ = [('Reserved1', ctypes.c_void_p),
                ('PebBaseAddress', ctypes.c_void_p),
                ('Reserved2', ctypes.c_void_p * 2),
                ('UniqueProcessId', ctypes.c_void_p),
                ('Reserved3', ctypes.c_void_p)]


# 读取目标进程 PEB 时用到的两个固定偏移（x64）。目标与本进程同为 64 位时成立；
# 对不上（32 位网关、提权、受保护进程）时 ReadProcessMemory 会失败，调用方回落到
# exe 路径，绝不猜。
_PEB_PROCESS_PARAMETERS = 0x20
_PARAMS_COMMAND_LINE = 0x70


def _port_owner_pids(port: int) -> list[int]:
    """谁在听这个端口。GetExtendedTcpTable，纯 ctypes，实测单次 0.4 毫秒。

    不走 `Get-NetTCPConnection`：那条路要起一个 PowerShell（约 1 秒，还会闪一下
    窗口），而托盘每 5 秒就要刷一次状态。
    """
    iphlpapi = ctypes.WinDLL('iphlpapi', use_last_error=True)
    size = ctypes.c_uint32(0)
    iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, AF_INET,
                                 TCP_TABLE_OWNER_PID_LISTENER, 0)
    if not size.value:
        return []
    buf = ctypes.create_string_buffer(size.value)
    if iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, AF_INET,
                                    TCP_TABLE_OWNER_PID_LISTENER, 0) != 0:
        return []
    count = ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint32))[0]
    row_size = ctypes.sizeof(_MibTcpRowOwnerPid)
    base = ctypes.addressof(buf) + ctypes.sizeof(ctypes.c_uint32)
    pids = []
    for i in range(count):
        row = _MibTcpRowOwnerPid.from_address(base + i * row_size)
        # dwLocalPort 是网络字节序（文档原话）。小端机上不 ntohs 的话，8317 读出来
        # 是 0x7D20，一个都匹配不上——这个坑当场踩过。
        if socket.ntohs(row.dwLocalPort & 0xFFFF) == port:
            pids.append(int(row.dwOwningPid))
    return pids


def _process_image_path(pid: int) -> str | None:
    """进程的 exe 全路径。读不到返回 None。"""
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    k32.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        size = ctypes.c_uint32(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if not k32.QueryFullProcessImageNameW(ctypes.c_void_p(handle), 0, buf,
                                              ctypes.byref(size)):
            return None
        return buf.value or None
    finally:
        k32.CloseHandle(ctypes.c_void_p(handle))


def _process_command_line(pid: int) -> str | None:
    """进程的完整命令行。读不到返回 None。

    走 PEB：NtQueryInformationProcess → PEB → RTL_USER_PROCESS_PARAMETERS →
    CommandLine。不用 WMI，因为那要起 PowerShell；不用 psutil，因为 .venv 里没有
    它，为一个诊断功能给它加依赖、还得改 build.bat，不划算。
    """
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    ntdll = ctypes.WinDLL('ntdll', use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    k32.CloseHandle.argtypes = (ctypes.c_void_p,)
    k32.ReadProcessMemory.argtypes = (ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_void_p, ctypes.c_size_t,
                                      ctypes.POINTER(ctypes.c_size_t))
    handle = k32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_VM_READ, False, int(pid))
    if not handle:
        return None
    handle = ctypes.c_void_p(handle)

    def read(addr, buf):
        """往 buf 里读。必须 byref：c_void_p 按值传过去是 NULL，写不进去还报成功。"""
        got = ctypes.c_size_t(0)
        ok = k32.ReadProcessMemory(handle, ctypes.c_void_p(addr),
                                   ctypes.byref(buf), ctypes.sizeof(buf),
                                   ctypes.byref(got))
        return bool(ok) and got.value == ctypes.sizeof(buf)

    try:
        basic = _ProcessBasicInformation()
        if ntdll.NtQueryInformationProcess(handle, 0, ctypes.byref(basic),
                                           ctypes.sizeof(basic), None) != 0:
            return None
        params = ctypes.c_void_p()
        if not read(basic.PebBaseAddress + _PEB_PROCESS_PARAMETERS, params):
            return None
        text = _UnicodeString()
        if not read(params.value + _PARAMS_COMMAND_LINE, text):
            return None
        if not text.Length or not text.Buffer:
            return None
        buf = ctypes.create_unicode_buffer(text.Length // 2)
        if not read(text.Buffer, buf):
            return None
        return buf.value or None
    finally:
        k32.CloseHandle(handle)


def _config_from_command_line(command: str) -> str | None:
    """从命令行里挑出 -config 的值。没写就返回 None。

    命令行是 Windows 拼好的**一个字符串**，不是 argv：路径带空格时会被引号包住，
    直接 split() 会把它劈成两半。所以自己按引号切。网关用的是 Go 的 flag 包，
    `-config X` 和 `-config=X` 两种写法都认。
    """
    tokens = []
    current = []
    quoted = False
    for ch in command:
        if ch == '"':
            quoted = not quoted
        elif ch in ' \t' and not quoted:
            if current:
                tokens.append(''.join(current))
                current = []
        else:
            current.append(ch)
    if current:
        tokens.append(''.join(current))
    for index, token in enumerate(tokens):
        lowered = token.lower()
        for flag in _CONFIG_FLAGS:
            if lowered == flag:
                return tokens[index + 1] if index + 1 < len(tokens) else None
            if lowered.startswith(flag + '='):
                return token[len(flag) + 1:] or None
    return None


def _same_path(left, right) -> bool:
    """两个路径指不指同一个文件。Windows 上大小写不敏感，分隔符还正反混用。"""
    try:
        norm = lambda p: os.path.normcase(os.path.abspath(str(p)))   # noqa: E731
        return norm(left) == norm(right)
    except (OSError, ValueError):
        return False


def _identify_pid(pid: int, port: int) -> dict:
    image = _process_image_path(pid)
    command = _process_command_line(pid)
    config = _config_from_command_line(command) if command else None
    found = {'pid': pid, 'config': config, 'image': image}
    if config:
        if _same_path(config, GATEWAY_CONFIG):
            return dict(found, state=IDENTITY_OK,
                        note='%d 上的网关读的是本目录的 %s' % (port, GATEWAY_CONFIG))
        return dict(found, state=IDENTITY_FOREIGN,
                    note='%d 上跑的不是本目录的配置：它读的是 %s，本目录的是 %s'
                         % (port, config, GATEWAY_CONFIG))
    if image and _same_path(image, GATEWAY_EXE):
        return dict(found, state=IDENTITY_UNKNOWN,
                    note='%d 上占着端口的就是本目录的 %s，但命令行里没读到 -config，'
                         '没法确认它读的是哪份配置' % (port, GATEWAY_EXE))
    if command:
        # 命令行读到了，只是里面没写 -config。这种情况下网关会去读同目录的
        # config.yaml，但"同目录"是**那个进程的**目录，不一定是本目录，所以不能算 ok。
        return dict(found, state=IDENTITY_UNKNOWN,
                    note='%d 上占着端口的是 %s（PID %d），它的命令行里没写 -config，'
                         '身份没法核实' % (port, image or '未知程序', pid))
    return dict(found, state=IDENTITY_UNKNOWN,
                note='%d 上占着端口的是 %s（PID %d），但进程信息读不出来'
                     '（提权、受保护或位数不同），身份没法核实'
                     % (port, image or '未知程序', pid))


def gateway_identity(port: int | None = None) -> dict:
    """这个端口上跑的，到底是不是本目录的网关。

    返回 {'state', 'pid', 'config', 'image', 'note'}；note 是一句能直接摆到界面上
    给用户看的话。四个状态：端口没人听 = down；-config 就是本目录的 config.yaml
    = ok；读得出 -config 但指的不是本目录 = foreign；端口在听、进程信息却读不出来
    （提权、受保护、跨位数）= unknown。

    **unknown 不许说成 ok**——"端口在听就当成本目录的网关"正是这次要修的毛病。

    **这个函数不带缓存**：托盘每 5 秒调一次，要的就是"此刻"的结论。要缓存请用
    gateway_identity_cached()。
    """
    port = GATEWAY_PORT if port is None else int(port)
    pids = _port_owner_pids(port)
    if not pids:
        return {'state': IDENTITY_DOWN, 'pid': None, 'config': None, 'image': None,
                'note': '%d 端口上没有进程在听' % port}
    found = [_identify_pid(pid, port) for pid in pids]
    # 一个端口上不止一个 PID 是可能的（SO_REUSEADDR 那套）。哪个说法更该让用户
    # 看见就选哪个：foreign > unknown > ok。
    for wanted in (IDENTITY_FOREIGN, IDENTITY_UNKNOWN, IDENTITY_OK):
        for item in found:
            if item['state'] == wanted:
                return item
    return found[0]


# ---------------------------------------------------------------- 带缓存的入口
#
# 给监控页用。监控页每 5 秒（壳）/ 10 秒（独立打开）轮询一次 /api/monitor。
#
# **先说成本，因为结论是"其实不需要缓存"**：本机实测（8317 上真坐着另一个部署的
# 进程，也就是最坏情况——读得到 PEB、拿得到命令行），40 次 gateway_identity() 的
# 耗时 min 0.283 / p50 0.333 / p95 0.800 / max 1.085 毫秒。0.4 毫秒 × 每 5 秒一次
# 完全可以忽略，裸调不会给轮询加出任何可感知的负担。
#
# 加缓存是冲着另一件事：这个函数**不是纯函数**，它读的是别的进程的内存。网关正在
# 退出、或用户刚换掉配置的那几秒里，同一秒内连打两次可能一次读得到 PEB、一次读不到
# ——页面上那一格就会先闪成 unknown、再闪回来。5 秒 TTL 让同一轮轮询里所有读
# gateway 字典的地方看到同一个说法。
#
# TTL 与壳的轮询周期对齐：正常一轮一次，等于没缓存；省下的是"同一轮里被问第二次"。
IDENTITY_TTL_SEC = 5.0
_identity_cache: dict = {'at': 0.0, 'port': None, 'value': None}


def gateway_identity_cached(port: int | None = None,
                            ttl: float = IDENTITY_TTL_SEC) -> dict:
    """gateway_identity() 的 TTL 缓存版。给高频轮询的展示层用。

    读不到身份时同样返回 unknown，**绝不当成 ok**；这个函数不抛异常（调用方是
    HTTP 处理线程，抛出去就是 500，一个诊断字段不值得）。
    """
    port = GATEWAY_PORT if port is None else int(port)
    now = time.monotonic()
    cached = _identity_cache
    if (cached['value'] is not None and cached['port'] == port
            and now - cached['at'] < ttl):
        return cached['value']
    try:
        value = gateway_identity(port)
    except Exception:                       # noqa: BLE001 - 展示层要的是"读不到"
        value = {'state': IDENTITY_UNKNOWN, 'pid': None, 'config': None,
                 'image': None,
                 'note': '%d 上跑的是什么，进程信息读不出来，身份没法核实' % port}
    cached['at'] = now
    cached['port'] = port
    cached['value'] = value
    return value


def identity_config(identity: dict | None) -> str | None:
    """从核对结果里取"那个网关实际读的配置路径"。没读到就是 None。

    监视页要单独显示这个路径，不希望它去理解 state 的语义。
    """
    if not isinstance(identity, dict):
        return None
    config = identity.get('config')
    return str(config) if config else None


if __name__ == '__main__':      # 手动看一眼真实输出：python core/identity.py
    import json
    import timeit
    result = gateway_identity()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print('本目录配置  : %s' % GATEWAY_CONFIG)
    print('单次耗时    : %.3f ms' % (timeit.timeit(gateway_identity, number=20) / 20 * 1000))
