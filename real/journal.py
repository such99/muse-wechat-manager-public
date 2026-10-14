"""Local per-instance JSONL journal; full chat bodies, never raw auth/protocol dumps."""
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path


class InstanceJournal:
    EVENT_FIELDS = {'request_id', 'user_id', 'message_id', 'reason', 'status',
        'exception_type', 'item_types', 'truncated', 'model', 'characters'}

    def __init__(self, settings, service, credentials=lambda: ()):
        policy = settings.get('logging', {})
        self.enabled = policy.get('enabled') is True
        self.chat_bodies = self.enabled and policy.get('chat_bodies') is True
        self.directory = Path(settings['runtime']) / 'logs'
        self.instance, self.service = settings['instance'], service
        if service not in ('muse', 'bridge'):
            raise ValueError('Unknown journal service')
        self.credentials = credentials
        self.lock = threading.Lock()
        self.failed_writes = 0

    def _path(self):
        day = datetime.now(timezone.utc).date().isoformat()
        return self.directory / (self.service + '-' + day + '.jsonl')

    def status(self):
        return {'enabled': self.enabled, 'chat_bodies': self.chat_bodies,
            'directory': str(self.directory), 'file': str(self._path()),
            'retention': 'until manually removed', 'failed_writes': self.failed_writes}

    def _write(self, event, data, exclude=()):
        if not self.enabled:
            return False
        if not re.fullmatch(r'[a-z][a-z0-9_.-]{0,79}', event):
            raise ValueError('Invalid journal event')
        try:
            # Only exact known runtime authentication values are omitted. Ordinary
            # chat text and user/message IDs are preserved, without PII masking.
            values = sorted({v for v in (*self.credentials(), *exclude)
                if isinstance(v, str) and len(v) >= 8}, key=len, reverse=True)
            def guard(value):
                if isinstance(value, str):
                    for secret in values:
                        value = value.replace(secret, '[credential omitted]')
                    return value
                if isinstance(value, list):
                    return [guard(v) for v in value]
                if isinstance(value, dict):
                    return {k: guard(v) for k, v in value.items()}
                return value
            record = {'time_utc': datetime.now(timezone.utc).isoformat(),
                'instance': self.instance, 'service': self.service, 'event': event,
                **guard(data)}
            line = json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n'
            with self.lock:
                self.directory.mkdir(parents=True, exist_ok=True)
                with self._path().open('a', encoding='utf-8', newline='\n') as handle:
                    handle.write(line)
            return True
        except Exception:
            # Never dump an exception that might contain a path, URL or secret.
            self.failed_writes += 1
            return False

    def event(self, event, **fields):
        if set(fields) - self.EVENT_FIELDS:
            raise ValueError('Raw protocol/credential fields cannot be journaled')
        return self._write(event, fields)

    def chat(self, event, *, text, user_id=None, message_id=None, request_id=None,
            item_types=None, exclude=()):
        if not self.chat_bodies:
            return False
        return self._write(event, {'text': text, 'user_id': user_id,
            'message_id': message_id, 'request_id': request_id,
            'item_types': item_types}, exclude)

    def api_request(self, request_id, messages, *, user_id=None, message_id=None):
        if not self.chat_bodies:
            return False
        # Copy only the text schema, not headers, configuration, cookies or media.
        data = [{'role': m['role'], 'content': m['content']} for m in messages]
        return self._write('api.request', {'request_id': request_id,
            'user_id': user_id, 'message_id': message_id, 'messages': data})

    def media(self,event,files,*,request_id=None,message_id=None):
        # Persist ordinary metadata only; never binary/base64, download refs or AES keys.
        data=[{k:f[k] for k in ('filename','mime','bytes','sha256')} for f in files]
        return self._write(event,{'files':data,'request_id':request_id,'message_id':message_id})

    def tail(self, limit=50):
        limit = max(1, min(int(limit), 100))
        with self.lock:
            path = self._path()
            if not path.is_file():
                return []
            with path.open('rb') as handle:
                handle.seek(0, 2)
                start = max(0, handle.tell() - 1024 * 1024)
                handle.seek(start)
                if start:
                    handle.readline()
                lines = handle.read().splitlines()[-limit:]
        return [json.loads(line) for line in lines]
