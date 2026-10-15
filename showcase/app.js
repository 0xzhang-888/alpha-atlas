/* Public, read-only research showcase. This page never starts a research job. */
'use strict';

const $ = (selector) => document.querySelector(selector);
const state = { records: [], snapshot: null, category: 'all', chain: 'all', query: '', sort: 'heat', limit: 8, loading: false, error: '', syncedAt: null, openedId: null, openedRecord: null };
const number = new Intl.NumberFormat('zh-CN');
const compactNumber = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 });
const dateFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
const timeFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
const chainNames = { ethereum: 'Ethereum', eth: 'Ethereum', bsc: 'BNB Chain', robinhood: 'Robinhood Chain', arc: 'Arc', near: 'NEAR', solana: 'Solana', base: 'Base', arbitrum: 'Arbitrum', optimism: 'Optimism', bitcoin: 'Bitcoin', polygon: 'Polygon', 'avalanche-c': 'Avalanche', sui: 'Sui', aptos: 'Aptos', ton: 'TON', tron: 'TRON', starknet: 'Starknet', 'cosmos-hub': 'Cosmos Hub', osmosis: 'Osmosis', celo: 'Celo', megaeth: 'MegaETH', scroll: 'Scroll', linea: 'Linea', monad: 'Monad', zksync: 'ZKsync', berachain: 'Berachain', sonic: 'Sonic', hyperliquid: 'Hyperliquid', hyperevm: 'HyperEVM', mantle: 'Mantle', opbnb: 'opBNB', unknown: '生态待识别' };
const chainOrder = ['ethereum', 'bsc', 'robinhood', 'arc', 'near', 'solana', 'base', 'arbitrum', 'optimism', 'bitcoin'];
const researchNames = { snapshot: '研究快照', stale: '研究待复核', insufficient: '证据不足', unavailable: '暂无研究快照' };
const documentNames = { read: '已读取正文', not_found: '未找到', unavailable: '正文未核实', skipped: '本次未研读' };
const relationNames = { bio: '项目简介', profile: '项目简介', official_bio: '项目简介', official_post: '官方推文', official_tweet: '官方推文', announcement: '官方声明', planned_launch: '计划上线', follows_official: '关注链官方', document: '网站 / 文档', website: '项目网站', reported_token_chain: '官方 CA 研究记录', token_market: '交易对链证据' };
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
const text = (value, fallback = '') => typeof value === 'string' ? value : fallback;
const count = (value) => Number.isInteger(value) && value >= 0 ? value : null;
const dateValue = (value) => typeof value === 'string' && Number.isFinite(Date.parse(value)) ? Date.parse(value) : 0;
const date = (value) => dateValue(value) ? dateFormat.format(new Date(value)) : '暂无记录';
const chainName = (id) => chainNames[id] || id;
const idsFor = (record) => record.chains.length ? record.chains : ['unknown'];
const format = (value) => count(value) === null ? '—' : compactNumber.format(value);
function safeUrl(value) {
  if (typeof value !== 'string') return null;
  try {
    const url = new URL(value);
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || !url.hostname.includes('.') || /(^|\.)(localhost|local|internal)$/.test(url.hostname)) return null;
    return url.href;
  } catch { return null; }
}
const link = (url, label) => safeUrl(url) ? `<a class="source-link" href="${esc(safeUrl(url))}" target="_blank" rel="noopener noreferrer">${esc(label)} <span aria-hidden="true">↗</span></a>` : '';
const avatar = (record) => `<span class="avatar${record.category === 'crypto_dev' ? ' dev-avatar' : ''}" aria-hidden="true">${esc(Array.from(record.name || record.handle).slice(0, 2).join(''))}</span>`;
const chainStatus = (record) => ({ declared: '项目自述', planned: '计划上线', documented: '文档线索', inferred: record.chainEvidence.length && record.chainEvidence.every((item) => item.relation === 'follows_official') ? '关注关系线索' : '生态归属线索', unknown: '等待更多线索' }[record.chainStatus] || '生态归属线索');

