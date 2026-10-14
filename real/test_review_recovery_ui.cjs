const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const {fixture}=require('./test_feed_fixture.cjs');
const source=fs.readFileSync(path.join(__dirname,'native_ui.js'),'utf8').replace('return {probe, bind, diagnose};',
  fs.readFileSync(path.join(__dirname,'native_feed.js'),'utf8')+'\nreturn {feedBind};');
async function run(){
 const f=fixture(),reader=vm.runInContext(source,f.context).feedBind();let checks=0;
 assert.equal(reader.reviewedRelease(null),true);assert.equal(f.calls.length,0);checks++;
 reader.submit('one','FAKE-TEXT',[]);
 assert.throws(()=>reader.reviewedRelease('one'),/previous_submission_unresolved/);checks++;
 f.ack({ok:false});await new Promise(resolve=>setImmediate(resolve));
 assert.throws(()=>reader.reviewedRelease('other'),/previous_submission_unresolved/);checks++;
 assert.equal(reader.reviewedRelease('one'),true);
 assert.equal(reader.attempt('one').phase,'reviewed');assert.equal(f.calls.length,1);checks++;
 const pending=fixture(),resolvers=[];
 pending.control.sendMessage=(...args)=>new Promise(resolve=>resolvers.push(resolve));
 const r=vm.runInContext(source,pending.context).feedBind();
 r.submit('old','FAKE-OLD',[]);
 assert.throws(()=>r.reviewedRelease('old','not_submitted_reviewed'),/previous_submission_unresolved/);
 assert.equal(r.reviewedRelease('old','acknowledged'),true);assert.equal(resolvers.length,1);checks++;
 r.submit('new','FAKE-NEW',[]);resolvers[0]({ok:true,messageId:'fake-old'});
 await new Promise(resolve=>setImmediate(resolve));
 assert.throws(()=>r.reviewedRelease('new','not_submitted_reviewed'),/previous_submission_unresolved/);
 assert.equal(resolvers.length,2);checks++;
 console.log(JSON.stringify({checks,real_messages_sent:0}));
}
run().catch(e=>{console.error(e.stack);process.exitCode=1});
