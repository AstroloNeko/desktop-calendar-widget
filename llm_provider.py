"""Small HTTPS tool-call adapter and Windows current-user protected configuration."""
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import secrets
import urllib.error
import urllib.parse
import urllib.request

from calendar_chat_tools import ChatError


class UserProtection:
    """DPAPI, current user scope; never fall back to plaintext on failure."""
    def __init__(self):
        if os.name != 'nt':
            raise OSError('AI凭据存储目前只支持Windows。')
        class Blob(ctypes.Structure):
            _fields_ = [('size', w.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
        self.Blob = Blob
        self.crypt, self.kernel = ctypes.WinDLL('crypt32', use_last_error=True), ctypes.WinDLL('kernel32', use_last_error=True)
        common = [ctypes.POINTER(Blob), w.LPCWSTR, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, w.DWORD, ctypes.POINTER(Blob)]
        self.crypt.CryptProtectData.argtypes = common
        self.crypt.CryptProtectData.restype = w.BOOL
        self.crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p] + common[2:]
        self.crypt.CryptUnprotectData.restype = w.BOOL
        self.kernel.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel.LocalFree.restype = ctypes.c_void_p

    def transform(self, raw, encrypt):
        buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        source, target = self.Blob(len(raw), buffer), self.Blob()
        function = self.crypt.CryptProtectData if encrypt else self.crypt.CryptUnprotectData
        if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
            raise OSError('无法保护或读取当前Windows用户的AI配置。')
        try:
            return ctypes.string_at(target.data, target.size)
        finally:
            self.kernel.LocalFree(target.data)

    def encrypt(self, raw):
        return self.transform(raw, True)

    def decrypt(self, raw):
        return self.transform(raw, False)


class AIConfiguration:
    def __init__(self, folder, protector=None):
        self.folder = Path(folder)
        self.protector = protector or UserProtection()
        self.path = self.folder / 'configuration.dpapi'

    def load(self):
        if not self.path.exists():
            return dict(endpoint='https://api.deepseek.com', model='deepseek-chat', api_key='', timeout=30,
                        temperature=0.1, consent=False, http_enabled=False, port=18746, token='')
        config = json.loads(self.protector.decrypt(self.path.read_bytes()).decode('utf-8'))
        if type(config) is not dict or config.get('version', 1) != 1:
            raise ValueError('AI配置版本不兼容；没有重置配置。')
        return config

    def save(self, config):
        url = urllib.parse.urlsplit(config['endpoint'])
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ChatError('invalid_arguments', '模型地址必须为不含账号、查询或片段的HTTPS地址。')
        if not isinstance(config['model'], str) or not 1 <= len(config['model']) <= 100:
            raise ChatError('invalid_arguments', '请填写有效模型名称。')
        if type(config['timeout']) is not int or not 5 <= config['timeout'] <= 60:
            raise ChatError('invalid_arguments', '超时应为5至60秒。')
        if type(config['port']) is not int or not 1024 <= config['port'] <= 65535:
            raise ChatError('invalid_arguments', '端口应为1024至65535。')
        config = dict(config)
        if type(config.get('consent')) is not bool or type(config.get('http_enabled')) is not bool:
            raise ChatError('invalid_arguments', '启用选项必须为布尔值。')
        if not isinstance(config.get('api_key'), str) or len(config['api_key']) > 2048:
            raise ChatError('invalid_arguments', '密钥格式不正确。')
        config['version'] = 1
        config['token'] = config.get('token') or secrets.token_urlsafe(32)
        config['temperature'] = 0.1
        self.folder.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_bytes(self.protector.encrypt(json.dumps(config).encode('utf-8')))
        temporary.replace(self.path)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Never forward a credential to a redirected host.


class DeepSeekProvider:
    def __init__(self, configuration):
        self.configuration = configuration

    def complete(self, messages, tools):
        return self.complete_bounded(messages, tools, 90)

    def complete_bounded(self, messages, tools, remaining):
        config = self.configuration.load()
        if not config.get('consent') or not config.get('api_key'):
            raise ChatError('model_unavailable', '请先配置自己的模型密钥，并确认远程数据传输说明。')
        body = json.dumps(dict(model=config['model'], messages=messages, tools=tools,
                               stream=False, temperature=config['temperature'], max_tokens=1600)).encode('utf-8')
        endpoint = config['endpoint'].rstrip('/') + '/chat/completions'
        request = urllib.request.Request(endpoint, data=body, headers={
            'Content-Type': 'application/json', 'Authorization': 'Bearer '+config['api_key']})
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=max(.1, min(config['timeout'], remaining))) as response:
                raw = response.read(262145)
                if len(raw) > 262144:
                    raise ValueError('large response')
                payload = json.loads(raw)
                message = payload['choices'][0]['message']
                if type(message) is not dict:
                    raise ValueError('invalid response')
                return message
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            # Do not include remote response bodies, URLs or credential-bearing request objects in errors.
            raise ChatError('model_unavailable', '模型请求失败或响应无效。请检查配置、网络和额度后重试。') from None