function normalizeRecord(value) {
  if (!value || !/^[a-zA-Z0-9_]{1,15}$/.test(value.handle) || !['project', 'crypto_dev'].includes(value.category) || !Array.isArray(value.chains)) throw new Error('invalid_record');
  return {
    ...value,
    id: text(value.id, value.handle.toLowerCase()),
    handle: value.handle,
    name: text(value.name, value.handle) || value.handle,
    chains: [...new Set(value.chains.filter((chain) => typeof chain === 'string' && chain.length > 0 && chain.length < 80))],
    summary: text(value.summary, '当前公开快照暂无研究摘要。'),
    followers: count(value.followers), distinctKols: count(value.distinctKols), distinctKols3m: count(value.distinctKols3m),
    signal3m: value.signal3m === true,
    chainEvidence: Array.isArray(value.chainEvidence) ? value.chainEvidence.filter((item) => item && typeof item === 'object') : [],
    officialCa: Array.isArray(value.officialCa) ? value.officialCa.filter((item) => item && typeof item === 'object') : [],
    sources: Array.isArray(value.sources) ? value.sources : [],
    catalystSources: Array.isArray(value.catalystSources) ? value.catalystSources : [],
    catalystEvents: Array.isArray(value.catalystEvents) ? value.catalystEvents.filter((item) => item && typeof item === 'object') : [],
  };
}

function filteredRecords() {
  const records = state.records.filter((record) => (state.category === 'all' || record.category === state.category) && (state.chain === 'all' || idsFor(record).includes(state.chain)) && (!state.query || `${record.name} ${record.handle}`.toLocaleLowerCase().includes(state.query)));
  return records.sort((a, b) => {
    if (state.sort === 'recent') return dateValue(b.lastEventAt) - dateValue(a.lastEventAt) || (b.distinctKols || 0) - (a.distinctKols || 0);
    if (state.sort === 'followers') return (b.followers ?? -1) - (a.followers ?? -1) || (b.distinctKols || 0) - (a.distinctKols || 0);
    return Number(b.signal3m) - Number(a.signal3m) || (b.distinctKols || 0) - (a.distinctKols || 0) || dateValue(b.lastEventAt) - dateValue(a.lastEventAt);
  });
}

function renderOverview() {
  const hasSnapshot = !!state.snapshot;
  const projects = state.records.filter((record) => record.category === 'project').length;
  const developers = state.records.filter((record) => record.category === 'crypto_dev').length;
  const chains = new Set(state.records.flatMap((record) => record.chains));
  $('#metric-projects').textContent = hasSnapshot ? number.format(projects) : '—';
  $('#metric-developers').textContent = hasSnapshot ? number.format(developers) : '—';
  $('#metric-chains').textContent = hasSnapshot ? number.format(chains.size) : '—';
  $('#count-all').textContent = hasSnapshot ? number.format(state.records.length) : '—';
  $('#count-project').textContent = hasSnapshot ? number.format(projects) : '—';
  $('#count-dev').textContent = hasSnapshot ? number.format(developers) : '—';
  $('#window-label').textContent = `最近 ${count(state.snapshot?.windowHours) || 48} 小时`;
  const featured = [...state.records].sort((a, b) => dateValue(b.lastEventAt) - dateValue(a.lastEventAt))[0];
  $('#overview-label').textContent = state.error ? '等待下一次同步' : featured ? 'LATEST OBSERVED ACCOUNT' : hasSnapshot ? '公开观察窗口' : '正在连接公开数据';
  if (featured) {
    $('#overview-feature-name').textContent = `${featured.name}  /  @${featured.handle}`;
    $('#overview-feature-text').textContent = featured.summary;
  } else {
    $('#overview-feature-name').textContent = hasSnapshot ? '当前窗口暂无可展示的线索。' : '让每一条线索，都有迹可循。';
    $('#overview-feature-text').textContent = '看板读取后台保存的关注事件与研究快照，打开页面不会启动额外 AI 研究。';
  }
  const validChains = [...new Set(state.records.flatMap(idsFor))].sort((a, b) => {
    const ai = chainOrder.indexOf(a), bi = chainOrder.indexOf(b);
    return (ai === -1 ? 100 : ai) - (bi === -1 ? 100 : bi) || chainName(a).localeCompare(chainName(b));
  });
  if (state.chain !== 'all' && !validChains.includes(state.chain)) validChains.push(state.chain);
  $('#chain-select').innerHTML = '<option value="all">所有链</option>' + validChains.map((chain) => `<option value="${esc(chain)}">${esc(chainName(chain))}</option>`).join('');
  $('#chain-select').value = state.chain;
}

