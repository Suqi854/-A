const {chromium} = require('playwright');
const {performance} = require('node:perf_hooks');
const {createHash} = require('node:crypto');
const sites = [
  ['worker', 'https://aq5-relay.phehwm62784.workers.dev/'],
  ['pages', 'https://suqi854.github.io/-A/'],
];
const median = rows => [...rows].sort((a,b)=>a-b)[Math.floor(rows.length/2)];
(async () => {
  const browser = await chromium.launch({channel:'chrome',headless:true});
  const samples = {worker:[],pages:[]};
  try {
    for (let i=0;i<3;i++) for (const [label,url] of i % 2 ? [...sites].reverse() : sites) {
      const page = await browser.newPage();
      const before = performance.now();
      try {
        const nav = await page.goto(url + '#forecast',{waitUntil:'commit',timeout:12000});
        await page.locator('#fc-query').waitFor({state:'visible',timeout:12000});
        const input = Math.round(performance.now()-before);
        const timing = await page.evaluate(() => ({
          ttfb: Math.round(performance.getEntriesByType('navigation')[0].responseStart),
          app: Math.round(performance.getEntriesByType('resource').find(x=>x.name.endsWith('/js/app.js'))?.duration || 0),
        }));
        samples[label].push({input,status:nav.status(),...timing});
      } catch(e) {
        samples[label].push({error:e.name});
      } finally { await page.close(); }
    }
    for (const [label, url] of sites) {
      const [app,overview]=await Promise.all([
        fetch(url+'js/app.js',{signal:AbortSignal.timeout(10000)}),
        fetch(url+'data/latest.json',{signal:AbortSignal.timeout(10000)}),
      ]);
      const source=await app.text();
      const rows=samples[label];
      const times=rows.filter(x=>Number.isInteger(x.input));
      console.log(JSON.stringify({origin:label,samples:rows,median_input_ms:times.length?median(times.map(x=>x.input)):null,
        median_ttfb_ms:times.length?median(times.map(x=>x.ttfb)):null,
        version_header:app.headers.get('x-aq5-release'),app_sha256:createHash('sha256').update(source).digest('hex').slice(0,12),
        nonblocking_forecast:source.includes("STATIC && page === 'forecast'"),
        snapshot_time:overview.ok?(await overview.json()).time:null}));
    }
  } finally { await browser.close(); }
})().catch(e=>{console.log(JSON.stringify({error:e.name,cause:e.cause?.code}));process.exitCode=1;});
