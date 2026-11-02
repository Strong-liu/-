'use strict';

const ui = {
  form: document.querySelector('#upload-form'), input: document.querySelector('#file-input'),
  dropzone: document.querySelector('#dropzone'), upload: document.querySelector('#upload-button'),
  uploadMessage: document.querySelector('#upload-message'), settings: document.querySelector('#settings-panel'),
  settingsForm: document.querySelector('#settings-form'), settingsMessage: document.querySelector('#settings-message'),
  cards: new Map(), configured:false, selected:null, uploading:false, refreshing:false, savedMode:'llm',
};
const statusNames = {review:'待核对术语',queued:'排队中',running:'正在翻译',succeeded:'已完成',failed:'需要重试'};
const phaseNames = {
  preparing:'正在准备内置资源',queued:'等待处理',review:'核对英文术语',finished:'翻译完成',failed:'处理未完成',
  cancelling:'正在停止翻译',cancelled:'已取消翻译',
  'Parse PDF and Create Intermediate Representation':'解析 PDF',DetectScannedFile:'检测扫描页面',
  'Parse Page Layout':'分析版面','Parse Paragraphs':'识别段落','Parse Formulas and Styles':'识别公式和样式',
  'Translate Paragraphs':'翻译段落',Typesetting:'重新排版','Add Fonts':'嵌入字体',
  'Generate drawing instructions':'生成页面','Subset font':'整理字体','Save PDF':'保存 PDF',
};

function termsFrom(text) {
  return [...new Set(String(text || '').split(/\r?\n/).map(item=>item.trim()).filter(Boolean))];
}

function showMessage(element, message, error=false) {
  element.textContent = message;
  element.classList.toggle('error',error);
  element.classList.toggle('success',Boolean(message) && !error);
}

async function request(url, options={}) {
  const controller = new AbortController();
  const {timeoutMs=300000,...fetchOptions}=options;
  const timer=setTimeout(()=>controller.abort(),timeoutMs);
  try {
    const response = await fetch(url, {...fetchOptions,signal:controller.signal});
    let data;
    try {data=await response.json();}
    catch(error) {if(error.name==='AbortError') throw error;throw new Error('本地服务未返回有效结果，请重新打开软件。');}
    if(!response.ok) {
      const detail=typeof data.detail==='string' ? data.detail : Array.isArray(data.detail) ? data.detail.map(item=>item.msg).join('；') : '请求未完成，请稍后重试。';
      throw new Error(detail);
    }
    return data;
  } catch(error) {if(error.name==='AbortError') throw new Error('本地服务响应超时，请稍后重试。');throw error;}
  finally {clearTimeout(timer);}
}

