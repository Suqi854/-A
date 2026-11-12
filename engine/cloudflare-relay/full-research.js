// Independent whole-market research lifecycle. No account state or trading duty changes.
const ROOT='/__aq5/research/', WORKFLOW='aq5-full-research.yml';
const ACTIVE=new Set(['queued','running','checkpoint']);
const HEADERS={'cache-control':'no-store','access-control-allow-origin':'*','x-content-type-options':'nosniff'};
const reply=(body,status=200)=>Response.json(body,{status,headers:HEADERS});
const error=(reason,status=400)=>reply({ok:false,error:reason},status);
const id=()=>crypto.randomUUID().replaceAll('-','');
const validId=value=>typeof value==='string'&&/^[a-f0-9]{32}$/.test(value);
function equal(a,b){if(typeof a!=='string'||!a.length||typeof b!=='string'||a.length!==b.length)return false;let n=0;for(let i=0;i<a.length;i++)n|=a.charCodeAt(i)^b.charCodeAt(i);return n===0;}
function authorized(req,env,publisher=false){return equal(env.LIVE_KEY,req.headers.get('x-aq5-live-key'))||!publisher&&equal(env.FORECAST_KEY,req.headers.get('x-aq5-forecast-token'));}
async function body(req,limit=4096){
  const reader=req.body?.getReader();if(!reader)return {};
  let length=0;const chunks=[];
  for(;;){const next=await reader.read();if(next.done)break;length+=next.value.byteLength;if(length>limit){await reader.cancel();throw Error('size');}chunks.push(next.value);}
  const bytes=new Uint8Array(length);let at=0;for(const chunk of chunks){bytes.set(chunk,at);at+=chunk.length;}
  const value=JSON.parse(new TextDecoder().decode(bytes)||'{}');
  if(!value||typeof value!=='object'||Array.isArray(value))throw Error('shape');return value;
}
function visible(build){if(!build)return null;const out={};for(const k of ['build_id','phase','requested_at','updated_at','stage','completed','total','coverage','snapshot_commit','run_id','reason'])if(build[k]!==undefined)out[k]=build[k];return out;}
function hasGaps(build){return Number(build.completed||0)<Number(build.total||0)||Object.values(build.coverage||{}).some(x=>Number(x.unknown||0)>0||Number(x.completed||0)<Number(x.total||0));}
async function github(env,path,options={}){
  if(!env.GH_TOKEN)throw Error('dispatch_credentials_unavailable');
  const response=await fetch('https://api.github.com/repos/Suqi854/-A/actions/workflows/'+WORKFLOW+path,{
    ...options,headers:{'User-Agent':'aq5-full-research','Accept':'application/vnd.github+json','Authorization':'Bearer '+String(env.GH_TOKEN).trim(),'Content-Type':'application/json'},signal:AbortSignal.timeout(10000)});
  return response;
}
function progress(value){
  if(!validId(value.build_id)||!['running','checkpoint','complete','partial','error'].includes(value.phase))throw Error('identity');
  const out={build_id:value.build_id,phase:value.phase};
  for(const key of ['completed','total'])if(value[key]!==undefined){if(!Number.isSafeInteger(value[key])||value[key]<0||value[key]>100000)throw Error('count');out[key]=value[key];}
  if(out.completed!==undefined&&out.total!==undefined&&out.completed>out.total)throw Error('count');
  for(const [key,max] of [['stage',40],['reason',160]])if(value[key]!==undefined){if(typeof value[key]!=='string'||value[key].length>max||/[\r\n]/.test(value[key]))throw Error('text');out[key]=value[key];}
  if(value.run_id!==undefined){if(!/^\d{1,24}$/.test(String(value.run_id)))throw Error('run');out.run_id=String(value.run_id);}
  if(value.snapshot_commit!==undefined){if(!/^[a-f0-9]{40}$/.test(value.snapshot_commit))throw Error('commit');out.snapshot_commit=value.snapshot_commit;}
  if(value.coverage!==undefined){
    if(!value.coverage||typeof value.coverage!=='object'||Array.isArray(value.coverage))throw Error('coverage');out.coverage={};
    for(const route of ['value','emotion'])if(value.coverage[route]!==undefined){
      const source=value.coverage[route],counts={};if(!source||typeof source!=='object')throw Error('coverage');
      for(const key of ['completed','total','eligible','unknown','excluded'])if(source[key]!==undefined){if(!Number.isSafeInteger(source[key])||source[key]<0||source[key]>100000)throw Error('count');counts[key]=source[key];}
      if(counts.total!==undefined&&Object.entries(counts).some(([key,n])=>key!=='total'&&n>counts.total))throw Error('count');out.coverage[route]=counts;
    }
  }
  return out;
}
export async function fullResearchRoute(req,env){
  const url=new URL(req.url);if(!url.pathname.startsWith(ROOT))return null;
  if(req.method==='OPTIONS')return new Response(null,{status:204,headers:{...HEADERS,'access-control-allow-methods':'GET, POST, OPTIONS','access-control-allow-headers':'content-type, x-aq5-forecast-token, x-aq5-live-key'}});
  const action=url.pathname.slice(ROOT.length);
  if(!['start','status','progress'].includes(action))return error('研究请求地址无效',404);
  if(!authorized(req,env,action==='progress'))return error('研究授权无效',403);
  if(!env.LIVE)return error('研究状态通道未就绪',503);
  return env.LIVE.get(env.LIVE.idFromName('full-research')).fetch(req);
}
export async function fullResearchFetch(req,ctx,env){
  const action=new URL(req.url).pathname.slice(ROOT.length), now=Date.now();
  if(!authorized(req,env,action==='progress'))return error('研究授权无效',403);
  if(action==='status'&&req.method==='GET')return reply({ok:true,started:false,build:visible(await ctx.storage.get('build'))});
  if(action==='progress'&&req.method==='POST'){
    let update;try{update=progress(await body(req));}catch{return error('研究进度格式无效');}
    let saved=null;
    await ctx.storage.transaction(async tx=>{
      const current=await tx.get('build');if(!current||current.build_id!==update.build_id)return;
      if(!ACTIVE.has(current.phase)&&current.phase!==update.phase)return;
      if(current.stage===update.stage&&update.completed!==undefined&&current.completed!==undefined&&update.completed<current.completed)return;
      const merged={...current,...update,updated_at:new Date(now).toISOString()};
      if(merged.completed>merged.total)return;
      if(merged.phase==='complete'&&(!Number.isSafeInteger(merged.total)||merged.total<=0||merged.completed!==merged.total))return;
      await tx.put('build',merged);saved=merged;
    });
    return saved?reply({ok:true,started:false,build:visible(saved)}):error('研究任务已替换或进度已过期',409);
  }
  if(action==='start'&&req.method==='POST'){
    try{await body(req,1024);}catch{return error('研究启动格式无效');}
    const token=id();let current=null,blocked=null;
    await ctx.storage.transaction(async tx=>{
      current=await tx.get('build');const reservation=await tx.get('start-reservation');
      if(reservation&&reservation.until>now){blocked='checking';return;}
      if(current&&ACTIVE.has(current.phase)&&now-Date.parse(current.updated_at)<15*60*1000){blocked='already_running';return;}
      if(current?.phase==='complete'&&!hasGaps(current)&&now-Date.parse(current.updated_at)<15*60*1000){blocked='recent_complete';return;}
      if(current&&now-Date.parse(current.requested_at)<60000){blocked='retry_later';return;}
      await tx.put('start-reservation',{token,until:now+60000});
    });
    if(blocked)return reply({ok:true,started:false,reason:blocked,build:visible(current)});
    try{
      const check=await github(env,'/runs?per_page=100');if(!check.ok)throw Error('github_status_unavailable');
      const list=await check.json();
      if(!Array.isArray(list.workflow_runs))throw Error('github_status_invalid');
      const active=list.workflow_runs.find(x=>['queued','in_progress','waiting','requested','pending'].includes(x.status));
      if(active){
        const captured=String(active.display_title||'').match(/\b([a-f0-9]{32})\b/);
        const found={...(current||{}),build_id:captured?.[1]||current?.build_id||token,
          phase:active.status==='in_progress'?'running':'queued',run_id:String(active.id),
          requested_at:current?.requested_at||new Date(now).toISOString(),updated_at:new Date(now).toISOString()};
        await ctx.storage.transaction(async tx=>{if((await tx.get('start-reservation'))?.token!==token)return;await tx.put('build',found);await tx.delete('start-reservation');});
        return reply({ok:true,started:false,reason:'github_pending',build:visible(found)});
      }
      const queued={build_id:token,phase:'queued',requested_at:new Date(now).toISOString(),updated_at:new Date(now).toISOString(),stage:'universe',completed:0,total:0};
      await ctx.storage.transaction(async tx=>{if((await tx.get('start-reservation'))?.token!==token)throw Error('start_lease_lost');await tx.put('build',queued);await tx.delete('start-reservation');});
      const dispatch=await github(env,'/dispatches',{method:'POST',body:JSON.stringify({ref:'main',inputs:{build_id:token}})});
      if(dispatch.status!==204){
        await ctx.storage.transaction(async tx=>{const row=await tx.get('build');if(row?.build_id===token&&row.phase==='queued')await tx.put('build',{...row,phase:'error',reason:'GitHub启动被拒绝',updated_at:new Date().toISOString()});});
        return error('全市场研究启动失败',503);
      }
      return reply({ok:true,started:true,build:visible(queued)});
    }catch{
      // An uncertain dispatch may have succeeded: retain queued ownership, never blindly resubmit.
      await ctx.storage.transaction(async tx=>{if((await tx.get('start-reservation'))?.token===token)await tx.delete('start-reservation');});
      return error('研究状态暂不可核对，请稍后刷新',503);
    }
  }
  return error('研究请求方式无效',405);
}
