// Static chain names and explicitly labelled demo/history records; never a live fallback.
export const chains = [
  ['ethereum','Ethereum','ETH','◆','#afbafd'],['bsc','BNB Smart Chain','BSC','◇','#f0cd54'],['robinhood','Robinhood Chain','Robinhood','R','#cbf879'],['arc','Arc','Arc','◜','#aeb3fb'],['near','NEAR Protocol','NEAR','N','#b9ede3'],['solana','Solana','SOL','≋','#b49af4'],['base','Base','Base','●','#658cff'],['arbitrum','Arbitrum One','ARB','A','#8cbfe6'],['optimism','OP Mainnet','OP','OP','#f07783'],['bitcoin','Bitcoin','BTC','₿','#edb467'],['polygon','Polygon PoS','POL','⬡','#b59aed'],['avalanche-c','Avalanche C-Chain','AVAX','△','#ec8894'],['sui','Sui','SUI','◡','#91c8f2'],['aptos','Aptos','APT','≡','#cee7e1'],['ton','TON','TON','▽','#82c7ed'],['tron','TRON','TRX','T','#f78988'],['starknet','Starknet','STRK','✦','#c3b5ed'],['cosmos-hub','Cosmos Hub','ATOM','⚛','#abaed8'],['osmosis','Osmosis','OSMO','O','#aeb4fc'],['celo','Celo','CELO','C','#e5ee98'],['megaeth','MegaETH','MegaETH','M','#e9d4c6'],['giwa','GIWA','GIWA','G','#aabcf7'],['monad','Monad','Monad','M','#b9a6f9'],['hyperevm','HyperEVM','HyperEVM','H','#92d8c0'],['unknown','生态待识别','未识别','?','#98a1a6']
].map(([id,name,short,symbol,color])=>({id,name,short,symbol,color}));

const project=(id,name,handle,network,narrative,summary,followers,kols,signal,logo,color)=>({id,name,handle,category:'project',chains:network,chainStatus:'demo',narrative,summary,followers,distinctKols:kols,signal3m:signal,logo,color,demo:true,researchStatus:'complete',website:{status:'demo',finding:'示例：已阅读项目网站，梳理产品定位与核心机制。'},whitepaper:{status:'demo',finding:'示例：已整理技术文档中的架构、假设和待验证项。'},officialCa:[],catalyst:'示例关注原因：新产品与开发进展带来关注，尚不代表产品采用、资金流入或投资背书。'});
const developer=(id,name,handle,network,summary,followers,kols,signal,logo,color)=>({...project(id,name,handle,network,'加密开发者',summary,followers,kols,signal,logo,color),category:'crypto_dev',catalyst:'示例：最近发布了一个开源原型，随后出现多位监控对象新增关注。时间上的相关性不等于确定因果，仍需核实本人公告和代码。'});

