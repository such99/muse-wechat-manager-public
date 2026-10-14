"""Independent reviewer regressions: manual recovery never resubmits an input."""
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parent))
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from common import LocalBodyLimit,secret_matches
from feed_store import FeedStore
from main_feed import MainFeed
from incoming_queue import IncomingQueue
from message_policy import ChatFailure
from test_feed_helpers import node_executable,service_settings,FakeEngine,cookies


def row(seq=1,**kw):
    return dict(dict(seq=seq,message_id='fake-'+str(seq),role='assistant',main=True,ready=True,
        status='completed',text='FAKE-REPLY',files=[],unsupported=False),**kw)

def feed_fixture(path=None):
    native=SimpleNamespace(probe=lambda:{'main_busy':False,'has_user_draft':False})
    events=[]
    feed=MainFeed(native,{},SimpleNamespace(chat=lambda *a,**k:None,event=lambda *a,**k:events.append((a,k))))
    if path:
        feed.store.close();feed.store=FeedStore(path)
    feed.store.initialize('fake-owner',0);feed.enabled=True
    calls=[];snapshot={'owner':'fake-owner','rows':[],'busy':False}
    def call(method,*args,**kw):
        calls.append((method,args))
        return snapshot if method=='scan' else True
    feed.call=call
    return feed,snapshot,calls,events


@pytest.mark.parametrize('resolution',['acknowledged','not_submitted_reviewed'])
def test_reviewed_inputs_survive_restart_without_replay(tmp_path,resolution):
    path=tmp_path/'feed.sqlite3'
    store=FeedStore(path);store.initialize('fake-owner',0)
    store.claim_input('a'*64,'fingerprint','b'*32);store.input_result('a'*64,'uncertain');store.close()
    feed,snapshot,calls,events=feed_fixture(path)
    feed.submission_uncertain=True
    result=feed.resolve_submission('a'*64,resolution,feed.store.get('owner'))
    assert not result['submission_uncertain'] and not feed.store.unresolved_input()
    assert ('reviewedRelease',('b'*32,resolution)) in calls and not any(c[0]=='submit' for c in calls)
    feed.resolve_submission('a'*64,resolution,feed.store.get('owner'))  # Same review is idempotent.
    feed.call=lambda method,*a,**k:({'phase':'acknowledged','message_id':'fake-new'} if method=='submit' else snapshot)
    assert feed.submit('c'*32,'FAKE-NEW-INPUT',[],'d'*64)['phase']=='acknowledged'
    feed.close();store=FeedStore(path)
    assert not store.unresolved_input() and not store.claim_input('a'*64,'fingerprint','c'*32)
    store.close()

def test_review_resolves_only_exact_input_and_conflicting_result_refused():
    feed,_,_,_=feed_fixture()
    for key in ('a'*64,'c'*64):feed.store.claim_input(key,'fp','b'*32)
    feed.resolve_submission('a'*64,'acknowledged',feed.store.get('owner'))
    assert feed.submission_uncertain and feed.store.input_record('c'*64)['state']=='claimed'
    with pytest.raises(ValueError):feed.resolve_submission('a'*64,'not_submitted_reviewed',feed.store.get('owner'))
    feed.close()

@pytest.mark.parametrize('fault',['owner','busy','draft','settling','changed_native_owner'])
def test_review_refuses_changed_identity_or_inflight_work(fault):
    feed,snapshot,calls,_=feed_fixture();feed.store.claim_input('a'*64,'fp','b'*32)
    owner=feed.store.get('owner')
    if fault=='owner':owner='0'*64
    if fault=='busy':snapshot['busy']=True
    if fault=='draft':feed.native.probe=lambda:{'main_busy':False,'has_user_draft':True}
    if fault=='settling':snapshot['rows']=[row(ready=False)]
    if fault=='changed_native_owner':snapshot['owner']='other-owner'
    with pytest.raises(ChatFailure):feed.resolve_submission('a'*64,'acknowledged',owner)
    assert feed.store.unresolved_input() and not any(c[0]=='reviewedRelease' for c in calls)
    feed.close()

@pytest.mark.parametrize('entry',[row(main=False,status='failed'),row(ready=False,status='interrupted')])
def test_nonmain_or_unconsumed_failure_never_halts(entry):
    feed,snapshot,_,_=feed_fixture();snapshot['rows']=[entry];feed.poll()
    assert not feed.generation_halted and feed.store.get('generation_pause') is None
    feed.close()

