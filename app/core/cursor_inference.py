"""Cursor 专用 HTTP 边界：鉴权、转发、流式输出与取消，不持有选择状态。"""
from __future__ import annotations

import hmac
import http.client
import json
import select
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import bridge

MODEL = 'prism-cursor'
PORT = 8319
MAX_BODY = 16 * 1024 * 1024


def rewrite_model(value, model):
    if isinstance(value, dict):
        # 不递归进工具参数或用户内容，保留其原始语义。
        if 'model' in value:
            value['model'] = model
        if isinstance(value.get('response'), dict) and 'model' in value['response']:
            value['response']['model'] = model
    return value


class InferenceServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, *args, profile, read_secrets, model_name=lambda: MODEL):
        self.profile = profile
        self.read_secrets = read_secrets
        self.model_name = model_name
        self.slots = threading.BoundedSemaphore(32)
        self.connections = set()
        self.connections_lock = threading.Lock()
        self.stopping = threading.Event()
        super().__init__(*args)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self.connections_lock:
            self.connections.add(request)
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            with self.connections_lock:
                self.connections.discard(request)
            self.slots.release()

    def close_connections(self):
        self.stopping.set()
        with self.connections_lock:
            sockets = list(self.connections)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    timeout = 30

    def log_message(self, *_):
        pass  # 不记录公网请求、凭据或请求体。

    def _json(self, status, value):
        raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def _fail(self, status, message):
        self._json(status, {'error': {'message': message, 'type': 'prism_cursor_error'}})

    def _auth(self):
        expected = self.server.read_secrets().get('cursor_access_key')
        got = self.headers.get('Authorization', '')
        return bool(expected) and hmac.compare_digest(got.encode(), ('Bearer ' + expected).encode())

    def do_GET(self):
        try:
            if not self._auth():
                return self._fail(401, '凭据无效')
            if self.path != '/v1/models':
                return self._fail(404, '接口不存在')
            self._json(200, {'object': 'list', 'data': [{'id': self.server.model_name(), 'object': 'model', 'owned_by': 'prism'}]})
        except bridge.RouteError:
            self._fail(409, 'Cursor 凭据不可用')
        except OSError:
            self.close_connection = True

    def do_POST(self):
        connection = None
        upstream_socket = None
        finished = threading.Event()
        headers_sent = False
        self.close_connection = True
        try:
            if not self._auth():
                return self._fail(401, '凭据无效')
            if self.path not in ('/v1/chat/completions', '/v1/responses'):
                return self._fail(404, '接口不存在')
            if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
                return self._fail(411, '需要唯一 Content-Length')
            try:
                length = int(self.headers.get('Content-Length', ''))
            except ValueError:
                return self._fail(400, 'Content-Length 无效')
            if length <= 0 or length > MAX_BODY:
                return self._fail(413 if length > MAX_BODY else 400, '请求体长度无效')
            raw = self.rfile.read(length)
            if len(raw) != length:
                return self._fail(400, '请求体不完整')
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                return self._fail(400, 'JSON 无效')
            if (not isinstance(body, dict) or not isinstance(body.get('model', MODEL), str)
                    or len(body.get('model', MODEL)) > 256):
                return self._fail(400, '请求格式无效')
            profile = self.server.profile()
            # 每条请求获取一次最新快照；已执行的请求不随之后的保存发生漂移。
            client_model = body.get('model') or profile.get('model_name', MODEL)
            body['model'] = profile['gateway_model']
            for key in ('reasoning', 'reasoning_effort', 'thinking'):
                body.pop(key, None)
            level = profile['level']
            # 等级只交给匹配头的最终 payload 覆盖；入口字段会先被网关校验/降档。
            local = self.server.read_secrets().get('api_key')
            if not local:
                raise bridge.RouteError('本地推理凭据缺失')
            target = urlsplit(bridge.GATEWAY_BASE)
            if target.scheme != 'http' or target.hostname != '127.0.0.1':
                raise bridge.RouteError('网关必须为本地 HTTP 入口')
            connection = http.client.HTTPConnection(target.hostname, target.port, timeout=600)
            headers = {'Authorization': 'Bearer ' + local, 'Content-Type': 'application/json',
                       'X-Prism-Client': 'cursor', 'Accept-Encoding': 'identity'}
            if level:
                headers['X-Prism-Reasoning-Effort'] = level
            with self.server.connections_lock:
                if self.server.stopping.is_set():
                    return
                connection.connect()
                upstream_socket = connection.sock
                self.server.connections.add(upstream_socket)
            def cancel_on_disconnect():
                while not finished.wait(.2):
                    try:
                        readable, _, _ = select.select([self.connection], [], [], .2)
                        if readable and not self.connection.recv(1, socket.MSG_PEEK):
                            upstream_socket.shutdown(socket.SHUT_RDWR)
                            return
                    except OSError:
                        try:
                            upstream_socket.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        return
            threading.Thread(target=cancel_on_disconnect, name='prism-cursor-cancel', daemon=True).start()
            connection.request('POST', self.path, json.dumps(body).encode(), headers)
            response = connection.getresponse()
            if response.status >= 400:
                # 网关错误可能包含实际模型/来源或请求内容，不向公网泄露。
                return self._fail(response.status, '网关拒绝请求')
            if 'text/event-stream' in response.getheader('Content-Type', ''):
                self.send_response(response.status)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Connection', 'close')
                self.end_headers()
                headers_sent = True
                # 一行一行传递，无整段缓存；只改 SSE data 的协议模型字段。
                while True:
                    line = response.readline(MAX_BODY + 1)
                    if not line:
                        break
                    if len(line) > MAX_BODY:
                        raise bridge.RouteError('SSE 单行过大')
                    if line.startswith(b'data:'):
                        data = line[5:].strip()
                        if data and data != b'[DONE]':
                            try:
                                line = b'data: ' + json.dumps(rewrite_model(json.loads(data), client_model),
                                                              ensure_ascii=False).encode() + b'\n'
                            except ValueError:
                                pass
                    self.wfile.write(line)
                    self.wfile.flush()
            else:
                raw = response.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    raise bridge.RouteError('网关响应过大')
                self._json(response.status, rewrite_model(json.loads(raw), client_model))
        except bridge.RouteError as exc:
            if not headers_sent:
                self._fail(409, str(exc))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except (OSError, ValueError, http.client.HTTPException):
            if not headers_sent:
                try:
                    self._fail(502, '网关连接中断或响应无效')
                except OSError:
                    pass
        finally:
            finished.set()
            if upstream_socket:
                with self.server.connections_lock:
                    self.server.connections.discard(upstream_socket)
            if connection:
                connection.close()

    def do_PUT(self):
        self._fail(404, '接口不存在')

    do_DELETE = do_PUT
    do_PATCH = do_PUT
    do_OPTIONS = do_PUT


