"""Durable ordinary message outbox; never credentials, context tokens or bytes."""
import hashlib
import json
import sqlite3
import threading
from media_memory import workspace_path,safe_name
from message_policy import ChatFailure


def split_text(text, limit=3500):
    """Lossless UTF-8 transport chunks; no bubble count cap or text truncation."""
    chunk=[];size=0
    for char in text:
        width=len(char.encode('utf-8'))
        if size+width>limit:
            yield ''.join(chunk);chunk=[];size=0
        chunk.append(char);size+=width
    if chunk:yield ''.join(chunk)


class FeedStore:
    def __init__(self,path,*,recover_interrupted=True):
        self.lock=threading.RLock()
        self.db=sqlite3.connect(path,check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,seq INTEGER,text TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS actions(n INTEGER PRIMARY KEY AUTOINCREMENT,message_id TEXT,kind TEXT,
            body TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',target TEXT,reason TEXT);
          CREATE TABLE IF NOT EXISTS inputs(id TEXT PRIMARY KEY,state TEXT NOT NULL);
          CREATE UNIQUE INDEX IF NOT EXISTS message_seq ON messages(seq) WHERE seq>=0;
        ''')
        if 'fingerprint' not in {r[1] for r in self.db.execute('PRAGMA table_info(inputs)')}:
            self.db.execute('ALTER TABLE inputs ADD COLUMN fingerprint TEXT')
        # An interrupted send can have reached WeChat. Never silently retry it.
        if recover_interrupted:
            self.db.execute("UPDATE actions SET state='uncertain',reason='interrupted_send' WHERE state='sending'")
        self.db.commit()

    def get(self,key):
        with self.lock:
            row=self.db.execute('SELECT v FROM meta WHERE k=?',(key,)).fetchone()
            return json.loads(row[0]) if row else None

    def _set(self,key,value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,json.dumps(value)))

    def initialize(self,owner,cursor):
        digest=hashlib.sha256(owner.encode()).hexdigest()
        with self.lock,self.db:
            before=self.get('owner')
            if before and before!=digest:raise ValueError('Feed owner changed')
            self._set('owner',digest)
            if self.get('cursor') is None:self._set('cursor',cursor)

    def ingest(self,rows,scrub=lambda s:s,recover=False):
        """Caller passes verified complete rows only, in canonical seq order."""
        with self.lock,self.db:
            cursor=self.get('cursor')
            for row in rows:
                if row['seq']<=cursor and not recover:continue
                if not row['ready']:break
                meaningful=(bool(row['text'].strip()) or bool(row['files']) or row.get('unsupported')
                            or row.get('status') in ('failed','interrupted'))
                ignored=row.get('ignore_reason') in ('task_status_card','empty_placeholder')
                if row['main'] and row['role']=='assistant' and meaningful and not ignored:
                    mid=row['message_id']
                    if mid.startswith('fallback:'):
                        mid=mid+':'+hashlib.sha256((row['role']+'\0'+row['text']).encode()).hexdigest()
                    inserted=self.db.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?)',
                        (mid,row['seq'],scrub(row['text']))).rowcount
                    if inserted:
                        actions=[('text',{'text':t}) for t in split_text(scrub(row['text']))]
                        for f in row['files']:
                            try:
                                clean={'path':workspace_path(f['path']),'filename':safe_name(f['filename']),'mime':f['mime']}
                                if not isinstance(clean['mime'],str) or len(clean['mime'])>160 or any(ord(c)<32 for c in clean['mime']):raise ValueError()
                                if any(scrub(v)!=v for v in clean.values()):raise ValueError()
                                actions.append(('file',clean))
                            except (KeyError,ValueError,TypeError,ChatFailure):
                                actions.append(('text',{'text':'【Muse 附件暂无法取得，请到主要聊天查看。】'}))
                        if row.get('unsupported'):
                            actions.append(('text',{'text':'【Muse 有暂无法转发的内容，请到主要聊天查看原文。】'}))
                        if row.get('status') in ('failed','interrupted'):
                            actions.append(('text',{'text':'【Muse 本条生成失败或被中断，未自动重新生成。】'}))
                        for kind,body in actions:
                            self.db.execute('INSERT INTO actions(message_id,kind,body) VALUES(?,?,?)',
                                (mid,kind,json.dumps(body,ensure_ascii=False)))
                cursor=max(cursor,row['seq'])
                self._set('cursor',cursor)

    def head(self):
        with self.lock:
            row=self.db.execute("SELECT * FROM actions WHERE state NOT IN ('sent','confirmed_received','cancelled') ORDER BY n LIMIT 1").fetchone()
            return {**dict(row),'body':json.loads(row['body'])} if row else None

    def notice(self,mid,text):
        with self.lock,self.db:
            mid='notice:'+mid
            if self.db.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?)',(mid,-1,text)).rowcount:
                self.db.execute("INSERT INTO actions(message_id,kind,body) VALUES(?,'text',?)",(mid,json.dumps({'text':text})))

    def recover_missing_files(self,row,owner_fingerprint,expected_cursor):
        """Explicit reviewed repair of an already-seen card, never history replay.

        Caller must reverify the same native assistant card and file bytes first.
        Refuse a moving/nonempty queue; keep text/cursor and existing sends intact.
        """
        if not row.get('ready') or not row.get('main') or row.get('role')!='assistant':
            raise ValueError('Verified completed assistant card required')
        files=[]
        for f in row['files']:
            clean={'path':workspace_path(f['path']),'filename':safe_name(f['filename']),'mime':f['mime']}
            if not isinstance(clean['mime'],str) or len(clean['mime'])>160 or any(ord(c)<32 for c in clean['mime']):
                raise ValueError('File metadata rejected')
            files.append(clean)
        if not files:raise ValueError('Verified files required')
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                message=self.db.execute('SELECT * FROM messages WHERE id=?',(row['message_id'],)).fetchone()
                if (self.get('owner')!=owner_fingerprint or self.get('cursor')!=expected_cursor or
                    not message or message['seq']!=row['seq'] or message['text']!=row['text'] or row['seq']>expected_cursor):
                    raise ValueError('Reviewed card ownership or cursor changed')
                previous=[json.loads(a[0]) for a in self.db.execute('SELECT body FROM actions WHERE message_id=?',(row['message_id'],))]
                paths={a.get('path') or a.get('unavailable_file',{}).get('path') for a in previous}
                missing=[]
                for f in files:
                    if f['path'] not in paths:missing.append(f);paths.add(f['path'])
                if missing and self.head() is not None:raise ValueError('Review existing queue first')
                for f in missing:
                    self.db.execute("INSERT INTO actions(message_id,kind,body) VALUES(?,'file',?)",
                        (row['message_id'],json.dumps(f,ensure_ascii=False)))
                self.db.commit()
                return len(missing)
            except Exception:
                self.db.rollback();raise

    def transition(self,n,old,new,target=None,reason=None):
        with self.lock,self.db:
            head=self.head()
            if not head or head['n']!=n or head['state']!=old:raise ValueError('Outbox state changed')
            if head['target'] and head['target']!=target:raise ValueError('Outbox target changed')
            self.db.execute('UPDATE actions SET state=?,target=?,reason=? WHERE n=?',(new,target,reason,n))

    def repair_reviewed_files(self,rows,owner_fingerprint,expected_cursor):
        """Explicit same-owner repair of missing media or a lost filename suffix.

        Original completed messages and sent text are untouched. A corrected
        file is queued once, retaining its original WeChat target. Never use
        this to resolve an uncertain send, regenerate, or rewind a cursor.
        """
        prepared=[]
        for row in rows:
            if not row.get('ready') or not row.get('main') or row.get('role')!='assistant' or row.get('unsupported'):
                raise ValueError('Reviewed completed assistant card required')
            for f in row['files']:
                clean={'path':workspace_path(f['path']),'filename':safe_name(f['filename']),'mime':f['mime']}
                if (clean['filename']!=clean['path'].rsplit('/',1)[-1] or '.' not in clean['filename']
                        or not isinstance(clean['mime'],str) or len(clean['mime'])>160
                        or any(ord(c)<32 for c in clean['mime'])):
                    raise ValueError('Actual workspace filename and MIME required')
                prepared.append((row,clean))
        if not prepared:raise ValueError('Reviewed media required')
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                if self.get('owner')!=owner_fingerprint or self.get('cursor')!=expected_cursor:
                    raise ValueError('Reviewed owner or cursor changed')
                additions=[];seen=set()
                for row,f in prepared:
                    key=(row['message_id'],f['path'],f['filename'])
                    if key in seen:continue
                    seen.add(key)
                    original=self.db.execute('SELECT * FROM messages WHERE id=?',(row['message_id'],)).fetchone()
                    if not original or original['seq']!=row['seq'] or original['text']!=row['text'] or row['seq']>expected_cursor:
                        raise ValueError('Original card changed')
                    actions=list(self.db.execute('SELECT * FROM actions WHERE message_id=?',(row['message_id'],)))
                    targets={a['target'] for a in actions if a['target']}
                    if len(targets)!=1:raise ValueError('Unique original delivery target required')
                    previous=[(a,json.loads(a['body'])) for a in actions if a['kind']=='file']
                    same=[(a,b) for a,b in previous if b.get('path')==f['path']]
                    if any(b.get('filename')==f['filename'] and b.get('mime')==f['mime'] for a,b in same):continue
                    reason='reviewed_missing_media'
                    if same:
                        if any(a['state'] not in ('sent','confirmed_received') or b.get('mime')!=f['mime']
                                or '.' in b.get('filename','') for a,b in same):
                            raise ValueError('Only a confirmed lost-suffix file can be corrected')
                        reason='reviewed_filename_suffix'
                    elif any(a['state'] not in ('sent','confirmed_received','cancelled') for a in actions):
                        raise ValueError('Original delivery must be resolved first')
                    additions.append((row['message_id'],f,next(iter(targets)),reason))
                if additions and self.head() is not None:raise ValueError('Existing queue must finish first')
                for mid,f,target,reason in additions:
                    self.db.execute("INSERT INTO actions(message_id,kind,body,target,reason) VALUES(?,'file',?,?,?)",
                        (mid,json.dumps(f,ensure_ascii=False),target,reason))
                self.db.commit()
                return len(additions)
            except Exception:
                self.db.rollback();raise

    def media_notice(self,n,target):
        with self.lock,self.db:
            h=self.head()
            if not h or h['n']!=n or h['state']!='sending' or h['target']!=target:raise ValueError('Queue changed')
            body={'text':'【Muse 附件未能转到微信，请回 Muse 主要聊天下载原件。没有重新生成。】',
                'unavailable_file':h['body']}
            self.db.execute("UPDATE actions SET kind='text',body=?,state='pending',reason='attachment_unavailable' WHERE n=?",(json.dumps(body,ensure_ascii=False),n))

    def claim_input(self,key,fingerprint=None):
        with self.lock,self.db:
            inserted=self.db.execute("INSERT OR IGNORE INTO inputs(id,state,fingerprint) VALUES(?,'claimed',?)",(key,fingerprint)).rowcount
            if inserted:return True
            if fingerprint is None:return False
            return bool(self.db.execute("UPDATE inputs SET state='claimed' WHERE id=? AND state='not_submitted' AND fingerprint=?",(key,fingerprint)).rowcount)

    def input_result(self,key,state):
        with self.lock,self.db:self.db.execute('UPDATE inputs SET state=? WHERE id=?',(state,key))

    def unresolved_input(self):
        with self.lock:return self.db.execute("SELECT count(*) FROM inputs WHERE state IN ('claimed','uncertain')").fetchone()[0]>0

    def status(self):
        with self.lock:
            counts={r[0]:r[1] for r in self.db.execute('SELECT state,count(*) FROM actions GROUP BY state')}
            head=self.head()
            return {'counts':counts,'cursor':self.get('cursor'),'head':head,'history_replay':False,
                'scope':'all_new_main_assistant_messages','text_truncation':False,'bubble_count_limit':None}

    def close(self):
        with self.lock:self.db.close()