@pytest.mark.parametrize('status',['failed','interrupted'])
def test_generation_review_is_exact_persistent_and_does_not_regenerate(tmp_path,status):
    path=tmp_path/'feed.sqlite3';feed,snapshot,calls,_=feed_fixture(path)
    snapshot['rows']=[row(status=status)];feed.poll();failure=feed.generation_failure
    assert feed.generation_halted and feed.store.get('generation_pause')==failure
    with pytest.raises(ValueError):feed.resolve_generation({**failure,'seq':9},feed.store.get('owner'))
    feed.resolve_generation(failure,feed.store.get('owner'))
    assert not feed.generation_halted and not any(c[0]=='submit' for c in calls)
    feed.close();store=FeedStore(path);assert store.get('generation_pause') is None;store.close()

def test_generation_confirmation_refuses_newer_failure():
    feed,snapshot,_,_=feed_fixture();snapshot['rows']=[row(status='failed')];feed.poll()
    old=feed.generation_failure;snapshot['rows']=[row(2,status='interrupted')]
    with pytest.raises(ValueError):feed.resolve_generation(old,feed.store.get('owner'))
    assert feed.generation_halted and feed.generation_failure['seq']==2
    feed.close()

def test_pause_restores_on_enable_and_cleared_pause_stays_cleared(tmp_path):
    path=tmp_path/'main-feed.sqlite3';feed,snapshot,_,_=feed_fixture(path)
    snapshot['rows']=[row(status='interrupted')];feed.poll();failure=feed.generation_failure;feed.close()
    for resolve in (True,False):
        reopened=MainFeed(SimpleNamespace(probe=lambda:{'main_busy':False,'has_user_draft':False},_evaluate=lambda *a:True),
            {'runtime':tmp_path},SimpleNamespace(chat=lambda *a,**k:None,event=lambda *a,**k:None))
        reopened.call=lambda *a,**k:{'owner':'fake-owner','rows':[],'busy':False}
        reopened.enable()
        assert reopened.generation_halted is resolve
        if resolve:reopened.resolve_generation(failure,reopened.store.get('owner'))
        reopened.close()

def queue_fixture(tmp_path,notices):
    return IncomingQueue({'runtime':tmp_path},lambda items:('FAKE-TEXT',[]),lambda:{'ready':False,'reason':'main_chat_busy'},
        lambda *a,**k:{'phase':'acknowledged'},lambda *a:notices.append(a),
        SimpleNamespace(event=lambda *a,**k:None,media=lambda *a,**k:None),threading.Event())

@pytest.mark.parametrize('shutdown',['crash','clean'])
def test_known_unsubmitted_redelivery_requeues_once_without_storing_body(tmp_path,shutdown):
    q=queue_fixture(tmp_path,[]);q.enqueue('fake-owner','mid',[{'text':'PRIVATE-FAKE-CONTENT'}])
    if shutdown=='crash':q.db.close()
    else:q.close()
    q=queue_fixture(tmp_path,[])
    assert q.enqueue('fake-owner','mid',[{'text':'PRIVATE-FAKE-CONTENT'}])=='queued'
    assert q.enqueue('fake-owner','mid',[])=='duplicate' and q.status()['pending']==1
    assert not q.db.execute("SELECT metadata FROM incoming WHERE metadata LIKE '%PRIVATE-FAKE-CONTENT%'").fetchone()
    q.close()

def test_restart_notice_covers_text_and_media_once_for_original_sender(tmp_path):
    notices=[];q=queue_fixture(tmp_path,notices)
    q.enqueue('owner-a','text',[{'type':1}]);q.enqueue('owner-a','image',[{'type':2}]);q.db.close()
    q=queue_fixture(tmp_path,notices);q.enqueue('owner-b','new-other',[])
    assert notices==[]
    q.enqueue('owner-a','new',[])
    assert len(notices)==1 and '2 条文字或附件' in notices[0][2]
    q.enqueue('owner-a','new2',[]);assert len(notices)==1
    q.close()

