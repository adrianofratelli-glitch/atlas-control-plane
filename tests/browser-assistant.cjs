const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
(async () => {
 const browser = await chromium.launch({headless:true});
 const page = await browser.newPage({viewport:{width:1440,height:1100}});
 const errors=[];page.on('pageerror', e=>errors.push(e.message));
 const clusters=[{project_id:'p1',cluster_name:'demo-a',project_name:'Projeto A',tier:'M10',status:'IDLE',cost_brl:300,cost_usd:60,mongo_version:'8.0',region_pretty:'AWS São Paulo',cluster_type:'REPLICASET'}, {project_id:'p2',cluster_name:'demo-b',project_name:'Projeto B',tier:'M20',status:'IDLE',cost_brl:600,cost_usd:120,mongo_version:'8.0',region_pretty:'AWS São Paulo',cluster_type:'REPLICASET'}];
 let requests=[], decisions=0, action=null;
 await page.route('**/api/**',async route=>{
  const req=route.request(),url=new URL(req.url()),path=url.pathname;let data={};
  if(path==='/api/config')data={atlas:true,anthropic:true,mongodb:false,pricing:{},tiers:{dedicated:['M10','M20']},usd_brl:5.7};
  else if(path==='/api/clusters')data={clusters};
  else if(path==='/api/alerts')data={open_alerts:0};
  else if(path==='/api/assistant/capabilities')data={tools:Array(28).fill({}),data_connection_configured:true};
  else if(path==='/api/assistant/actions/list')data={actions:action?[action]:[]};
  else if(path==='/api/assistant'){
   requests.push(req.postDataJSON());
   action={id:'example',state:'pending',expires_at:Date.now()/1000+900,operation:'mongo_create_index',arguments:{namespace:'demo.orders',keys:[{status:1}]},details:{operation:'Criar índice',cluster:'demo-b',namespace:'demo.orders',impact:'Melhora a consulta por status; adiciona custo de escrita.'},destructive:false};
   const events=[{type:'connected',tools:28},{type:'tool_start',id:'t1',label:'Buscar índices recomendados'},{type:'tool_end',id:'t1',label:'Buscar índices recomendados',ok:true,message:'Consulta concluída'},{type:'text',text:'Encontrei quatro recomendações de índices. Preparei o índice solicitado; ele aguarda sua aprovação.'},{type:'action',action},{type:'done'}];
   return route.fulfill({status:200,contentType:'application/x-ndjson',body:events.map(e=>JSON.stringify(e)).join('\n')+'\n'});
  } else if(path==='/api/assistant/actions/example'){
   decisions++;await new Promise(r=>setTimeout(r,250));action={...action,state:'completed',result:{index:'status_1'}};data=action;
  }
  return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});
 });
 await page.goto(process.env.TORRE_TEST_URL || 'http://127.0.0.1:5290/');
 await page.getByRole('button',{name:'Índices',exact:true}).click();
 await page.locator('select:visible').first().selectOption('p2:demo-b');
 await page.getByRole('button',{name:'Assistente',exact:true}).click();
 await page.getByRole('heading',{name:'Converse com seus dados'}).waitFor();
 assert.equal(await page.getByLabel('Cluster da conversa').inputValue(),'p2:demo-b');
 await page.getByRole('button',{name:/Melhorar desempenho/}).click();
 await page.getByRole('button',{name:'Aprovar e executar'}).waitFor();
 assert.equal(requests[0].project_id,'p2');assert.equal(requests[0].cluster_name,'demo-b');assert.equal(decisions,0);
 await page.screenshot({path:'/private/tmp/torre-assistant-desktop.png',fullPage:true});
 await page.getByRole('button',{name:'Aprovar e executar'}).dblclick({force:true});
 await page.getByText('Operação confirmada pelo servidor.').waitFor();assert.equal(decisions,1);
 await page.getByRole('button',{name:'Visão Geral',exact:true}).click();
 await page.getByRole('button',{name:'Assistente',exact:true}).click();
 assert.ok(await page.getByText(/Encontrei quatro recomendações/).isVisible());
 await page.setViewportSize({width:390,height:844});
 await page.screenshot({path:'/private/tmp/torre-assistant-mobile.png',fullPage:true});
 const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth);
 assert.equal(overflow,false);assert.deepEqual(errors,[]);
 console.log(JSON.stringify({passed:true,sharedCluster:true,approvalOnce:decisions,navigationPreservesConversation:true,mobileNoOverflow:true,pageErrors:errors}));
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
