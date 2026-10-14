import json
from pathlib import Path
import subprocess
import sys
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parent))
from feed_store import FeedStore
from main_feed import MainFeed
from types import SimpleNamespace
from test_feed_helpers import node_executable

def row(seq,text='',**kw):
    return dict(dict(seq=seq,message_id='fixture-'+str(seq),role='assistant',main=True,ready=True,
                text=text,files=[],unsupported=False,status='completed'),**kw)

@pytest.mark.parametrize('text',['','   ','\n\t'])
def test_empty_placeholder_advances_without_actions(tmp_path,text):
    store=FeedStore(tmp_path/'q');store.initialize('fake-owner',0)
    store.ingest([row(1,text),row(2,'正常正文')])
    assert store.get('cursor')==2
    assert list(store.db.execute('SELECT message_id FROM actions'))[0][0]=='fixture-2'
    assert store.db.execute('SELECT count(*) FROM actions').fetchone()[0]==1
    store.close()

@pytest.mark.parametrize('reason',['task_status_card','empty_placeholder'])
def test_explicitly_ignored_rows_never_enqueue(tmp_path,reason):
    store=FeedStore(tmp_path/'q');store.initialize('fake-owner',0)
    store.ingest([row(1,'已停止',ignore_reason=reason),row(2,'正常内容')])
    assert store.get('cursor')==2 and store.head()['message_id']=='fixture-2'
    store.close()

@pytest.mark.parametrize('status',['failed','interrupted'])
def test_actual_generation_failure_retains_only_failure_notice(tmp_path,status):
    store=FeedStore(tmp_path/'q');store.initialize('fake-owner',0)
    entry=row(1);entry['status']=status;store.ingest([entry])
    texts=[json.loads(r[0])['text'] for r in store.db.execute('SELECT body FROM actions')]
    assert len(texts)==1 and '生成失败或被中断' in texts[0]
    store.close()

def test_multibubble_files_and_real_unsupported_notices_survive(tmp_path):
    store=FeedStore(tmp_path/'q');store.initialize('fake-owner',0)
    rows=[row(i,'已停止' if i==1 else '气泡'+str(i)) for i in range(1,16)]
    entry=row(16);entry['files']=[{'path':'workspace/image.png','filename':'image.png','mime':'image/png'},
                               {'path':'workspace/report.txt','filename':'report.txt','mime':'text/plain'}]
    rows+=[entry,row(17,unsupported=True)]
    missing=row(18);missing['files']=[{'unavailable':True}];rows.append(missing)
    store.ingest(rows)
    actions=[(r[0],json.loads(r[1])) for r in store.db.execute('SELECT kind,body FROM actions ORDER BY n')]
    assert [v['text'] for k,v in actions[:15]]==['已停止']+['气泡'+str(i) for i in range(2,16)]
    assert [k for k,v in actions[15:17]]==['file','file']
    assert '暂无法转发' in actions[17][1]['text'] and '附件暂无法取得' in actions[18][1]['text']
    store.close()

def test_ignored_failed_task_does_not_halt_generation(tmp_path):
    feed=MainFeed(SimpleNamespace(),{'runtime':tmp_path},SimpleNamespace(chat=lambda *a,**k:None))
    feed.store.initialize('fake-owner',0);feed.enabled=True
    ignored=row(1,ignore_reason='task_status_card');ignored.update(main=False,status=None)
    feed.call=lambda *a,**k:{'owner':'fake-owner','busy':False,'rows':[ignored,row(2,'正常正文')]}
    feed.poll()
    assert not feed.generation_halted and feed.observed==1 and feed.store.head()['body']['text']=='正常正文'
    feed.close()

def test_task_and_placeholder_native_schema_contract():
    node=node_executable()
    result=subprocess.run([str(node),str(Path(__file__).with_name('test_feed_task_filter_ui.cjs'))],
        capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['checks']==39
