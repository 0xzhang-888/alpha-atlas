import { chains, demoRecords, snapshotRecords } from './data.js';

const state={source:'live',chain:'all',kind:'project',sort:'signal',signalOnly:false};
const live={data:null,records:[],loading:false,error:'',timer:null};
const $=s=>document.querySelector(s);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const safeUrl=v=>{try{const u=new URL(v);return ['https:','http:'].includes(u.protocol)&&!u.username&&!u.password?u.href:null;}catch{return null;}};
const link=(url,label)=>safeUrl(url)?`<a class="source-link" href="${esc(safeUrl(url))}" target="_blank" rel="noopener noreferrer">${esc(label)} ↗</a>`:'';
const icons={people:'<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="9" cy="7" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3M16 4a3 3 0 0 1 0 6m1 4a5 5 0 0 1 4 5v2"/></svg>',check:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 12 4 4 8-8"/><circle cx="12" cy="12" r="10"/></svg>'};
const records=()=>state.source==='live'?live.records:state.source==='demo'?demoRecords:snapshotRecords;
const registry=()=>[...chains,...[...new Set(records().flatMap(r=>r.chains))].filter(id=>!chains.some(c=>c.id===id)).map(id=>({id,name:id,short:id,symbol:'◈',color:'#a2bbc1'}))];
const getChain=id=>registry().find(c=>c.id===id)||{id,name:id,short:id,symbol:'◈',color:'#a2bbc1'};
const chainIds=r=>r.chains.length?r.chains:['unknown'];
const chainMatch=r=>state.chain==='all'||chainIds(r).includes(state.chain);
const chainLabel=r=>({declared:'项目自述',planned:'计划上线',documented:'官网 / 文档线索',inferred:r.chainEvidence?.every(e=>e.relation==='follows_official')?'关注关系推定':'归属线索',unknown:'生态待识别'})[r.chainStatus]||'生态线索';
const format=n=>n==null?'待获取':new Intl.NumberFormat('en',{notation:'compact',maximumFractionDigits:1}).format(n);
const date=v=>v&&Number.isFinite(Date.parse(v))?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(v)):'暂无记录';
const chainIcon=c=>`<span class="chain-icon ${esc(c.id)}" style="--chain-color:${esc(c.color)}" aria-hidden="true">${esc(c.symbol)}</span>`;
const avatar=r=>`<span class="project-avatar ${r.category==='crypto_dev'?'dev-avatar':''}" style="--logo-bg:${esc(r.color[0])};--logo-color:${esc(r.color[1])}" aria-hidden="true">${esc(r.logo)}</span>`;
const sourceLabel=()=>({live:'实时监控',demo:'演示数据',snapshot:'历史样例快照'})[state.source];

function normalizeRecord(r){
  if(!r||typeof r.id!=='string'||typeof r.handle!=='string'||!['project','crypto_dev'].includes(r.category)||!Array.isArray(r.chains))throw new Error('invalid_record');
  return {...r,name:r.name||r.handle,summary:r.summary||'暂无研究摘要',chains:r.chains.filter(c=>typeof c==='string'&&c.length<80),
    followers:Number.isInteger(r.followers)&&r.followers>=0?r.followers:null,distinctKols:Number.isInteger(r.distinctKols)?r.distinctKols:null,
    officialCa:Array.isArray(r.officialCa)?r.officialCa:[],logo:(r.name||r.handle).slice(0,2),color:r.category==='crypto_dev'?['#263c3e','#a9d4cb']:['#303745','#c6d6ec'],demo:false};
}

