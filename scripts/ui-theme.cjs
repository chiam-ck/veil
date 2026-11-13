// User-facing theme, branding and favicon checks on all shipped pages.
const {chromium} = require('../.browser/node_modules/playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const base = process.env.VEIL_TEST_URL || 'https://127.0.0.1:8787';
const folder = 'evidence/branding';
fs.mkdirSync(folder, {recursive:true});
function contrast(a,b){
  const luminance=s=>{const rgb=s.match(/[\d.]+/g).slice(0,3).map(Number).map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4});return rgb[0]*.2126+rgb[1]*.7152+rgb[2]*.0722};
  const x=luminance(a),y=luminance(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05);
}
(async()=>{
  const browser=await chromium.launch({executablePath:(process.env.VEIL_CHROME_EXECUTABLE||'/usr/bin/google-chrome'),headless:true,args:['--no-sandbox']});
  try{
    const context=await browser.newContext({ignoreHTTPSErrors:true,colorScheme:'light',viewport:{width:1280,height:1100}});
    const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
    const paths=['/','/developer-guide.html','/review-criteria.html'];
    for(const theme of ['light','dark']){
      await page.goto(base);
      if(await page.locator('html').getAttribute('data-theme')!==theme)
        await page.getByRole('button',{name:'Switch to '+theme+' mode',exact:true}).click();
      for(const path of paths){
        await page.goto(base+path);
        assert.equal(await page.locator('html').getAttribute('data-theme'),theme);
        assert.equal(await page.locator('a.logo img').count(),1);
        assert.ok(await page.locator('a.logo img').evaluate(img=>img.complete&&img.naturalWidth>0));
        assert.ok(!(await page.locator('header').textContent()).includes('◐'));
        const icon=await page.locator('link[rel=icon]').first().getAttribute('href');
        assert.equal(icon,'/favicon.ico');assert.equal((await page.request.get(base+icon)).status(),200);
        const colors=await page.evaluate(()=>{
          const root=getComputedStyle(document.documentElement);
          const text=root.color,bg=root.backgroundColor;
          const sample=document.createElement('p');sample.textContent='Sample';document.body.append(sample);
          const muted=getComputedStyle(sample).color;sample.remove();return{text,bg,muted};
        });
        assert.ok(contrast(colors.text,colors.bg)>=4.5,'Body text contrast');
        assert.ok(contrast(colors.muted,colors.bg)>=4.5,'Muted text contrast');
        for(const [width,height,label]of[[1280,1100,'desktop'],[390,844,'mobile']]){
          await page.setViewportSize({width,height});
          assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`${path} ${theme} ${label} overflow`);
          const slug=path==='/'?'workspace':path.replace('/','').replace('.html','');
          await page.screenshot({path:`${folder}/${slug}-${theme}-${label}.png`,fullPage:true});
        }
      }
    }
    await page.reload();assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
    await page.emulateMedia({colorScheme:'light'});
    assert.equal(await page.locator('html').getAttribute('data-theme'),'dark','Saved choice must override OS preference');
    const systemContext=await browser.newContext({ignoreHTTPSErrors:true,colorScheme:'dark'});
    const systemPage=await systemContext.newPage();await systemPage.goto(base);
    assert.equal(await systemPage.locator('html').getAttribute('data-theme'),'dark','Follow OS without saved preference');
    await systemPage.emulateMedia({colorScheme:'light'});
    await systemPage.waitForFunction(()=>document.documentElement.dataset.theme==='light');
    await systemContext.close();
    const blockedContext=await browser.newContext({ignoreHTTPSErrors:true,colorScheme:'light'});
    const blockedPage=await blockedContext.newPage();blockedPage.on('pageerror',e=>errors.push(e.message));
    await blockedPage.addInitScript(()=>Object.defineProperty(window,'localStorage',{get(){throw new Error('Storage unavailable')}}));
    await blockedPage.goto(base);await blockedPage.getByRole('button',{name:'Switch to dark mode',exact:true}).click();
    assert.equal(await blockedPage.locator('html').getAttribute('data-theme'),'dark');
    await blockedContext.close();assert.deepEqual(errors,[]);
    fs.writeFileSync(folder+'/browser-check.json',JSON.stringify({pages:paths,themes:['light','dark'],viewports:[1280,390],logoLoaded:true,faviconLoaded:true,themePersists:true,systemPreference:true,storageUnavailableHandled:true,textContrast:'WCAG AA',mobileOverflow:false,errors},null,2));
    console.log('Branding and theme checks passed across all pages, both themes and viewports.');
  }finally{await browser.close()}
})().catch(error=>{console.error(error.message);process.exit(1)});
