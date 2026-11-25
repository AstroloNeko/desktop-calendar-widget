"""Firebase SSE reader. A notification can only wake the existing queue worker."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import logging
import threading
import time
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from desktop_calendar.remote_cloud import AccountMismatch, NotificationSession, firebase_origin, valid_account
from desktop_calendar.remote_transport import StatusError, Unavailable


MAX_EVENT_BYTES = 64 * 1024
READ_TIMEOUT = 45
STABLE_CONNECTION = 30
MAX_REDIRECTS = 3
AUTH_REJECTED = 'auth_rejected'
ACCOUNT_MISMATCH = 'account_mismatch'


@dataclass(frozen=True)
class VerifiedAccount:
    account_id: str = field(repr=False)


def sse_events(chunks):
    """Split byte streams on SSE lines and dispatch only on an empty line."""
    line = bytearray()
    name, data = b'', []
    size = 0
    first_line, after_cr = True, False
    for chunk in chunks:
        for byte in chunk:
            if after_cr and byte == 10:
                after_cr = False
                continue
            after_cr = byte == 13
            if byte not in (10, 13):
                line.append(byte)
                if len(line) + size > MAX_EVENT_BYTES:
                    raise Unavailable
                continue
            raw = bytes(line)
            line.clear()
            if first_line:
                raw = raw.removeprefix(b'\xef\xbb\xbf')
                first_line = False
            if not raw:
                if data:
                    yield name or b'message', b'\n'.join(data)
                name, data, size = b'', [], 0
                continue
            field, _, value = raw.partition(b':')
            value = value.removeprefix(b' ')
            if field == b'event':
                name = value
            elif field == b'data':
                size += len(value) + 1
                if size > MAX_EVENT_BYTES:
                    raise Unavailable
                data.append(value)


@contextmanager
def private_request_logging():
    # urllib3 can log the request target (including Firebase's auth query).
    owner = threading.get_ident()
    loggers = [logging.getLogger(name) for name in ('urllib3.connectionpool', 'urllib3.util.retry')]
    filter_thread = lambda record: record.thread != owner
    for logger in loggers:
        logger.addFilter(filter_thread)
    try:
        yield
    finally:
        for logger in loggers:
            logger.removeFilter(filter_thread)


@contextmanager
def open_notification_stream(session, credentials, stop, deadline):
    url = credentials.stream_url
    original = urlsplit(url)
    original_origin = firebase_origin(f'{original.scheme}://{original.netloc}')
    namespace = original.hostname.split('.')[0]
    params = {'auth': credentials.id_token}
    for _ in range(MAX_REDIRECTS + 1):
        remaining = deadline - time.monotonic()
        if stop.is_set() or remaining <= 0:
            yield None
            return
        with session.get(url, params=params, headers={'Accept': 'text/event-stream', 'Accept-Encoding': 'identity'},
                         stream=True, allow_redirects=False, verify=True,
                         timeout=(min(10, remaining), min(READ_TIMEOUT, remaining))) as response:
            if response.status_code not in (301, 302, 307, 308):
                yield response
                return
            location = response.headers.get('Location')
            if not location:
                raise Unavailable
            redirected = urlsplit(urljoin(url, location))
            target_origin = firebase_origin(f'{redirected.scheme}://{redirected.netloc}')
            routing = parse_qs(redirected.query, max_num_fields=16).get('ns')
            if (redirected.fragment or unquote(redirected.path) != unquote(original.path)
                    or routing not in (None, [namespace])
                    or target_origin != original_origin and routing != [namespace]):
                raise Unavailable
            # Rebuild the query so Location cannot replace the ID token or add parameters.
            url = target_origin + original.path
            params = {'auth': credentials.id_token, **({'ns': namespace} if routing else {})}
    raise Unavailable


def stream_notifications(credentials, stop, ready, wake, *, requests_module=None):
    try:
        if requests_module is None:
            import requests as requests_module
    except ImportError:
        raise Unavailable from None

    deadline = credentials.refresh_at if credentials.refresh_at is not None else credentials.expires_at
    if stop.is_set() or time.monotonic() >= deadline:
        return False
    try:
        with private_request_logging(), requests_module.Session() as session:
            session.trust_env = False
            with open_notification_stream(session, credentials, stop, deadline) as response:
                if response is None:
                    return not stop.is_set()
                media_type = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
                if response.status_code != 200 or media_type != 'text/event-stream':
                    raise Unavailable

                def chunks():
                    # A one-byte read allows cancellation and expiry checks even on partial events.
                    for chunk in response.iter_content(chunk_size=1):
                        if stop.is_set() or time.monotonic() >= deadline:
                            break
                        yield chunk

                online = False
                for name, payload in sse_events(chunks()):
                    if name in (b'cancel', b'auth_revoked'):
                        break
                    if name not in (b'put', b'patch'):
                        continue
                    update = json.loads(payload)
                    if not isinstance(update, dict) or not isinstance(update.get('path'), str) or 'data' not in update:
                        raise Unavailable
                    if not online:
                        online = True
                        ready()
                    else:
                        wake()
    except requests_module.exceptions.RequestException:
        if time.monotonic() >= deadline and not stop.is_set():
            return True
        raise Unavailable from None
    except (ValueError, OSError, RecursionError):
        raise Unavailable from None
    return not stop.is_set() and time.monotonic() >= deadline


class NotificationListener:
    """Reuse a single listener thread across address and credential changes."""

    def __init__(self, contract, client_factory, changed, *, stream=stream_notifications, retry=(1, 30)):
        self.contract, self.client_factory, self.changed = contract, client_factory, changed
        self.stream, self.retry = stream, retry
        self.condition = threading.Condition()
        self.active = None
        self.thread = None
        self.stopped = False

    def configure(self, generation, url=None, token=None, account=''):
        with self.condition:
            if self.active:
                self.active[-1].set()
            self.active = (generation, url, token, account, threading.Event()) if url and not self.stopped else None
            if self.active and self.thread is None:
                self.thread = threading.Thread(target=self._run, name='calendar-wechat-notifications', daemon=True)
                self.thread.start()
            self.condition.notify_all()

    def stop(self):
        with self.condition:
            self.stopped = True
            self.configure(None)

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.stopped or self.active is not None)
                if self.stopped:
                    return
                generation, url, token, account, stop = self.active
            delay = self.retry[0]

            def ready():
                nonlocal connected_at
                connected_at = time.monotonic()
                if not stop.is_set():
                    self.changed(generation, True)

            def wake():
                if not stop.is_set():
                    self.changed(generation, True)

            while not stop.is_set():
                connected_at = None
                try:
                    client = self.client_factory(url)
                    if stop.is_set():
                        break
                    try:
                        credentials = self.contract.notification_session(client, token, account)
                    except (AccountMismatch, StatusError) as error:
                        if isinstance(error, StatusError) and error.status not in (401, 403):
                            raise
                        if not stop.is_set():
                            reason = ACCOUNT_MISMATCH if isinstance(error, AccountMismatch) else AUTH_REJECTED
                            self.changed(generation, reason)
                        with self.condition:
                            if self.active and self.active[-1] is stop:
                                self.active = None
                            stop.set()
                        break
                    if (not isinstance(credentials, NotificationSession) or credentials.id_token == token
                            or token in unquote(credentials.stream_url) or not valid_account(credentials.account_id)):
                        raise ValueError('通知会话格式无效。')
                    if stop.is_set():
                        break
                    if self.changed(generation, VerifiedAccount(credentials.account_id)) is False:
                        raise Unavailable
                    if not stop.is_set() and self.stream(credentials, stop, ready, wake):
                        delay = self.retry[0]
                        continue
                except Exception:
                    # Requests and the cloud adapter can include credentials in exceptions.
                    # Disconnect and back off without printing the underlying error.
                    pass
                finally:
                    if not stop.is_set():
                        self.changed(generation, False)
                if connected_at is not None and time.monotonic() - connected_at >= STABLE_CONNECTION:
                    delay = self.retry[0]
                if stop.wait(delay):
                    break
                delay = min(self.retry[1], delay * 2)