async function refreshLive(){
  if(live.loading)return;
  live.loading=true;renderStatus();
  const controller=new AbortController();
  const timeout=setTimeout(()=>controller.abort(),15000);
  try{
    const response=await fetch('/api/alpha',{headers:{Accept:'application/json'},credentials:'same-origin',cache:'no-store',signal:controller.signal});
    if(response.status===401&&document.documentElement.dataset.passwordAuth==='true'){location.assign('/login');return;}
    if(!response.ok)throw new Error(response.status===401||response.status===403?'auth_required':'upstream_unavailable');
    const data=await response.json();
    if(data.schemaVersion!==1||data.source!=='railway-monitor'||!Array.isArray(data.records)||!Number.isFinite(Date.parse(data.generatedAt)))throw new Error('invalid_data');
    const next=data.records.map(normalizeRecord);
    live.data=data;live.records=next;live.error='';
  }catch(error){live.error=error.message==='auth_required'?'登录状态已失效，请重新打开网站登录。':'实时数据暂时无法更新，请稍后重试。';}
  finally{clearTimeout(timeout);live.loading=false;if(state.source==='live')render();else renderStatus();}
}

function renderStatus(){
  const isLive=state.source==='live';
  $('#refresh-button').hidden=!isLive;
  $('#refresh-button').disabled=live.loading;
  $('#refresh-button').setAttribute('aria-busy',String(live.loading));
  $('.demo-indicator').dataset.state=isLive?(live.error?'error':live.data?'live':'loading'):'demo';
  $('#data-badge').textContent=isLive?(live.error?'连接异常':live.data?'实时连接':'连接中'):sourceLabel();
  $('#data-notice').classList.toggle('error',isLive&&!!live.error);
  let message,tag;
  if(!isLive){message=state.source==='demo'?'当前为交互演示，项目、粉丝和关注信号均为示例，不代表实时监控。':'仅展示此前保留的两条研究样例，不代表当前状态；切回实时监控查看生产数据。';tag=state.source==='demo'?'DEMO DATA':'HISTORICAL SAMPLE';}
  else if(live.error){message=live.error+(live.data?` 当前保留 ${date(live.data.generatedAt)} 的上次成功快照。`:' 未展示演示数据。');tag=live.data?'STALE DATA':'OFFLINE';}
  else if(!live.data){message='正在连接监控数据库…';tag='CONNECTING';}
  else{
    const pending=live.data.totals?.unclassified||0;
    const excluded=(live.data.totals?.other||0)+(live.data.totals?.trader||0);
    message=`近 ${live.data.windowHours} 小时 · ${date(live.data.generatedAt)} 更新 · 每 30 秒刷新${pending?` · ${pending} 个账号待分类`:''}${excluded?` · ${excluded} 个其他/交易员账号未纳入此视图`:''}${live.data.truncated?' · 结果达到展示上限':''}`;
    tag=live.loading?'REFRESHING':'LIVE MONITOR';
  }
  const detail=isLive&&live.data?`<span class="connection-detail">最近关注事件：${date(live.data.lastEventAt)}（北京时间）· 研究使用已保存的快照，刷新不触发 AI 调用。</span>`:'';
  $('#data-notice').innerHTML=`<span class="notice-icon">i</span><span>${esc(message)}${detail}</span><span class="notice-tag">${tag}</span>`;
}

function renderChains(){
  const query=$('#chain-search').value.trim().toLowerCase(),all=records(),available=registry();
  $('#chain-total').textContent=available.filter(c=>c.id!=='unknown').length;
  $('#all-count').textContent=state.source==='live'&&!live.data?'—':all.length;
  $('.all-link').classList.toggle('active',state.chain==='all');
  $('.all-link').setAttribute('aria-current',state.chain==='all'?'page':'false');
  const filtered=available.filter(c=>`${c.name} ${c.short} ${c.id}`.toLowerCase().includes(query));
  $('#chain-list').innerHTML=filtered.map(c=>`<button class="chain-link ${state.chain===c.id?'active':''}" data-chain="${esc(c.id)}" aria-current="${state.chain===c.id?'page':'false'}">${chainIcon(c)}<span>${esc(c.id==='bsc'?'BSC':c.name)}</span><span class="chain-count">${all.filter(r=>chainIds(r).includes(c.id)).length}</span></button>`).join('')||'<p class="no-chain-results">暂无匹配链。新链会随有证据的数据自动加入列表。</p>';
}

