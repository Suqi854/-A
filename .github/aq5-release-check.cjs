const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const {performance} = require('node:perf_hooks');

const site = 'https://aq5-relay.phehwm62784.workers.dev';
const release = 'fbe9b65646bd422e8ef5b898ba220c20477152c6';
const previous = '243aa536765d4fff5fb83d33f585202dcae92d57';
const codes = process.env.AQ5_VERIFY_CODES.split(',');
const password = process.env.AQ5_KEY;
const median = a => [...a].sort((x,y)=>x-y)[Math.floor(a.length/2)];
const fetchQuick = url => fetch(url, {signal: AbortSignal.timeout(15000)});

async function firstInput(browser, script) {
  const page = await browser.newPage();
  try {
    await page.route('**/js/app.js', route => route.fulfill({contentType:'application/javascript; charset=utf-8', body:script}));
    const start = performance.now();
    await page.goto(site + '/#forecast', {waitUntil:'commit', timeout:15000});
    await page.locator('#fc-query').waitFor({state:'visible',timeout:15000});
    return Math.round(performance.now()-start);
  } finally { await page.close(); }
}

async function verifyStock(browser, code, index) {
  const page = await browser.newPage();
  let status = 'opening';
  try {
    await page.goto(site + '/#forecast/sh' + code, {waitUntil:'domcontentloaded',timeout:20000});
    await page.locator('#fc-query').waitFor({state:'visible',timeout:15000});
    status = 'unlocking';
    await page.locator('#unlockBtn').click();
    await page.locator('#ulR').uncheck();
    await page.locator('#ulP').fill(password);
    await page.locator('#ulGo').click();
    await page.waitForFunction(() => document.body.classList.contains('unlocked'), null, {timeout:40000});
    await page.locator('.modal').waitFor({state:'hidden',timeout:30000});
    status = 'submitting';
    const endpoint = site + '/__aq5/forecast/request';
    const submit = page.waitForResponse(r => r.url() === endpoint && r.request().method()==='POST',{timeout:25000});
    const returned = page.waitForResponse(r => r.url().startsWith(site+'/__aq5/forecast/result?id='), {timeout:8*60*1000});
    returned.catch(()=>{});
    const start = performance.now();
    await page.locator('#fc-analyze').click();
    const sent = await submit;
    if (!sent.ok()) throw new Error('submit-http-'+sent.status());
    const task = await sent.json();
    assert.match(task.id,/^[a-f0-9]{32}$/);
    status = 'awaiting-result';
    const response = await returned;
    assert.equal(response.url(),site+'/__aq5/forecast/result?id='+task.id);
    if (!response.ok()) throw new Error('result-http-'+response.status());
    status = 'rendering-result';
    await page.locator('.fc-summary').waitFor({state:'visible',timeout:30000});
    const quality = await page.evaluate(() => ({
      summary: Boolean(document.querySelector('.fc-summary')?.textContent?.trim()),
      hasHistoricalBadge: [...document.querySelectorAll('#p-forecast .chip')].some(x=>x.textContent === '历史分析'),
      kline: Boolean(document.querySelector('.fc-chart svg')),
    }));
    assert.ok(quality.summary);
    assert.match(await page.locator('#fc-query').inputValue(),new RegExp(code+'$'));
    console.log(JSON.stringify({case:index,status:'rendered',elapsed_ms:Math.round(performance.now()-start),...quality}));
    return !quality.hasHistoricalBadge;
  } catch(e) {
    console.log(JSON.stringify({case:index,status,error:e.name,reason:/^submit-http-\d+$|^result-http-\d+$/.test(e.message)?e.message:undefined}));
    return false;
  } finally { await page.close(); }
}

(async()=>{
  const live = await fetchQuick(site+'/js/app.js');
  assert.equal(live.status,200);
  assert.equal(live.headers.get('x-aq5-release'),release);
  const currentScript = await live.text();
  assert.ok(currentScript.includes("STATIC && page === 'forecast'"));
  const old = await fetchQuick('https://raw.githubusercontent.com/Suqi854/-A/'+previous+'/engine/web/js/app.js');
  assert.equal(old.status,200);
  const priorScript = await old.text();
  const browser = await chromium.launch({channel:'chrome',headless:true});
  try {
    const before = [], after = [];
    for(let n=0;n<3;n++){
      before.push(await firstInput(browser,priorScript));
      after.push(await firstInput(browser,currentScript));
    }
    console.log(JSON.stringify({release,first_input_before_ms:before,first_input_after_ms:after,before_median:median(before),after_median:median(after)}));
    const ai = await fetchQuick(site+'/data/ai.json');
    if (ai.ok) {
      const ledger = await ai.json();
      console.log(JSON.stringify({ai_snapshot_last_fill:ledger.trades?.[0]?.time || null}));
    }
    const results = await Promise.all(codes.map((code,i)=>verifyStock(browser,code,i+1)));
    if (results.some(x=>!x)) process.exitCode=1;
  } finally { await browser.close(); }
})().catch(e=>{
  console.log(JSON.stringify({status:'smoke-failed',error:e.name,assertion:e.code==='ERR_ASSERTION'?String(e.message).slice(0,150):undefined,cause:e.cause?.code}));
  process.exitCode=1;
});
