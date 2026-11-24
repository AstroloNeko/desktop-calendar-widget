"""HTTPS transport shared by the Windows WeChat worker and acceptance probe."""

from http.client import HTTPException
import json
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, HTTPSHandler, Request


INTERVAL = 15
SAFETY_CHECK_MINUTES = (0, 10, 30, 60)
TIMEOUT = 10
MAX_RESPONSE = 256 * 1024
CLAIM_PATH = "/api/jobs/claim"


def normalize_url(url):
    if not isinstance(url, str):
        raise ValueError("服务器地址必须是 HTTPS 服务根地址。")
    try:
        parts = urlsplit(url)
        port = parts.port
        valid = (parts.scheme == "https" and parts.hostname and
                 parts.username is None and port != 0 and
                 parts.path in ("", "/") and not parts.query and not parts.fragment)
    except ValueError:
        valid = False
    if not valid or any(c.isspace() or ord(c) < 32 or c in "\\?#" for c in url):
        raise ValueError("服务器地址必须是 HTTPS 服务根地址。")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"https://{host}:{port}" if port not in (None, 443) else f"https://{host}"


def valid_token(token):
    return isinstance(token, str) and 0 < len(token) <= 2048 and all(33 <= ord(char) <= 126 for char in token)


class Unavailable(Exception):
    """No trustworthy response. The remote request may have taken effect."""


class StatusError(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__(f"HTTP {status}")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, url):
        self.url = normalize_url(url)
        try:
            self.opener = build_opener(HTTPSHandler(context=ssl.create_default_context()), NoRedirect())
        except OSError:
            raise Unavailable from None

    def request(self, path, token=None, body=None):
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.url + path, data=body, headers=headers,
                          method="POST" if token is not None or body is not None else "GET")
        if token is not None:
            request.add_unredirected_header("X-Worker-Token", token)
        try:
            with self.opener.open(request, timeout=TIMEOUT) as response:
                if response.status != 200:
                    raise StatusError(response.status)
                raw = response.read(MAX_RESPONSE + 1)
        except HTTPError as exc:
            exc.close()
            raise StatusError(exc.code) from None
        except (URLError, OSError, HTTPException, ValueError):
            raise Unavailable from None
        if len(raw) <= MAX_RESPONSE:
            try:
                result = json.loads(raw.decode("utf-8"))
                if isinstance(result, dict):
                    return result
            except (ValueError, RecursionError):
                pass
        raise Unavailable


def transient(status):
    return status in (408, 429) or 500 <= status <= 599