function card(r){
  const statuses={unavailable:'暂无深研记录',insufficient:'证据不足 · 待补充',stale:'历史研究 · 需要复核',snapshot:'已有研究快照'};
  const label=r.demo?'示例研究 · 官网 / 文档':statuses[r.researchStatus]||'已有研究快照';
  const noResearch=['unavailable','insufficient','stale'].includes(r.researchStatus);
  const currentChain=state.chain!=='all'&&r.chains.includes(state.chain)?state.chain:chainIds(r)[0];
  return `<article class="alpha-card"><button class="card-open" data-detail="${esc(r.id)}" aria-label="查看 ${esc(r.name)} 的研究详情"></button><div class="card-top">${avatar(r)}<div class="card-identity"><h3>${esc(r.name)}</h3><div class="handle">@${esc(r.handle)}</div></div><span class="card-chevron" aria-hidden="true">↗</span></div><div class="card-tags"><span class="tag chain-tag" title="${esc(chainLabel(r))}">${esc(getChain(currentChain).short)}${r.chains.length>1?` +${r.chains.length-1}`:''}</span>${!r.demo&&r.chains.length?`<span class="tag narrative-tag">${esc(chainLabel(r))}</span>`:''}<span class="tag ${r.watchlisted?'watch-tag':'narrative-tag'}">${esc(r.narrative)}</span>${r.signal3m?'<span class="tag signal-tag">ϟ 3 分钟共振</span>':''}</div><p class="card-summary">${esc(r.summary)}</p><div class="card-evidence ${noResearch?'incomplete':''}">${icons.check}${label}</div><div class="card-stats"><span class="card-stat">${icons.people}<strong>${format(r.followers)}</strong>粉丝</span><span class="card-stat" title="${state.source==='live'?'过去 48 小时不同监控对象的关注数':'观察窗口中的不同监控对象数'}"><strong class="${r.distinctKols>=3?'hot':''}">${r.distinctKols??'—'}</strong>位监控对象关注</span></div><div class="card-foot"><span class="status-dot"></span><span>${r.demo?'演示观察记录':state.source==='live'?`最新关注 ${date(r.lastEventAt)}`:`研究 ${date(r.researchedAt)}`}</span><span>${r.demo?'示例数据':state.source==='live'?'48H 窗口':'非实时'}</span></div></article>`;
}