def test_acknowledged_uncertain_and_reviewed_inputs_are_never_requeued(tmp_path):
    q=queue_fixture(tmp_path,[])
    for mid,state in [('a','acknowledged'),('u','uncertain'),('r','not_submitted_reviewed')]:
        key=hashlib.sha256(json.dumps(['owner',mid],ensure_ascii=True).encode()).hexdigest()
        q.db.execute('INSERT INTO incoming VALUES(?,?,?,?,NULL)',(key,mid,state,None))
        assert q.enqueue('owner',mid,[])=='duplicate'
    q.db.commit();assert q.status()['pending']==0;q.close()

def test_bridge_review_key_cannot_unlock_another_sender(tmp_path):
    q=queue_fixture(tmp_path,[]);q.enqueue('owner','mid',[])
    q.db.execute("UPDATE incoming SET state='uncertain'");q.db.commit()
    with pytest.raises(ValueError):q.review_key('other','mid','acknowledged')
    key=q.review_key('owner','mid','acknowledged');q.resolve('mid','acknowledged',key)
    assert not q.halted;q.close()

@pytest.mark.parametrize('actual',['é','Bearer é','\ud800',None])
def test_nonascii_or_malformed_credentials_fail_closed(actual):
    assert not secret_matches(actual,'FAKE-ASCII-SECRET')

def test_local_body_limit_rejects_chunked_body_before_handler():
    called=[]
    async def handler(scope,receive,send):called.append(True)
    messages=iter([{'type':'http.request','body':b'a'*40000,'more_body':True},
        {'type':'http.request','body':b'b'*30000,'more_body':False}])
    sent=[]
    async def receive():return next(messages)
    async def send(message):sent.append(message)
    asyncio.run(LocalBodyLimit(handler)({'type':'http','path':'/local/import','method':'POST'},receive,send))
    assert not called and sent[0]['status']==413

def test_review_api_requires_confirmation_auth_and_owner(tmp_path,monkeypatch):
    from muse_service import create_service
    monkeypatch.delenv('MUSE_RETAINED_BRIDGE',raising=False)
    monkeypatch.delenv('MUSE_FIXTURE_COOKIES_JSON',raising=False)
    monkeypatch.setenv('MUSE_FIXTURE_API_KEY','FAKE-FEED-API-KEY')
    app=create_service(service_settings(tmp_path),FakeEngine())
    app.state.account.inject(cookies('FAKE-COOKIE'));app.state.status['owner_confirmed']=True
    feed=app.state.feed;feed.enabled=True;feed.store.initialize('owner',0)
    feed.native.probe=lambda:{'main_busy':False,'has_user_draft':False}
    calls=[]
    def call(method,*a,**k):
        calls.append(method);return {'owner':'owner','rows':[],'busy':False} if method=='scan' else True
    feed.call=call;feed.store.claim_input('a'*64,'fp','b'*32);feed.submission_uncertain=True
    body={'source_id':'a'*64,'resolution':'not_submitted_reviewed','owner_fingerprint':feed.store.get('owner')}
    local={'Origin':'http://127.0.0.1:18611','X-Local-Action':'1'}
    with TestClient(app,base_url='http://127.0.0.1:18611') as client:
        assert client.post('/local/resolve-input',json=body).status_code==403
        assert client.post('/local/resolve-input',headers=local,json=body).status_code==409
        body['confirm_checked_main_chat']=True
        assert client.post('/v1/muse/resolve-input',json=body).status_code==401
        assert client.post('/local/resolve-input',headers=local,json={**body,'owner_fingerprint':'0'*64}).status_code==409
        assert client.post('/local/resolve-input',headers=local,json=body).status_code==200
        assert not feed.store.unresolved_input() and 'submit' not in calls


