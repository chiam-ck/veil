'use strict';
const $=id=>document.getElementById(id);
const terminal=new Set(['completed','review_required','blocked','failed']);
let rows=[],batchId=null,batchData=null,uploading=false,paused=false,polling=false,timer=null;
let jid=null,selected=null,selectionVersion=0,loadedReport=null;
let detectorReady=true,workspaceReady=false;
let singleJobId=null,manualRedactions=[],revisionBusy=false;
let limits={max_batch_documents:50,max_file_bytes:30*1024*1024,supported_formats:['.docx','.pptx','.xlsx','.pdf']};
function node(tag,text,cls){const n=document.createElement(tag);n.textContent=text;if(cls)n.className=cls;return n}
async function api(url,options){const r=await fetch(url,{...options,headers:{...options?.headers,...(options?.method&&!['GET','HEAD'].includes(options.method.toUpperCase())?{'X-Veil-Request':'1'}:{})}});if(!r.ok){let d;try{d=await r.json()}catch{d={detail:'Request failed'}}const e=Error(typeof d.detail==='string'?d.detail:'Request could not be validated');e.status=r.status;e.retryAfter=Number(r.headers.get('Retry-After'))||2;throw e}return r.status===204?null:r.json()}
const size=n=>n==null?'—':n<1024*1024?(n/1024).toFixed(1)+' KiB':(n/1024/1024).toFixed(1)+' MiB';
const label=s=>({ready:'Ready',uploading:'Uploading',waiting:'Waiting for capacity',upload_failed:'Upload failed',not_submitted:'Not uploaded',receiving:'Receiving',queued:'Queued',processing:'Processing',waiting_provider:'Waiting for model',completed:'Completed',review_required:'Review required',blocked:'Blocked',failed:'Failed',deleted:'Deleted / expired'}[s]||s);
function state(r){return r.job?.status||r.state}
function valid(file){const suffix=file.name.slice(file.name.lastIndexOf('.')).toLowerCase();if(!limits.supported_formats.includes(suffix))return 'Unsupported format';if(!file.size)return 'Empty file';if(file.size>limits.max_file_bytes)return 'File exceeds '+size(limits.max_file_bytes);return null}
function addFiles(files){if(batchId||singleJobId){$('error').textContent='Start a new batch before selecting more documents.';return}const add=Array.from(files);if(rows.length+add.length>limits.max_batch_documents){$('error').textContent='A batch can contain up to '+limits.max_batch_documents+' documents. Your existing selection has been kept.';return}for(const file of add)rows.push({index:rows.length,file,name:file.name,size:file.size,state:'ready',error:valid(file),job:null});$('file').value='';$('error').textContent='';renderRows()}
function renderRows(){
 $('batch').hidden=!rows.length;$('selection-count').textContent=rows.length+' / '+limits.max_batch_documents;
 $('file').disabled=Boolean(batchId||singleJobId)||uploading;
 $('submit').disabled=!workspaceReady||!detectorReady||uploading||!rows.length||rows.some(r=>r.error)||!rows.some(r=>r.file&&!r.job);
 $('submit').textContent=batchId?'Resume pending uploads':'Start batch';$('pause').hidden=!uploading;
 const body=$('files');body.replaceChildren();
 for(const row of rows){const tr=document.createElement('tr');tr.dataset.index=String(row.index);if(row.index===selected)tr.className='selected';
  tr.append(node('td',String(row.index+1).padStart(2,'0')));const name=node('td',row.name,'filename');name.title=row.name;tr.append(name,node('td',size(row.size)));
  const status=document.createElement('td');status.append(node('span',row.error||label(state(row)),'badge '+(row.error?'failed':state(row))));if(row.message)status.append(node('small',row.message));else if(row.job?.status==='review_required'){const reasons=row.job.review_reasons||[];status.append(node('small',reasons.length?`${row.job.review_summary?.review_items||reasons.length} review item(s) · ${reasons[0].message}`:(row.job.warnings||[])[0]||'Open for review details'))}else if(row.job?.status==='waiting_provider'){status.append(node('small',row.job.stage||'Retrying automatically when the model is ready.'))}tr.append(status);
  const action=document.createElement('td');const button=node('button',row.job?'View':'Remove','secondary small');button.type='button';button.dataset.action=row.job?'view':'remove';
  if(row.job){button.onclick=()=>selectRow(row.index)}else{button.disabled=Boolean(batchId)||uploading;button.onclick=()=>{rows.splice(row.index,1);rows.forEach((r,i)=>r.index=i);renderRows()}}
  action.append(button);tr.append(action);body.append(tr);
 }
 if(!batchId&&!singleJobId){$('batch-status').textContent=rows.some(r=>r.error)?'Remove invalid documents before starting.':rows.length+' documents selected';$('batch-counts').replaceChildren();$('batch-link').hidden=true;$('batch-progress').value=0}
}
function renderBatch(){
 if(!batchData)return;
 $('batch-link').hidden=false;$('batch-link').href=singleJobId?'#job='+singleJobId:'#batch='+batchId;$('batch-link').textContent=(singleJobId?'Job '+singleJobId.slice(0,8):'Batch '+batchId.slice(0,8))+' · bookmark to return';
 const c=batchData.counts;$('batch-status').textContent=`${batchData.total-c.not_submitted}/${batchData.total} received · ${batchData.finished}/${batchData.total} finished`+(paused?' · pending uploads paused':'');
 $('batch-progress').max=batchData.total;$('batch-progress').value=batchData.finished;$('batch-counts').replaceChildren();
 for(const key of ['completed','review_required','blocked','failed','processing','waiting_provider','queued','deleted'])if(c[key])$('batch-counts').append(node('div',`${label(key)}: ${c[key]}`,'metric '+key));
 const link=$('batch-download');link.hidden=Boolean(singleJobId)||!batchData.ready;link.href=`/api/batches/${batchId}/download`+(batchData.export_ready?'':'?partial=true');link.textContent=batchData.export_ready?'Download batch ZIP':'Download ready results ZIP (partial)';
 $('save-index').hidden=!rows.some(r=>r.job&&r.name&&!r.restored);
 if(rows.some(r=>r.restored)&&c.not_submitted)$('batch-note').textContent='Some files never reached the server. Accepted jobs continue processing. Start a new batch for missing files; a partial ZIP contains only finished results.';
}
async function refreshBatch(){
 if((!batchId&&!singleJobId)||polling)return;polling=true;const id=singleJobId||batchId;
 try{let data;if(singleJobId){const job=await api('/api/jobs/'+id);const counts={not_submitted:0};for(const key of ['completed','review_required','blocked','failed','processing','waiting_provider','queued','deleted'])counts[key]=Number(job.status===key);data={total:1,finished:Number(terminal.has(job.status)),ready:Number(terminal.has(job.status)),export_ready:terminal.has(job.status),counts,items:[{...job,index:0}]}}else data=await api('/api/batches/'+id);if((singleJobId||batchId)!==id)return;batchData=data;if(!singleJobId&&rows.length!==data.total){const known=new Map(rows.filter(r=>r.job).map(r=>[r.job.id,r]));rows=Array.from({length:data.total},(_,index)=>({index,file:null,name:'Document '+String(index+1).padStart(2,'0'),size:null,state:'not_submitted',restored:true,job:null}));for(const job of data.items){const previous=known.get(job.id);if(previous){previous.index=job.index;rows[job.index]=previous}if(job.id===jid)selected=job.index}}
  for(const job of data.items){const row=rows[job.index];if(row){if(row.job?.id!==job.id&&row.index===selected)clearPreview();row.job=job;row.file=null;row.message=null}}
  renderRows();renderBatch();if(selected==null&&rows.some(r=>r.job))selectRow(rows.find(r=>r.job).index);else await renderSelected();
  if(data.export_ready&&!uploading){clearInterval(timer);timer=null}
 }catch(e){$('error').textContent=e.status===404?'This batch is unavailable in this browser workspace, or was deleted or expired.':e.message}finally{polling=false}
}
function startPolling(){if(!timer)timer=setInterval(refreshBatch,1300)}
async function sendRow(row,id){
 row.state='uploading';row.message=null;renderRows();
 const form=new FormData();const suffix=row.file.name.slice(row.file.name.lastIndexOf('.')).toLowerCase();form.append('file',row.file,'upload'+suffix);form.append('batch_id',id);form.append('batch_index',String(row.index));
 try{const job=await api('/api/jobs',{method:'POST',body:form});if(batchId!==id)return;row.job=job;row.file=null;row.state='queued';return 'ok'}
 catch(e){if(batchId!==id)return 'stop';
  if(e.status===429){row.state='waiting';row.message='The shared worker queue is full. Retrying automatically.';renderRows();await new Promise(resolve=>setTimeout(resolve,Math.min(30,Math.max(1,e.retryAfter))*1000));if(!row.job)row.state='ready';return 'retry'}
  // Reconcile an interrupted response before offering a retry: the server may
  // already have accepted this position. Duplicate positions are never overwritten.
  try{const data=await api('/api/batches/'+id);const accepted=data.items.find(j=>j.index===row.index);if(accepted){row.job=accepted;row.file=null;return 'ok'}}catch{}
  row.state='upload_failed';row.message=e.message;return 'failed';
 }finally{if(batchId===id)renderRows()}
}
async function submitBatch(event){event.preventDefault();if(uploading)return;$('error').textContent='';
 if(!rows.length||rows.length>limits.max_batch_documents||rows.some(r=>r.error))return;
 uploading=true;renderRows();try{if(!batchId){const created=await api('/api/batches',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({count:rows.length})});batchId=created.id;history.replaceState(null,'','#batch='+batchId)}
  const id=batchId;uploading=true;paused=false;$('pause').disabled=false;$('pause').textContent='Pause pending uploads';for(const row of rows)if(row.state==='upload_failed')row.state='ready';renderRows();startPolling();
  const pump=async()=>{while(batchId===id&&!paused){const row=rows.find(r=>r.file&&!r.job&&r.state==='ready');if(!row)return;await sendRow(row,id)}};
  await Promise.all([pump(),pump()]);uploading=false;renderRows();await refreshBatch();
 }catch(e){uploading=false;$('error').textContent=e.message;renderRows()}
}
function clearPreview(){manualRedactions=[];$('redaction-list').replaceChildren();$('redaction-text').value='';$('corrections').hidden=true;$('revision-links').replaceChildren();$('revision-message').dataset.error='false';$('revision-message').textContent='Creates a new revision from the retained original. Earlier redactions are kept; the previous result remains available until its cleanup deadline.';selectionVersion++;loadedReport=null;jid=null;$('job').hidden=true;$('clean').textContent='';$('original').textContent='';$('original-panel').hidden=true;$('pdf').hidden=true;$('pdf').removeAttribute('src');$('pdf-label').hidden=true;$('reveal').hidden=true;$('downloads').replaceChildren();$('metrics').replaceChildren();$('warning').textContent='';$('review-findings').hidden=true;$('review-items').replaceChildren();$('information').hidden=true;$('information').open=false;$('information-items').replaceChildren()}
async function selectRow(index){if(selected!==index){clearPreview();selected=index}renderRows();await renderSelected()}
function renderFindings(report){
 const issues=report.issues||[],reviews=issues.filter(i=>i.severity==='review'),information=issues.filter(i=>i.severity==='info');
 if(!report.review_policy)for(const warning of report.warnings||[])reviews.push({message:warning,location_label:'Legacy report',action:'Resubmit the original for current, item-specific review criteria.'});
 function fill(container,items){container.replaceChildren();for(const item of items.slice(0,100)){const card=node('div',null,'finding');card.append(node('strong',item.message),node('small',item.location_label||item.location),node('p',item.action));container.append(card)}if(items.length>100)container.append(node('p',`Showing 100 of ${items.length} items. Download report.json for the full list.`))}
 $('review-findings').hidden=!reviews.length;$('review-heading').textContent=`Review needed · ${reviews.length} item(s)`;fill($('review-items'),reviews);
 $('information').hidden=!information.length;$('information-heading').textContent=`Processing information · ${information.length} item(s) — no review required for these notices`;fill($('information-items'),information);
}
async function renderSelected(){
 const row=rows[selected];if(!row?.job||row.job.status==='deleted'){$('job').hidden=true;return}
 const job=row.job;const version=selectionVersion;jid=job.id;$('job').hidden=false;$('document-title').textContent=row.name;$('jobid').textContent='Job '+jid;$('status').textContent=label(job.status);$('status').className='badge '+job.status;$('stage').textContent=job.stage||'Waiting for worker';$('progress').value=job.status==='processing'?2:terminal.has(job.status)?4:1;$('delete').disabled=revisionBusy||['receiving','processing'].includes(job.status);renderRevisionControls(job);
 if(!terminal.has(job.status)||loadedReport===jid)return;
 const id=jid;loadedReport=id;
 try{const report=await api('/api/jobs/'+id+'/report');if(version!==selectionVersion||id!==jid)return;
  $('warning').textContent=[!report.review_policy?'Older result: this job used the previous warning rules. Resubmit the original to apply the current review criteria.':null,report.reason].filter(Boolean).join(' • ');
  renderFindings(report);
  $('metrics').replaceChildren();if(report.detector)$('metrics').append(node('div','Detector: '+report.detector.mode+(report.detector.model?' · '+report.detector.model:''),'metric'));for(const [k,v]of Object.entries(report.entity_counts||{}))$('metrics').append(node('div',k+': '+v,'metric'));
  if(report.detector?.usage){const d=report.detector;for(const text of ['Model requests: '+d.calls,'Input tokens: '+d.usage.prompt_tokens,'Output tokens: '+d.usage.completion_tokens,'Model time: '+d.seconds.toFixed(2)+' s',...(d.queue_seconds!=null?['Model queue: '+d.queue_seconds.toFixed(2)+' s']:[]),...(d.effective_output_tokens_per_second!=null?['Output tokens/s (including prompt and request time): '+d.effective_output_tokens_per_second]:[])])$('metrics').append(node('div',text,'metric'))}
  for(const [k,v]of Object.entries(report.content_changes||{}))$('metrics').append(node('div',k+': '+v,'metric'));
  for(const [k,v]of Object.entries(report.timings_seconds||{}))$('metrics').append(node('div',k+': '+v.toFixed(2)+' s','metric'));
  for(const k of Object.keys(report.language_coverage?.observed_segment_counts||{}))$('metrics').append(node('div',k,'metric'));
  $('downloads').replaceChildren();for(const name of report.artifacts||[]){const a=node('a',name);a.href=`/api/jobs/${id}/download/${name}`;a.download=name;$('downloads').append(a)}
  if(['completed','review_required'].includes(job.status)){const r=await fetch(`/api/jobs/${id}/download/sanitised.txt`);if(!r.ok)throw Error('Preview unavailable');const text=await r.text();if(version!==selectionVersion||id!==jid)return;$('clean').textContent=text;$('reveal').hidden=false;
   const pdf=(report.artifacts||[]).includes('sanitised.pdf');$('pdf').hidden=!pdf;$('pdf-label').hidden=!pdf;if(pdf)$('pdf').src=`/api/jobs/${id}/preview/1`;
  }
 }catch(e){if(id===jid){loadedReport=null;$('warning').textContent=e.message}}
}
function renderRevisionControls(job){
 const available=terminal.has(job.status)&&!job.superseded_by;
 $('corrections').hidden=!available;
 const editable=available&&['completed','review_required'].includes(job.status);
 $('manual-controls').hidden=!editable;$('apply-redactions').hidden=!editable;
 $('apply-redactions').disabled=revisionBusy||!manualRedactions.length;
 $('rerun').disabled=revisionBusy;$('add-redaction').disabled=revisionBusy;$('use-selection').disabled=revisionBusy;
 $('revision-links').replaceChildren();
 for(const [id,text] of [[job.parent_id,'Previous result'],[job.superseded_by,'Newer result']])if(id){const a=node('a',text);a.href='/#job='+id;$('revision-links').append(a)}
}
function revisionMessage(text,error=false){$('revision-message').textContent=text;$('revision-message').dataset.error=String(error)}
function renderManualRules(){
 $('redaction-list').replaceChildren();
 manualRedactions.forEach((text,index)=>{const li=node('li',text);const remove=node('button','Remove phrase','secondary small');remove.type='button';remove.disabled=revisionBusy;remove.onclick=()=>{manualRedactions.splice(index,1);renderManualRules()};li.append(remove);$('redaction-list').append(li)});
 $('apply-redactions').disabled=revisionBusy||!manualRedactions.length;
}
$('use-selection').onclick=()=>{
 const selection=window.getSelection();
 const inside=id=>selection&&selection.rangeCount&&!selection.isCollapsed&&$(id).contains(selection.anchorNode)&&$(id).contains(selection.focusNode);
 if(!inside('clean')&&!inside('original')){revisionMessage('Select missed text inside the document preview first.',true);return}
 const text=selection.toString();if(text.length>512){revisionMessage('Select a phrase of at most 512 characters.',true);return}
 $('redaction-text').value=text;revisionMessage('Selected text is ready. Add the phrase to your redactions.');
};
$('add-redaction').onclick=()=>{
 const text=$('redaction-text').value;
 if(!text.trim()||/\[[A-Z_]+_\d{3,}\]/.test(text)){revisionMessage('Enter missed original text, without existing redaction placeholders.',true);return}
 if(manualRedactions.length>=100){revisionMessage('Apply these phrases before adding more (100 per revision).',true);return}
 if(!manualRedactions.includes(text))manualRedactions.push(text);
 $('redaction-text').value='';renderManualRules();revisionMessage('Phrases are applied to every match in the regenerated document and preview.');
};
async function createRevision(mode){
 if(revisionBusy||!jid)return;
 if($('redaction-text').value.trim()){revisionMessage('Add the entered phrase before continuing, or clear it.',true);return}
 if(mode==='manual'&&!manualRedactions.length)return;
 const id=jid;revisionBusy=true;renderManualRules();renderRevisionControls(rows[selected].job);
 revisionMessage(mode==='manual'?'Preparing manual redactions…':'Queuing another model inspection…');
 try{const revision=await api('/api/jobs/'+id+'/reprocess',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode,redactions:manualRedactions})});revisionBusy=false;const row=rows.find(r=>r.job?.id===id);if(row){clearPreview();row.job={...row.job,...revision};selected=row.index;singleJobId=null;batchId=revision.batch_id;history.replaceState(null,'','#batch='+batchId);renderRows();await renderSelected();startPolling();await refreshBatch()}else{location.href='/#batch='+revision.batch_id;location.reload()}}
 catch(e){revisionBusy=false;if(id===jid){revisionMessage(e.message,true);renderManualRules();renderRevisionControls(rows[selected].job)}}
}
$('apply-redactions').onclick=()=>createRevision('manual');$('rerun').onclick=()=>createRevision('rerun');
let workspaceChannel=null;try{workspaceChannel=new BroadcastChannel('veil-workspace');workspaceChannel.onmessage=event=>{if(event.data==='ended'){clearPreview();rows=[];location.replace('/')}}}catch{}
$('end-workspace').onclick=async()=>{
 const button=$('end-workspace');
 if(button.dataset.confirm!=='yes'){button.dataset.confirm='yes';button.textContent='End workspace and lose access';$('workspace-end-note').hidden=false;return}
 button.disabled=true;
 try{await api('/api/workspace/end',{method:'POST'});clearPreview();rows=[];workspaceChannel?.postMessage('ended');try{localStorage.setItem('veil-workspace-ended',String(Date.now()))}catch{}location.replace('/')}
 catch(e){button.disabled=false;$('error').textContent=e.message}
};
window.addEventListener('storage',event=>{if(event.key==='veil-workspace-ended'){clearPreview();rows=[];location.replace('/')}});
window.addEventListener('pageshow',event=>{if(event.persisted)location.reload()});
window.addEventListener('hashchange',()=>location.reload());
$('upload').onsubmit=submitBatch;$('file').onchange=()=>addFiles($('file').files);
for(const event of ['dragenter','dragover'])$('dropzone').addEventListener(event,e=>{e.preventDefault();$('dropzone').classList.add('dragover')});
for(const event of ['dragleave','drop'])$('dropzone').addEventListener(event,e=>{e.preventDefault();$('dropzone').classList.remove('dragover')});
$('dropzone').addEventListener('drop',e=>{if(!uploading)addFiles(e.dataTransfer.files)});
$('pause').onclick=()=>{paused=true;$('pause').disabled=true;$('pause').textContent='Pausing…';renderBatch()};
$('new-batch').onclick=()=>{if(uploading){$('error').textContent='Pause pending uploads before starting another batch.';return}clearInterval(timer);timer=null;rows=[];batchId=null;singleJobId=null;batchData=null;selected=null;paused=false;clearPreview();history.replaceState(null,'',location.pathname);$('error').textContent='';$('batch-download').hidden=true;$('save-index').hidden=true;$('batch-note').textContent='ZIP folders use document numbers and opaque job IDs. Save the filename index if you need the mapping. ZIPs exclude originals.';$('pause').disabled=false;$('pause').textContent='Pause pending uploads';renderRows()};
$('save-index').onclick=()=>{const blob=new Blob([JSON.stringify({batch:batchId,documents:rows.map(r=>({number:r.index+1,filename:r.name,job_id:r.job?.id||null}))},null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='veil-batch-index.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
$('reveal').onclick=async()=>{const id=jid,version=selectionVersion;try{const r=await api(`/api/jobs/${id}/original-text`);if(id!==jid||version!==selectionVersion)return;$('original').textContent=r.segments.filter(s=>s.include_text!==false).map(s=>s.text).join('\n\n');$('original-panel').hidden=false}catch(e){$('warning').textContent=e.message}};
$('delete').onclick=async()=>{const id=jid;try{await api('/api/jobs/'+id,{method:'DELETE'});clearPreview();await refreshBatch()}catch(e){$('warning').textContent=e.message}};
async function initializeWorkspace(){return navigator.locks?.request?navigator.locks.request('veil-workspace-bootstrap',()=>api('/api/workspace')):api('/api/workspace')}
(async()=>{try{await initializeWorkspace();workspaceReady=true;limits=await api('/api/config');const detector=limits.detector||{mode:'llm'};detectorReady=detector.configured;$('detector-mode').textContent=detector.model?'Model · '+detector.model:'Model unavailable';$('detector-mode').title=detector.model||'Model unavailable';$('detector-mode').closest('details').open=!detectorReady;$('detector-description').textContent=!detectorReady?(detector.issue||'Ask the administrator to finish model setup.'):'Extracted document text is sent to '+new URL(detector.endpoint).hostname+'. OCR and document editing run locally. Credentials are managed by your administrator.';$('max-files').textContent=limits.max_batch_documents;$('file-limit').textContent=size(limits.max_file_bytes)+' per document';const hours=limits.retention_hours;$('retention-period').textContent=typeof hours==='number'&&Number.isFinite(hours)?(hours>0?'Server copies of originals, extracted text, sanitised files and reports are automatically deleted '+hours.toLocaleString()+' '+(hours===1?'hour':'hours')+' after each job finishes, including blocked or failed jobs. You can delete finished jobs sooner; downloads on your device are unaffected.':'Server copies of originals, extracted text, sanitised files and reports are automatically deleted as soon as each job finishes, including blocked or failed jobs. Downloads on your device are unaffected.'):'Retention policy unavailable. Ask your administrator before uploading.';
 const single=location.hash.match(/^#job=([0-9a-f]{32})$/);if(single){singleJobId=single[1];const job=await api('/api/jobs/'+singleJobId);rows=[{index:0,file:null,name:'Document '+(job.suffix||''),size:null,state:job.status,restored:true,job}];startPolling();await refreshBatch()}
 const match=location.hash.match(/^#batch=([0-9a-f]{32})$/);if(match){batchId=match[1];const data=await api('/api/batches/'+batchId);rows=Array.from({length:data.total},(_,index)=>({index,file:null,name:'Document '+String(index+1).padStart(2,'0'),size:null,state:'not_submitted',restored:true,job:null}));for(const job of data.items){rows[job.index].job=job;rows[job.index].name+=' '+(job.suffix||'')}startPolling();await refreshBatch()}
 renderRows();
 }catch(e){$('retention-period').textContent='Retention policy unavailable. Ask your administrator before uploading.';$('error').textContent=e.message}})();