function renderStatus() {
  const hasSnapshot = !!state.snapshot;
  $('#live-state').textContent = state.error ? hasSnapshot ? '快照保留' : '连接待恢复' : state.loading ? '同步中' : hasSnapshot ? '已连接' : '连接中';
  for (const element of [$('#live-dot'), $('#overview-dot')]) {
    element.classList.toggle('connected', hasSnapshot && !state.error);
    element.classList.toggle('issue', !!state.error);
  }
  const synced = state.syncedAt ? timeFormat.format(state.syncedAt) : '尚未同步';
  $('#sync-time').textContent = synced;
  $('#hero-sync').textContent = state.syncedAt ? `${synced} CST` : '等待首次同步';
  $('#refresh-button').classList.toggle('refreshing', state.loading);
  $('#refresh-button').disabled = state.loading;
  const notice = $('#load-notice');
  notice.hidden = !state.error;
  notice.innerHTML = state.error ? `${esc(state.error)}${hasSnapshot ? ' 当前继续展示上次成功同步的快照。' : ''}<button type="button" data-retry>重新连接</button>` : '';
}

function card(record) {
  const chains = idsFor(record);
  const shownChains = chains.slice(0, 3).map((chain) => `<span class="chain-pill${chain === 'unknown' ? ' unknown' : ''}">${esc(chainName(chain))}</span>`).join('');
  const chainExtra = chains.length > 3 ? `<span class="chain-pill">+${chains.length - 3}</span>` : '';
  const signal = record.signal3m ? '<span class="signal-badge"><svg viewBox="0 0 12 12" aria-hidden="true"><path d="m7 0-6 7h4l-1 5 7-8H7z"/></svg>3 分钟共振</span>' : '';
  const status = ['snapshot', 'stale', 'insufficient', 'unavailable'].includes(record.researchStatus) ? record.researchStatus : 'unavailable';
  return `<article class="record-card${record.signal3m ? ' signal-card' : ''}"><button type="button" class="card-button" data-detail="${esc(record.id)}" aria-label="查看 ${esc(record.name)} 的研究详情"><div class="card-title-row">${avatar(record)}<div class="card-identity"><h3 class="card-name">${esc(record.name)}</h3><div class="card-handle">@${esc(record.handle)}</div></div><span class="category-label${record.category === 'crypto_dev' ? ' dev' : ''}">${record.category === 'crypto_dev' ? '开发者' : '项目'}</span></div><div class="card-tags">${shownChains}${chainExtra}${signal || `<span class="evidence-kind">${esc(chainStatus(record))}</span>`}</div><p class="card-summary">${esc(record.summary)}</p><div class="card-metrics"><div><strong>${format(record.distinctKols)}</strong><span>位关注</span></div><div><strong>${format(record.followers)}</strong><span>粉丝</span></div><span class="metric-arrow" aria-hidden="true">↗</span></div><div class="card-time"><span>信号 ${esc(date(record.lastEventAt))}</span><span class="research-state ${status}">${esc(researchNames[status])}</span></div></button></article>`;
}

