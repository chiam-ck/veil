// Real browser -> API -> worker workflow, with synthetic content only.
const {chromium}=require('../.browser/node_modules/playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
(async()=>{
 const browser=await chromium.launch({executablePath:(process.env.VEIL_CHROME_EXECUTABLE||'/usr/bin/google-chrome'),headless:true,args:['--no-sandbox']});
 const base=process.env.VEIL_TEST_URL||'https://127.0.0.1:8787', folder='evidence/manual-revisions';fs.mkdirSync(folder,{recursive:true});
 try{
  const page=await browser.newPage({ignoreHTTPSErrors:true,viewport:{width:1280,height:1000}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
  async function done(){await page.waitForFunction(()=>['Completed','Review required','Blocked','Failed'].includes(document.getElementById('status')?.textContent),{},{timeout:180000});assert.ok(['Completed','Review required'].includes(await page.locator('#status').textContent()));await page.waitForFunction(()=>document.querySelectorAll('#downloads a').length>0)}
  async function job(){return (await page.locator('#jobid').textContent()).slice(4)}
  await page.goto(base);await page.locator('#file').setInputFiles(['/tmp/veil-manual-ui.docx','/tmp/veil-manual-ui.docx']);await page.locator('#submit').click();await page.waitForFunction(()=>document.querySelectorAll('#files .badge.completed').length===2,{},{timeout:180000});await page.locator('#files button').nth(1).click();await done();
  const original=await job(), batch=page.url().split('#batch=')[1];
  assert.ok((await page.locator('#clean').textContent()).includes('quiet meadow'));
  assert.ok(!(await page.locator('#clean').textContent()).includes('amelia.quill@example.test'));
  await page.evaluate(()=>{const text=document.getElementById('clean').firstChild,start=text.textContent.indexOf('quiet meadow');const range=document.createRange();range.setStart(text,start);range.setEnd(text,start+'quiet meadow'.length);const selection=getSelection();selection.removeAllRanges();selection.addRange(range)});
  await page.locator('#use-selection').click();assert.equal(await page.locator('#redaction-text').inputValue(),'quiet meadow');await page.locator('#add-redaction').click();
  assert.equal(await page.locator('#redaction-list li').count(),1);
  await page.locator('#apply-redactions').click();await page.waitForFunction(id=>document.getElementById('jobid')?.textContent&&document.getElementById('jobid').textContent!=='Job '+id,original);await done();
  const manual=await job();assert.notEqual(manual,original);
  assert.ok(!(await page.locator('#clean').textContent()).includes('quiet meadow'));
  assert.ok((await page.locator('#clean').textContent()).includes('silver horizon'));
  const manualReport=await(await page.request.get(base+'/api/jobs/'+manual+'/report')).json();assert.equal(manualReport.detector.mode,'manual');assert.equal(manualReport.detector.calls,0);
  fs.writeFileSync(folder+'/manual-report.json',JSON.stringify(manualReport,null,2));
  await page.screenshot({path:folder+'/manual-desktop.png',fullPage:true});
  await page.getByRole('link',{name:'Previous result',exact:true}).click();await page.waitForURL('**/#job='+original);await done();
  assert.ok((await page.locator('#clean').textContent()).includes('quiet meadow'));assert.equal(await page.locator('#corrections').isVisible(),false);
  await page.getByRole('link',{name:'Newer result',exact:true}).click();await page.waitForURL('**/#job='+manual);await done();
  await page.locator('#redaction-text').fill('silver horizon');await page.locator('#add-redaction').click();await page.locator('#rerun').click();
  await page.waitForFunction(id=>document.getElementById('jobid')?.textContent&&document.getElementById('jobid').textContent!=='Job '+id,manual);await done();
  const rerun=await job(), report=await(await page.request.get(base+'/api/jobs/'+rerun+'/report')).json();assert.equal(report.detector.mode,'llm');assert.ok(report.detector.calls>0);
  const clean=await page.locator('#clean').textContent();for(const value of ['quiet meadow','silver horizon','amelia.quill@example.test'])assert.ok(!clean.includes(value));
  const result=await page.request.get(base+'/api/jobs/'+rerun+'/download/sanitised.docx');fs.writeFileSync(folder+'/sanitised.docx',await result.body());
  fs.writeFileSync(folder+'/rerun-report.json',JSON.stringify(report,null,2));
  const archive=await page.request.get(base+'/api/batches/'+batch+'/download');assert.equal(archive.status(),200);fs.writeFileSync(folder+'/batch.zip',await archive.body());
  const restored=await(await page.request.get(base+'/api/batches/'+batch)).json();assert.equal(restored.items[1].id,rerun);assert.equal(restored.total,2);
  await page.setViewportSize({width:390,height:844});await page.locator('.theme-toggle').click();assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);await page.screenshot({path:folder+'/rerun-dark-mobile.png',fullPage:true});
  const storage=await page.evaluate(()=>JSON.stringify({local:{...localStorage},session:{...sessionStorage}}));assert.ok(!storage.includes('quiet meadow')&&!storage.includes('silver horizon'));
  for(const path of ['/developer-guide.html','/developer-guide.md']){const text=await(await page.request.get(base+path)).text();assert.ok(!/\/home\/|192\.168\./.test(text));assert.ok(text.includes('veil.example.internal'))}
  assert.deepEqual(errors,[]);
  fs.writeFileSync(folder+'/browser-check.json',JSON.stringify({passed:true,realProvider:true,batch,original,manual,rerun,selectedPreviewText:true,manualRequests:manualReport.detector.calls,rerunRequests:report.detector.calls,previousResultRetained:true,batchUsesNewestRevision:true,draftsNotPersisted:true,guideObfuscated:true,mobileOverflow:false,errors},null,2));
  console.log('Manual correction, revision navigation, model re-run, downloads and guide checks passed.');
 }finally{await browser.close()}
})().catch(e=>{console.error(e.stack);process.exit(1)});
