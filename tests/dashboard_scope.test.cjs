// Run with node --test tests/dashboard_scope.test.cjs (no packages required).
// Plugin detail panels must remain text-only and clear on scope changes.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const template=fs.readFileSync('src/anima/adapters/dashboard/assets/dashboard.js','utf8');
const messages=JSON.parse(fs.readFileSync('src/anima/adapters/dashboard/assets/messages.json','utf8'));
function localizedSource(locale){return template.replace(/__ANIMA_I18N_([a-z0-9_]+)__/g,(_,key)=>messages[locale][key].replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/"/g,'\\"').replace(/`/g,'\\`').replace(/\$\{/g,'\\${').replace(/\n/g,'\\n').replace(/\r/g,'\\r'))}
const source=localizedSource('ja');
test('English UI formats status and preserves user memory and custom extension text',()=>{
  const h=harness(false,'en');
  const value=status(a);
  value.memory_contents={nonempty_documents:1,documents:[{path:'memory/self.md',kind:'self',lines:1,content:'日本語の記憶はそのまま'}]};
  value.storage.memory_lines=1;
  value.bot={running:true,connected:true,user:'Custom persona'};
  value.plugins=[{name:'custom',enabled:true,available:true,running:true,description:'独自説明'}];
  h.context.render(value);
  assert.equal(h.get('presence').textContent,'Online');
  assert.equal(h.get('activity-mode').textContent,'Reply and react');
  assert.equal(h.get('memory-reader-title').textContent,'Self memory');
  assert.equal(h.get('memory-reader-body').textContent,'日本語の記憶はそのまま');
  assert.equal(h.get('plugins').children[0].children[1].textContent,'独自説明');
  assert.equal(vm.runInContext("ago(null)",h.context),'No records');
  assert.equal(vm.runInContext("fmt(12345)",h.context),'12,345');
  assert.match(vm.runInContext("ago(Date.now()-5000)",h.context),/s ago/);
  h.context.renderBranding({heading:'カスタム見出し',memory_guide:'記憶の書庫'});
  assert.equal(h.get('dashboard-heading').textContent,'カスタム見出し');
  assert.equal(h.get('memory-guide').textContent,'記憶の書庫');
});
const a={key:'guild:1',id:'1',kind:'guild',name:'A',enabled:true,activity:{mode:'react',effective_mode:'react'}};
const b={key:'dm:2',id:'2',kind:'dm',name:'B',enabled:false,activity:{mode:'reply',effective_mode:'disabled'}};
function harness(desktop=false,locale='ja'){
  const elements=new Map();
  function element(){return {children:[],hidden:true,textContent:'',value:'',style:{},dataset:{},classList:{toggle(){}},
    appendChild(e){this.children.push(e)},append(...items){this.children.push(...items)},replaceChildren(){this.children=[]},
    addEventListener(name,fn){this[name]=fn},setAttribute(name,value){this[name]=value},querySelector(){return element()},remove(){},set innerHTML(v){this.children=[]}}}
  const get=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id)};
  const pending=[];
  const menu=element(),groups=Array.from({length:4},()=>element());
  const media={matches:desktop,addEventListener(name,fn){this[name]=fn}};
  const context=vm.createContext({matchMedia:()=>media,document:{getElementById:get,createElement:element,createElementNS:element,querySelector:selector=>selector==='.section-menu'?menu:element(),querySelectorAll:()=>groups},
    location:{search:'',href:'http://localhost/'},history:{replaceState(){}},URL,URLSearchParams,Intl,Date,
    setInterval(){},fetch(url,options){return new Promise(resolve=>pending.push({url,options,resolve}))}});
  vm.runInContext(localizedSource(locale),context);
  return {context,get,pending,menu,groups,media};
}
test('navigation exposes desktop index and collapses independently on narrow screens',()=>{
  const h=harness(true);
  assert.ok(h.groups.every(g=>g.open));
  h.menu.click({target:{closest:()=>({})}});
  assert.ok(h.groups.every(g=>g.open));
  h.menu.toggle({target:h.groups[0]});
  assert.ok(h.groups.every(g=>g.open));
  h.media.matches=false;h.media.change();
  assert.ok(h.groups.every(g=>!g.open));
  h.groups[0].open=true;h.groups[1].open=true;
  h.menu.toggle({target:h.groups[1]});
  assert.equal(h.groups[0].open,false);assert.equal(h.groups[1].open,true);
  h.menu.toggle({target:h.groups[0]});
  assert.equal(h.groups[1].open,true);
  h.menu.click({target:{closest:()=>null}});
  assert.equal(h.groups[1].open,true);
  h.menu.click({target:{closest:()=>({})}});
  assert.ok(h.groups.every(g=>!g.open));
  h.media.matches=true;h.media.change();
  assert.ok(h.groups.every(g=>g.open));
});
async function respond(request,body,ok=true){request.resolve({ok,json:async()=>body});await new Promise(setImmediate)}
test('token history filters operations, handles zero and single timestamps, and clears',()=>{
  const h=harness();
  h.get('usage-history-operation').value='respond';
  vm.runInContext(`tokenHistory=[{ts:'2026-10-02T00:00:00Z',operation:'respond',model:'test',input_tokens:0},{ts:'2026-10-02T01:00:00Z',operation:'self_time',input_tokens:999},{ts:'bad',operation:'respond',input_tokens:8}];renderTokenHistory()`,h.context);
  assert.match(h.get('usage-history-summary').textContent,/1件 · 最大 0/);
  assert.equal(h.get('usage-history-chart').children.length,1);
  h.get('usage-history-operation').value='';
  vm.runInContext('renderTokenHistory()',h.context);
  assert.match(h.get('usage-history-summary').textContent,/2件 · 最大 999/);
  vm.runInContext('tokenHistory=[];renderTokenHistory()',h.context);
  assert.equal(h.get('usage-history-chart').children.length,0);
  assert.match(h.get('usage-history-summary').textContent,/履歴はありません/);
});
test('plugin details are text-only and clear when the scope has no logs',()=>{
  const h=harness();
  vm.runInContext(`renderPluginLogs([{plugin:'drawing',event:'request',event_id:'job',log_id:'abc',prompt:'<script>private</script>',ts:'2026-10-02T00:00:00Z'}])`,h.context);
  const row=h.get('plugin-logs').children[0];
  assert.equal(row.id,'plugin-log-abc');
  assert.match(row.children[1].textContent,/<script>private/);
  vm.runInContext('renderPluginLogs([])',h.context);
  assert.equal(h.get('plugin-logs').children.length,0);
});
function status(item){return {sandbox_key:item.key,selection:item,bot:{},state:{habitus:[]},queue:{},openai_usage:{},storage:{},dashboard:{panels:[]},memory_contents:{documents:[],nonempty_documents:0},memory_vector_store:{state:'empty',current_bytes:0,document_count:0}}}
test('person memories show identity and entry count, not a generic heading',()=>{
  const h=harness();
  vm.runInContext(`renderMemory({storage:{memory_lines:4},memory_contents:{nonempty_documents:2,documents:[{kind:'people',path:'memory/people/1.md',title:'にもちゃん／お姉ちゃん',person_id:'1',entries:2,lines:4,content:'## 名前'},{kind:'people',path:'memory/people/2.md',person_id:'2',lines:0,content:''}]}})`,h.context);
  const buttons=h.get('memory-documents').children;
  assert.equal(buttons[0].children[0].textContent,'にもちゃん／お姉ちゃん');
  assert.match(buttons[0].children[1].textContent,/2件 · ID: 1/);
  assert.equal(h.get('memory-reader-title').textContent,'にもちゃん／お姉ちゃん');
  buttons[1].click();
  assert.equal(h.get('memory-reader-title').textContent,'人物 2');
  assert.match(h.get('memory-reader-meta').textContent,/0件 · ID: 2/);
});
test('context occupancy renders sorted plugin shares and clears stale scope',()=>{
  const h=harness();
  vm.runInContext(`renderContextUsage({respond:{total:100,round:2,ts:'2026-09-30T00:00:00Z',components:[{plugin:'music',total:20,percent:20,tools:10,instructions:10,response:0},{plugin:'core',total:80,percent:80,tools:0,instructions:80,response:0}]}})`,h.context);
  const sections=h.get('context-usage').children;
  assert.match(sections[0].children[0].textContent,/100文字/);
  assert.match(sections[0].children[0].textContent,/旧集計・画像データ込み/);
  assert.match(sections[0].children[1].children[0].textContent,/コア・履歴 · 80%/);
  assert.equal(sections[0].children[2].children[1].value,20);
  assert.match(sections[1].children[0].textContent,/記録なし/);
  vm.runInContext(`renderContextUsage({self_time:{measurement_version:2,total:200,round:3,images:{count:2,embedded_count:1,remote_count:1,payload_bytes:3500000},components:[]}})`,h.context);
  const imageSection=h.get('context-usage').children[1];
  assert.match(imageSection.children[0].textContent,/画像データ除外/);
  assert.match(imageSection.children[1].textContent,/画像 2件（埋込 1 \/ URL 1）/);
  assert.match(imageSection.children[1].textContent,/3,500,000バイト/);
  vm.runInContext('renderContextUsage({})',h.context);
  assert.equal(h.get('context-usage').children[0].children.length,1);
});
test('scope switching rejects delayed responses and hides failures',async()=>{
  const h=harness();
  await respond(h.pending.shift(),{sandboxes:[a,b],default:a.key});
  const old=h.pending.splice(0);
  assert.match(old[0].url,/guild%3A1/);
  h.get('sandbox-select').value=b.key;h.get('sandbox-select').change();
  assert.equal(h.get('scoped-content').hidden,true);
  await respond(h.pending.shift(),{sandboxes:[a,b],default:a.key});
  const current=h.pending.splice(0);
  await respond(current[0],status(b));await respond(current[1],{sandbox_key:b.key,events:[]});
  assert.equal(h.get('scope-name').textContent,'B');
  assert.match(h.get('scope-detail').textContent,/導入停止中/);
  assert.equal(h.get('scoped-content').hidden,false);
  await respond(old[0],status(a));await respond(old[1],{sandbox_key:a.key,events:[]});
  assert.equal(h.get('scope-name').textContent,'B');
  const refresh=vm.runInContext('refresh()',h.context);
  await respond(h.pending.shift(),{},false);await refresh;
  assert.equal(h.get('scoped-content').hidden,true);
  assert.match(h.get('load-error').textContent,/取得できません/);
});
test('empty list never requests unscoped data',async()=>{
  const h=harness();await respond(h.pending.shift(),{sandboxes:[],default:null,branding:{browser_title:'Persona Console',heading:'Persona Observatory',eyebrow:'PERSONA',memory_guide:'Persona memory'}});
  assert.equal(h.pending.length,0);assert.equal(h.get('scoped-content').hidden,true);
  assert.equal(h.get('sandbox-select').disabled,true);
  assert.equal(h.context.document.title,'Persona Console');
  assert.equal(h.get('dashboard-heading').textContent,'Persona Observatory');
  assert.equal(h.get('memory-guide').textContent,'Persona memory');
});
test('mismatched payload fails closed',async()=>{
  const h=harness();await respond(h.pending.shift(),{sandboxes:[a,b],default:a.key});
  await respond(h.pending.shift(),status(b));await respond(h.pending.shift(),{sandbox_key:a.key,events:[]});
  assert.equal(h.get('scoped-content').hidden,true);
  assert.match(h.get('load-error').textContent,/取得できません/);
});
test('operational status renders queue retry failure and gateway ping',()=>{
  const h=harness();
  const value=status(a);
  value.bot={running:true,connected:true,user:'test-agent',gateway_ping_ms:42};
  value.voice={enabled:true,initialized:false,connected:false};
  value.operations={observed_queue_depth:3,current_event_id:'event-1',maintenance:{active:true,operation:'sleep'},last_retry:{operation:'openai.respond',attempt:2,delay_seconds:1,reason:'timeout'},last_openai:{state:'failed',operation:'openai.respond',error_type:'TimeoutError',reason:'timeout'}};
  value.openai_usage={total_tokens:100,requests:2,by_operation:{respond:{requests:2,total_tokens:100},image_generation:{requests:0,total_tokens:0,image_count:2}},by_model:{main:{requests:2,total_tokens:100}},by_day:{'2026-09-05':{requests:2,total_tokens:100},'2026-09-07':{requests:1,total_tokens:10},'2026-09-06':{requests:1,total_tokens:50}}};
  value.state.research={searched_at:new Date().toISOString(),summary:'検索要約',queries:['検索語'],sources:[{title:'出典',url:'https://example.test/source'}]};
  value.music={enabled:true,playing:true,title:'夜の歌',track_id:'song-1',track_count:12,volume:.55,ducking_volume:.16,dj:{enabled:true,theme:'夜',played_count:3}};
  value.reminders=[{id:'abcd1234',message:'薬を飲む',channel_name:'#test',channel_id:'20',due_at:'2026-09-13T21:00:00+09:00'}];
  value.plugins=[{name:'music',enabled:true,available:true,running:true,provides:['music'],description:'音楽を再生します'},{name:'voice',enabled:false,available:false,running:false,provides:[]}];
  value.dashboard={panels:[
    {id:'voice',plugin:'voice',title:'声の待機列',renderer:'voice',order:30},
    {id:'music',plugin:'music',title:'音楽の観測卓',renderer:'music',order:40,description:'再生状況です',eyebrow:'NOW PLAYING'},
    {id:'reminders',plugin:'reminders',title:'約束のメモ',renderer:'reminders',order:50},
    {id:'research',plugin:'web_search',title:'直近の検索メモ',renderer:'research',order:70}]};
  value.operations.music_activity=[{event:'music.started',title:'夜の歌',ts:new Date().toISOString()}];
  value.storage.memory_lines=3;
  value.memory_contents={nonempty_documents:3,documents:[
    {path:'memory/self.md',kind:'self',lines:2,content:'## 自分\n- 猫が好き'},
    {path:'memory/world.md',kind:'world',lines:1,content:'- 海は広い'},
    {path:'habitus.md',kind:'habitus',lines:1,content:'- よく笑う'}]};
  value.memory_vector_store={state:'stale',synced_at:new Date().toISOString(),current_bytes:2048,indexed_bytes:1024,document_count:3,vector_store_id:'vs_123456789012345',file_id:'file_123456789012345'};
  h.context.render(value);
  assert.equal(h.get('gateway-ping').textContent,'42 ms');
  assert.equal(h.get('actor-state').textContent,'処理中');
  assert.equal(h.get('maintenance-state').textContent,'実行中');
  assert.equal(h.get('api-state').textContent,'失敗');
  assert.match(h.get('retry-detail').textContent,/2回目/);
  assert.equal(h.get('usage-operation').children.length,2);
  assert.match(h.get('usage-operation').children[1].children[1].textContent,/2枚/);
  assert.deepEqual(h.get('usage-day').children.map(row=>row.children[0].textContent),['2026-09-07','2026-09-06','2026-09-05']);
  assert.equal(h.get('activity-mode').textContent,'リアクションもする');
  assert.equal(h.get('voice-state').textContent,'有効・未初期化');
  assert.equal(h.get('voice-detail').textContent,'最初の応答時に準備');
  assert.equal(h.get('research-summary').textContent,'検索要約');
  assert.equal(h.get('research-queries').textContent,'検索: 検索語');
  assert.equal(h.get('research-sources').children[0].children[0].href,'https://example.test/source');
  // Optional renderers are supplied by extensions, not required by the base runtime.
  assert.equal(h.get('plugin-panels-status').children.length,4);
  assert.equal(h.get('plugin-summary').textContent,'1 / 2 有効');
  assert.equal(h.get('plugins').children[0].children[0].children[1].textContent,'稼働中');
  assert.equal(h.get('plugins').children[0].children[1].textContent,'音楽を再生します');
  assert.match(source,/spec\.eyebrow/);
  assert.match(source,/spec\.description/);
  assert.match(h.get('memory-summary').textContent,/3冊/);
  assert.equal(h.get('memory-reader-title').textContent,'自分の記憶');
  assert.equal(h.get('memory-reader-body').textContent,'## 自分\n- 猫が好き');
  assert.equal(h.get('memory-vector-state').textContent,'更新待ち');
  assert.match(h.get('memory-vector-size').textContent,/2.0 KB/);
  assert.match(h.get('memory-vector-id').textContent,/vs_123456789/);
  h.get('memory-documents').children[1].click();
  assert.equal(h.get('memory-reader-title').textContent,'世界の記憶');
  h.context.render(value);
  assert.equal(h.get('memory-reader-body').textContent,'- 海は広い');
  h.get('memory-documents').children[2].click();
  assert.equal(h.get('memory-reader-title').textContent,'身についたこと');
  assert.equal(h.get('memory-reader-body').textContent,'- よく笑う');
  value.bot={running:false,connected:false,last_unclean_shutdown_at:new Date().toISOString()};
  h.context.render(value);
  assert.match(h.get('presence-detail').textContent,/前回の異常終了/);
});
test('plugin contributions determine which dashboard panels exist',()=>{
  const h=harness(),value=status(a);
  value.dashboard={panels:[{id:'music',plugin:'music',title:'音楽',renderer:'music',order:40}]};
  value.music={enabled:false};value.operations={music_activity:[]};
  h.context.render(value);
  assert.equal(h.get('plugin-panels-status').children.length,1);
  assert.equal(h.get('plugin-panels-status').children[0].dataset.plugin,'music');
  value.dashboard={panels:[]};
  h.context.render(value);
  assert.equal(h.get('plugin-panels-status').children.length,0);
  assert.equal(h.get('plugin-menu-status').children.length,0);
  value.dashboard={panels:[{id:'future',plugin:'future',title:'将来機能',renderer:'unknown',order:100}]};
  h.context.render(value);
  assert.equal(h.get('plugin-panels-status').children.length,1);
});
test('persona configuration loads on demand and saves with an admin token',async()=>{
  const h=harness();
  await respond(h.pending.shift(),{sandboxes:[],default:null});
  h.get('config-load').click();
  const loading=h.pending.shift();assert.equal(loading.url,'/api/config');
  await respond(loading,{writable:true,documents:[{id:'persona',label:'ペルソナ',format:'markdown',content:'old',max_length:16000}]});
  assert.equal(h.get('config-editor').hidden,false);
  assert.equal(h.get('config-content').value,'old');
  h.get('config-token').value='secret-token-1234';h.get('config-content').value='new';
  h.get('config-save').click();
  const saving=h.pending.shift();assert.equal(saving.url,'/api/config/persona');
  assert.equal(saving.options.method,'PUT');
  assert.equal(saving.options.headers['X-Dashboard-Token'],'secret-token-1234');
  await respond(saving,{id:'persona',saved:true,content:'new\n'});
  assert.equal(h.get('config-content').value,'new\n');
  assert.match(h.get('config-message').textContent,/保存しました/);
  h.get('config-reload').click();
  const reloading=h.pending.shift();assert.equal(reloading.url,'/api/config/reload');
  assert.equal(reloading.options.method,'POST');
  assert.equal(reloading.options.headers['X-Dashboard-Token'],'secret-token-1234');
  await respond(reloading,{reloaded:true,active_sandboxes:2});
  const branding=h.pending.shift();assert.equal(branding.url,'/api/sandboxes');
  await respond(branding,{branding:{heading:'Reloaded'}});
  assert.equal(h.get('dashboard-heading').textContent,'Reloaded');
  assert.match(h.get('config-message').textContent,/2領域/);
});
