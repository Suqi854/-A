const {chromium} = require('playwright');
(async () => {
  const browser = await chromium.launch({channel:'chrome',headless:true});
  try {
    const page = await browser.newPage();
    await page.goto('https://aq5-relay.phehwm62784.workers.dev/#sim',{waitUntil:'domcontentloaded',timeout:20000});
    const latestTrade = () => {
      const box = [...document.querySelectorAll('#p-sim .panel')].find(x=>x.querySelector('h3')?.textContent.includes('TRADES'));
      return box?.querySelector('tr:nth-child(2) td')?.textContent.trim() || '';
    };
    const before = await page.waitForFunction(latestTrade,null,{timeout:30000}).then(r=>r.jsonValue());
    console.log(JSON.stringify({old_trade_display:before}));
    if (!before.startsWith('11-11')) throw new Error('unexpected_baseline');
    const result = await page.waitForFunction(() => {
      const box = [...document.querySelectorAll('#p-sim .panel')].find(x=>x.querySelector('h3')?.textContent.includes('TRADES'));
      const newest = box?.querySelector('tr:nth-child(2) td')?.textContent.trim() || '';
      return newest.startsWith('11-12') && newest;
    },null,{timeout:8*60*1000}).then(r=>r.jsonValue());
    console.log(JSON.stringify({after_close_autorefresh:true,new_trade_display:result}));
  } finally {await browser.close();}
})().catch(e => {console.log(JSON.stringify({after_close_autorefresh:false,error:e.name}));process.exitCode=1;});