export const demoRecords = [
  project('demo-veil','Veil Protocol','veil_demo',['ethereum','base'],'链上隐私','基于零知识证明的隐私执行层，让已有钱包在不更换地址的情况下进行私密交互。',24800,5,true,'V',['#333745','#cad1ee']),
  project('demo-lattice','Lattice Finance','lattice_demo',['ethereum'],'DeFi · 收益','将链上收益策略拆解为可组合模块，聚焦透明的风险敞口与自动化仓位管理。',18600,4,false,'⌘',['#303923','#c2e58e']),
  project('demo-scope','Scope Network','scope_demo',['ethereum'],'基础设施','面向链上应用的可验证数据网络，把跨协议数据转化为可追溯的开发组件。',12300,3,false,'◉',['#263c3e','#8fceca']),
  project('demo-amber','Amber Market','amber_demo',['bsc'],'DeFi · 交易','围绕长尾资产构建链上交易路由，为小流动性池提供聚合报价与风险提示。',32100,6,true,'a',['#423521','#f2c979']),
  project('demo-sprout','Sprout Labs','sprout_demo',['bsc'],'AI · Agent','为链上智能体提供权限受控的钱包组件，降低自动执行策略的集成门槛。',8200,2,false,'✳',['#233d2f','#9bcab0']),
  project('demo-meridian','Meridian','meridian_demo',['robinhood'],'RWA · 基础设施','探索真实世界资产的链上结算工具，为资产发行与合规流转提供模块化接口。',9400,3,false,'M',['#354024','#cdf185']),
  project('demo-current','Current Pay','current_demo',['arc'],'稳定币 · 支付','面向开发者的稳定币支付组件，聚焦商户收款、结算追踪与可编程资金流。',6700,3,true,'↗',['#343047','#c5b5f3']),
  project('demo-moss','Moss Intents','moss_demo',['near'],'链抽象 · 意图','通过意图表达简化多步链上操作，探索跨应用的用户体验与执行验证。',15700,4,false,'m',['#253d38','#9ddaca']),
  project('demo-orbit','Orbit Engine','orbit_demo',['solana'],'开发工具','为链上游戏提供低延迟状态同步与交易编排组件，降低实时交互开发成本。',11200,2,false,'◎',['#3c2b46','#d0acf0']),
  project('demo-unmapped','Unmapped Research','unmapped_demo',[],'新项目观察','已进入观察列表，当前没有足够证据确认部署链，等待官网或官方合约进一步核实。',null,2,false,'?',['#33383b','#b3c0c4']),
  developer('demo-elias','elias.eth','elias_demo',['ethereum','base'],'正在构建可验证的隐私账户原型，近期发布了钱包集成演示与开源说明。',14200,4,true,'el',['#313d42','#b5d0d8']),
  developer('demo-nova','nova.build','nova_demo',['bsc'],'聚焦 Agent 钱包权限设计，持续发布交易模拟和安全授权方面的开发进展。',5600,2,false,'nv',['#3b3825','#e8d991']),
  developer('demo-kai','kai / onchain','kai_demo',['robinhood'],'探索 RWA 的链上结算路径，最近开放了一个资产流转工具的原型。',4300,3,false,'ka',['#304022','#c4e999']),
  developer('demo-iris','iris.dev','iris_demo',['arc'],'构建稳定币结算 SDK，最近发布了面向开发者的可编程支付示例。',7800,3,true,'ir',['#3d3046','#d5b5e9']),
  developer('demo-anya','anya.near','anya_demo',['near'],'研究意图执行与跨链用户体验，持续迭代开源求解器工具。',6100,2,false,'an',['#253d38','#aadace']),
  developer('demo-ren','ren.sol','ren_demo',['solana'],'聚焦高频链上交互与性能优化，最近公开了交易批处理工具的实现思路。',19500,4,false,'re',['#39304a','#c9b8ee'])
];

export const snapshotRecords = [
  {id:'nullmask',name:'NullMask',handle:'NullMaskio',category:'project',chains:['ethereum','arbitrum','megaeth','base','bsc'],chainStatus:'snapshot',narrative:'隐私 · ZK',summary:'已有研究快照：通过自定义 RPC 与零知识证明提供隐私交互，涵盖屏蔽存入、转账、提取和兑换。不是当前实时状态。',followers:null,distinctKols:null,signal3m:false,logo:'N',color:['#303447','#c8cbee'],demo:false,researchStatus:'snapshot',researchedAt:'2026-09-27',website:{status:'read',url:'https://docs.nullmask.io/',finding:'此前研究读取了官方文档，产品定位为链上隐私协议，通过自定义 RPC 使用现有钱包。'},whitepaper:{status:'read',url:'https://docs.nullmask.io/introduction/introduction',finding:'文档研究记录包含 Noir / Barretenberg 证明、Solidity 合约与隐私交互流程。当前页面没有重新验证官网状态。'},officialCa:[],catalyst:'没有可靠的新增关注原因快照。不能仅凭项目叙事认定受关注原因。',chainEvidence:'链归属来自此前官方文档研究记录，包含 Ethereum、Arbitrum、MegaETH、Base、BSC；不是本页面对部署合约的实时核验。'},
  {id:'arguspay',name:'ArgusPay',handle:'ArgusPay',category:'project',chains:[],chainStatus:'unknown',narrative:'待深入核实',summary:'此前账号简介提到将 Argus 代币费用路由至 X Money。仅为项目方自述，产品、链归属和官方合作关系均待独立核实。',followers:null,distinctKols:null,signal3m:false,logo:'A',color:['#38372d','#d5cfb0'],demo:false,researchStatus:'insufficient',researchedAt:'2026-09-27',website:{status:'unavailable',finding:'当前可用快照未包含已核实的项目网站。'},whitepaper:{status:'unavailable',finding:'当前可用快照未包含白皮书或技术文档。'},officialCa:[],catalyst:'没有足够证据判断关注原因。',chainEvidence:'没有可验证的链归属证据，暂列“待核实”。'}
];
