// Pin a matching frontend independently of the job publishing market data.
export const RESEARCH_UI_COMMIT='123d1704a1f26863212df071eca4e58fa1306d65';
const JS='application/javascript; charset=utf-8',CSS='text/css; charset=utf-8';
const WEB=new Map([
  ['/index.html','text/html; charset=utf-8'],['/icon.svg','image/svg+xml'],['/manifest.json','application/manifest+json'],
  ['/css/app.css',CSS],['/css/themes.css',CSS],['/css/mobile.css',CSS],
  ['/js/static.js',JS],['/js/app.js',JS],['/js/charts.js',JS],['/js/lite.js',JS],['/js/forecast.js',JS],
]);

export async function researchUiRoute(req,env={}){
  if(!['GET','HEAD'].includes(req.method))return null;
  const {pathname}=new URL(req.url), path=pathname==='/'?'/index.html':pathname;
  const configured=/^[a-f0-9]{40}$/.test(env.AQ5_UI_COMMIT||'');
  if(!WEB.has(path)||(!configured&&path!=='/js/app.js'))return null;
  const release=configured?env.AQ5_UI_COMMIT:RESEARCH_UI_COMMIT;
  const headers={'content-type':WEB.get(path),'cache-control':'no-cache',
    'x-content-type-options':'nosniff','x-aq5-mode':'static','x-aq5-release':release};
  if(path==='/js/static.js')return new Response(req.method==='HEAD'?null:'window.AQ_STATIC = true;\n',{headers});
  const source='https://raw.githubusercontent.com/Suqi854/-A/'+release+'/engine/web'+path;
  try{
    const response=await fetch(source,{method:req.method,cf:{cacheTtl:86400,cacheEverything:true}});
    if(response.ok){
      if(req.method==='HEAD')return new Response(null,{headers});
      if(path!=='/index.html')return new Response(response.body,{headers});
      const html=await response.text(),boot='<script src="js/charts.js"></script>';
      if(html.includes(boot))return new Response(html.replace(boot,'<script src="js/static.js"></script>\n'+boot),{headers});
    }
  }catch{
    // Keep the assets on one revision even if the upstream is unavailable.
  }
  return new Response('Reviewed frontend temporarily unavailable',{status:503,headers:{'cache-control':'no-store'}});
}
