"""Opt-in loopback-only authenticated adapter. No Tk/Store references here."""
from collections import deque
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import threading
import time
from urllib.parse import urlsplit, parse_qs

from desktop_calendar.assistant.calendar_chat_protocol import OWNER, ChatError, envelope


class LocalChatHTTP:
    def __init__(self, service, token, port=18746, host='127.0.0.1'):
        if host != '127.0.0.1' or not isinstance(token, str) or len(token) < 32:
            raise ValueError('本机API只能监听127.0.0.1，且必须使用足够随机的Token。')
        self.service, self.token = service, token
        self.port, self.server, self.thread = port, None, None
        self.rate, self.rate_lock = deque(), threading.Lock()
        self.requests = threading.Condition()
        self.active_requests = 0

    def start(self):
        if self.server:
            return
        adapter = self
        class Server(ThreadingHTTPServer):
            daemon_threads = True
            block_on_close = False
            allow_reuse_address = False
            permits = threading.BoundedSemaphore(8)

            def process_request(self, request, address):
                if not self.permits.acquire(blocking=False):
                    request.close()
                    return
                try:
                    with adapter.requests:
                        adapter.active_requests += 1
                    super().process_request(request, address)
                except Exception:
                    with adapter.requests:
                        adapter.active_requests -= 1
                        adapter.requests.notify_all()
                    self.permits.release()
                    raise

            def process_request_thread(self, request, address):
                try:
                    super().process_request_thread(request, address)
                finally:
                    self.permits.release()
                    with adapter.requests:
                        adapter.active_requests -= 1
                        adapter.requests.notify_all()

            def handle_error(self, request, client_address):
                pass  # Never dump request data, tokens or service traceback to stderr/logs.

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def reply(self, code, payload):
                raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
                self.send_response(code)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                self.wfile.write(raw)

            def do_OPTIONS(self):
                self.reply(403, envelope(status='failed', error_code='authentication_failed'))

            def do_POST(self):
                self.route('POST')

            def do_GET(self):
                self.route('GET')

            def do_DELETE(self):
                self.route('DELETE')

            def route(self, method):
                try:
                    host = self.headers.get('Host', '')
                    if host != f'127.0.0.1:{adapter.port}' or self.headers.get('Origin') or self.headers.get('Sec-Fetch-Site'):
                        raise ChatError('authentication_failed', '拒绝浏览器跨站请求或无效Host。')
                    with adapter.rate_lock:
                        now = time.monotonic()
                        while adapter.rate and adapter.rate[0] < now-60:
                            adapter.rate.popleft()
                        if len(adapter.rate) >= 120:
                            self.reply(429, envelope(status='failed', error_code='rate_limited'))
                            return
                        adapter.rate.append(now)
                    parsed = urlsplit(self.path)
                    if parsed.path == '/v1/health' and method == 'GET':
                        self.reply(200, {'status': 'ok', 'api_version': 1})
                        return
                    if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer '+adapter.token):
                        raise ChatError('authentication_failed', '本机API认证失败。')
                    body = {}
                    if method == 'POST':
                        if self.headers.get('Transfer-Encoding') or self.headers.get_content_type() != 'application/json':
                            raise ChatError('invalid_arguments', '请使用JSON请求体。')
                        length = int(self.headers.get('Content-Length', '-1'))
                        if not 0 < length <= 16384:
                            self.reply(413, envelope(status='failed', error_code='request_too_large'))
                            return
                        raw = self.rfile.read(length)
                        if len(raw) != length:
                            raise ChatError('invalid_arguments', '请求体不完整。')
                        body = json.loads(raw)
                        if type(body) is not dict:
                            raise ChatError('invalid_arguments', '请求体必须为JSON对象。')
                    if parsed.path == '/v1/sessions' and method == 'POST':
                        if body:
                            raise ChatError('invalid_arguments', '此接口不接受sender_id或身份字段。')
                        result = adapter.service.new_session(OWNER)
                    elif parsed.path == '/v1/chat/messages' and method == 'POST':
                        if set(body) != {'session_id', 'message', 'request_id'}:
                            raise ChatError('invalid_arguments', '消息字段不正确。')
                        result = adapter.service.send_message(owner=OWNER, source='loopback-http', **body)
                    else:
                        match = re.fullmatch(r'/v1/(sessions|operations)/([A-Za-z0-9_-]{16,64})(?:/(messages|pending|confirm|cancel))?', parsed.path)
                        if not match:
                            self.reply(404, envelope(status='failed', error_code='not_found'))
                            return
                        kind, identifier, action = match.groups()
                        if kind == 'sessions' and method == 'GET':
                            if action == 'pending':
                                result = adapter.service.get_pending_operations(identifier)
                            elif action in ('messages', None):
                                result = adapter.service.get_history(identifier)
                            else:
                                raise ChatError('invalid_arguments', '无效路由。')
                        elif kind == 'sessions' and method == 'DELETE' and action is None:
                            result = adapter.service.clear_history(identifier)
                        elif kind == 'operations':
                            if method == 'GET' and action is None:
                                params = parse_qs(parsed.query, strict_parsing=True)
                                if set(params) != {'session_id'} or len(params['session_id']) != 1:
                                    raise ChatError('invalid_arguments', '需要唯一session_id。')
                                result = adapter.service.get_operation_result(params['session_id'][0], identifier)
                            elif method == 'POST' and action == 'confirm' and set(body) == {'session_id', 'revision'}:
                                if not isinstance(body['revision'], str) or not body['revision']:
                                    raise ChatError('invalid_arguments', '需要当前预览的 revision。')
                                result = adapter.service.confirm_operation(body['session_id'], identifier, source='loopback-http', revision=body['revision'])
                            elif method == 'POST' and action == 'cancel' and set(body) == {'session_id'}:
                                result = adapter.service.cancel_operation(body['session_id'], identifier, source='loopback-http')
                            else:
                                raise ChatError('invalid_arguments', '无效操作。')
                        else:
                            raise ChatError('invalid_arguments', '无效路由。')
                    self.reply(200, result)
                except ChatError as error:
                    self.reply(401 if error.code == 'authentication_failed' else 400,
                               envelope(status='failed', error_code=error.code, error_message=str(error)))
                except (ValueError, TypeError, KeyError, TimeoutError):
                    self.reply(400, envelope(status='failed', error_code='invalid_arguments', error_message='请求格式或参数无效。'))
                except Exception:
                    self.reply(503, envelope(status='failed', error_code='service_unavailable', error_message='本机服务暂不可用。'))
        self.server = Server(('127.0.0.1', self.port), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .25}, daemon=True, name='calendar-chat-http')
        self.thread.start()

    def stop(self, *, wait_requests=False, timeout=10):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
            self.server = None
        if wait_requests:
            with self.requests:
                if not self.requests.wait_for(lambda: self.active_requests == 0, timeout):
                    raise ChatError('service_unavailable', 'HTTP请求尚未结束，关闭未完成。')