function render(){
  renderChains();renderStatus();
  const scoped=records().filter(chainMatch),unknown=state.source==='live'&&!live.data;
  const projects=scoped.filter(r=>r.category==='project').length,devs=scoped.filter(r=>r.category==='crypto_dev').length;
  $('#project-count').textContent=unknown?'—':projects;$('#developer-count').textContent=unknown?'—':devs;
  $('#ecosystem-count').textContent=unknown?'—':new Set(scoped.flatMap(r=>r.chains)).size;
  $('#signal-count').textContent=unknown||state.source==='snapshot'?'—':scoped.filter(r=>r.signal3m).length;
  $('#project-tab-count').textContent=projects;$('#developer-tab-count').textContent=devs;
  $('#breadcrumb-chain').textContent=state.chain==='all'?'全部生态':getChain(state.chain).name;
  $('#page-title').textContent=state.chain==='all'?'项目与开发者':`${getChain(state.chain).name} 生态`;
  document.querySelectorAll('[data-kind]').forEach(b=>{const selected=b.dataset.kind===state.kind;b.classList.toggle('active',selected);b.setAttribute('aria-selected',String(selected));b.tabIndex=selected?0:-1;});
  $('#results').setAttribute('aria-labelledby',state.kind==='project'?'projects-tab':'developers-tab');
  if(unknown){$('#results-summary').textContent='等待实时数据';$('#results').innerHTML=`<div class="empty-state"><span class="empty-symbol ${live.error?'':'loading-symbol'}">${live.error?'◈':'↻'}</span><h2>${live.error?'暂时无法连接监控':'正在载入真实项目'}</h2><p>${live.error?esc(live.error)+' 页面不会用演示项目填补数据。':'正在读取已保存的分类、关注事件和研究结果。'}</p>${live.error?'<button class="subtle-button" data-retry>重新连接</button>':''}</div>`;return;}
  const visible=scoped.filter(r=>r.category===state.kind&&(!state.signalOnly||r.signal3m));
  visible.sort((a,b)=>state.sort==='followers'?(b.followers??-1)-(a.followers??-1):state.sort==='name'?a.name.localeCompare(b.name):Number(b.signal3m)-Number(a.signal3m)||(b.distinctKols??-1)-(a.distinctKols??-1));
  $('#results-summary').textContent=`${visible.length} 个${state.kind==='project'?'项目':'开发者'} · ${sourceLabel()}${state.chain==='all'?' · 按主生态分组':''}`;
  $('#results-summary').title='全部生态下每个账号仅显示一次；多链账号在各所属链筛选中均可找到。';
  if(!visible.length){$('#results').innerHTML=`<div class="empty-state"><span class="empty-symbol">◈</span><h2>${state.signalOnly?'当前没有 3 分钟共振':'暂无符合条件的记录'}</h2><p>${state.source==='live'?'近 48 小时的已分类账号中暂无匹配结果。新关注需要等待监控完成分类；没有链归属证据的账号位于“待核实”。':state.source==='snapshot'?'历史样例中没有符合条件的记录，切换实时监控可查看最新数据库。':'当前没有符合筛选条件的示例。新链项目接入后会自动展示。'}</p><button class="subtle-button" id="reset-filter">查看全部${state.kind==='project'?'项目':'开发者'}</button></div>`;return;}
  const groups=state.chain==='all'?[...new Set(visible.map(r=>chainIds(r)[0]))]:[state.chain];
  const order=registry().map(c=>c.id);groups.sort((a,b)=>order.indexOf(a)-order.indexOf(b));
  $('#results').innerHTML=groups.map(id=>{const c=getChain(id),items=state.chain==='all'?visible.filter(r=>chainIds(r)[0]===id):visible;return `<section class="chain-section" aria-label="${esc(c.name)}"><div class="section-heading">${chainIcon(c)}<h2>${esc(c.name)}</h2><span class="section-name">${esc(c.short)}</span><span class="section-count">${String(items.length).padStart(2,'0')} ${state.kind==='project'?'PROJECTS':'BUILDERS'}</span></div><div class="card-grid">${items.map(card).join('')}</div></section>`;}).join('');
}