function renderRecords() {
  const records = filteredRecords();
  $('#record-grid').setAttribute('aria-busy', String(state.loading && !state.snapshot));
  for (const button of document.querySelectorAll('[data-category]')) {
    const active = button.dataset.category === state.category;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  }
  $('#pagination').hidden = records.length <= state.limit;
  $('#pagination-count').textContent = `已展示 ${Math.min(state.limit, records.length)} / ${number.format(records.length)} 条线索`;
  if (!state.snapshot) {
    $('#result-count').textContent = state.error ? '公开数据暂时不可用' : '正在读取公开快照…';
    if (state.error) $('#record-grid').innerHTML = '<div class="empty-state"><span class="empty-icon" aria-hidden="true">⌁</span><h3>暂时无法连接数据服务</h3><p>公开数据接口未返回可用快照。恢复连接后，这里会自动显示真实研究记录。</p><button class="button secondary small-button" type="button" data-retry>重新连接 ↗</button></div>';
    return;
  }
  const category = state.category === 'project' ? '项目' : state.category === 'crypto_dev' ? '开发者' : '线索';
  $('#result-count').textContent = `${number.format(records.length)} 条${category}${state.chain !== 'all' ? ` · ${chainName(state.chain)}` : ''}${state.snapshot.truncated ? ' · 公开窗口已截取' : ''}`;
  if (!records.length) {
    const filtered = state.query || state.category !== 'all' || state.chain !== 'all';
    $('#record-grid').innerHTML = `<div class="empty-state"><span class="empty-icon" aria-hidden="true">⌁</span><h3>${filtered ? '当前筛选条件下没有匹配线索' : '当前窗口暂无可公开的研究记录'}</h3><p>${filtered ? '可以更换生态、类别，或缩短搜索关键词。' : '后台会继续监测与研究；有新的公开记录后，页面将自动同步。'}</p>${filtered ? '<button type="button" class="button secondary small-button" data-reset>清除筛选 ↗</button>' : ''}</div>`;
  } else $('#record-grid').innerHTML = records.slice(0, state.limit).map(card).join('');
}

function render() { renderOverview(); renderStatus(); renderRecords(); }

async function refresh() {
  if (state.loading) return;
  state.loading = true;
  renderStatus();
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch('/api/public/alpha', { headers: { Accept: 'application/json' }, credentials: 'omit', cache: 'no-store', signal: controller.signal });
    if (!response.ok) throw new Error('service_unavailable');
    const snapshot = await response.json();
    if (snapshot.source !== 'alpha-atlas-public' || snapshot.scope !== 'public' || snapshot.schemaVersion !== 1 || !Array.isArray(snapshot.records) || !dateValue(snapshot.generatedAt)) throw new Error('invalid_snapshot');
    const records = snapshot.records.map(normalizeRecord);
    if (new Set(records.map((record) => record.id)).size !== records.length) throw new Error('duplicate_record');
    state.records = records;
    state.snapshot = snapshot;
    state.syncedAt = new Date();
    state.error = '';
  } catch {
    state.error = '本次公开数据同步未完成，30 秒后自动重试。';
  } finally {
    clearTimeout(timeout);
    state.loading = false;
    render();
  }
}

function documentRow(label, value) {
  const document = value && typeof value === 'object' ? value : {};
  return `<div class="document-row"><div>${esc(label)}<br><span class="document-state">${esc(documentNames[document.status] || '暂无快照')}${document.partial ? ' · 部分正文' : ''}</span></div><div><p>${esc(text(document.finding, '当前公开快照暂无可展示的研读结果。'))}</p>${link(document.url, '查看原始页面')}</div></div>`;
}

