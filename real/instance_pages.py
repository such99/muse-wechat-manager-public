"""Adapt local tools to the credential storage used by managed instances."""
from common import CODE_ROOT


def instance_page(settings, filename):
    page = (CODE_ROOT / 'real' / filename).read_text(encoding='utf-8')
    page = page.replace('__INSTANCE__', settings['instance'])
    if not settings.get('managed'):
        return page
    if filename == 'portal.html':
        page = page.replace(
            '凭据保存在进程级环境变量、内存及隔离浏览器会话中；关闭服务后重新认证。',
            '确认主聊天后，必要登录凭据使用当前 Windows 用户的 DPAPI 加密保存在本机；重新打开软件会尝试恢复，失效时须本人重新认证。')
        page = page.replace('重启后仍需重新认证，保存的日志不会自动恢复聊天上下文。',
                            '重启会尝试恢复原账号，普通日志不用于重放聊天输入。')
        page = page.replace('Cookie 不显示、不下载、不写文件。',
                            'Cookie 不显示、不下载；确认主聊天后加密保存必要登录凭据。')
    return page
