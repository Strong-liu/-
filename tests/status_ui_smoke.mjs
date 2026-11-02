// All API calls are intercepted in the browser. This test never reads,
// creates or cancels a real translation job and never calls Baidu.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {chromium} from 'playwright';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const output=path.join(root,'build','status-ui-0.3.2');
await fs.mkdir(output,{recursive:true});
const base=new URL(process.argv[2] || 'http://127.0.0.1:8766');
assert.ok(['127.0.0.1','localhost','[::1]'].includes(base.hostname),'Use a local app instance');
const now=Date.now()/1000;
const runningId='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const queuedId='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
const jobs=[{
  id:runningId,name:'Synthetic_running_paper.pdf',status:'running',phase:'Translate Paragraphs',
  pages:24,bytes:1024*1024,created_at:now-700,started_at:now-650,finished_at:null,
  stage_current:48,stage_total:496,overall_progress:43.05,message:'Translation in progress',
  terms:['EEGNet','Transformer'],preserve_references:true,
  telemetry:{requests:76,cache_hits:12,preserved_paragraphs:7,translated_paragraphs:48,
    fragment_recoveries:3,active_requests:1,last_activity_seconds:40,elapsed_seconds:650,retries:4,last_retry_code:'30000'},
},{
  id:queuedId,name:'Synthetic_queued_paper.pdf',status:'queued',phase:'queued',
  pages:8,bytes:2048,created_at:now-600,started_at:null,finished_at:null,queue_position:2,
  terms:['CCA'],preserve_references:true,
}];
const options={headless:true};
if(process.env.PLAYWRIGHT_CHROME_PATH) options.executablePath=process.env.PLAYWRIGHT_CHROME_PATH;
else {
  for(const candidate of ['C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe','C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe']) {
    try {await fs.access(candidate);options.executablePath=candidate;break;} catch {}
  }
}
const browser=await chromium.launch(options);
const context=await browser.newContext({viewport:{width:1280,height:1100},serviceWorkers:'block'});
const page=await context.newPage();
const errors=[];
const writes=[];
const blocked=[];
const report={passed:false,api_isolation:'All /api calls are mocked; no live job or service API is accessed',writes,blocked,errors};
page.on('pageerror',error=>errors.push(error.message));
page.on('console',message=>{if(message.type()==='error') errors.push(message.text());});
await context.route('**/api/**',async route=>{
  const request=route.request();
  const url=new URL(request.url());
  const method=request.method();
  const reply=(body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
  if(method==='GET' && url.pathname==='/api/settings') {
    return reply({service_mode:'llm',qps:1,app_id:'',api_key_configured:true,secret_configured:false,custom_terms:[],domain:'academic'});
  }
  if(method==='GET' && url.pathname==='/api/jobs') return reply(jobs);
  if(method==='POST' && [`/api/jobs/${runningId}/cancel`,`/api/jobs/${queuedId}/cancel`].includes(url.pathname)) {
    writes.push({method,path:url.pathname,body:request.postDataJSON()});
    const job=jobs.find(item=>url.pathname===`/api/jobs/${item.id}/cancel`);
    job.status='failed';job.phase='cancelled';job.finished_at=Date.now()/1000;
    job.message='已取消翻译，已完成的缓存会保留，可随时重试。';
    return reply(job,202);
  }
  blocked.push({method,path:url.pathname});
  return route.abort('blockedbyclient');
});
try {
  await page.goto(base.href,{waitUntil:'networkidle'});
  const running=page.locator('.job-card').filter({hasText:'Synthetic_running_paper.pdf'});
  const queued=page.locator('.job-card').filter({hasText:'Synthetic_queued_paper.pdf'});
  await running.waitFor();await queued.waitFor();
  const message=await running.locator('.job-message').innerText();
  assert.match(message,/48 \/ 496 段/);
  const meta=await running.locator('.job-meta').innerText();
  assert.match(meta,/已用时 10 分/);
  const statistics=await running.locator('.job-statistics').innerText();
  for(const text of ['总进度 43%','请求 76 次','复用缓存 12 段','保留原文 7 段','服务重试 4 次（30000）','百度处理中 1 个请求','最近处理进展在 40 秒前']) {
    assert.ok(statistics.includes(text),`Missing operational status: ${text}`);
  }
  const track=running.getByRole('progressbar');
  assert.equal(await track.getAttribute('aria-valuenow'),'43');
  assert.equal(await track.locator('div').evaluate(element=>element.style.width),'43.05%');
  assert.match(await queued.locator('.job-message').innerText(),/第 2 个/);
  assert.equal(await running.getByRole('link',{name:'运行日志'}).getAttribute('href'),`/api/jobs/${runningId}/log`);
  await page.screenshot({path:path.join(output,'running-desktop.png'),fullPage:true});
  report.display={message,meta,statistics,overall_progress:43.05};

  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'390px mobile overflow');
  await page.screenshot({path:path.join(output,'running-mobile.png'),fullPage:true});
  await queued.getByRole('button',{name:'取消排队',exact:true}).click();
  await queued.getByRole('button',{name:'调整完成，重新翻译',exact:true}).waitFor();
  assert.equal(await queued.locator('.status-badge').innerText(),'已取消');
  assert.match(await running.locator('.status-badge').innerText(),/正在翻译/);
  await running.getByRole('button',{name:'停止翻译',exact:true}).click();
  await running.getByRole('button',{name:'调整完成，重新翻译',exact:true}).waitFor();
  assert.equal(await running.locator('.status-badge').innerText(),'已取消');
  assert.match(await running.locator('.job-message').innerText(),/缓存会保留/);
  assert.match(await running.locator('.job-statistics').innerText(),/请求 76 次/);
  assert.ok((await running.locator('.terms-editor').inputValue()).includes('EEGNet'));
  assert.equal(await running.getByRole('button',{name:'调整完成，重新翻译',exact:true}).isEnabled(),true);
  assert.deepEqual(writes,[
    {method:'POST',path:`/api/jobs/${queuedId}/cancel`,body:{}},
    {method:'POST',path:`/api/jobs/${runningId}/cancel`,body:{}},
  ]);
  assert.deepEqual(blocked,[]);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'Cancelled card mobile overflow');
  await page.screenshot({path:path.join(output,'cancelled-mobile.png'),fullPage:true});
  assert.deepEqual(errors,[]);
  report.passed=true;
  report.mobile_width=390;
  console.log(JSON.stringify(report));
} catch(error) {
  report.failure=error.stack || String(error);
  await page.screenshot({path:path.join(output,'failure.png'),fullPage:true}).catch(()=>{});
  throw error;
} finally {
  await fs.writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));
  await browser.close();
}