function openDetail(id){
  const r=records().find(x=>x.id===id);if(!r)return;
  const statusNames={read:'已读取',not_found:'未找到',unavailable:'暂无记录',skipped:'未展开',demo:'演示'};
  const researchRow=(name,obj={})=>`<div class="research-row"><span>${name}</span><div><p><span class="research-label ${obj.status==='read'?'':'missing'}">${statusNames[obj.status]||'待核查'}${obj.partial?' · 摘录':''}</span>${esc(obj.finding||'暂无记录')}</p>${link(obj.url,'查看原始来源')}</div></div>`;
  const evidence=Array.isArray(r.chainEvidence)?r.chainEvidence.map(e=>`<div>${esc(getChain(e.chain).name)} · ${esc(({bio:'简介声明',official_post:'官方推文',planned_launch:'上线计划',research_document:'官网 / 文档研究',follows_official:'关注关系推定',reported_token_chain:'官方 CA 线索',token_market:'交易对线索'})[e.relation]||'生态线索')}<br>${esc(e.finding)}${link(e.source,'查看原始来源')}${link(e.officialAccountSource,'链官方账号依据')}${e.observedAt?`<small> · 观察于 ${date(e.observedAt)}</small>`:''}</div>`).join('')||'目前没有可用的简介、官方上线声明或正向关注关系线索。未发币、没有 CA 不影响归类。':esc(r.chainEvidence||'暂未取得生态线索。');
  const ca=r.officialCa.length?r.officialCa.map(c=>`<div class="research-row"><span>${esc(c.chain||'链待核实')}</span><div><p>${esc(c.address)}<br>${esc(c.narrative||'暂无已核实叙事')}</p>${link(c.source,'官方 CA 来源')}</div></div>`).join(''):'<p>没有已核实的官方 CA。不依据同名代币或第三方猜测生成合约地址。</p>';
  const sourceLinks=(r.sources||[]).map((u,i)=>link(u,`研究来源 ${i+1}`)).join('');
  const related=[...(r.devs||[]),...(r.devProjects||[])];
  const relatedHtml=related.length?`<section class="detail-section"><h3>${r.category==='crypto_dev'?'关联项目':'关联开发者'}</h3><div class="related-accounts">${related.map(h=>link(`https://x.com/${encodeURIComponent(h)}`,`@${h}`)).join('')}</div></section>`:'';
  const preflightHtml=r.demo?'':`<p class="snapshot-time">AI 查链：${esc(({complete:'已完成',unknown:'已检查，证据不足',failed:'本轮未取得结果，深研继续',not_checked:'尚无有效预检记录，后续深研前执行'})[r.chainCheck?.status]||'尚无有效预检记录，后续深研前执行')}${r.chainCheck?.checkedAt?` · ${date(r.chainCheck.checkedAt)}`:''}</p>`;
  $('#detail-content').innerHTML=`<div class="detail-header"><span class="detail-kicker">${r.category==='project'?'PROJECT INTELLIGENCE':'BUILDER INTELLIGENCE'}</span><button class="detail-close" id="detail-close" aria-label="关闭研究详情" autofocus>×</button><div class="card-top">${avatar(r)}<div><h2 id="detail-title">${esc(r.name)}</h2><div class="handle">@${esc(r.handle)}</div></div></div><div class="detail-stats"><div class="detail-stat">粉丝数<strong>${format(r.followers)}</strong></div><div class="detail-stat">${state.source==='live'?'48 小时':'窗口内'}关注人数<strong>${r.distinctKols??'—'}</strong></div><div class="detail-stat">3 分钟共振<strong>${r.signal3m?'已触发':state.source==='snapshot'?'无数据':'未触发'}</strong></div></div></div><div class="detail-body"><div class="detail-note">${r.demo?'此账号、数据和研究内容均为虚构示例。':state.source==='live'?`真实关注事件 + 已保存研究快照。研究时间：${date(r.researchedAt)}。${r.researchStatus==='stale'?' 该研究已超过复核时效。':''}${r.watchlisted?' 按规则纳入项目观察，身份仍待证据确认。':''}`:'历史样例快照，不代表当前状态。'}</div>${r.followersObservedAt?`<div class="snapshot-time">粉丝快照：${date(r.followersObservedAt)} · ${esc(r.followersSource)}</div>`:''}<section class="detail-section"><h3>${r.category==='project'?'项目一览':'开发者方向'}</h3><p>${esc(r.summary)}</p></section><section class="detail-section"><h3>所属生态 · ${esc(chainLabel(r))}</h3><div class="tag-line">${chainIds(r).map(id=>`<span class="tag chain-tag">${esc(getChain(id).name)}</span>`).join('')}</div><p class="snapshot-time">生态归属可依据简介、项目官方上线计划或关注链官方，不要求 CA；自述、计划和关注线索均不等于已部署。</p>${preflightHtml}<div class="chain-proof">${r.demo?'示例链归属。真实数据需要官方声明或部署证据。':evidence}</div></section><section class="detail-section"><h3>为什么受到关注</h3><p>${esc(r.catalyst)}</p><div class="source-list">${(r.catalystSources||[]).map((u,i)=>link(u,`催化来源 ${i+1}`)).join('')}</div>${(r.catalystEvents||[]).map(e=>`<div class="research-row"><span>${esc(e.date)}</span><div><p>${esc(e.finding)}</p>${link(e.source,'事件来源')}</div></div>`).join('')}<div class="chain-proof">近期事件与关注的时间相关性，不等于确定因果；没有证据时不推断。</div></section><section class="detail-section"><h3>研究与证据</h3>${researchRow('项目网站',r.website)}${researchRow('白皮书 / 文档',r.whitepaper)}${r.thirdParty?.finding?`<div class="research-row"><span>第三方介绍</span><div><p>${esc(r.thirdParty.finding)}</p>${(r.thirdParty.sources||[]).map((u,i)=>link(u,`第三方来源 ${i+1}`)).join('')}</div></div>`:''}<div class="source-list">${sourceLinks}</div></section><section class="detail-section"><h3>官方合约与叙事</h3>${ca}</section>${relatedHtml}<section class="detail-section"><h3>信号说明</h3><p>“3 分钟共振”指最近 3 分钟内至少 2 位不同的监控对象关注同一账号。${state.source==='live'?`当前 3 分钟内：${r.distinctKols3m??0} 位。`:''}普通粉丝数与监控对象关注数分开统计。网页不会触发播报或额外 AI 研究。</p></section><div class="snapshot-time">${r.demo?'示例数据 · 不连接真实账号':`研究时间：${date(r.researchedAt)}（北京时间）${link(`https://x.com/${encodeURIComponent(r.handle)}`,'查看 X 账号')}`}</div></div>`;
  $('#detail-dialog').showModal();$('#detail-close').focus();
}

