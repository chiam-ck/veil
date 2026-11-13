// Separate browser contexts model two anonymous users; only synthetic content.
const {chromium,request}=require('../.browser/node_modules/playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
(async()=>{
 const base=process.env.VEIL_TEST_URL||'https://127.0.0.1:8787';
 const folder='evidence/private-workspaces';fs.mkdirSync(folder,{recursive:true});
 const browser=await chromium.launch({executablePath:(process.env.VEIL_CHROME_EXECUTABLE||'/usr/bin/google-chrome'),headless:true,args:['--no-sandbox']});
 try{
  const a=await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:1280,height:1000}});
  const b=await browser.newContext({ignoreHTTPSErrors:true});
  const errors=[];const page=await a.newPage();page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base);await page.locator('#file').setInputFiles('/tmp/veil-manual-ui.docx');await page.locator('#submit').click();
  await page.waitForFunction(()=>document.getElementById('status')?.textContent==='Completed',{},{timeout:180000});
  await page.waitForFunction(()=>document.querySelectorAll('#downloads a').length>0);
  const batch=page.url().split('#batch=')[1],jid=(await page.locator('#jobid').textContent()).slice(4);
  const cookie=(await a.cookies()).find(c=>c.name==='__Host-veil-workspace');
  assert.ok(cookie&&cookie.httpOnly&&cookie.secure&&cookie.sameSite==='Strict');
  assert.ok(!(await page.evaluate(()=>document.cookie)).includes('__Host-veil-workspace'));
  const sameBrowser=await a.newPage();await sameBrowser.goto(base+'/#batch='+batch);
  await sameBrowser.waitForFunction(()=>document.getElementById('status')?.textContent==='Completed');
  const other=await b.newPage();other.on('pageerror',e=>errors.push(e.message));await other.goto(base+'/#batch='+batch);
  await other.waitForFunction(()=>document.getElementById('error').textContent.length>0);
  assert.equal(await other.locator('#clean').textContent(),'');
  const targets=[['GET','/api/jobs/'+jid],['GET','/api/jobs/'+jid+'/report'],['GET','/api/jobs/'+jid+'/original-text'],['GET','/api/jobs/'+jid+'/preview/1'],['GET','/api/jobs/'+jid+'/download/sanitised.docx'],['GET','/api/jobs/'+jid+'/download/report.json'],['GET','/api/batches/'+batch],['GET','/api/batches/'+batch+'/download'],['DELETE','/api/jobs/'+jid],['POST','/api/jobs/'+jid+'/reprocess']];
  for(const [method,path]of targets){const response=await b.request.fetch(base+path,{method,headers:{'X-Veil-Request':'1'},...(method==='POST'?{data:{mode:'rerun'}}:{})});assert.equal(response.status(),404,method+' '+path)}
  assert.equal((await a.request.get(base+'/api/jobs/'+jid+'/download/sanitised.docx')).status(),200);
  assert.equal((await b.request.post(base+'/api/batches',{data:{count:1}})).status(),403);
  const replay=await request.newContext({ignoreHTTPSErrors:true,extraHTTPHeaders:{Cookie:cookie.name+'='+cookie.value}});
  assert.equal((await replay.get(base+'/api/jobs/'+jid)).status(),200);
  await page.locator('.workspace-info summary').click();await page.locator('#end-workspace').click();await page.locator('#end-workspace').click();
  await page.waitForURL(base+'/');await sameBrowser.waitForURL(base+'/');
  await sameBrowser.waitForFunction(()=>document.getElementById('job').hidden&&document.getElementById('clean').textContent==='');
  assert.equal((await replay.get(base+'/api/jobs/'+jid)).status(),401);
  await replay.dispose();
  await page.waitForFunction(()=>document.getElementById('detector-mode').textContent.startsWith('Model · '));
  assert.equal((await a.request.get(base+'/api/jobs/'+jid)).status(),404);
  // Cookie loss creates a new workspace, never ownership of a known old link.
  await b.clearCookies();await other.reload();
  await other.waitForFunction(()=>document.getElementById('error').textContent.length>0);
  assert.equal((await b.request.get(base+'/api/jobs/'+jid)).status(),404);
  await page.setViewportSize({width:390,height:844});await page.locator('.workspace-info summary').click();
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.screenshot({path:folder+'/workspace-mobile.png',fullPage:true});
  assert.deepEqual(errors,[]);
  fs.writeFileSync(folder+'/browser-check.json',JSON.stringify({passed:true,separateBrowsersDeniedKnownIds:targets.length,sameProfileTabsShareAccess:true,secureHttpOnlyStrictCookie:true,credentialHiddenFromJavaScript:true,csrfEnforced:true,logoutRevokesReplayedCredential:true,logoutClearsOtherTab:true,cookieLossCannotReclaim:true,realGemmaUploadCompleted:true,certificateTrustBypassedForBrowserTestOnly:true,errors},null,2));
  console.log('Private workspace isolation, secure cookies, logout, cross-tab clearing and cookie-loss checks passed.');
  await a.close();await b.close();
 }finally{await browser.close()}
})().catch(e=>{console.error(e.stack);process.exit(1)});
