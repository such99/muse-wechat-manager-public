// Deterministic production-adapter regression. No network or real account.
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const {File}=require('node:buffer');
const script=fs.readFileSync(require('node:path').join(__dirname,'native_ui.js'),'utf8');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function fixture(){
  const callbacks=new Set(),calls=[],reads=[],nodes=new Map();let ack,now=1000;
  const state={chat:{messagesById:{old:{id:'old',role:'assistant',content:'OLD MUST NOT LEAK'}},mainHistorySourceKey:'fake-main'},
    composer:{inputValue:'',pendingAttachments:[],pendingSend:null},activity:{currentChatRouteKey:'main'}};
  Object.defineProperty(state.chat,'messageIds',{get:()=>state.orderOverride||Object.keys(state.chat.messagesById)});
  const route={activeChatRoute:{kind:'main'},baseChatRoute:{kind:'main'}},auth={isAuthenticated:true,userId:'fake-owner'};
  const control={ensureActiveChatSendReady(){},quotedMessage:null,sendMessage(...args){calls.push(args);return new Promise(resolve=>ack=resolve)}};
  const rpc={isReady:true,gatewayScopeKey:'fake-scope',gatewayUrl:'https://fake.invalid',vmName:'fake-vm',
    onEvent(name,callback){assert.equal(name,'delta.message_done');callbacks.add(callback);return()=>callbacks.delete(callback)},
    async sendRequest(name,params,options){reads.push({name,params,options});return{data_base64:Buffer.from('PNGDATA').toString('base64'),len:7,eof:true}}};
  const ready={activeHistoryReady:true,chatSubscriptionReady:true,activeHistoryFailed:false,gatewayScopeKey:rpc.gatewayScopeKey,gatewayUrl:rpc.gatewayUrl};
  let first=null;for(const value of [control,route,auth,rpc,ready,{getState:()=>state}])first={memoizedValue:value,next:first};
  const input={value:'',offsetWidth:1,getClientRects:()=>[1],'__reactFiber$fake':{dependencies:{firstContext:first},return:null}};
  const stop={offsetWidth:1,getClientRects:()=>[1]},options={stop:false,hiddenStop:false,noBubbleIds:false,hideReply:false};
  const bubbleList=()=>Object.entries(state.chat.messagesById).filter(([id,m])=>m.role==='assistant'&&!(options.hideReply&&id!=='old')).map(([id,m])=>{
    if(!nodes.has(id))nodes.set(id,{className:'hatch-chat-groupable-bubble hatch-agent-bubble-bg',offsetWidth:1,getClientRects:()=>[1],closest:()=>null});
    const node=nodes.get(id);node.innerText=m.content||'';
    node['__reactFiber$fake']={memoizedProps:options.noBubbleIds?{}:{messageId:id},return:null};return node;
  });
  const querySelectorAll=selector=>selector.startsWith('div[class*="hatch-chat-groupable-bubble"]')?bubbleList():
    selector.startsWith('[data-testid=')?(options.stop?[stop]:options.hiddenStop?[{offsetWidth:0,getClientRects:()=>[]}]:[]):[input];
  const context=vm.createContext({document:{querySelectorAll},location:{origin:'https://muse.ai',pathname:'/'},
    Uint8Array,File,Promise,Map,Set,Error,JSON,Number,Date:{now:()=>now},atob:x=>Buffer.from(x,'base64').toString('binary')});
  const api=vm.runInContext(script,context);
  return{api,state,route,auth,control,rpc,input,calls,reads,context,nodes,options,advance:(ms=250)=>now+=ms,
    ack:r=>ack(r),emit:e=>{for(const c of callbacks)c(e)},
    addContext:value=>input['__reactFiber$fake'].dependencies.firstContext={memoizedValue:value,next:input['__reactFiber$fake'].dependencies.firstContext}};
}
module.exports={fixture};
