"""Memory-only OpenAI adapter over WeChat-Bridge's history manager; no retries."""
import time
import uuid
import sys
from collections import OrderedDict

from common import ROOT, CODE_ROOT
sys.path.insert(0, str(CODE_ROOT / 'vendor' / 'WeChat-Bridge' / 'app'))

import requests
from ai_chat import AIChatManager


class ChatFailure(Exception):
    def __init__(self, reason, notice):
        self.reason, self.notice = reason, notice
        super().__init__(reason)


class SafeChatManager(AIChatManager):
    def __init__(self, config, session, journal=None):
        super().__init__(lambda: config, lambda _: None)
        self.session = session
        self.auth_failed = False
        self.journal = journal

    def chat(self, user_id, text, message_id=None):
        if self.auth_failed:
            raise ChatFailure('authentication_failed', 'Muse 认证已失效，请在服务器重新认证本人的账号。')
        config = self._load_config()
        if not self._check_daily_limit(config):
            raise ChatFailure('local_limit', '本实例今日使用量达到本地限制，请稍后再试。不会切换到其他人的账号。')
        if len(text) > 16000:
            raise ChatFailure('input_too_long', '消息过长（最多 16000 字符），请缩短后重新发送。')
        with self._lock:
            history = list(self._get_history(user_id))[-config['max_history'] * 2:]
        messages = [{'role': 'system', 'content': config['system_prompt']}] + history + [{'role': 'user', 'content': text}]
        if sum(len(m['content']) for m in messages) > 24000 or any(len(m['content']) > 16000 for m in messages):
            raise ChatFailure('context_too_long', '当前上下文过长。发送 /清空 后再发送较短消息。')
        request_id = uuid.uuid4().hex
        if self.journal:
            self.journal.api_request(request_id, messages, user_id=user_id, message_id=message_id)
        try:
            response = self.session.post(config['base_url'] + '/chat/completions',
                headers={'Authorization': 'Bearer ' + config['api_key'], 'X-Request-ID': request_id},
                json={'model': config['model'], 'messages': messages, 'stream': False}, timeout=(5, 330))
            try:
                if response.status_code == 401:
                    self.auth_failed = True
                    raise ChatFailure('authentication_failed', 'Muse 认证已失效，请在服务器重新认证本人的账号。')
                if response.status_code in (413, 422):
                    raise ChatFailure('input_too_long', '消息或上下文过长，请缩短消息，或发送 /清空 后重试。')
                if response.status_code == 429:
                    raise ChatFailure('upstream_limit', 'Muse 暂时限流或额度不足，请稍后检查本人的账号。')
                response.raise_for_status()
                reply = response.json()['choices'][0]['message']['content']
                if not isinstance(reply, str) or not reply.strip():
                    raise ChatFailure('empty_reply', 'Muse 返回了空回复，本次没有保存对话。请稍后重新发送。')
                if self.journal:
                    self.journal.chat('api.reply', text=reply, user_id=user_id,
                        message_id=message_id, request_id=request_id)
            finally:
                response.close()
        except ChatFailure:
            if self.journal:
                self.journal.event('api.failed', request_id=request_id, status='known_failure')
            raise
        except requests.Timeout:
            if self.journal:
                self.journal.event('api.failed', request_id=request_id, reason='timeout_no_retry')
            raise ChatFailure('timeout', 'Muse 请求超时，结果尚未确认，未自动重试。请稍后检查后再发送。') from None
        except Exception:
            if self.journal:
                self.journal.event('api.failed', request_id=request_id, reason='upstream_failed_no_retry')
            raise ChatFailure('upstream_failed', 'Muse 暂时无法回复，本次未自动重试。请稍后检查服务状态。') from None
        with self._lock:
            self._histories[user_id] = (history + [{'role': 'user', 'content': text},
                {'role': 'assistant', 'content': reply}])[-config['max_history'] * 2:]
        self._record_usage(config, len(text) + len(reply))
        return reply


def fit_reply(text, max_bytes=3500):
    """One bounded UTF-8 message, explicit truncation; no partial-byte characters."""
    if len(text.encode('utf-8')) <= max_bytes:
        return text, False
    suffix = '\n【回复过长，已截断；可请 Muse 分段简述。】'
    size = max_bytes - len(suffix.encode('utf-8'))
    return text.encode('utf-8')[:size].decode('utf-8', errors='ignore') + suffix, True


class MessagePolicy:
    def __init__(self, clock=time.monotonic, capacity=10000):
        self.clock, self.capacity = clock, capacity
        self.seen = OrderedDict()
        self.last_call = -100

    def accept(self, uid, mid):
        now = self.clock()
        while self.seen and now - next(iter(self.seen.values())) > 86400:
            self.seen.popitem(last=False)
        if not mid:
            return 'missing_message_id'
        key = (uid, str(mid))
        if key in self.seen:
            return 'duplicate'
        if len(self.seen) >= self.capacity:
            return 'dedupe_capacity_reached'
        self.seen[key] = now
        return 'accepted'

    def can_generate(self):
        now = self.clock()
        if now - self.last_call < 2:
            return False
        self.last_call = now
        return True
