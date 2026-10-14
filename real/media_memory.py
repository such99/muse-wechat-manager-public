"""Bounded iLink media transport in memory. No URLs/keys/binary bodies in logs."""
import base64
import hashlib
import io
import mimetypes
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlsplit

from common import ROOT, CODE_ROOT

sys.path.insert(0, str(CODE_ROOT / 'vendor' / 'WeChat-Bridge' / 'app'))
from message_policy import ChatFailure
from media import _decode_aes_key, decrypt_aes_ecb, encrypted_size_for_plain_size

MAX_FILE = 40 * 1024 * 1024  # observed Muse DEFAULT_UPLOAD_CONFIG
MAX_TOTAL = 64 * 1024 * 1024
MAX_FILES = 10
CDN = 'https://novac2c.cdn.weixin.qq.com/c2c/download'
TEXT_MIMES = {'.md': 'text/markdown', '.markdown': 'text/markdown', '.html': 'text/html',
    '.htm': 'text/html', '.xml': 'text/xml', '.css': 'text/css', '.js': 'text/javascript',
    '.mjs': 'text/javascript', '.ts': 'text/typescript', '.tsx': 'text/typescript',
    '.json': 'application/json', '.yaml': 'text/yaml', '.yml': 'text/yaml',
    '.toml': 'application/toml', '.csv': 'text/csv', '.tsv': 'text/tab-separated-values',
    '.graphql': 'application/graphql', '.gql': 'application/graphql', '.vue': 'text/x-vue',
    '.svelte': 'text/x-svelte'}
TEXT_EXT = set(('.txt .text .rst .log .py .pyi .rb .php .lua .rs .go .c .h .cpp .cc .hpp .cs '
    '.java .kt .scala .swift .m .mm .hs .ml .clj .ex .exs .erl .sh .bash .zsh .ps1 .bat '
    '.dockerfile .tf .buck .bzl .sql .proto .thrift .diff .patch .ini .cfg .conf .env .properties').split())


def safe_name(name):
    if not isinstance(name, str) or not name or any(ord(c) < 32 for c in name):
        raise ChatFailure('invalid_attachment_name', '附件文件名无效，本条未提交 Muse。')
    # No filesystem access; preserve ordinary Unicode filenames, discard paths.
    result = name.replace('\\', '/').rsplit('/', 1)[-1]
    if result in ('', '.', '..') or len(result) > 200:
        raise ChatFailure('invalid_attachment_name', '附件文件名无效，本条未提交 Muse。')
    return result