function openDetail(id) {
  const record = state.records.find((item) => item.id === id);
  if (!record) return;
  state.openedId = id;
  state.openedRecord = record;
  const evidence = record.chainEvidence.map((item) => `<div class="evidence-row"><span class="evidence-label">${esc(chainName(text(item.chain)))} · ${esc(relationNames[item.relation] || '研究线索')}</span><p>${esc(text(item.finding, '请结合来源原文理解该条链归属线索。'))}</p>${link(item.source, '核对来源')}</div>`).join('');
  const events = record.catalystEvents.map((item) => `<div class="evidence-row"><span class="evidence-label">${esc(text(item.date, '事件记录'))}</span><p>${esc(text(item.finding))}</p>${link(item.source, '查看事件来源')}</div>`).join('');
  const ca = record.officialCa.length ? record.officialCa.map((item, index) => `<div class="ca-row"><span class="ca-chain">${esc(chainName(text(item.chain))) || '链待核实'}</span><div class="ca-address">${esc(text(item.address))}</div><p>${esc(text(item.narrative))}</p><button type="button" data-copy-ca="${index}">复制地址</button>${link(item.source, '项目方公布来源')}</div>`).join('') : '<p>当前公开研究快照未包含可展示的官方 CA 记录。项目可能尚未发币，生态归属仍可依据其他证据识别。</p>';
  const sources = [...new Set([...record.sources, ...record.catalystSources, ...(record.thirdParty?.sources || []), ...record.chainEvidence.map((item) => item.source), record.website?.url, record.whitepaper?.url, ...record.officialCa.map((item) => item.source)].filter(safeUrl))];
  const chainCheck = { complete: 'AI 链归属预检已完成', unknown: 'AI 已检查，链归属证据仍不足', failed: '本轮链预检未取得有效结果', not_checked: '当前无有效链预检快照' }[record.chainCheck?.status] || '当前无有效链预检快照';
  const observed = date(record.followersObservedAt);
  $('#detail-content').innerHTML = `<div class="detail-head"><span class="detail-eyebrow">${record.category === 'crypto_dev' ? 'BUILDER' : 'PROJECT'} INTELLIGENCE / PUBLIC SNAPSHOT</span><button type="button" class="dialog-close" data-close aria-label="关闭研究详情" autofocus>×</button><div class="detail-identity">${avatar(record)}<div><h2 id="detail-title">${esc(record.name)}</h2><div class="card-handle">@${esc(record.handle)}</div></div></div><div class="detail-stats"><div class="detail-stat"><span>窗口内独立关注</span><strong>${record.distinctKols === null ? '—' : number.format(record.distinctKols)} 位</strong></div><div class="detail-stat"><span>粉丝快照</span><strong>${format(record.followers)}</strong></div><div class="detail-stat"><span>最新关注信号</span><strong>${esc(date(record.lastEventAt))}</strong></div><div class="detail-stat"><span>AI 研究时间</span><strong>${esc(date(record.researchedAt))}</strong></div></div></div><div class="detail-body"><p class="detail-context">这是一份已保存的研究快照。页面同步时间：${esc(state.syncedAt ? dateFormat.format(state.syncedAt) : '暂无记录')}；${esc(researchNames[record.researchStatus] || '暂无研究快照')}。时间均为北京时间。</p><section class="detail-section"><h3>${record.category === 'crypto_dev' ? '开发者方向' : '项目一览'}</h3><p>${esc(record.summary)}</p>${observed !== '暂无记录' ? `<div class="detail-note">粉丝数据观测于 ${esc(observed)}${record.followersSource ? ` · ${esc(record.followersSource)}` : ''}</div>` : ''}</section><section class="detail-section"><h3>所属生态 <span>${esc(chainStatus(record))}</span></h3><div class="tag-row">${idsFor(record).map((chain) => `<span class="chain-pill${chain === 'unknown' ? ' unknown' : ''}">${esc(chainName(chain))}</span>`).join('')}</div><p class="detail-note">链归属依据项目简介、官方上线计划、网站文档或关注链官方等线索；计划与关注关系不代表已部署。</p><p class="detail-note">${esc(chainCheck)}${record.chainCheck?.checkedAt ? ` · ${esc(date(record.chainCheck.checkedAt))}` : ''}</p>${evidence || '<p class="detail-note">当前公开快照暂无可展示的链归属来源。</p>'}</section><section class="detail-section"><h3>为什么受到关注</h3><p>${esc(text(record.catalyst, '当前公开快照暂无有证据的关注原因记录。'))}</p>${events}<p class="detail-note">事件与关注的时间相关性，需要结合原始来源判断。</p></section><section class="detail-section"><h3>网站与文档研读</h3>${documentRow('项目网站', record.website)}${documentRow('白皮书 / 文档', record.whitepaper)}${record.thirdParty?.finding ? `<div class="document-row"><div>第三方介绍</div><div><p>${esc(record.thirdParty.finding)}</p>${(record.thirdParty.sources || []).map((url) => link(url, '介绍来源')).join('')}</div></div>` : ''}</section><section class="detail-section"><h3>官方合约与叙事</h3>${ca}</section><section class="detail-section"><h3>研究来源</h3><div class="source-list">${sources.length ? sources.map((url, index) => link(url, `来源 ${index + 1} · ${new URL(url).hostname.replace(/^www\./, '')}`)).join('') : '<p class="detail-note">当前公开快照暂未包含可展示的外部来源。</p>'}</div>${link(`https://x.com/${encodeURIComponent(record.handle)}`, '查看 X 账号')}</section><section class="detail-section"><h3>信号口径</h3><p>观察窗口为最近 ${count(state.snapshot?.windowHours) || 48} 小时；窗口内 ${record.distinctKols ?? '—'} 位不同监控对象关注该账号。最近 3 分钟内 ${record.distinctKols3m ?? '—'} 位，${record.signal3m ? '达到即时预警条件' : '未达到即时预警条件'}。公开页面不显示私人监控名单。</p></section></div>`;
  $('#detail-dialog').showModal();
  $('#detail-dialog').scrollTop = 0;
  $('[data-close]').focus();
}

