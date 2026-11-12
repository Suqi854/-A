import assert from 'node:assert/strict';
import {test} from 'node:test';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const source=readFileSync(new URL('../web/js/app.js',import.meta.url),'utf8');
const names=['researchProgressText','renderResearchPreview','stopResearchProgress','readResearchStatus','watchResearchProgress','loadResearchPreview'];
const implementation=names.map(name=>source.match(new RegExp(`^  (?:async )?function ${name}\\(.*?^  }`,'ms'))[0]).join('\n');
const BUILD='a'.repeat(32),COMMIT='b'.repeat(40);
const build=(phase,completed=0)=>({ok:true,build:{build_id:BUILD,phase,completed,total:3195,updated_at:'2026-11-12T11:30:00+08:00'}});
const report=()=>({kind:'research_preview',quote_time_status:'known',asof:'2026-11-11',generated_at:'2026-11-12T08:00:00+08:00',
  routes:{value:{},emotion:{}},full_research:build('queued')});
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};};
const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
function harness({initial=report(),statuses=[build('running',50)],final,fetcher,derive,local=false}={}){
 const S={page:'pick',priv:{},privateSession:{key:{}},timers:{}},timers=new Map(),paths=[],network=[];let seq=0,derives=0;
 const el={hidden:false,dataset:{},_html:'',note:null,querySelector(){return this.note;},
   set innerHTML(value){this._html=value;const match=value.match(/data-research-progress[^>]*>(.*?)<\/div>/s);this.note=match?{textContent:match[1]}:null;},get innerHTML(){return this._html;}};
 const api=async path=>{paths.push(path);if(path==='research/preview')return structuredClone(initial);
   if(path.startsWith('research/preview?read_only=1'))return structuredClone(final||{...initial,snapshot_commit:COMMIT,generated_at:'2026-11-12T11:30:00+08:00'});
   if(path==='research/status')return structuredClone(statuses.shift());throw Error('unexpected API '+path);};
 const context=vm.createContext({S,STATIC:!local,document:{hidden:false},$:()=>el,api,
  esc:value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;'),sourceTime:x=>x||'--',researchRun:r=>'<div class="existing-content">'+r.generated_at+'</div>',panel:(_title,html)=>html,
  relayBase:()=> 'https://relay.example',TextEncoder,Uint8Array,AbortController,
  crypto:{subtle:{deriveBits:async()=>{derives++;return derive?derive():new Uint8Array(32).buffer;}}},
  setTimeout:(fn,delay)=>{const id=++seq;timers.set(id,{fn,delay});return id;},clearTimeout:id=>timers.delete(id),
  fetch:async(url,opt)=>{network.push({url,opt});if(fetcher)return fetcher(url,opt);return {ok:true,json:async()=>structuredClone(statuses.shift())};}
 });
 vm.runInContext(implementation,context);
 const fire=()=>{const next=[...timers.entries()].find(([,t])=>t.delay===20000);if(!next)return null;timers.delete(next[0]);return next[1].fn();};
 return{context,S,el,paths,network,timers,fire,derives:()=>derives,start:()=>context.loadResearchPreview(true)};
}