def classify(data, name, kind):
    """Observed supported types only. Never relabel audio/SILK as text or image."""
    ext = Path(name).suffix.lower()
    if kind == 'voice':
        raise ChatFailure('voice_not_supported', 'Muse 当前网页附件入口未确认支持微信语音，本条未提交；请改发文字或文件。')
    images = ((b'\x89PNG\r\n\x1a\n','image/png','.png'), (b'\xff\xd8\xff','image/jpeg','.jpg'),
        (b'GIF87a','image/gif','.gif'), (b'GIF89a','image/gif','.gif'))
    for header, mime, suffix in images:
        if data.startswith(header):
            return mime, 'image', suffix
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp', 'image', '.webp'
    if len(data) >= 12 and data[4:8] == b'ftyp':
        brand = data[8:12]
        if brand in (b'heic', b'heix', b'heif', b'mif1', b'avif', b'avis'):
            mime, suffix = ('image/avif','.avif') if brand in (b'avif',b'avis') else ('image/heic','.heic')
            return mime, 'image', suffix
        if kind == 'video' or ext in ('.mp4','.mov'):
            return ('video/quicktime','video','.mov') if brand == b'qt  ' or ext == '.mov' else ('video/mp4','video','.mp4')
    if kind == 'image':
        raise ChatFailure('unsupported_image', '这张图片的格式无法确认，本条未提交 Muse；请改发 PNG 或 JPEG。')
    if data.startswith(b'%PDF-'):
        return 'application/pdf', 'document', '.pdf'
    if ext == '.docx' and data.startswith(b'PK\x03\x04'):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if '[Content_Types].xml' in archive.namelist() and 'word/document.xml' in archive.namelist():
                    return 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'document', '.docx'
        except zipfile.BadZipFile:
            pass
    if ext == '.doc' and data.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'):
        return 'application/msword', 'document', '.doc'
    if ext in TEXT_MIMES or ext in TEXT_EXT:
        try:
            data.decode('utf-8-sig')
        except UnicodeDecodeError:
            # Preserve an ordinary file as binary, never invent decoded text.
            if kind=='file':return 'application/octet-stream','file',ext
            raise ChatFailure('unsupported_text_encoding', '此文本附件编码无法确认，本条未提交 Muse。') from None
        if b'\0' not in data:
            return TEXT_MIMES.get(ext, 'text/plain'), 'text', ext
    if kind=='file':
        # Transport opaque original bytes. This does not claim Muse can parse
        # every format. No archive extraction, execution, renaming or relabelling.
        known={'.zip':'application/zip','.7z':'application/x-7z-compressed',
            '.rar':'application/vnd.rar','.tar':'application/x-tar','.gz':'application/gzip'}
        mime=known.get(ext) or mimetypes.guess_type(name,strict=False)[0] or 'application/octet-stream'
        return mime,'file',ext
    raise ChatFailure('unsupported_file_type', '此媒体格式无法确认，原件未提交；请作为文件发送。')


@dataclass(frozen=True)
class MemoryAttachment:
    filename: str
    mime: str
    category: str
    data: bytes

    def wire(self):
        return {'filename': self.filename, 'mime': self.mime, 'category': self.category,
            'data_base64': base64.b64encode(self.data).decode('ascii')}

    def metadata(self):
        return {'filename': self.filename, 'mime': self.mime, 'bytes': len(self.data),
            'sha256': hashlib.sha256(self.data).hexdigest()}


