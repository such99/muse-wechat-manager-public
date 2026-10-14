/* Native Muse main-chat adapter. No credentials, persistent state or network interception.
   Uses reviewed live frontend interfaces; fail closed when their shape changes. */
(function () {
  const objects = () => {
    const candidates=new Map();
    // The current main page lazily mounts its textarea after interaction; its
    // stable composer container already owns the same native React contexts.
    // Prefer the hydrated textarea, whose ancestors include providers mounted
    // inside the container. An unhydrated SSR shell is never a valid fallback.
    const anchors=[...document.querySelectorAll('textarea'),...document.querySelectorAll('[data-hatch-composer-chrome]')];
    for(const input of anchors){
      if(!(input.offsetWidth || input.offsetHeight || input.getClientRects().length))continue;
      const key = Object.keys(input).find(k => k.startsWith('__reactFiber$'));
      const values = new Set();
      for (let f = key ? input[key] : null, depth = 0; f && depth < 160; f = f.return, depth++) {
        const p = f.memoizedProps;
        if (p?.value && typeof p.value === 'object') values.add(p.value);
        for (let c = f.dependencies?.firstContext; c; c = c.next)
          if (c.memoizedValue && typeof c.memoizedValue === 'object') values.add(c.memoizedValue);
      }
      const controls=[...values].filter(v=>typeof v.sendMessage==='function');
      if(controls.length>1)throw Error('context_ambiguous');
      if(controls.length===1&&!candidates.has(controls[0]))candidates.set(controls[0],{input,values:[...values]});
    }
    if(candidates.size!==1)throw Error(candidates.size?'context_ambiguous':'composer_missing');
    return candidates.values().next().value;
  };
  const one = list => { if (list.length !== 1) throw Error('context_ambiguous'); return list[0]; };
  const discover = () => {
    if (location.origin !== 'https://muse.ai' || location.pathname !== '/') throw Error('main_route_required');
    const {values,input} = objects();
    const control = one(values.filter(v => typeof v.sendMessage === 'function' && typeof v.ensureActiveChatSendReady === 'function'));
    const route = one(values.filter(v => v.activeChatRoute && v.baseChatRoute));
    // The live frontend exposes the same RPC facade through several providers.
    // Collapse only exact method/target aliases; distinct backends still fail.
    const rpcKeys=['sendRequest','onEvent','isReady','gatewayScopeKey','gatewayUrl','vmName'];
    const rpcs=values.filter(v=>typeof v.sendRequest==='function'&&typeof v.onEvent==='function');
    const rpc=one(rpcs.filter((v,i)=>rpcs.findIndex(w=>rpcKeys.every(k=>w[k]===v[k]))===i));
    const stores = values.filter(v => typeof v.getState === 'function').map(v => ({store: v, state: v.getState()}))
      .filter(v => v.state?.chat?.messagesById && typeof v.state?.composer?.inputValue==='string');
    const {store, state} = one(stores);
    const auth = one(values.filter(v => typeof v.isAuthenticated === 'boolean' && typeof v.userId === 'string'));
    const readiness = one(values.filter(v => typeof v.activeHistoryReady === 'boolean' && typeof v.chatSubscriptionReady === 'boolean'));
    if(typeof rpc.gatewayScopeKey!=='string'||!rpc.gatewayScopeKey||typeof rpc.gatewayUrl!=='string'||!rpc.gatewayUrl||
       typeof rpc.vmName!=='string'||!rpc.vmName||rpc.gatewayScopeKey!==readiness.gatewayScopeKey||rpc.gatewayUrl!==readiness.gatewayUrl)
      throw Error('main_identity_missing');
    if (!auth.isAuthenticated || !auth.userId) throw Error('authentication_required');
    if (route.activeChatRoute.kind !== 'main' || route.baseChatRoute.kind !== 'main') throw Error('effective_route_not_main');
    if (!rpc.isReady || !readiness.activeHistoryReady || !readiness.chatSubscriptionReady || readiness.activeHistoryFailed)
      throw Error('main_not_ready');
    if (state.activity?.currentChatRouteKey !== 'main' || !state.chat.mainHistorySourceKey) throw Error('main_identity_missing');
    return {control, route, rpc, store, state, auth,input};
  };
  const identity = c => JSON.stringify([c.auth.userId, c.rpc.gatewayScopeKey, c.rpc.gatewayUrl,
    c.rpc.vmName, c.state.chat.mainHistorySourceKey, c.state.activity.currentChatRootAgentId]);
  const draft = c => c.state.composer.inputValue || c.input.value || c.input.querySelector?.('textarea')?.value || '';
  const clean = c => {
    const composer = c.state.composer;
    if (c.control.quotedMessage || composer.pendingSend || composer.pendingAttachments?.length ||
        draft(c).length) throw Error('composer_has_user_draft');
    if (busy(c) || historyMoving(c)) throw Error('main_chat_busy');
  };
  const visible = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const historyMoving = c => ['isLoading','isLoadingOlder','isLoadingNewer','hasProvisionalCachedHistory',
    'isRefreshingCachedHistory','isCheckingPersistentCache','isViewingHistory'].some(k=>c.state.chat[k]===true);
  const mainActivityBusy = c => {
    const a=c.state.activity,entry=a.agentActivitiesByRootAgentId?.[a.currentChatRootAgentId];
    const code=entry?.agentActivityCode ?? (a.agentActivityRootAgentId===a.currentChatRootAgentId?a.agentActivityCode:null);
    // The reviewed main-page idle enum is "online". Unknown/active codes wait;
    // do not infer full-turn completion from a single bubble's done event.
    return typeof code==='string' && code!=='online';
  };
  const busy = c => mainActivityBusy(c) || c.state.chat.isStreaming===true || c.state.chat.isExpectingBotResponse===true ||
    Object.values(c.state.chat.messagesById).some(m=>m.isTextStreaming===true) ||
    [...document.querySelectorAll('[data-testid="hatch-composer-stop-button"],button[aria-label*="Stop" i],button[aria-label*="停止"]')].some(visible);
  const ordered = c => {
    const ids=c.state.chat.messageIds,ms=c.state.chat.messagesById;
    if(!Array.isArray(ids)||new Set(ids).size!==ids.length||ids.some(id=>typeof id!=='string'||!id||!ms[id]))
      throw Error('message_anchor_unavailable');
    // A second projection outside the ordered list cannot safely be ignored.
    if(Object.keys(ms).length!==ids.length)throw Error('message_anchor_unavailable');
    return ids;
  };
  const payload = m => JSON.stringify([m.role,m.content,m.images||[],m.files||[]]);
  const bubbles = c => [...document.querySelectorAll('div[class*="hatch-chat-groupable-bubble"]')]
    .filter(e=>/hatch-agent-bubble-bg/.test(e.className||'') && visible(e) && !e.closest('[role="dialog"],[role="menu"]'))
    .map(node=>{
      // IDs identify a rendered bubble; they are NOT a causal reply-to mapping.
      const key=Object.keys(node).find(k=>k.startsWith('__reactFiber$')),ids=new Set();
      for(let f=key?node[key]:null,d=0;f&&d<12;f=f.return,d++)
        for(const k of ['messageId','hostMessageId']){
          const id=f.memoizedProps?.[k];
          if(typeof id==='string'&&c.state.chat.messagesById[id]?.role==='assistant')ids.add(id);
        }
      if(ids.size>1)throw Error('bubble_identity_ambiguous');
      return {node,id:ids.size===1?[...ids][0]:null};
    });
  const diagnose = () => {
    const {values}=objects(),rpc=values.find(v=>typeof v.sendRequest==='function'&&typeof v.onEvent==='function'),
      ready=values.find(v=>typeof v.activeHistoryReady==='boolean'&&typeof v.chatSubscriptionReady==='boolean'),
      route=values.find(v=>v.activeChatRoute&&v.baseChatRoute),
      auth=values.find(v=>typeof v.isAuthenticated==='boolean');
    return {rpc_ready:rpc?.isReady===true,history_ready:ready?.activeHistoryReady===true,
      subscription_ready:ready?.chatSubscriptionReady===true,history_failed:ready?.activeHistoryFailed===true,
      effective_main:route?.activeChatRoute?.kind==='main',base_main:route?.baseChatRoute?.kind==='main',
      authenticated:auth?.isAuthenticated===true,controller_count:values.filter(v=>typeof v.sendMessage==='function').length,
      interface_counts:{
        control:values.filter(v=>typeof v.sendMessage==='function'&&typeof v.ensureActiveChatSendReady==='function').length,
        route:values.filter(v=>v.activeChatRoute&&v.baseChatRoute).length,
        rpc:values.filter(v=>typeof v.sendRequest==='function'&&typeof v.onEvent==='function').length,
        store:values.filter(v=>typeof v.getState==='function').map(v=>v.getState()).filter(v=>v?.chat?.messagesById&&typeof v?.composer?.inputValue==='string').length,
        auth:values.filter(v=>typeof v.isAuthenticated==='boolean'&&typeof v.userId==='string').length,
        readiness:values.filter(v=>typeof v.activeHistoryReady==='boolean'&&typeof v.chatSubscriptionReady==='boolean').length},
      rpc_shapes:values.filter(v=>typeof v.sendRequest==='function'&&typeof v.onEvent==='function').map(v=>({
        ready_boolean:typeof v.isReady==='boolean',scope_string:typeof v.gatewayScopeKey==='string',
        url_string:typeof v.gatewayUrl==='string',vm_string:typeof v.vmName==='string',
        scope_matches_readiness:typeof ready?.gatewayScopeKey==='string'&&v.gatewayScopeKey===ready.gatewayScopeKey,
        url_matches_readiness:typeof ready?.gatewayUrl==='string'&&v.gatewayUrl===ready.gatewayUrl})),
      rpc_equivalent:values.filter(v=>typeof v.sendRequest==='function'&&typeof v.onEvent==='function').every(v=>
        ['sendRequest','onEvent','isReady','gatewayScopeKey','gatewayUrl','vmName'].every(k=>v[k]===rpc?.[k])),
      credential_values_read:false};
  };
  const probe = () => {
    const c = discover();
    const ids=ordered(c),rendered=bubbles(c);
    return {main: true, authenticated: true, controller: true, readiness: true,
      stable_identity: true, has_user_draft: !!(c.control.quotedMessage || c.state.composer.pendingSend ||
        c.state.composer.pendingAttachments?.length || draft(c).length),
      message_count: Object.keys(c.state.chat.messagesById).length,
      anchor_ready:true,anchor_message_count:ids.length,anchor_bubble_count:rendered.length,
      main_busy:busy(c)||historyMoving(c),reply_association:'guarded_batch_anchor',
      credentials_read: false, messages_sent: 0};
  };
  const bind = () => {
    const original = discover(); clean(original);
    const bound = identity(original);
    const runs = new Map();
    let active = null;
    let committedIds=[...ordered(original)];
    const verify = () => { const c = discover(); if (identity(c) !== bound) throw Error('main_identity_changed'); return c; };
    const prune = () => { if (runs.size >= 10000) throw Error('request_capacity_reached'); };
    const read = id => {
      const run = runs.get(id);
      if (!run) return {phase: 'missing'};
      if(run.result)return run.result;
      if(['uncertain','failed','anchor_ambiguous','correlation_missing'].includes(run.phase))return {phase:run.phase,code:run.code||null};
      const fail = code => {run.phase='anchor_ambiguous';run.code=code;run.unsubscribe?.();run.unsubscribe=null;return {phase:run.phase,code};};
      const c=verify(),ms=c.state.chat.messagesById;
      let ids;try{ids=ordered(c);}catch(_){return fail('message_anchor_unavailable');}
      if(historyMoving(c)||ids.length<run.anchor.length || run.anchor.some((k,i)=>ids[i]!==k||payload(ms[k])!==run.before.get(k)))
        return fail('history_anchor_changed');
      const added=ids.slice(run.anchor.length),assistants=added.filter(k=>ms[k].role==='assistant'),users=added.filter(k=>ms[k].role==='user');
      if(users.length>1||added.length>assistants.length+users.length)return fail('concurrent_messages');
      if(assistants.length>8)return fail('reply_batch_too_large');
      if(!run.ack)return {phase:run.phase,code:run.code||null};
      const ack=ms[run.ack];
      if(!ack)return {phase:'waiting_ack_projection'};
      if(run.before.has(run.ack))return fail('ack_precedes_anchor');
      if(ack.role==='user'){
        if(users.length!==1||users[0]!==run.ack||ack.content!==run.text)return fail('input_anchor_mismatch');
      }else if(ack.role==='assistant'){
        if(users.length && ms[users[0]].content!==run.text)return fail('input_anchor_mismatch');
      }else{return fail('ack_role_unknown');}
      if(!assistants.length)return {phase:'waiting_new_bubble'};
      if(ack.role==='assistant' && (assistants.length!==1||assistants[0]!==run.ack))return fail('assistant_ack_mismatch');
      if(users.length && assistants.some(k=>ids.indexOf(k)<=ids.indexOf(users[0])))return fail('reply_precedes_input');
      // A batch must have one exact user ACK, an append-only sequence of native
      // runtime messages and individually identified DOM bubbles. This remains
      // guarded structural association, not a causal claim about proactive work.
      if(assistants.length>1 && (ack.role!=='user'||assistants.some(k=>ms[k].source!=='runtime'||ms[k].channelSender)))
        return fail('reply_batch_source_unconfirmed');
      if(assistants.some(k=>typeof ms[k].replyToMessageId==='string'&&ms[k].replyToMessageId &&
          ms[k].replyToMessageId!==run.ack&&!assistants.includes(ms[k].replyToMessageId)))return fail('reply_parent_conflict');
      if(run.answerIds.some((k,i)=>assistants[i]!==k))return fail('reply_identity_changed');
      run.answerIds=[...assistants];
      // Terminal events are optional corroboration. They never provide the
      // missing causal mapping and never replace the visual completion guard.
      if([...run.events.keys()].some(k=>!run.before.has(k)&&k!==run.ack&&!assistants.includes(k))){
        if(busy(c)){run.stable=null;return {phase:'generating'};}
        return fail('foreign_completion_event');
      }
      const failed=assistants.map(k=>run.events.get(k)).find(e=>e&&e.status!=='completed');
      if(failed){
        run.phase='failed';run.code='native_generation_'+failed.status;run.unsubscribe?.();run.unsubscribe=null;
        return {phase:run.phase,code:run.code};
      }
      let rendered;try{rendered=bubbles(c);}catch(_){return fail('bubble_identity_ambiguous');}
      if(assistants.some(k=>rendered.filter(b=>b.id===k).length>1))return fail('duplicate_reply_bubbles');
      if(assistants.length>1){
        if(assistants.some(k=>!rendered.some(b=>b.id===k))){
          const addedBubbles=rendered.filter(b=>!run.bubbleAnchor.some(old=>old.node===b.node));
          if(addedBubbles.length>=assistants.length&&addedBubbles.some(b=>!b.id))return fail('batch_bubble_identity_missing');
          return {phase:'waiting_new_bubble'};
        }
        const newIds=rendered.filter(b=>assistants.includes(b.id)).map(b=>b.id);
        if(newIds.some((k,i)=>k!==assistants[i]))return fail('render_anchor_mismatch');
      }else if(!rendered.some(b=>b.id===assistants[0])){
        // DOM identity fallback: all old bubbles must still form the exact
        // prefix; a re-render/virtualization must not make old text look new.
        if(rendered.length!==run.bubbleAnchor.length+1 || run.bubbleAnchor.some((b,i)=>rendered[i]?.node!==b.node))
          return rendered.some(b=>!run.bubbleAnchor.some(old=>old.node===b.node))?fail('render_anchor_changed'):{phase:'waiting_new_bubble'};
        const next=rendered[rendered.length-1];
        if(next.id&&next.id!==assistants[0])return fail('render_anchor_mismatch');
        if(run.bubbleAnchor.some(b=>b.node===next.node))return fail('render_anchor_changed');
      }
      if(rendered.some(b=>b.id&&!run.before.has(b.id)&&!assistants.includes(b.id)))return fail('foreign_reply_bubble');
      const signature=JSON.stringify(assistants.map(k=>[k,payload(ms[k])])),now=Date.now();
      if(busy(c)){run.stable=null;return {phase:'generating'};}
      if(!run.stable||run.stable.signature!==signature)run.stable={signature,since:now,last:now,count:1};
      else if(now-run.stable.last>=180){run.stable.last=now;run.stable.count++;}
      if(run.stable.count<12||now-run.stable.since<3000)return {phase:'waiting_stable_text'};
      const messages=assistants.map((k,bubble_index)=>({message_id:k,text:typeof ms[k].content==='string'?ms[k].content:'',
        files:[...(ms[k].images||[]),...(ms[k].files||[])].map(f=>({filename:f.filename,mime:f.mimeType,path:f.path||null,bubble_index}))}));
      const files=messages.flatMap(m=>m.files);
      run.phase='completed';run.unsubscribe?.();run.unsubscribe=null;
      run.committedIds=[...ids];
      run.result={phase:'completed',message_id:assistants.length===1?assistants[0]:null,message_ids:[...assistants],messages,
        text:messages.map(m=>m.text).join('\n\n'),files,association:'guarded_batch_anchor',
        terminal_event_observed:assistants.every(k=>run.events.get(k)?.status==='completed')};
      return run.result;
    };
    const start = (id, text, files) => {
      if (runs.has(id)) return {phase: runs.get(id).phase, duplicate: true};
      if (active !== null) throw Error('previous_submission_unresolved');
      const c = verify(); clean(c); prune();
      const current=ordered(c);
      if(current.length!==committedIds.length||current.some((k,i)=>k!==committedIds[i]))throw Error('message_anchor_unavailable');
      if (typeof text !== 'string' || !Array.isArray(files) || (!text.trim() && !files.length)) throw Error('empty_input');
      // Capture before send, including the entire ordered history and visible
      // bubble identities. No hidden history is transmitted back to Muse.
      const anchor=[...ordered(c)],bubbleAnchor=bubbles(c);
      const run = {phase:'submitting',ack:null,answerIds:[],text,anchor,bubbleAnchor,
        before:new Map(anchor.map(k=>[k,payload(c.state.chat.messagesById[k])])),stable:null,result:null,
        events:new Map(),unsubscribe:null};
      runs.set(id, run); active = id;
      run.unsubscribe = c.rpc.onEvent('delta.message_done', e => {
        if (typeof e.message_id !== 'string' || (e.stream_lane ?? e.transcript_surface) === 'subagent') return;
        if (run.events.size >= 128 && !run.events.has(e.message_id)) {
          run.phase = 'uncertain'; run.code = 'completion_event_capacity'; return;
        }
        run.events.set(e.message_id, {status: ['completed','failed','interrupted'].includes(e.status) ? e.status : 'unknown'});
      });
      let attachments;
      try {
        attachments = files.map(f => {
          const raw = Uint8Array.from(atob(f.data_base64), c => c.charCodeAt(0));
          const file = new File([raw], f.filename, {type: f.mime});
          return {filename: f.filename, mimeType: f.mime, category: f.category,
            dataBase64: f.data_base64, rawFile: file};
        });
      } catch (_) { run.phase = 'failed'; run.code = 'invalid_attachment'; run.unsubscribe?.(); active = null; return {phase: run.phase, code: run.code}; }
      // Explicit main target, NO session_id, system prompt, hiddenPrompt, history or retry queue.
      Promise.resolve(c.control.sendMessage(text, attachments, {targetRoute: {kind:'main'},
        waitForReadiness: false, failureReporting: 'caller', consumeComposerInput: false})).then(result => {
        if (result?.ok === true && typeof result.messageId === 'string' && result.messageId) {
          run.ack = result.messageId;
          if(run.phase==='submitting')run.phase = 'acknowledged';
        } else {
          run.phase = 'uncertain'; run.code = 'submission_not_confirmed';
          run.unsubscribe?.(); run.unsubscribe = null;
          // No resend or automatic unlocking after an ambiguous native failure.
        }
      }).catch(() => { run.phase = 'uncertain'; run.code = 'submission_not_confirmed'; run.unsubscribe?.(); run.unsubscribe = null; });
      return {phase: 'submitting', duplicate: false};
    };
    const release = id => {
      const result = read(id);
      if (!['completed','failed'].includes(result.phase)) throw Error('submission_unresolved');
      if (active === id) active = null;
      if(result.phase==='completed')committedIds=runs.get(id).committedIds;
      runs.get(id).unsubscribe?.();
      // Retain only a dedup tombstone, not 10,000 copies of main-chat history,
      // File objects and completed private replies in the browser heap.
      runs.set(id,{phase:'released'});
      return {ok: true};
    };
    const download = async (id, index, offset, limit) => {
      const c = verify(), result = read(id);
      if (result.phase !== 'completed' || !Number.isSafeInteger(index) || index < 0 || index >= result.files.length ||
          !Number.isSafeInteger(offset) || offset < 0 || !Number.isSafeInteger(limit) || limit < 1 || limit > 1048576)
        throw Error('invalid_media_request');
      let path = result.files[index].path;
      if (typeof path !== 'string') throw Error('attachment_path_missing');
      path = path.replace(/^sandbox:\/\//,'').replace(/^\/+/, '');
      if (!/^(workspace\/|mnt\/data\/)/.test(path) || /[\\:?#\x00-\x1f]/.test(path) || path.split('/').includes('..'))
        throw Error('attachment_path_rejected');
      const r = await c.rpc.sendRequest('fs.read', {path, offset, len: limit},
        {expectedGatewayTargetKey: JSON.stringify([c.rpc.gatewayUrl,c.rpc.vmName])});
      verify();
      if (!Number.isSafeInteger(r.len) || r.len < 0 || r.len > limit || typeof r.data_base64 !== 'string' ||
          atob(r.data_base64).length !== r.len || typeof r.eof !== 'boolean') throw Error('invalid_media_payload');
      return {data_base64: r.data_base64, len: r.len, eof: r.eof};
    };
    return {probe, start, read, release, download, status: () => ({bound:true, unresolved:active!==null, retained_attempts:runs.size})};
  };
  return {probe, bind, diagnose};
})()