test('polling updates only the existing hint and never queues repeated screens',async()=>{
 const h=harness({statuses:[build('running',100),build('checkpoint',200)]});await h.start();await flush();
 const layout=h.el.innerHTML;assert.equal(h.paths.length,1);await h.fire();await h.fire();
 assert.equal(h.paths.length,1);assert.equal(h.network.length,2);assert.equal(h.derives(),1);
 assert.ok(h.network.every(x=>x.url.endsWith('/__aq5/research/status')&&x.opt.method===undefined));
 assert.match(h.el.note.textContent,/200\/3195/);assert.match(h.el.note.textContent,/2026-11-12T08:00:00/);
 assert.equal(h.el.innerHTML,layout); // No table/layout replacement while merely updating progress.
});
test('completion refreshes compact summary once in read-only mode and stops',async()=>{
 const completed=build('complete',3195);completed.build.snapshot_commit=COMMIT;
 const h=harness({statuses:[completed]});await h.start();await h.fire();await flush();
 assert.deepEqual(h.paths,['research/preview','research/preview?read_only=1&cache_key='+BUILD]);
 assert.equal(h.fire(),null);assert.equal(h.timers.size,0);assert.match(h.el.innerHTML,/11:30:00/);
 await h.context.loadResearchPreview(true);assert.equal(h.paths.length,2);
});
test('partial termination refreshes once without starting another build',async()=>{
 const partial=build('partial',3195);partial.build.coverage={emotion:{completed:3195,total:3195,unknown:100}};
 const h=harness({statuses:[partial]});await h.start();await h.fire();await flush();
 assert.match(h.el.note.textContent,/部分完成/);assert.match(h.el.note.textContent,/缺口 100/);
 assert.equal(h.paths.filter(x=>x.includes('read_only=1')).length,1);
 assert.equal(h.network.some(x=>x.url.includes('/start')),false);assert.equal(h.timers.size,0);
});
test('already matching terminal snapshot does not enqueue another summary read',async()=>{
 const initial=report();initial.snapshot_commit=COMMIT;initial.full_research=build('complete',3195);initial.full_research.build.snapshot_commit=COMMIT;
 const h=harness({initial});await h.start();await flush();assert.equal(h.paths.length,1);assert.equal(h.network.length,0);assert.equal(h.timers.size,0);
});
test('leaving page aborts in-flight status and ignores a late response',async()=>{
 const pending=deferred();const h=harness({fetcher:()=>pending.promise});await h.start();const polling=h.fire();await flush();
 assert.equal(h.network.length,1);const before=h.el.note.textContent;
 h.context.stopResearchProgress();h.S.page='anly';h.S.researchPreviewRequest=null;
 assert.equal(h.network[0].opt.signal.aborted,true);
 pending.resolve({ok:true,json:async()=>build('complete',3195)});await polling;
 assert.equal(h.el.note.textContent,before);assert.equal(h.paths.length,1);assert.equal(h.timers.size,0);
});
test('locking during token derivation cannot send or publish research data',async()=>{
 const pending=deferred();const h=harness({derive:()=>pending.promise});await h.start();const polling=h.fire();await flush();
 h.S.priv=null;h.S.privateSession=null;pending.resolve(new Uint8Array(32).buffer);await polling;
 assert.equal(h.network.length,0);assert.equal(h.paths.length,1);assert.equal(h.timers.size,0);
});
test('three status failures stop polling while preserving old snapshot',async()=>{
 const h=harness({fetcher:async()=>{throw Error('offline');}});await h.start();const before=h.el.innerHTML;
 await h.fire();await h.fire();await h.fire();assert.equal(h.network.length,3);assert.equal(h.timers.size,0);
 assert.equal(h.el.innerHTML,before);assert.match(h.el.note.textContent,/保留现有快照/);assert.equal(h.paths.length,1);
});
test('hidden document pauses network reads and resumes on visibility',async()=>{
 const h=harness();await h.start();h.context.document.hidden=true;await h.fire();assert.equal(h.network.length,0);
 h.context.document.hidden=false;await h.fire();assert.equal(h.network.length,1);
});
test('local mode reads authenticated local status without browser secret derivation',async()=>{
 const h=harness({local:true});await h.start();await h.fire();assert.deepEqual(h.paths,['research/preview','research/status']);assert.equal(h.derives(),0);assert.equal(h.network.length,0);
});
test('CI triggers and runs full research and progress tests without adding schedule',()=>{
 const ci=readFileSync(new URL('../../.github/workflows/aq5-forecast-checks.yml',import.meta.url),'utf8');
 assert.match(ci,/\.github\/workflows\/aq5-full-research\.yml/);
 assert.match(ci,/node --test engine\/tests\/test_full_research\.mjs engine\/tests\/test_research_progress\.mjs/);
});