class MemoryMedia:
    def __init__(self, session):
        self.session = session

    def download(self, item):
        kind = {2:'image',3:'voice',4:'file',5:'video'}.get(item.get('type'))
        if not kind:
            raise ChatFailure('unsupported_attachment', '此消息类型尚未支持，本条未提交 Muse。')
        if kind == 'voice':
            classify(b'', 'voice.silk', kind)
        obj = item.get(kind + '_item') or {}
        media = obj.get('media') or {}
        query = media.get('encrypt_query_param') or media.get('encrypted_query_param') or obj.get('encrypted_query_param') or obj.get('encrypt_query_param')
        key = media.get('aes_key') or obj.get('aes_key') or obj.get('aesKey') or obj.get('aes_key_base64')
        if not query:
            url = urlsplit(media.get('full_url') or obj.get('full_url') or '')
            if url.scheme == 'https' and url.hostname == 'novac2c.cdn.weixin.qq.com' and url.path == '/c2c/download':
                query = parse_qs(url.query).get('encrypted_query_param', [''])[0]
        if not key and obj.get('aeskey'):
            try:
                key = base64.b64encode(obj['aeskey'].encode('ascii')).decode('ascii')
            except Exception:
                key = None
        if not isinstance(query, str) or not query or not isinstance(key, str) or not key:
            raise ChatFailure('media_credentials_missing', '微信没有提供可用的附件下载信息，本条未提交 Muse；请重新发送附件。')
        try:
            declared = int(obj.get('len') or obj.get('hd_size') or obj.get('mid_size') or 0)
            if declared < 0 or declared > encrypted_size_for_plain_size(MAX_FILE):
                raise ChatFailure('attachment_too_large', '附件超过 40 MiB，本条未提交 Muse。')
            aes = _decode_aes_key(key, media_type=kind)
            with self.session.get(CDN, params={'encrypted_query_param': query}, timeout=(5,60),
                    stream=True, allow_redirects=False) as response:
                if response.status_code != 200:
                    raise ValueError('CDN did not return a file')
                size = int(response.headers.get('Content-Length') or 0)
                limit = encrypted_size_for_plain_size(MAX_FILE)
                if size < 0 or size > limit:
                    raise ChatFailure('attachment_too_large', '附件超过 40 MiB，本条未提交 Muse。')
                data = bytearray()
                for chunk in response.iter_content(65536):
                    if len(data) + len(chunk) > limit:
                        raise ChatFailure('attachment_too_large', '附件超过 40 MiB，本条未提交 Muse。')
                    data.extend(chunk)
            if not data or len(data) % 16:
                raise ValueError('Invalid encrypted file')
            plain = decrypt_aes_ecb(bytes(data), aes)
            if not plain or len(plain) > MAX_FILE:
                raise ValueError('Invalid plaintext size')
            name = safe_name(obj.get('file_name') or ('wechat-' + kind + ('.mp4' if kind=='video' else '.bin')))
            mime, category, ext = classify(plain, name, kind)
            if kind != 'file':
                name = 'wechat-' + kind + ext
            return MemoryAttachment(name, mime, category, plain)
        except ChatFailure:
            raise
        except Exception:
            raise ChatFailure('media_download_failed', '微信附件下载或解密失败，本条未提交 Muse；请重新发送。') from None

    def incoming(self, items):
        # No placeholder text, OCR, transcription, system prompt or history.
        if len([i for i in items if i.get('type') != 1]) > MAX_FILES:
            raise ChatFailure('too_many_attachments', '一条消息最多 10 个附件，本条未提交 Muse。')
        texts, files, total = [], [], 0
        for item in items:
            if item.get('type') == 1:
                text = (item.get('text_item') or {}).get('text')
                if not isinstance(text, str):
                    raise ChatFailure('invalid_text_item', '微信文字消息格式异常，本条未提交 Muse。')
                texts.append(text)
            else:
                attachment = self.download(item)
                total += len(attachment.data)
                if total > MAX_TOTAL:
                    raise ChatFailure('attachments_too_large', '一条消息附件合计超过 64 MiB，本条未提交 Muse。')
                files.append(attachment)
        # Each text item is concatenated exactly, without inventing newline bytes.
        return ''.join(texts), files


def workspace_path(value):
    if not isinstance(value, str) or len(value) > 1000 or any(ord(c)<32 for c in value):
        raise ValueError('Invalid workspace path')
    path = value.removeprefix('sandbox://').lstrip('/')
    parts = PurePosixPath(path).parts
    if not parts or any(p in ('.','..') for p in parts) or '\\' in path or ':' in path or '?' in path or '#' in path:
        raise ValueError('Invalid workspace path')
    if not path.startswith(('workspace/','mnt/data/')):
        raise ValueError('Not an attachment workspace')
    return path


def decode_uploads(entries):
    if not isinstance(entries,list) or len(entries)>MAX_FILES:
        raise ChatFailure('invalid_attachments','附件数量或格式异常，本条未提交。')
    result,total=[],0
    for entry in entries:
        try:
            name=safe_name(entry['filename'])
            encoded=entry['data_base64']
            if not isinstance(encoded,str) or len(encoded)>((MAX_FILE+2)//3)*4:
                raise ValueError('Oversized encoding')
            data=base64.b64decode(encoded,validate=True)
            if not data or len(data)>MAX_FILE:
                raise ValueError('Invalid attachment size')
            total+=len(data)
            if total>MAX_TOTAL:
                raise ValueError('Attachments exceed budget')
            mime,category,_=classify(data,name,'image' if entry.get('category')=='image' else 'file')
            if entry.get('mime')!=mime or entry.get('category')!=category:
                raise ValueError('MIME/category mismatch')
            result.append(MemoryAttachment(name,mime,category,data))
        except ChatFailure:
            raise
        except Exception:
            raise ChatFailure('invalid_attachment','附件格式或大小核验失败，本条未提交。') from None
    return result
