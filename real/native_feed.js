/* Inserted inside the reviewed native_ui closure. Output is an explicit text /
   workspace-file schema, never raw RPC/auth/media-URL objects. */
const feedBind = () => {
  const first=discover(); clean(first);
  const owner=identity(first), durableOwner=JSON.stringify([first.auth.userId,first.state.activity.currentChatRootAgentId??null]), attempts=new Map(), stable=new Map();
  let active=null;
  const verify=()=>{const c=discover();if(identity(c)!==owner)throw Error('main_identity_changed');return c;};
  const file=f=>{
    if(!f||typeof f!=='object')return {unavailable:true};
    let path=f.path||f.workspace_path;
    if(typeof path==='string')path=path.replace(/^sandbox:\/\//,'').replace(/^\/+/, '');
    if(typeof path!=='string'||!/^(workspace\/|mnt\/data\/)/.test(path)||/[\\:?#\x00-\x1f]/.test(path)||path.split('/').includes('..'))
      return {unavailable:true};
    if(f.missing===true)return {unavailable:true};
    // A presentation label is a display title, often without .docx/.txt/.png.
    // Keep an explicit native filename or the real workspace basename.
    return {path,filename:typeof f.filename==='string'&&f.filename?f.filename:path.split('/').pop(),
      mime:typeof (f.mimeType||f.mime_type||f.mime)==='string'?(f.mimeType||f.mime_type||f.mime):'application/octet-stream'};
  };
  const scan=async after=>{
    const c=verify(),params={limit:100,transcript_mode:'messages'};
    if(after!==null){if(!Number.isSafeInteger(after)||after<0)throw Error('invalid_feed_cursor');params.after_seq=after;}
    const h=await c.rpc.sendRequest('chat.history',params,{expectedGatewayTargetKey:JSON.stringify([c.rpc.gatewayUrl,c.rpc.vmName])});
    const current=verify(),ms=current.state.chat.messagesById;
    if(!['all','main'].includes(h.channel)||!Array.isArray(h.chat_events)||typeof h.has_more!=='boolean')throw Error('feed_history_shape_changed');
    const rows=h.chat_events.map(e=>{
      if(!Number.isSafeInteger(e.seq)||e.seq<0)throw Error('feed_history_shape_changed');
      if(typeof e.message_id!=='string'||!e.message_id){
        if(!Number.isFinite(e.occurred_at_ms))throw Error('feed_history_shape_changed');
        e={...e,message_id:'fallback:'+e.seq+':'+Math.floor(e.occurred_at_ms/10000)};
      }
      const p=e.payload||{},m=ms[e.message_id];
      // The transcript surface, not originating_channel, defines visibility.
      // Scheduled/proactive sources can still produce main-chat messages.
      const surface=e.transcript_surface||p.transcript_surface;
      const main=p.is_thread!==true && ['main','main_chat'].includes(surface);
      let text=typeof e.display_text==='string'?e.display_text:typeof p.display_text==='string'?p.display_text:
        typeof p.content==='string'?p.content:typeof m?.content==='string'?m.content:null;
      // Native file cards are separate canonical messages, not m.files.
      // Read only the reviewed workspace-file fields; never copy variants/URLs.
      const list=v=>Array.isArray(v)?v:[];
      const presentations=list(m?.presentations);
      const taskCard=card=>['browser_task','browser_activity'].includes(card?.kind);
      // Native task presentations carry activity labels, not assistant prose.
      // An explicitly typed text/message payload remains authored text even
      // when a task presentation shares the projected record.
      const taskCarrier=taskCard(p)||(!p.kind&&m?.presentationCarrier===true&&
        presentations.length>0&&presentations.every(taskCard));
      if(taskCarrier)text=null;
      let sources=[...list(m?.images),...list(m?.files),...list(p.metadata?.attachments)];
      let unknownCard=false,hasTaskCard=false;
      const texts=text&&text.trim()?[text]:[],buttonLabels=new Set(),cardNotices=new Set();let hasButtons=false;
      const addText=v=>{if(typeof v==='string'&&v.trim()&&!texts.includes(v))texts.push(v);};
      // Reviewed public frontend contracts: video=data.videos[], audio=data.audio,
      // slides=data.deck. Extract only workspace refs, never variants or URLs.
      const readCard=card=>{
        const data=card?.data;
        switch(card?.kind){
          case 'browser_task':case 'browser_activity':hasTaskCard=true;return;
          case 'html':case 'external_link':
            cardNotices.add('[网页] 请到 Muse 主对话查看或操作。');
            addText(data?.title);break;
          case 'space_build_completed':
            cardNotices.add('[网页] 页面已生成，请到 Muse 主对话打开。');
            addText(data?.display_name||data?.spaceBuildCompleted?.display_name);
            addText(data?.summary||data?.spaceBuildCompleted?.summary);break;
          case 'navigation':
            cardNotices.add('[提醒/任务入口] 请到 Muse 主对话查看详情或操作。');
            addText(data?.title);addText(data?.summary);break;
          case 'scheduled_task_created_activity':
            cardNotices.add('[提醒] 已创建，请到 Muse 主对话核对时间与设置。');
            addText(data?.title);addText(data?.summary);break;
          case 'internal_link_action_card':
            cardNotices.add('[操作入口] 请到 Muse 主对话操作，微信不执行授权或按钮动作。');
            addText(data?.title);addText(data?.subtitle);break;
          case 'file':sources.push(data?.file);break;
          case 'image':case 'video':{
            const values=card.kind==='image'?data?.images:data?.videos;
            sources.push(...list(values));
            if(!Array.isArray(values)||!values.length)unknownCard=true;
            break;
          }
          case 'audio':case 'slides':{
            const value=card.kind==='audio'?data?.audio:data?.deck;
            sources.push(value);if(!value||typeof value!=='object')unknownCard=true;
            break;
          }
          case 'text_with_button':case 'complimentary_subscription':{
            // Wire data is the button payload; hydrated presentation wraps it.
            const body=data?.textWithButton||data;
            if(typeof body?.text!=='string'||!body.text.trim())unknownCard=true;
            addText(body?.text);hasButtons=true;
            for(const button of [body?.button,...list(body?.secondary_buttons)])
              if(typeof button?.label==='string'&&button.label.trim())buttonLabels.add(button.label);
            // Action values, URLs, approval payloads are deliberately not read.
            break;
          }
          case 'fallback':
            if(typeof data?.markdown!=='string'||!data.markdown.trim())unknownCard=true;
            addText(data?.markdown);break;
          case 'text':case 'message':break;
          default:unknownCard=true;
        }
        // Even an unknown card may have an explicitly public readable title.
        // Keep its missing-content notice rather than treating this as support.
        addText(card?.displayText);addText(card?.display_text);
      };
      if(p.kind)readCard(p);
      for(const card of presentations)readCard(card);
      if(hasButtons)texts.push('【'+(buttonLabels.size?'按钮：'+[...buttonLabels].join('、')+'。':'')+
        '此卡片需在 Muse 主要聊天操作，微信仅显示文字。】');
      const resources=list(e.resources);
      if((!taskCarrier||texts.length||sources.length)&&resources.some(r=>r?.kind==='citation'))
        cardNotices.add('[网页来源] 引用信息请到 Muse 主对话查看。');
      for(const notice of cardNotices)addText(notice);
      const files=[],seenFiles=new Set(),fileTitles=new Set();
      for(const source of sources){
        const f=file(source),key=f.unavailable?'unavailable':f.path;
        if(!f.unavailable)for(const title of [source?.label,f.filename,f.path.split('/').pop()])
          if(typeof title==='string'&&title.trim())fileTitles.add(title.trim());
        if(!seenFiles.has(key)){files.push(f);seenFiles.add(key);}
      }
      // Media display_text is commonly just its label/filename, not authored
      // prose. Do not turn that synthetic title into a separate WeChat bubble.
      // Only exact, whole title-only values are suppressed, with a valid file;
      // keep real captions, unavailable-file context, and ordinary text rows.
      const assistantMedia=(e.role||p.role)==='assistant'&&fileTitles.size>0;
      if(assistantMedia&&!taskCarrier){addText(p.content);addText(m?.content);}
      const titleOnly=v=>{
        const lines=v.trim().split(/\r?\n/).map(s=>s.trim()).filter(Boolean);
        return lines.length>0&&lines.every(line=>fileTitles.has(line));
      };
      const keptTexts=assistantMedia?texts.filter(v=>!titleOnly(v)):texts;
      text=keptTexts.length?keptTexts.join('\n\n'):texts.length?'':text;
      const pureTask=hasTaskCard&&!text?.trim()&&!files.length&&!unknownCard;
      const unsupported=unknownCard || (!pureTask&&resources.some(r=>r?.kind!=='citation')&&!files.length);
      // A failed task card is still a task status, whereas a failed assistant
      // generation must remain visible and stop new submissions as before.
      const status=!taskCarrier&&!pureTask&&typeof p.status==='string'?p.status:null;
      const emptyPlaceholder=!text?.trim()&&!files.length&&!unsupported&&!['failed','interrupted'].includes(status);
      const ignore_reason=pureTask?'task_status_card':emptyPlaceholder?'empty_placeholder':null;
      const row={seq:e.seq,message_id:e.message_id,role:e.role||p.role,main:main&&!ignore_reason,
        text:text||'',files,unsupported,status,ignore_reason,card_notice:[...cardNotices].join('\n')};
      const signature=JSON.stringify(row),previous=stable.get(row.message_id),now=Date.now();
      if(!previous||previous.signature!==signature)stable.set(row.message_id,{signature,since:now});
      // Canonical completed text has an explicit end signal. Require a second
      // unchanged observation, but do not impose the media hydration window.
      // Missing/unknown status and all cards/files keep the original 3s guard.
      const completedText=row.role==='assistant'&&row.main&&row.status==='completed'&&
        !!m&&row.text.trim().length>0&&!row.unsupported&&!row.files.length&&
        sources.length===0&&!hasButtons&&(!p.kind||['text','message'].includes(p.kind))&&
        !m.presentationCarrier&&!list(m.presentations).length;
      row.ready=!busy(current)&&!historyMoving(current)&&m?.isTextStreaming!==true&&
        now-stable.get(row.message_id).since>=(completedText?400:3000);
      return row;
    }).sort((a,b)=>a.seq-b.seq);
    if(new Set(rows.map(r=>r.seq)).size!==rows.length)throw Error('feed_history_shape_changed');
    const currentIds=new Set(rows.map(r=>r.message_id));
    for(const id of stable.keys())if(!currentIds.has(id))stable.delete(id);
    return {owner:durableOwner,rows,has_more:h.has_more,busy:busy(current)||historyMoving(current)};
  };
  const submit=(id,text,files)=>{
    if(attempts.has(id))return attempts.get(id);
    if(active)throw Error('previous_submission_unresolved');
    const c=verify();clean(c);
    if(typeof text!=='string'||!Array.isArray(files)||(!text.trim()&&!files.length))throw Error('empty_input');
    const attachments=files.map(f=>({filename:f.filename,mimeType:f.mime,category:f.category,dataBase64:f.data_base64,
      rawFile:new File([Uint8Array.from(atob(f.data_base64),c=>c.charCodeAt(0))],f.filename,{type:f.mime})}));
    const run={phase:'submitting',message_id:null};attempts.set(id,run);active=id;
    Promise.resolve(c.control.sendMessage(text,attachments,{targetRoute:{kind:'main'},waitForReadiness:false,
      failureReporting:'caller',consumeComposerInput:false})).then(result=>{
      if(result?.ok===true&&typeof result.messageId==='string'&&result.messageId){
        run.phase='acknowledged';run.message_id=result.messageId;if(active===id)active=null;
        for(const [old,entry] of attempts)if(attempts.size>1000&&old!==id&&entry.phase==='acknowledged')attempts.delete(old);
      }else run.phase='uncertain';
    }).catch(()=>run.phase='uncertain');
    return {...run};
  };
  const download=async(path,offset,limit)=>{
    const c=verify(),f=file({path});if(f.unavailable)throw Error('attachment_path_rejected');
    if(!Number.isSafeInteger(offset)||offset<0||!Number.isSafeInteger(limit)||limit<1||limit>1048576)throw Error('invalid_media_payload');
    const r=await c.rpc.sendRequest('fs.read',{path:f.path,offset,len:limit},{expectedGatewayTargetKey:JSON.stringify([c.rpc.gatewayUrl,c.rpc.vmName])});
    verify();if(!Number.isSafeInteger(r.len)||r.len<0||r.len>limit||typeof r.data_base64!=='string'||
      atob(r.data_base64).length!==r.len||typeof r.eof!=='boolean')throw Error('invalid_media_payload');
    return {data_base64:r.data_base64,len:r.len,eof:r.eof};
  };
  const reviewedRelease=(id,resolution)=>{
    const c=verify();clean(c);
    // A pending ACK may be released only after the human confirms the exact
    // input already exists. "Not submitted" cannot cancel an in-flight send.
    if(active&&(active!==id||(attempts.get(active)?.phase==='submitting'&&resolution!=='acknowledged')))
      throw Error('previous_submission_unresolved');
    if(active){attempts.get(active).phase='reviewed';active=null;}
    return true; // No sendMessage call, attempt deletion, cursor reset or replay.
  };
  return {scan,submit,download,reviewedRelease,attempt:id=>attempts.get(id)||{phase:'missing'}};
};
