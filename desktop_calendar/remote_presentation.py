"""Connection and model-status presentation shared by settings and the tray."""
from datetime import datetime
from dataclasses import dataclass


CONNECTION_LABELS = {
    'disabled': ('远程服务已关闭', '启用并保存后即可接收微信消息。'),
    'verifying': ('正在核验绑定账号', '服务器核验完成前，暂停领取任务和上传回执。'),
    'connecting': ('正在连接', '正在连接服务器并检查待处理消息。'),
    'live': ('实时在线', '实时通知已连接。'),
    'reconnecting': ('正在重新连接', '实时通知暂时断开，正在恢复。'),
    'backup': ('在线（备用轮询）', '备用检查已成功，正在恢复实时通知。'),
    'unavailable': ('暂时无法连接服务器', '会自动尝试恢复连接。'),
    'invalid': ('绑定已失效，请重新绑定', '请在对应的微信公众号中发送“重新获取激活码”，然后重新绑定本机。'),
    'mismatch': ('绑定账号不符，请重新绑定', '服务器返回的账号与本机绑定不一致。请在对应的微信公众号中重新获取激活码。'),
}


def model_status(has_key, consent, failed):
    if not has_key:
        return '尚未配置模型密钥'
    if not consent:
        return '尚未授权使用模型'
    if failed:
        return '最近一次模型请求失败'
    return '已配置'


@dataclass(frozen=True)
class RemoteTrayConfig:
    configured: bool = False
    bound: bool = False
    enabled: bool = False
    has_model_key: bool = False
    model_consent: bool = False

    @classmethod
    def from_settings(cls, settings):
        return cls(bool(settings.get('remote_was_configured') or settings.get('remote_url')),
                   bool(settings.get('remote_url') and settings.get('remote_token')),
                   settings.get('remote_enabled') is True, bool(settings.get('api_key')),
                   settings.get('consent') is True)

    def tray_status(self, snapshot):
        if not self.configured:
            return None, ''
        connection = snapshot.connection
        title = CONNECTION_LABELS[connection][0]
        completion = snapshot.completion
        if not self.bound or connection == 'disabled':
            color = 'gray'
            if not self.bound:
                title = '尚未绑定'
        else:
            ai = model_status(self.has_model_key, self.model_consent, completion.model_unavailable if completion else False)
            if snapshot.request_failed and connection == 'live':
                title += ' · 任务接口暂不可用'
            if ai != '已配置':
                title += f' · AI {ai}'
            if connection in ('unavailable', 'invalid', 'mismatch') or snapshot.request_failed or ai != '已配置':
                color = 'red'
            else:
                color = 'green' if connection == 'live' else 'yellow'
        tip = f'微信远控：{title}'
        if completion and completion.processed_at:
            when = datetime.fromtimestamp(completion.processed_at).strftime('%H:%M')
            tip += f'\n最近处理：{when} · {completion.result}'
        if snapshot.last_checked_at is not None:
            when = datetime.fromtimestamp(snapshot.last_checked_at).strftime('%m-%d %H:%M:%S')
            tip += f'\n最近成功检查：{when}'
        return color, tip