const mobileQuery=matchMedia('(max-width: 700px)');
function syncMenu(){$('#sidebar').inert=mobileQuery.matches&&!$('#sidebar').classList.contains('open');}
function closeMenu(){const focused=$('#sidebar').contains(document.activeElement);$('#sidebar').classList.remove('open');$('#sidebar-scrim').hidden=true;$('#menu-button').setAttribute('aria-expanded','false');syncMenu();if(focused&&mobileQuery.matches)$('#menu-button').focus();}
document.addEventListener('click',e=>{
  const c=e.target.closest('[data-chain]');if(c){state.chain=c.dataset.chain;render();closeMenu();return;}
  const k=e.target.closest('[data-kind]');if(k){state.kind=k.dataset.kind;render();return;}
  const d=e.target.closest('[data-detail]');if(d){openDetail(d.dataset.detail);return;}
  if(e.target.closest('#detail-close'))$('#detail-dialog').close();
  if(e.target.closest('[data-retry],#refresh-button'))refreshLive();
  if(e.target.closest('#reset-filter')){state.chain='all';state.signalOnly=false;$('#signal-only').checked=false;render();}
});
$('#chain-search').addEventListener('input',renderChains);
$('#source-select').addEventListener('change',e=>{state.source=e.target.value;render();if(state.source==='live')refreshLive();});
$('#sort-select').addEventListener('change',e=>{state.sort=e.target.value;render();});
$('#signal-only').addEventListener('change',e=>{state.signalOnly=e.target.checked;render();});
$('#menu-button').addEventListener('click',()=>{const open=!$('#sidebar').classList.contains('open');$('#sidebar').classList.toggle('open',open);$('#sidebar-scrim').hidden=!open;$('#menu-button').setAttribute('aria-expanded',String(open));syncMenu();if(open)$('#chain-search').focus();});
$('#sidebar-scrim').addEventListener('click',closeMenu);
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeMenu();});
$('.view-tabs').addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();state.kind=e.key==='Home'?'project':e.key==='End'?'crypto_dev':state.kind==='project'?'crypto_dev':'project';render();$(state.kind==='project'?'#projects-tab':'#developers-tab').focus();});
$('#detail-dialog').addEventListener('click',e=>{if(e.target===$('#detail-dialog')&&e.clientX<$('#detail-dialog').getBoundingClientRect().left)$('#detail-dialog').close();});
mobileQuery.addEventListener('change',()=>{if(!mobileQuery.matches)closeMenu();syncMenu();});
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&state.source==='live')refreshLive();});
render();syncMenu();refreshLive();
live.timer=setInterval(()=>{if(!document.hidden&&state.source==='live')refreshLive();},30000);