function jsonRequest(url, method, data) {
  return request(url,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
}

function openSettings() {
  ui.settings.open=true;
  ui.settings.scrollIntoView({behavior:'smooth',block:'start'});
}
document.querySelector('#open-settings').addEventListener('click',openSettings);

function showServiceMode(mode) {
  const academic=mode==='academic';
  document.querySelector('#llm-credentials').hidden=academic;
  document.querySelector('#academic-credentials').hidden=!academic;
  const help=document.querySelector('#service-help');
  const link=document.createElement('a');link.target='_blank';link.rel='noopener noreferrer';
  if(academic) {
    help.textContent='高级模式需要百度翻译开放平台 APPID、对应密钥和学术论文领域 API 权限。它与大模型 API Key、网页学术版会员分别开通，额度以百度账户控制台为准。 ';
    link.href='https://api.fanyi.baidu.com/product/123';link.textContent='查看学术论文领域 API 说明 ↗';
  } else {
    help.textContent='使用百度翻译开放平台的 API Key，按学术阅读风格翻译；英文专名由本机术语清单保护。免费额度需满足百度账户认证与开通条件，以控制台权益为准，并非无限免费。 ';
    link.href='https://fanyi-api.baidu.com/product/13';link.textContent='查看大模型文本翻译说明 ↗';
  }
  help.append(link);
}
document.querySelector('#service-mode').addEventListener('change',event=>showServiceMode(event.target.value));

function applySettings(settings, fill=true) {
  ui.savedMode=settings.service_mode || 'llm';
  ui.configured=ui.savedMode==='academic'?Boolean(settings.app_id && settings.secret_configured):Boolean(settings.api_key_configured);
  if(fill) {
    document.querySelector('#service-mode').value=ui.savedMode;
    document.querySelector('#app-id').value=settings.app_id || '';
    const qps=document.querySelector('#qps');
    if(![...qps.options].some(item=>item.value===String(settings.qps || 1))) {
      const option=document.createElement('option'); option.value=String(settings.qps); option.textContent=`${settings.qps} 次 / 秒`; qps.append(option);
    }
    qps.value=String(settings.qps || 1);
    document.querySelector('#custom-terms').value=(settings.custom_terms || []).join('\n');
  }
  const secret=document.querySelector('#api-secret');
  secret.value='';
  secret.placeholder=settings.secret_configured ? '已保存在本机，留空保持已有密钥' : '填写百度翻译开放平台密钥';
  const apiKey=document.querySelector('#api-key');apiKey.value='';
  apiKey.placeholder=settings.api_key_configured?'已保存在本机，留空保持已有 API Key':'填写百度大模型文本翻译 API Key';
  const badge=document.querySelector('#settings-badge');
  badge.textContent=ui.configured?(ui.savedMode==='academic'?'已保存凭据 · 学术领域':'API Key 已保存在本机'):'首次使用请配置 API';
  badge.classList.toggle('configured',ui.configured);
  document.querySelector('#service-eyebrow').textContent=ui.savedMode==='academic'?'PDFMathTranslate · 百度学术论文领域 API':'PDFMathTranslate · 百度大模型翻译';
  showServiceMode(document.querySelector('#service-mode').value);
  document.querySelector('#test-connection').disabled=!ui.configured;
  if(!ui.configured) ui.settings.open=true;
  for(const card of ui.cards.values()) updateCard(card,card.job);
}

ui.settingsForm.addEventListener('submit',async event=>{
  event.preventDefault();
  const save=document.querySelector('#save-settings'); save.disabled=true;
  try {
    const settings=await jsonRequest('/api/settings','PUT',{
      app_id:document.querySelector('#app-id').value.trim(),secret:document.querySelector('#api-secret').value.trim(),
      api_key:document.querySelector('#api-key').value.trim(),service_mode:document.querySelector('#service-mode').value,
      qps:Number(document.querySelector('#qps').value),custom_terms:termsFrom(document.querySelector('#custom-terms').value),
    });
    applySettings(settings);
    showMessage(ui.settingsMessage,ui.configured?'设置已保存在本机。可以核对论文术语并开始翻译。':'设置已保存。还需填写当前翻译服务的凭据，才能开始翻译。');
  } catch(error) {showMessage(ui.settingsMessage,error.message,true);}
  finally {save.disabled=false;}
});

document.querySelector('#test-connection').addEventListener('click',async()=>{
  const button=document.querySelector('#test-connection'); button.disabled=true;
  showMessage(ui.settingsMessage,'正在向百度发送一次短句请求，验证凭据和服务权限……');
  try {
    const result=await request('/api/service/test',{method:'POST'});
    showMessage(ui.settingsMessage,result.message || (result.ok?'连接成功。':'连接未成功，请检查账户权限和额度。'),!result.ok);
  } catch(error) {showMessage(ui.settingsMessage,error.message,true);}
  finally {button.disabled=!ui.configured;}
});

function selectFile(file) {
  if(!file || ui.uploading) return;
  showMessage(ui.uploadMessage,'');
  if(!file.name.toLowerCase().endsWith('.pdf')) {
    ui.selected=null; ui.upload.disabled=true;
    showMessage(ui.uploadMessage,'请选择 PDF 文件。',true); return;
  }
  if(file.size>100*1024*1024 || !file.size) {
    ui.selected=null; ui.upload.disabled=true;
    showMessage(ui.uploadMessage,file.size?'单个 PDF 最大支持 100 MB。':'文件为空，请重新选择。',true); return;
  }
  ui.selected=file;
  document.querySelector('#file-title').textContent=file.name;
  document.querySelector('#file-description').textContent=`${(file.size/1024/1024).toFixed(2)} MB · 点击“上传并提取术语”继续`;
  ui.dropzone.classList.add('selected'); ui.upload.disabled=false;
}
ui.input.addEventListener('change',()=>selectFile(ui.input.files[0]));
ui.dropzone.addEventListener('keydown',event=>{
  if(event.key==='Enter' || event.key===' ') {event.preventDefault();ui.input.click();}
});
for(const name of ['dragenter','dragover']) ui.dropzone.addEventListener(name,event=>{event.preventDefault();ui.dropzone.classList.add('dragover');});
for(const name of ['dragleave','drop']) ui.dropzone.addEventListener(name,event=>{event.preventDefault();ui.dropzone.classList.remove('dragover');});
ui.dropzone.addEventListener('drop',event=>selectFile(event.dataTransfer.files[0]));

ui.form.addEventListener('submit',async event=>{
  event.preventDefault();
  if(!ui.selected || ui.uploading) return;
  ui.uploading=true; ui.upload.disabled=true; ui.upload.textContent='正在提取术语……';
  const body=new FormData();body.append('file',ui.selected);
  try {
    const job=await request('/api/jobs',{method:'POST',body});
    ui.selected=null;ui.input.value='';ui.dropzone.classList.remove('selected');
    document.querySelector('#file-title').textContent='拖入 PDF，或点击选择下一篇论文';
    document.querySelector('#file-description').textContent='先在本地提取英文专名，核对后再翻译';
    showMessage(ui.uploadMessage,'已提取英文术语，请在下方清单中核对后开始翻译。');
    await refresh();
    const card=ui.cards.get(job.id);
    if(card) card.element.scrollIntoView({behavior:'smooth',block:'start'});
  } catch(error) {showMessage(ui.uploadMessage,error.message,true);}
  finally {ui.uploading=false;ui.upload.textContent='上传并提取术语 ↗';ui.upload.disabled=!ui.selected;}
});

function elapsed(job) {
  if(!job.started_at) return '';
  const seconds=Math.max(0,Math.floor((job.finished_at || Date.now()/1000)-job.started_at));
  return `${Math.floor(seconds/60)} 分 ${seconds%60} 秒`;
}

function createLink(url,label,secondary=false) {
  const link=document.createElement('a');link.textContent=label;
  // Download endpoints are local; never turn a returned external URL into a link.
  if(typeof url==='string' && url.startsWith('/api/jobs/')) link.href=url;
  link.classList.toggle('secondary',secondary);return link;
}

function createCard(job) {
  const element=document.querySelector('#job-template').content.firstElementChild.cloneNode(true);
  const card={element,job,kind:job.status,pending:false};
  const review=element.querySelector('.review-panel');
  review.hidden=!['review','failed'].includes(job.status);
  if(['review','failed'].includes(job.status)) {
    const editor=element.querySelector('.terms-editor');
    const label=element.querySelector('.terms-label');
    editor.id=`terms-${job.id}`;label.htmlFor=editor.id;
    editor.value=(job.terms || []).join('\n');
    const count=element.querySelector('.term-count');
    const updateCount=()=>{count.textContent=`${termsFrom(editor.value).length} 个词条`;};
    editor.addEventListener('input',updateCount);updateCount();
    const groupNames={authors:'作者',journals:'期刊',methods:'模型方法',custom:'自定义'};
    for(const [group,name] of Object.entries(groupNames)) {
      const items=job.term_groups?.[group] || (group==='methods'?job.term_groups?.models_methods:undefined);
      if(Array.isArray(items) && items.length) {
        const chip=document.createElement('span');chip.textContent=`${name} ${items.length}`;
        chip.title=items.map(item=>typeof item==='string'?item:item.term || item.text || '').filter(Boolean).join('、');
        element.querySelector('.term-groups').append(chip);
      }
    }
    const start=document.createElement('button');start.type='button';start.className='start-translation';start.textContent=job.status==='failed'?'调整完成，重新翻译':'核对完成，开始翻译';
    start.addEventListener('click',async()=>{
      if(!ui.configured) {openSettings();return;}
      card.pending=true;updateCard(card,card.job);
      try {
        await jsonRequest(`/api/jobs/${job.id}/start`,'POST',{terms:termsFrom(editor.value),preserve_references:true});
        await refresh();
      } catch(error) {showMessage(element.querySelector('.job-action-message'),error.message,true);}
      finally {card.pending=false;updateCard(card,card.job);}
    });
    element.querySelector('.job-actions').append(start);
    const config=document.createElement('button');config.className='secondary-button configure-api';config.type='button';config.textContent='配置百度 API';
    config.addEventListener('click',openSettings);element.querySelector('.job-actions').append(config);
  }
  if(job.status==='failed') {
    element.querySelector('.job-actions').append(createLink(`/api/jobs/${job.id}/log`,'运行日志',true));
  }
  if(['running','queued'].includes(job.status)) {
    const cancel=document.createElement('button');cancel.type='button';cancel.className='secondary-button cancel-job';
    cancel.addEventListener('click',async()=>{
      card.pending=true;updateCard(card,card.job);
      try {await jsonRequest(`/api/jobs/${job.id}/cancel`,'POST',{});await refresh();}
      catch(error) {showMessage(element.querySelector('.job-action-message'),error.message,true);}
      finally {card.pending=false;updateCard(card,card.job);}
    });
    element.querySelector('.job-actions').append(cancel);
    if(job.status==='running') element.querySelector('.job-actions').append(createLink(`/api/jobs/${job.id}/log`,'运行日志',true));
  }
  if(job.status==='succeeded' && job.downloads) {
    element.querySelector('.job-actions').append(createLink(job.downloads.mono,'下载中文 PDF'),createLink(job.downloads.dual,'下载中英对照 PDF'));
  }
  updateCard(card,job);return card;
}

function updateCard(card,job) {
  card.job=job;
  const element=card.element;
  element.className=`job-card ${job.status}`;
  element.querySelector('h3').textContent=job.name;
  const badge=element.querySelector('.status-badge');badge.className=`status-badge ${job.status}`;badge.textContent=job.phase==='cancelled'?'已取消':statusNames[job.status] || job.status;
  const date=new Date(job.created_at*1000).toLocaleString('zh-CN');
  element.querySelector('.job-meta').textContent=`${job.pages} 页 · ${(job.bytes/1024/1024).toFixed(2)} MB · ${date}${elapsed(job)?' · 已用时 '+elapsed(job):''}`;
  let message=job.message || '';
  if(job.status==='queued') message=`排队中${job.queue_position?'：第 '+job.queue_position+' 个':''}，轮到这篇论文时会自动处理。`;
  if(job.status==='running' && phaseNames[job.phase]) {
    message=phaseNames[job.phase];
    if(Number.isFinite(job.stage_current) && Number.isFinite(job.stage_total) && job.stage_total>0 && job.phase!=='cancelling') {
      message+=`：${job.stage_current} / ${job.stage_total} ${job.phase==='Translate Paragraphs'?'段':'项'}`;
    } else message+='……';
  }
  element.querySelector('.job-message').textContent=message;
  const telemetry=job.telemetry || {};
  const statistics=[];
  if(job.status==='running' && Number.isFinite(job.overall_progress)) statistics.push(`总进度 ${Math.round(Math.min(100,Math.max(0,job.overall_progress)))}%`);
  if(Number.isFinite(telemetry.requests)) statistics.push(`请求 ${telemetry.requests} 次`);
  if(Number.isFinite(telemetry.cache_hits)) statistics.push(`复用缓存 ${telemetry.cache_hits} 段`);
  if(Number.isFinite(telemetry.preserved_paragraphs)) statistics.push(`保留原文 ${telemetry.preserved_paragraphs} 段`);
  if(telemetry.retries>0) statistics.push(`服务重试 ${telemetry.retries} 次${telemetry.last_retry_code?'（'+telemetry.last_retry_code+'）':''}`);
  if(job.status==='running' && job.phase!=='cancelling') {
    if(telemetry.active_requests>0) statistics.push(`百度处理中 ${telemetry.active_requests} 个请求`);
    if(telemetry.last_activity_seconds>=30) statistics.push(`最近处理进展在 ${Math.floor(telemetry.last_activity_seconds)} 秒前`);
    if(String(telemetry.last_retry_code)==='30000') statistics.push('本次曾遇百度 AI 服务暂时异常');
  }
  const details=element.querySelector('.job-statistics');details.textContent=statistics.join(' · ');details.hidden=!statistics.length;
  const track=element.querySelector('.progress-track');track.hidden=job.status!=='running';
  const progress=Number.isFinite(job.overall_progress)?Math.min(100,Math.max(0,job.overall_progress)):null;
  track.classList.toggle('determinate',progress!==null);
  track.firstElementChild.style.width=progress===null?'':`${progress}%`;
  track.setAttribute('aria-label',progress===null?'正在处理':`总进度 ${Math.round(progress)}%`);
  if(progress===null) track.removeAttribute('aria-valuenow');else track.setAttribute('aria-valuenow',String(Math.round(progress)));
  const cancel=element.querySelector('.cancel-job');
  if(cancel) {cancel.disabled=card.pending || job.phase==='cancelling';cancel.textContent=card.pending || job.phase==='cancelling'?'正在停止……':job.status==='queued'?'取消排队':'停止翻译';}
  const start=element.querySelector('.start-translation');
  if(start) {start.disabled=card.pending || !ui.configured;start.textContent=card.pending?'正在加入队列……':job.status==='failed'?'调整完成，重新翻译':'核对完成，开始翻译';}
  const retry=element.querySelector('.retry-job');if(retry) retry.disabled=card.pending;
  const config=element.querySelector('.configure-api');if(config) config.hidden=ui.configured;
  element.querySelector('.configuration-hint').hidden=ui.configured;
}

function render(jobs) {
  const container=document.querySelector('#jobs');
  const ids=new Set(jobs.map(job=>job.id));
  for(const [id,card] of ui.cards) if(!ids.has(id)) {card.element.remove();ui.cards.delete(id);}
  if(jobs.length) container.querySelector('.empty-state')?.remove();
  for(const job of jobs) {
    let card=ui.cards.get(job.id);
    if(!card || card.kind!==job.status) {
      const replacement=createCard(job);
      if(card) card.element.replaceWith(replacement.element);else container.append(replacement.element);
      card=replacement;ui.cards.set(job.id,card);
    } else updateCard(card,job);
  }
  // Reorder only when necessary; polling never replaces an active terms editor.
  jobs.forEach((job,index)=>{
    const element=ui.cards.get(job.id).element;
    if(container.children[index]!==element) container.insertBefore(element,container.children[index] || null);
  });
  document.querySelector('#job-count').textContent=jobs.length?`${jobs.length} 篇论文 · 自动保存在本机`:'自动保存在本机';
}

async function refresh() {
  if(ui.refreshing) return;
  ui.refreshing=true;
  try {render(await request('/api/jobs',{timeoutMs:15000}));document.querySelector('#connection-message').textContent='';}
  catch(error) {document.querySelector('#connection-message').textContent='本地服务暂未连接。请从开始菜单或桌面重新打开“橙意翻译助手”。';}
  finally {ui.refreshing=false;}
}

async function initialize() {
  try {applySettings(await request('/api/settings'));}
  catch(error) {showMessage(ui.settingsMessage,error.message,true);ui.settings.open=true;}
  await refresh();
}
initialize();
setInterval(refresh,3000);