let toastTimeout;
function toast(message) {
  clearTimeout(toastTimeout);
  $('#toast').textContent = message;
  $('#toast').hidden = false;
  toastTimeout = setTimeout(() => { $('#toast').hidden = true; }, 2500);
}

document.addEventListener('click', (event) => {
  const category = event.target.closest('[data-category]');
  if (category) { state.category = category.dataset.category; state.limit = 8; renderRecords(); return; }
  const detail = event.target.closest('[data-detail]');
  if (detail) { openDetail(detail.dataset.detail); return; }
  if (event.target.closest('[data-retry], #refresh-button')) { refresh(); return; }
  if (event.target.closest('[data-close]')) { $('#detail-dialog').close(); return; }
  if (event.target.closest('[data-reset]')) { state.category = 'all'; state.chain = 'all'; state.query = ''; state.limit = 8; $('#search-input').value = ''; render(); return; }
  const copy = event.target.closest('[data-copy-ca]');
  if (copy) {
    const record = state.openedRecord;
    const address = record?.officialCa[Number(copy.dataset.copyCa)]?.address;
    if (typeof address === 'string' && navigator.clipboard) navigator.clipboard.writeText(address).then(() => toast('合约地址已复制')).catch(() => toast('复制失败，请长按地址复制'));
    else toast('请长按地址复制');
  }
});
$('#search-input').addEventListener('input', (event) => { state.query = event.target.value.trim().replace(/^@/, '').toLocaleLowerCase(); state.limit = 8; renderRecords(); });
$('#chain-select').addEventListener('change', (event) => { state.chain = event.target.value; state.limit = 8; renderRecords(); });
$('#sort-select').addEventListener('change', (event) => { state.sort = event.target.value; state.limit = 8; renderRecords(); });
$('#show-more').addEventListener('click', () => { state.limit += 8; renderRecords(); });
$('#detail-dialog').addEventListener('click', (event) => { if (event.target === $('#detail-dialog')) { const rect = event.target.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) event.target.close(); } });
$('#detail-dialog').addEventListener('close', () => { state.openedId = null; state.openedRecord = null; });
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
render();
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 30000);
