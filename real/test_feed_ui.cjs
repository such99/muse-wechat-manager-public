const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const {fixture}=require('./test_feed_fixture.cjs');
const original=fs.readFileSync(path.join(__dirname,'native_ui.js'),'utf8');
const feed=fs.readFileSync(path.join(__dirname,'native_feed.js'),'utf8');
const source=original.replace('return {probe, bind, diagnose};',feed+'\nreturn {feedBind};');
let checks=0;
const event=(seq,role,text,extra={})=>({seq,message_id:'native-'+seq,role,transcript_surface:'main_chat',chat_context:{originating_channel:'main'},
  payload:{content:text,status:'completed',is_thread:false},...extra});
async function run(){
 const f=fixture(),requests=[];let events=[event(1,'assistant','历史')];
 let channel='all';
 f.rpc.sendRequest=async(name,params)=>{requests.push({name,params});return {channel,chat_events:events.filter(e=>params.after_seq==null||e.seq>params.after_seq),has_more:false};};
 const b=vm.runInContext(source,f.context).feedBind();
 assert.equal((await b.scan(null)).rows[0].ready,false);f.advance(3100);
 assert.equal((await b.scan(null)).rows[0].ready,true);checks++;
 // Explicitly completed, hydrated plain text needs two stable reads at 400ms.
 events=[event(301,'assistant','已完成文字')];
 f.state.chat.messagesById['native-301']={content:'已完成文字',isTextStreaming:false};
 assert.equal((await b.scan(300)).rows[0].ready,false);f.advance(399);
 assert.equal((await b.scan(300)).rows[0].ready,false);f.advance(1);
 assert.equal((await b.scan(300)).rows[0].ready,true);checks++;
 // A text change restarts the clock; active generation/loading never goes fast.
 events[0].payload.content='最终完整文字';
 assert.equal((await b.scan(300)).rows[0].ready,false);f.advance(400);
 f.state.chat.isStreaming=true;assert.equal((await b.scan(300)).rows[0].ready,false);
 f.state.chat.isStreaming=false;f.state.chat.messagesById['native-301'].isTextStreaming=true;
 assert.equal((await b.scan(300)).rows[0].ready,false);
 f.state.chat.messagesById['native-301'].isTextStreaming=false;f.state.chat.isLoading=true;
 assert.equal((await b.scan(300)).rows[0].ready,false);f.state.chat.isLoading=false;
 assert.equal((await b.scan(300)).rows[0].ready,true);checks++;
 // Unknown completion, missing projection, media, and cards retain 3 seconds.
 for(const mode of ['unknown','unhydrated','media','card']){
   events=[event(302,'assistant','保守确认')];
   f.state.chat.messagesById['native-302']={content:'保守确认'};
   if(mode==='unknown')delete events[0].payload.status;
   if(mode==='unhydrated')delete f.state.chat.messagesById['native-302'];
   if(mode==='media')events[0].payload.metadata={attachments:[{path:'workspace/test.txt'}]};
   if(mode==='card')events[0].payload.kind='fallback';
   await b.scan(301);f.advance(400);assert.equal((await b.scan(301)).rows[0].ready,false);
   f.advance(2600);assert.equal((await b.scan(301)).rows[0].ready,true);
   events=[];await b.scan(302);
 }
 delete f.state.chat.messagesById['native-301'];delete f.state.chat.messagesById['native-302'];checks++;
 // Web user inputs are explicitly tagged and never turned into assistant rows.
 events=[event(2,'user','网页输入'),...Array.from({length:15},(_,i)=>event(i+3,'assistant','原生气泡'+i))];
 let snap=await b.scan(1);f.advance(3100);snap=await b.scan(1);
 assert.equal(snap.rows.length,16);assert.equal(snap.rows.filter(r=>r.role==='assistant').length,15);
 assert.ok(snap.rows.every(r=>r.ready));assert.deepEqual(JSON.parse(JSON.stringify(requests.at(-1).params)),{limit:100,transcript_mode:'messages',after_seq:1});checks++;
 // No DOM old-node prefix is used, even if the projection has been replaced.
 f.nodes.clear();delete f.state.chat.messagesById.old;assert.equal((await b.scan(1)).rows.length,16);checks++;
 events=[event(18,'assistant','主动提醒',{chat_context:{originating_channel:'scheduler'}})];await b.scan(17);f.advance(3100);f.state.chat.isStreaming=true;
 assert.equal((await b.scan(17)).rows[0].ready,false);f.state.chat.isStreaming=false;assert.equal((await b.scan(17)).rows[0].main,true);checks++;
 events=[event(19,'assistant','旁聊',{payload:{content:'旁聊',is_thread:true}}),event(20,'assistant','子代理',{transcript_surface:'subagent'}),event(201,'assistant','未知界面',{transcript_surface:null})];
 assert.ok((await b.scan(18)).rows.every(r=>!r.main));checks++;
 channel='thread';await assert.rejects(()=>b.scan(18),/feed_history_shape_changed/);channel='all';
 events=[event(21,'assistant','附件',{payload:{content:'附件',metadata:{attachments:[{filename:'a.txt',path:'workspace/a.txt',mime_type:'text/plain',previewUrl:'FAKE-AUTH-URL'},
   {path:'https://fake.invalid/?token=FAKE-SECRET'}]}}})];
 snap=await b.scan(20);assert.equal(snap.rows[0].files[0].path,'workspace/a.txt');assert.ok(snap.rows[0].files[1].unavailable);
 assert.ok(!JSON.stringify(snap.rows).includes('FAKE-'));checks++;
 // Real observed schema: canonical payload + projection duplicate one file card.
 const card={path:'sandbox://workspace/example.txt',label:'示例',mime:'text/plain',missing:false,
   variants:{original:'https://fake.invalid/?token=FAKE-CARD-SECRET'},byte_len:27};
 events=[event(211,'assistant','示例.txt',{payload:{kind:'file',display_text:'示例.txt',data:{file:card},is_thread:false}})];
 f.state.chat.messagesById['native-211']={content:'示例.txt',presentationCarrier:true,
   presentations:[{kind:'file',data:{file:{...card,byteLen:27}}}]};
 snap=await b.scan(210);assert.deepEqual(JSON.parse(JSON.stringify(snap.rows[0].files)),
   [{path:'workspace/example.txt',filename:'example.txt',mime:'text/plain'}]);
 assert.equal(snap.rows[0].unsupported,false);assert.ok(!JSON.stringify(snap.rows).includes('FAKE-CARD'));checks++;
 // Canonical cards must work even when they have not hydrated into the projection.
 delete f.state.chat.messagesById['native-211'];assert.equal((await b.scan(210)).rows[0].files.length,1);checks++;
 events=[event(212,'assistant','卡片',{payload:{kind:'unknown-card',data:{url:'FAKE-SECRET'}}})];
 snap=await b.scan(211);assert.equal(snap.rows[0].unsupported,true);assert.ok(!JSON.stringify(snap.rows).includes('FAKE-SECRET'));checks++;
 events=[event(213,'assistant','丢失附件',{payload:{kind:'file',data:{file:{...card,missing:true}}}})];
 assert.equal((await b.scan(212)).rows[0].files[0].unavailable,true);checks++;
 // Observed document titles must not replace actual extensions in WeChat.
 const doc={...card,path:'workspace/examples/示例行动清单.docx',label:'示例行动清单',
   mime:'application/vnd.openxmlformats-officedocument.wordprocessingml.document'};
 events=[event(214,'assistant','文档',{payload:{kind:'file',data:{file:doc}}})];
 snap=await b.scan(213);assert.equal(snap.rows[0].files[0].filename,'示例行动清单.docx');checks++;
 // Real image card payload and projection identify the same image once.
 const image={...card,path:'sandbox://workspace/examples/示例行动清单.png',label:'示例行动清单',mime:'image/png'};
 events=[event(215,'assistant','图片',{payload:{kind:'image',data:{images:[image]}}})];
 f.state.chat.messagesById['native-215']={presentations:[{kind:'image',data:{images:[image]}}]};
 snap=await b.scan(214);assert.equal(snap.rows[0].files.length,1);
 assert.equal(snap.rows[0].files[0].filename,'示例行动清单.png');assert.equal(snap.rows[0].unsupported,false);
 assert.ok(!JSON.stringify(snap.rows).includes('FAKE-CARD'));checks++;
 // Image-only cards are supported without inventing a text reply or URL fetch.
 delete f.state.chat.messagesById['native-215'];delete events[0].payload.content;
 snap=await b.scan(214);assert.equal(snap.rows[0].unsupported,false);assert.equal(snap.rows[0].files.length,1);checks++;
 events=[event(216,'assistant','坏图片',{payload:{kind:'image',data:{images:[]}}})];
 assert.equal((await b.scan(215)).rows[0].unsupported,true);checks++;
 // Published native video contract: original references, multiple files once.
 const video={...card,path:'workspace/movie.mp4',label:'演示',mime:'video/mp4'};
 const video2={...video,path:'workspace/movie2.mp4'};
 events=[event(217,'assistant','视频',{payload:{kind:'video',data:{videos:[video,video2]}}})];
 f.state.chat.messagesById['native-217']={presentations:[{kind:'video',data:{videos:[video,video2]}}]};
 snap=await b.scan(216);assert.equal(snap.rows[0].files.length,2);
 assert.equal(snap.rows[0].files[0].filename,'movie.mp4');assert.equal(snap.rows[0].unsupported,false);
 assert.ok(!JSON.stringify(snap.rows).includes('FAKE-CARD'));checks++;
 // Native audio is an original file, not a claimed WeChat voice bubble.
 const audio={...card,path:'workspace/song.mp3',mime:'audio/mpeg'};
 events=[event(218,'assistant','',{payload:{kind:'audio',data:{audio}}})];
 snap=await b.scan(217);assert.equal(snap.rows[0].files[0].filename,'song.mp3');
 assert.equal(snap.rows[0].unsupported,false);checks++;
 const deck={...card,path:'workspace/deck.pptx',mime:'application/vnd.openxmlformats-officedocument.presentationml.presentation'};
 events=[event(219,'assistant','幻灯片',{payload:{kind:'slides',data:{deck,slug:'example-slides'}}})];
 snap=await b.scan(218);assert.equal(snap.rows[0].files[0].filename,'deck.pptx');
 assert.equal(snap.rows[0].unsupported,false);assert.ok(!JSON.stringify(snap.rows).includes('example-slides'));checks++;
 // Buttons become readable body/labels, never actions or authorization URLs.
 const buttonData={text:'请确认这条提醒',button:{label:'确认',url:'FAKE-AUTH-URL',value:'FAKE-ACTION'},
   secondary_buttons:[{label:'取消',value:'FAKE-CANCEL'}]};
 events=[event(220,'assistant','',{payload:{kind:'text_with_button',data:buttonData}})];
 f.state.chat.messagesById['native-220']={presentations:[{kind:'text_with_button',data:{textWithButton:buttonData}}]};
 snap=await b.scan(219);assert.equal(snap.rows[0].unsupported,false);
 assert.equal(snap.rows[0].text.split('请确认这条提醒').length-1,1);
 assert.ok(snap.rows[0].text.includes('确认、取消'));assert.ok(snap.rows[0].text.includes('主要聊天操作'));
 assert.ok(!JSON.stringify(snap.rows).includes('FAKE-'));checks++;
 events=[event(221,'assistant','',{payload:{kind:'future-widget',data:{secret:'FAKE-SECRET'}}})];
 f.state.chat.messagesById['native-221']={presentations:[{kind:'future-widget',displayText:'可读标题',data:{url:'FAKE-SECRET'}}]};
 snap=await b.scan(220);assert.equal(snap.rows[0].text,'可读标题');assert.equal(snap.rows[0].unsupported,true);
 assert.ok(!JSON.stringify(snap.rows).includes('FAKE-'));checks++;
 events=[event(222,'assistant','',{payload:{kind:'video',data:{videos:[]}}}),
   event(223,'assistant','',{payload:{kind:'audio',data:{audio:{url:'FAKE-SECRET'}}}})];
 snap=await b.scan(221);assert.ok(snap.rows[0].unsupported);assert.ok(snap.rows[1].files[0].unavailable);
 assert.ok(!JSON.stringify(snap.rows).includes('FAKE-'));checks++;
 events=[event(224,'assistant','',{payload:{kind:'fallback',data:{markdown:'保留的卡片正文',authorization:'FAKE-SECRET'}}})];
 snap=await b.scan(223);assert.equal(snap.rows[0].text,'保留的卡片正文');assert.equal(snap.rows[0].unsupported,false);
 assert.ok(!JSON.stringify(snap.rows).includes('FAKE-'));checks++;
 // A media card title is not an extra authored message before the original.
 events=[event(225,'assistant','',{payload:{kind:'file',display_text:'示例',data:{file:card}}})];
 f.state.chat.messagesById['native-225']={content:'示例',presentationCarrier:true,
   presentations:[{kind:'file',displayText:'示例',data:{file:card}}]};
 snap=await b.scan(224);assert.equal(snap.rows[0].text,'');assert.equal(snap.rows[0].files.length,1);
 assert.equal(snap.rows[0].unsupported,false);checks++;
 // Filename captions, image labels, and a pure multi-file list all disappear.
 events=[event(226,'assistant','',{payload:{kind:'file',display_text:'example.txt',data:{file:card}}}),
   event(227,'assistant','',{payload:{kind:'image',display_text:'示例行动清单',data:{images:[image]}}}),
   event(228,'assistant','',{payload:{kind:'video',display_text:'movie.mp4\nmovie2.mp4',data:{videos:[video,video2]}}})];
 snap=await b.scan(225);assert.ok(snap.rows.every(r=>r.text===''&&!r.unsupported&&r.files.length>0));checks++;
 // A genuine authored caption wins over an unrelated synthetic display label.
 events=[event(229,'assistant','',{payload:{kind:'file',content:'这是整理好的报告，请查收。',display_text:'示例',data:{file:card}}})];
 f.state.chat.messagesById['native-229']={content:'示例',presentations:[{kind:'file',displayText:'example.txt',data:{file:card}}]};
 snap=await b.scan(228);assert.equal(snap.rows[0].text,'这是整理好的报告，请查收。');checks++;
 // Naming a file in ordinary prose, and a filename in a separate text bubble,
 // are not enough to discard actual conversation content.
 events=[event(230,'assistant','',{payload:{kind:'file',content:'请打开 example.txt 查看结果。',data:{file:card}}}),
   event(231,'assistant','example.txt')];
 snap=await b.scan(229);assert.equal(snap.rows[0].text,'请打开 example.txt 查看结果。');
 assert.equal(snap.rows[1].text,'example.txt');checks++;
 // User text is untouched; the receiver still excludes user rows from outbox.
 events=[event(232,'user','',{payload:{kind:'file',content:'example.txt',data:{file:card}}})];
 snap=await b.scan(231);assert.equal(snap.rows[0].text,'example.txt');checks++;
 // Keep a missing file's title as context; never hide its failure notice.
 events=[event(233,'assistant','',{payload:{kind:'file',display_text:'示例',data:{file:{...card,missing:true}}}})];
 snap=await b.scan(232);assert.equal(snap.rows[0].text,'示例');assert.ok(snap.rows[0].files[0].unavailable);checks++;
 events=[event(22,'assistant','无ID',{message_id:null,occurred_at_ms:100000})];
 assert.ok((await b.scan(21)).rows[0].message_id.startsWith('fallback:'));checks++;
 b.submit('r','仅本次原文',[]);f.ack({ok:true,messageId:'ack'});await new Promise(r=>setImmediate(r));
 assert.equal(b.attempt('r').phase,'acknowledged');b.submit('r','重复',[]);assert.equal(f.calls.length,1);
 assert.equal(f.calls[0][0],'仅本次原文');assert.equal(f.calls[0][2].targetRoute.kind,'main');checks++;
 f.auth.userId='another-person';await assert.rejects(()=>b.scan(22),/main_identity_changed/);checks++;
 console.log(JSON.stringify({result:'PASS',checks,real_messages_sent:0}));
}
run().catch(e=>{console.error('Feed fake fixture failed: '+e.stack);process.exitCode=1});