def test_bridge_review_coordinates_muse_before_unlocking_local_queue(tmp_path,monkeypatch):
    from bridge_service import create_bridge
    import time
    import requests
    monkeypatch.setenv('MUSE_FIXTURE_API_KEY','FAKE-FEED-API-KEY')
    monkeypatch.setenv('MUSE2API_API_KEY','FAKE-FEED-API-KEY')
    class Link:
        logged_in=True;user_id='fake-owner';bot_id='fake-bot';bot_token='FAKE-BOT'
        saved_binding={'user_id':'fake-owner','role':'owner'}
        def get_updates(self):time.sleep(.01);return []
        def clear_token(self):self.logged_in=False
        def send_text(self,*a):raise AssertionError('No actual delivery allowed')
    app=create_bridge(service_settings(tmp_path),Link())
    manager=app.state.manager;queue=manager.incoming
    queue.enqueue('fake-owner','mid',[]);queue.record(queue.tasks[0],'uncertain');queue.halted=True
    muse,_,calls,_=feed_fixture()
    source=hashlib.sha256(json.dumps(['fixture:fake-owner','mid'],ensure_ascii=True).encode()).hexdigest()
    muse.store.claim_input(source,'fp','b'*32);muse.submission_uncertain=True
    refused=[True];resolved=[]
    def api(path,body=None,**kwargs):
        if path=='/feed':return muse.status()
        if path=='/resolve-input':
            if refused[0]=='timeout':raise requests.Timeout('FAKE-REVIEW-TIMEOUT')
            if refused[0]:raise ChatFailure('main_chat_busy','FAKE-BUSY')
            assert body['source_id']==source
            result=muse.resolve_submission(body['source_id'],body['resolution'],body['owner_fingerprint'])
            resolved.append(source);return result
        raise AssertionError(path)
    manager.api=api
    headers={'Origin':'http://127.0.0.1:18612','X-Local-Action':'1'}
    body={'message_id':'mid','resolution':'not_submitted_reviewed','confirm_checked_main_chat':True}
    with TestClient(app,base_url='http://127.0.0.1:18612') as client:
        assert client.post('/local/resolve-incoming',headers=headers,json=body).status_code==409
        assert queue.halted and muse.store.unresolved_input()
        refused[0]='timeout'
        assert client.post('/local/resolve-incoming',headers=headers,json=body).status_code==409
        assert queue.halted and muse.store.unresolved_input()
        refused[0]=False
        assert client.post('/local/resolve-incoming',headers=headers,json=body).status_code==200
        assert not queue.halted and not muse.store.unresolved_input() and resolved==[source]
        assert client.post('/local/resolve-incoming',headers=headers,json=body).status_code==409
        assert 'submit' not in calls
    muse.close()

def test_native_review_release_never_unlocks_pending_or_foreign_attempt():
    result=subprocess.run([node_executable(),str(Path(__file__).with_name('test_review_recovery_ui.cjs'))],
        capture_output=True,text=True,encoding='utf-8',timeout=20)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['checks']==6

def test_new_instance_logs_do_not_persist_chat_bodies(tmp_path):
    from manager_core import Registry
    from journal import InstanceJournal
    cfg=Registry(tmp_path).add('fixture','测试实例');cfg['runtime']=tmp_path/'runtime'/'fixture'
    assert cfg['logging']['chat_bodies'] is False
    journal=InstanceJournal(cfg,'muse');journal.chat('muse.test',text='FAKE-PRIVATE-BODY')
    assert all('FAKE-PRIVATE-BODY' not in p.read_text(encoding='utf-8') for p in cfg['runtime'].rglob('*.jsonl'))

@pytest.mark.parametrize('headless',[False,True])
def test_strict_browser_launch_cannot_use_vendor_unsafe_flags(monkeypatch,headless):
    from strict_engine import StrictEngine
    engine=StrictEngine.__new__(StrictEngine);engine.proc=None;engine.browser=None;engine.headless=headless
    engine.cfg=SimpleNamespace(chromium='FAKE-CHROME',cdp_port=18613,profile_dir='FAKE-PROFILE')
    captured=[]
    class CapturedLaunch(Exception):pass
    class FakeSocket:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def bind(self,address):assert address==('127.0.0.1',18613)
    def launch(args,**kwargs):captured.append((args,kwargs));raise CapturedLaunch()
    monkeypatch.setattr('strict_engine.os.path.isfile',lambda path:True)
    monkeypatch.setattr('strict_engine.socket.socket',FakeSocket)
    monkeypatch.setattr('strict_engine.subprocess.Popen',launch)
    monkeypatch.setenv('FAKE_REVIEW_TOKEN','FAKE-PRIVATE-TOKEN')
    with pytest.raises(CapturedLaunch):engine.start()
    args,kwargs=captured[0]
    assert '--incognito' in args and '--remote-debugging-address=127.0.0.1' in args
    assert '--remote-allow-origins=http://127.0.0.1:18613' in args
    assert '--no-sandbox' not in args and '--remote-allow-origins=*' not in args
    assert 'FAKE_REVIEW_TOKEN' not in kwargs['env']
