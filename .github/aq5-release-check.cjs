const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const {createHash}=require('node:crypto');
const {performance}=require('node:perf_hooks');
const worker='https://aq5-relay.phehwm62784.workers.dev/';
const pages='https://suqi854.github.io/-A/';
const codes=process.env.AQ5_VERIFY_CODES.split(',');
const pause=ms=>new Promise(r=>setTimeout(r,ms));
async function unlock(page,root){
  await page.goto(root+'#forecast',{waitUntil:'domcontentloaded',timeout:20000});
  await page.locator('#fc-query').waitFor({state:'visible',timeout:20000});
  await page.locator('#unlockBtn').click();
  await page.locator('#ulR').uncheck();
  await page.locator('#ulP').fill(process.env.AQ5_KEY);
  await page.locator('#ulGo').click();
  await page.waitForFunction(()=>document.body.classList.contains('unlocked'),null,{timeout:35000});
  await page.locator('.modal').waitFor({state:'hidden',timeout:30000});
}
async function measure(browser,kind,code){
  const page=await browser.newPage();
  let step='unlock';
  try{
    await unlock(page,worker);
    step='request';
    const post=page.waitForResponse(r=>r.url()===worker+'__aq5/forecast/request'&&r.request().method()==='POST',{timeout:25000});
    post.catch(()=>{});
    const start=performance.now();
    if(kind==='analysis') await page.evaluate(code=>location.hash='anly/sh'+code,code);
    else{
      await page.locator('#fc-query').fill(code);
      await page.locator('#fc-analyze').click();
    }
    const accepted=await post;
    assert.equal(accepted.status(),200);
    const body=await accepted.json();
    console.log(JSON.stringify({kind,accepted:true,cached:body.status==='ready'}));
    step='result';
    const selector=kind==='analysis'?'#p-anly .qhead':'.fc-summary';
    await page.locator(selector).waitFor({state:'visible',timeout:8*60*1000});
    if(kind==='analysis') assert.match(await page.locator(selector).innerText(),new RegExp(code));
    else{
      assert.match(await page.locator('#fc-query').inputValue(),new RegExp(code+'$'));
      assert.equal(await page.getByText('历史分析',{exact:true}).count(),0);
    }
    console.log(JSON.stringify({kind,elapsed_ms:Math.round(performance.now()-start),rendered:true}));
    return true;
  }catch(e){console.log(JSON.stringify({kind,step,error:e.name}));return false;}
  finally{await page.close();}
}
async function pick(browser){
  const page=await browser.newPage(),calls=[];
  try{
    await unlock(page,pages);
    page.on('request',r=>{
      if(r.method()==='POST'&&r.url().includes('/__aq5/')){
        let data={};try{data=r.postDataJSON()}catch{}
        calls.push({path:new URL(r.url()).pathname,kind:data?.kind,read_only:data?.read_only===true});
      }
    });
    const list=await page.evaluate(()=>fetch('data/runs.json').then(r=>r.json()));
    const start=performance.now();
    await page.evaluate(()=>location.hash='pick');
    await page.locator('#runDetail .ph').first().waitFor({state:'visible',timeout:15000});
    const selected=await page.locator('#runList tr.bgup').getAttribute('data-id');
    const initialMs=Math.round(performance.now()-start);
    await pause(18000);
    assert.equal(selected,String(list[0].id));
    assert.ok(calls.every(x=>x.path==='/__aq5/forecast/request'&&x.kind==='screen'&&x.read_only));
    console.log(JSON.stringify({pick_latest:true,first_result_ms:initialMs,readonly_summary_reads:calls.length,full_research_starts:0}));
    return true;
  }catch(e){console.log(JSON.stringify({pick_latest:false,error:e.name}));return false;}
  finally{await page.close();}
}
(async()=>{
  const hashes=[],newFlags=[];
  const browser=await chromium.launch({channel:'chrome',headless:true});
  try{
    for(const root of [worker,pages]){
      const res=await fetch(root+'js/app.js',{signal:AbortSignal.timeout(12000)});
      assert.equal(res.status,200);
      const js=await res.text();
      const hash=createHash('sha256').update(js).digest('hex').slice(0,12);
      const updated=js.includes('S.staticReads ||= new Map()');
      hashes.push(hash);newFlags.push(updated);
      const samples=[];
      for(let i=0;i<3;i++){
        const page=await browser.newPage();
        try{
          const started=performance.now();
          await page.goto(root+'#forecast',{waitUntil:'commit',timeout:16000});
          await page.locator('#fc-query').waitFor({state:'visible',timeout:16000});
          samples.push(Math.round(performance.now()-started));
        } finally {await page.close();}
      }
      console.log(JSON.stringify({entry:root===worker?'worker':'pages',hash,updated,input_ms:samples,median_input_ms:samples.sort((a,b)=>a-b)[1]}));
    }
    const updated=newFlags.every(Boolean);
    if(updated) assert.equal(hashes[0],hashes[1]);
    const results=await Promise.all([measure(browser,'analysis',codes[0]),measure(browser,'forecast',codes[1]),...(updated?[pick(browser)]:[])]);
    if(results.some(x=>!x))process.exitCode=1;
  }finally{await browser.close();}
})().catch(e=>{console.log(JSON.stringify({speed_check_failed:e.name,cause:e.cause?.code}));process.exitCode=1;});
