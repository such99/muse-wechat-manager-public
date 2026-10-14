const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const {fixture}=require('./test_feed_fixture.cjs');
const source=fs.readFileSync(path.join(__dirname,'native_ui.js'),'utf8').replace('return {probe, bind, diagnose};',
  fs.readFileSync(path.join(__dirname,'native_feed.js'),'utf8')+'\nreturn {feedBind};');
const event=(seq,text,extra={})=>({seq,message_id:'fixture-'+seq,role:'assistant',transcript_surface:'main_chat',
  payload:{content:text,status:'completed'},...extra});
let checks=0;
async function run(){
 const f=fixture();let events=[];
 f.rpc.sendRequest=async()=>({channel:'all',chat_events:events,has_more:false});
 const b=vm.runInContext(source,f.context).feedBind();
 for(const kind of ['html','external_link','space_build_completed','navigation','scheduled_task_created_activity','internal_link_action_card']){
   events=[event(90,'',{payload:{kind,data:{title:'假数据标题',summary:'假数据说明',
     html:'FAKE-PRIVATE-HTML',url:'FAKE-AUTH-URL',data:{url:'FAKE-AUTH-URL'},ui_control:{action:'navigate'}}}})];
   const row=(await b.scan(null)).rows[0];
   assert.equal(row.main,true);assert.equal(row.unsupported,false);assert.ok(row.card_notice.length>0);
   assert.ok(!JSON.stringify(row).includes('FAKE-'));assert.ok(row.text.includes(row.card_notice));checks++;
 }
 events=[event(91,'正常搜索结果',{resources:[{kind:'citation',uri:'FAKE-SECRET-URL',metadata:{token:'FAKE-SECRET'}}]})];
 let cited=(await b.scan(null)).rows[0];assert.equal(cited.unsupported,false);
 assert.ok(cited.text.includes('正常搜索结果'));assert.ok(cited.text.includes('[网页来源]'));
 assert.ok(!JSON.stringify(cited).includes('FAKE-'));checks++;
 events=[event(92,'普通主动提醒',{chat_context:{originating_channel:'scheduler'}})];
 cited=(await b.scan(null)).rows[0];assert.equal(cited.main,true);assert.equal(cited.text,'普通主动提醒');checks++;
 for(const status of ['running','stopped','failed','interrupted']){
   events=[event(1,'已停止',{display_text:'已停止',payload:{kind:'browser_task',content:'已停止',status,
      data:{browser_task_id:'fake-task',status,preview_url:'FAKE-SECRET-URL'}}})];
   const row=(await b.scan(null)).rows[0];
   assert.equal(row.ignore_reason,'task_status_card');assert.equal(row.main,false);
   assert.equal(row.status,null);assert.equal(row.text,'');assert.equal(row.unsupported,false);
   assert.ok(!JSON.stringify(row).includes('FAKE-SECRET'));checks++;
 }
 f.state.chat.messagesById['fixture-2']={content:'已停止',presentationCarrier:true,
   presentations:[{kind:'browser_task',displayText:'已停止',data:{browserTask:{status:'stopped'}}}]};
 events=[event(2,'已停止')];assert.equal((await b.scan(null)).rows[0].ignore_reason,'task_status_card');checks++;
 events=[event(2,'普通正文：已停止',{payload:{kind:'text',content:'普通正文：已停止',status:'completed'}})];
 let row=(await b.scan(null)).rows[0];assert.equal(row.main,true);assert.equal(row.text,'普通正文：已停止');checks++;
 for(const text of ['已停止','任务运行中','我已经停止上一次操作，现在继续回答。']){
   events=[event(3,text)];row=(await b.scan(null)).rows[0];
   assert.equal(row.main,true);assert.equal(row.text,text);assert.equal(row.ignore_reason,null);checks++;
 }
 for(const text of [null,'','   ']){
   events=[event(4,text)];row=(await b.scan(null)).rows[0];
   assert.equal(row.main,false);assert.equal(row.unsupported,false);assert.equal(row.ignore_reason,'empty_placeholder');checks++;
 }
 events=[event(5,undefined,{payload:{status:'completed'}})];
 assert.equal((await b.scan(null)).rows[0].ignore_reason,'empty_placeholder');checks++;
 f.state.chat.messagesById['fixture-6']={content:'',isTextStreaming:true};events=[event(6,'')];
 await b.scan(null);f.advance(4000);assert.equal((await b.scan(null)).rows[0].ready,false);
 f.state.chat.messagesById['fixture-6'].isTextStreaming=false;
 f.state.chat.messagesById['fixture-6'].content='生成完成后的正文';events[0].payload.content='生成完成后的正文';
 row=(await b.scan(null)).rows[0];assert.equal(row.main,true);assert.equal(row.ready,false);
 f.advance(400);assert.equal((await b.scan(null)).rows[0].ready,true);checks++;
 for(const status of ['failed','interrupted']){
   events=[event(7,'',{payload:{status,content:''}})];row=(await b.scan(null)).rows[0];
   assert.equal(row.main,true);assert.equal(row.ignore_reason,null);assert.equal(row.status,status);checks++;
 }
 for(const payload of [{kind:'unreviewed_card',data:{secret:'FAKE-ONLY'}},{kind:'image',data:{images:[]}}]){
   events=[event(8,'',{payload})];row=(await b.scan(null)).rows[0];
   assert.equal(row.main,true);assert.equal(row.unsupported,true);checks++;
 }
 events=[event(9,'',{resources:[{kind:'unknown'}]})];assert.equal((await b.scan(null)).rows[0].unsupported,true);checks++;
 const file={path:'workspace/fixture.txt',filename:'fixture.txt',mime:'text/plain'};
 events=[event(10,'已停止',{payload:{kind:'browser_task',data:{status:'stopped'},metadata:{attachments:[file]}}})];
 row=(await b.scan(null)).rows[0];assert.equal(row.main,true);assert.equal(row.files.length,1);assert.equal(row.text,'');checks++;
 f.state.chat.messagesById['fixture-11']={presentationCarrier:true,presentations:[
   {kind:'browser_task',displayText:'已停止'},{kind:'fallback',data:{markdown:'需要保留的正式说明'}}]};
 events=[event(11,'已停止',{payload:{kind:'browser_task',content:'已停止'}})];
 row=(await b.scan(null)).rows[0];assert.equal(row.main,true);assert.equal(row.text,'需要保留的正式说明');checks++;
 // Task failures belong to the card, including when files or authored text survive.
 for(const status of ['failed','interrupted']){
   events=[event(110,'已停止',{payload:{kind:'browser_task',status,content:'已停止',
     data:{status},metadata:{attachments:[file]}}})];
   row=(await b.scan(null)).rows[0];
   assert.equal(row.main,true);assert.equal(row.files.length,1);assert.equal(row.text,'');
   assert.equal(row.status,null);assert.equal(row.ignore_reason,null);checks++;
   f.state.chat.messagesById['fixture-111']={presentationCarrier:true,presentations:[
     {kind:'browser_task',displayText:'已停止'},{kind:'fallback',data:{markdown:'任务之后的正式说明'}}]};
   events=[event(111,'已停止',{payload:{kind:'browser_task',content:'已停止',status}})];
   row=(await b.scan(null)).rows[0];
   assert.equal(row.main,true);assert.equal(row.text,'任务之后的正式说明');
   assert.equal(row.status,null);assert.equal(row.ignore_reason,null);checks++;
 }
 // Actual assistant-generation failures still retain their failure state.
 for(const kind of ['text','message'])for(const status of ['failed','interrupted']){
   events=[event(112,'部分正文',{payload:{kind,content:'部分正文',status}})];
   row=(await b.scan(null)).rows[0];assert.equal(row.main,true);
   assert.equal(row.text,'部分正文');assert.equal(row.status,status);checks++;
 }
 events=[event(12,'',{payload:{kind:'file',data:{file:{...file,missing:true}}}})];
 row=(await b.scan(null)).rows[0];assert.equal(row.main,true);assert.equal(row.files[0].unavailable,true);checks++;
 events=Array.from({length:15},(_,i)=>event(20+i,'气泡'+i));
 let rows=(await b.scan(null)).rows;assert.equal(rows.length,15);assert.ok(rows.every(r=>r.main));
 assert.deepEqual(JSON.parse(JSON.stringify(rows.map(r=>r.text))),Array.from({length:15},(_,i)=>'气泡'+i));checks++;
 console.log(JSON.stringify({result:'PASS',checks,real_messages_sent:0}));
}
run().catch(e=>{console.error(e.stack);process.exitCode=1});
