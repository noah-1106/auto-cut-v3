/* domsim.js — Studio 前端 DOM 执行模拟模板（e2e T64 的执行体）。
   2026-10-15 缘起：t 遮蔽 i18n t() 一族运行时崩溃穿过了 node --check + audit + e2e 全部门禁——
   因为没有任何测试真正【执行】过 index.html 的 JS。本模板 = DOM 桩 + fetch MOCK + 断言尾；
   e2e.t64_dom_execution_sim() 从运行中的 studio 实时抓 15 个端点注入 __MOCK__，
   换入 __PROJ__/__STORY__（夹具项目），拼上 index.html 的 <script> 本体后交给 node 执行。
   覆盖关键路径：boot → 开项目 → 渲染幕/包/切片 → 搜索命中 → 7 个注册表抽屉。 */

const store={};
function mk(){return {style:{},dataset:{},children:[],
  classList:{_s:new Set(),add(c){this._s.add(c)},remove(c){this._s.delete(c)},toggle(c,f){f?this._s.add(c):this._s.delete(c)},contains(c){return this._s.has(c)}},
  appendChild(c){this.children.push(c)},remove(){},addEventListener(){},
  querySelector:(sel)=>mk(),querySelectorAll:()=>[]}}
function el(id){if(!store[id])store[id]=Object.assign(mk(),{id,innerHTML:'',value:'',textContent:'',files:[]});return store[id]}
['s-config','s-create','s-library','s-registry'].forEach(i=>el(i));
global.document={querySelectorAll:(sel)=>{
  if(sel==='main>section')return ['s-config','s-create','s-library','s-registry'].map(i=>store[i]);
  return [];},
  getElementById:el,createElement:mk,documentElement:{lang:''},addEventListener:()=>{},hidden:false,title:''};
global.window={addEventListener:()=>{}};
global.localStorage={getItem:()=>null,setItem:()=>{}};
global.location={reload(){}};
__MOCK__;
global.fetch=async(url)=>{const u=url.split('?')[0];return {json:async()=>(MOCK[u]!==undefined?MOCK[u]:{})}};
global.alert=m=>{throw new Error('ALERT: '+m)};
global.confirm=()=>true;global.prompt=()=>null;
global.Audio=class{play(){return Promise.resolve()}};
global.CSS={escape:s=>s};
global.setInterval=()=>0;global.clearInterval=()=>{};

(async()=>{
  await loadConfig();
  let openErr=null;
  try{await openProj('__PROJ__','__STORY__',true);}catch(e){openErr=e.message+' | '+(e.stack&&e.stack.split('\n')[1]||'')}
  console.log('OPEN:',openErr||'OK','| active=',['s-config','s-create','s-library','s-registry'].filter(i=>store[i].classList.contains('active')).join(','));
  if(!openErr)console.log('metabar=',el('metabar').innerHTML.length,'beatcards=',el('beatcards').innerHTML.length,'packgroups=',el('packgroups').innerHTML.length,'cuts=',el('cutgrid').innerHTML.length,'outline=',el('outline').value.length);
  // 搜索命中路径：q 命中转写文本 → packCard 走 hit 分支（t.text.slice 一族曾在此崩）
  const pf=(P.pack_files||[]).find(f=>f.transcript&&f.transcript.text);
  if(pf){
    const frag=pf.transcript.text.slice(0,10);
    el('tsearch').value=pf.transcript.text.slice(0,2);
    try{renderPack();const html=el('packgroups').innerHTML;
      console.log('SEARCH len=',html.length,'| textarea-shows-transcript=',html.indexOf(esc(frag))>=0);
    }catch(e){console.log('SEARCH ERR:',e.message);}
  }else console.log('SEARCH: mock 无转写包，跳过');
  for(const rn of ['bgm','subtitles','transitions','sfx','stickers','video_types','platforms']){
    REGCUR=rn;PROFILE_SEL=null;
    try{await renderRegMgr();
      const html=el('regmgr-body').innerHTML;
      // 断串签名（2026-10-17 pfField 实锤：value= 拼在标签闭合 '>' 之后 → 页面裸显 value="…" 文本。
      // 桩 DOM 不解析 HTML，浏览器才现形——用正则锚死这个模式）
      const broken=/>\s+value="/.test(html)||/<\/(input)>\s*value="/.test(html);
      console.log('DRAWER',rn,'len=',html.length,'| no-broken-tag=',!broken);}
    catch(e){console.log('DRAWER',rn,'ERR:',e.message);}
  }
})();



