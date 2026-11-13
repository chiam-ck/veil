// Exercise the waiting state without uploading files or changing live jobs.
const {chromium} = require('../.browser/node_modules/playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
(async () => {
  const browser = await chromium.launch({executablePath:(process.env.VEIL_CHROME_EXECUTABLE||'/usr/bin/google-chrome'),headless:true,args:['--no-sandbox']});
  try {
    const page = await browser.newPage({ignoreHTTPSErrors:true,viewport:{width:390,height:844}});
    const errors=[];page.on('pageerror', e=>errors.push(e.message));
    const batch='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', job='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
    const counts={not_submitted:0,receiving:0,queued:0,processing:0,waiting_provider:1,completed:0,review_required:0,blocked:0,failed:0,deleted:0};
    await page.route('**/api/batches/'+batch,route=>route.fulfill({json:{id:batch,total:1,finished:0,ready:0,export_ready:false,counts,items:[{id:job,index:0,suffix:'.docx',status:'waiting_provider',stage:'The model is temporarily unavailable. Retrying automatically.'}]}}));
    let artifactRequests=0;
    page.on('request',r=>{if(r.url().includes('/api/jobs/'+job+'/'))artifactRequests++});
    await page.goto((process.env.VEIL_TEST_URL||'https://127.0.0.1:8787')+'/#batch='+batch);
    await page.locator('#status').filter({hasText:'Waiting for model'}).waitFor();
    assert.equal(await page.locator('#files .waiting_provider').textContent(),'Waiting for model');
    assert.equal(await page.locator('#batch-counts').textContent(),'Waiting for model: 1');
    assert.ok((await page.locator('#stage').textContent()).includes('Retrying automatically'));
    assert.equal(await page.locator('#batch-download').isVisible(),false);
    assert.equal(await page.locator('#downloads a').count(),0);
    assert.equal(await page.locator('#delete').isEnabled(),true);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    assert.equal(artifactRequests,0);assert.deepEqual(errors,[]);
    fs.mkdirSync('evidence/provider-recovery',{recursive:true});
    await page.screenshot({path:'evidence/provider-recovery/waiting-mobile.png',fullPage:true});
    fs.writeFileSync('evidence/provider-recovery/browser-check.json',JSON.stringify({waitingVisible:true,automaticRetryExplained:true,noPartialDownloads:true,noArtifactRequests:true,mobileOverflow:false,errors},null,2));
    console.log('Provider recovery browser checks passed.');
  } finally {await browser.close()}
})().catch(e=>{console.error(e.message);process.exit(1)});
