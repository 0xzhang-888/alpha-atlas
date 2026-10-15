"""Early-stage ecosystem evidence. Pure functions: no network, AI, or database writes."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

# Names are not inferred from token tickers, contract address formats or account names.
CHAIN_NAMES = {
    'ethereum': ('Ethereum', '以太坊'), 'bsc': ('BSC', 'BNB Chain', 'BNB Smart Chain'),
    'robinhood': ('Robinhood Chain', 'Robinhood链', 'Robinhood 链'),
    'arc': ('Arc',), 'near': ('NEAR', 'NEAR Protocol'), 'solana': ('Solana',),
    'base': ('Base',), 'arbitrum': ('Arbitrum',), 'optimism': ('Optimism', 'OP Mainnet'),
    'bitcoin': ('Bitcoin', '比特币'), 'polygon': ('Polygon',),
    'avalanche-c': ('Avalanche', 'Avalanche C-Chain'), 'sui': ('Sui',), 'aptos': ('Aptos',),
    'ton': ('TON', 'The Open Network'), 'tron': ('TRON',), 'starknet': ('Starknet',),
    'cosmos-hub': ('Cosmos Hub',), 'osmosis': ('Osmosis',), 'celo': ('Celo',),
    'megaeth': ('MegaETH',), 'giwa': ('GIWA',), 'monad': ('Monad',),
    'scroll': ('Scroll',), 'linea': ('Linea',), 'zksync': ('zkSync', 'ZKsync Era'),
    'berachain': ('Berachain',), 'sonic': ('Sonic',), 'hyperliquid': ('Hyperliquid',),
    'hyperevm': ('HyperEVM',), 'mantle': ('Mantle',), 'opbnb': ('opBNB',),
    'abstract': ('Abstract',), 'ink': ('Ink',), 'sei': ('Sei',), 'plasma': ('Plasma',),
}
ALIASES = {name.lower(): chain for chain, names in CHAIN_NAMES.items() for name in names}
ALIASES.update({'eth':'ethereum','bnb':'bsc','sol':'solana','arb':'arbitrum','op':'optimism',
                'btc':'bitcoin','avax':'avalanche-c','avalanche':'avalanche-c','robinhood':'robinhood'})

# Populated only with official accounts linked from the chain's own website.
OFFICIAL_ACCOUNTS = {
    'ethereum': {'chain':'ethereum','source':'https://ethereum.org/community/online'},
    'ethereumfndn': {'chain':'ethereum','source':'https://ethereum.org/community/online'},
    'ethdotorg': {'chain':'ethereum','source':'https://ethereum.org/community/online'},
    'bnbchain': {'chain':'bsc','source':'https://www.bnbchain.org/en'},
    'robinhoodcrypto': {'chain':'robinhood','source':'https://docs.robinhood.com/chain/terms-of-service/'},
    'arc': {'chain':'arc','source':'https://www.arc.io/'},
    'base': {'chain':'base','source':'https://www.base.org/'},
    'buildonbase': {'chain':'base','source':'https://www.base.org/'},
    'arbitrum': {'chain':'arbitrum','source':'https://arbitrum.foundation/'},
    'giwa_by_upbit': {'chain':'giwa','source':'https://giwa.io/home'},
    'nearfoundation': {'chain':'near','source':'https://near.foundation/'},
    'nearprotocol': {'chain':'near','source':'https://www.near.org/'},
    'solana': {'chain':'solana','source':'https://solana.com/community'},
    'optimism': {'chain':'optimism','source':'https://github.com/ethereum-optimism/docs/blob/main/components/Footer.tsx'},
}

def normalize_chain(value):
    if not isinstance(value, str):
        return None
    name = value.strip().lower()
    if name in {'', 'unknown', 'evm', 'multi-chain', 'multichain', '待核实', '未确认', '链待核实'}:
        return None
    return ALIASES.get(name) or (name if re.fullmatch(r'[a-z][a-z0-9-]{1,35}', name) else None)


def own_x_source(source, handle, *, post=False):
    try:
        p = urlsplit(source)
        parts = p.path.strip('/').split('/')
        return (p.scheme == 'https' and p.hostname in {'x.com','twitter.com','www.x.com','www.twitter.com'}
                and not p.username and not p.password and parts[0].lower() == handle.lower()
                and (len(parts) == 3 and parts[1] == 'status' and parts[2].isdigit() if post else len(parts) == 1))
    except (ValueError, TypeError, IndexError):
        return False


def _sentences(text):
    return [s.strip() for s in re.split(r'[\n。！？;；]|(?<=[.!?])\s+', str(text)[:16000]) if s.strip()]


def mentioned_chains(sentence):
    found = set()
    for chain, names in CHAIN_NAMES.items():
        for name in names:
            # Plain near/base/arc/ink/scroll etc. need relationship language, not substring matches.
            flags = 0 if name in {'NEAR', 'TON'} else re.I
            if re.search(r'(?<![a-zA-Z0-9_@])' + re.escape(name) + r'(?![a-zA-Z0-9_])', sentence, flags):
                found.add(chain)
    for match in re.finditer(r'@([A-Za-z0-9_]{1,15})\b', sentence):
        account = OFFICIAL_ACCOUNTS.get(match[1].lower())
        if account:
            found.add(account['chain'])
    return found


def _positive(sentence, chain, kind='bio'):
    # Do not turn a denial, comparison, market commentary or quoted third-party plan into affiliation.
    if re.search(r'\b(?:not|never|unlike|versus|vs|instead of|no longer)\b|不(?:是|在|会|支持|部署|属于)|未(?:部署|决定|确定)|尚未确定|无关|相比|而非|并非|不再', sentence, re.I):
        return False
    if re.search(r'\b(?:wallet supports?|bridg(?:e|ing) to|our investors?|our partners?|they|their|RT @)\b|钱包支持|投资方|投资者|合作方|转推', sentence, re.I):
        return False
    aliases = list(CHAIN_NAMES.get(chain, (chain,))) + ['@'+h for h, entry in OFFICIAL_ACCOUNTS.items() if entry['chain'] == chain]
    names = '(?:' + '|'.join(re.escape(n) for n in sorted(aliases, key=len, reverse=True)) + ')'
    # Keep relation and chain in the same short phrase, including explicit multichain lists.
    prefix = r'(?:\b(?:on|onto|powered by|native to|ecosystem of|launching on|coming to)\s+(?:the\s+)?|(?:基于|构建于|搭建于|部署[在于]|将[在于]|在)\s*)'
    all_names = {n for group in CHAIN_NAMES.values() for n in group} | {'@'+h for h in OFFICIAL_ACCOUNTS} | set(aliases)
    any_name = '(?:' + '|'.join(re.escape(n) for n in sorted(all_names, key=len, reverse=True)) + r')(?![a-zA-Z0-9_])'
    chain_list = any_name + r'(?:\s*(?:and|&|,|/|、|和|及)\s*' + any_name + ')*'
    for match in re.finditer(prefix + '(' + chain_list + ')', sentence, re.I):
        if re.search(r'(?<![a-zA-Z0-9_])' + names + r'(?![a-zA-Z0-9_])', match[1], re.I):
            return True
    if re.search(names + r'\s*(?:链上|链的|链生态|生态|原生|上线|\b(?:chain|network|ecosystem|native)\b)', sentence, re.I):
        return True
    # A self-authored bio such as "Solana DeFi protocol" is also a useful early-stage claim.
    return kind == 'bio' and chain not in {'base','arc','ink','scroll','sonic','abstract','plasma','near'} and bool(
        re.search(names + r'\s+(?:DeFi\s+)?(?:protocol|dapp|dex|launchpad|builder|developer)\b', sentence, re.I))


def quote_names_chain(quote, chain):
    if chain in mentioned_chains(quote):
        return True
    # New chains remain extensible, but need an actual chain/network entity in the quote.
    return chain not in CHAIN_NAMES and bool(re.search(r'(?<![a-z0-9_])' + re.escape(chain).replace(r'\-', r'[- ]') + r'\s*(?:chain|network|L1|L2|链)\b', quote, re.I))


def valid_chain_claim(quote, chain, kind):
    return quote_names_chain(quote, chain) and any(_positive(s, chain, kind) for s in _sentences(quote))


def text_evidence(value, source, kind, observed_at=None):
    """Extract only positive self-claims, with a source. No CA is required."""
    if not source or not isinstance(value, str):
        return []
    out = []
    for sentence in _sentences(value):
        for chain in sorted(mentioned_chains(sentence)):
            if not _positive(sentence, chain, kind):
                continue
            planned = bool(re.search(r'\b(?:will|soon|plan(?:s|ned|ning)?|launching|coming|testnet)\b|计划|即将|将于|将上线|拟在|测试网', sentence, re.I))
            relation = 'planned_launch' if planned else ('research_document' if kind == 'document' else 'bio' if kind == 'bio' else 'official_post')
            out.append({'chain':chain,'relation':relation,'source':source,'finding':sentence[:450],
                        'observedAt':observed_at,'confidence':'planned' if planned else 'documented' if kind == 'document' else 'declared'})
    return merge_evidence(out)


def structured_evidence(rows):
    """Read evidence already validated at research time; tolerate legacy reports."""
    out = []
    for row in rows[:12] if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not (chain := normalize_chain(row.get('chain'))):
            continue
        kind = row.get('kind')
        if kind not in {'bio','official_post','official_docs'} or not row.get('source') or not row.get('quote'):
            continue
        if not valid_chain_claim(str(row['quote']), chain, kind):
            continue
        relation = 'planned_launch' if row.get('relation') == 'planned_launch' else 'research_document' if kind == 'official_docs' else kind
        out.append({'chain':chain,'relation':relation,'source':row['source'],'finding':str(row['quote'])[:450],
                    'observedAt':row.get('observed_at'),'confidence':'planned' if relation == 'planned_launch' else 'documented' if kind == 'official_docs' else 'declared'})
    return merge_evidence(out)


def follow_evidence(handle, edges):
    out = []
    for edge in edges:
        if str(edge.get('follower','')).lower() != handle.lower():
            continue
        followed = str(edge.get('followed','')).lower()
        official = OFFICIAL_ACCOUNTS.get(followed)
        if official:
            out.append({'chain':official['chain'],'relation':'follows_official','source':None,
                        'finding':f'监控捕获 @{handle} 关注 @{followed}；仅为关注关系推定，不代表已宣布部署。',
                        'observedAt':edge.get('ts'),'confidence':'inferred',
                        'officialAccount':followed,'officialAccountSource':official['source']})
    return merge_evidence(out)


def merge_evidence(*groups):
    order = {'declared':0,'planned':1,'documented':2,'inferred':3}
    out, seen = [], set()
    for row in sorted((r for g in groups for r in g), key=lambda r: order.get(r.get('confidence'), 4)):
        key = (row.get('chain'), row.get('relation'), row.get('source'))
        if key not in seen:
            out.append(row)
            seen.add(key)
    return out[:30]


def evidence_status(rows):
    for status in ('declared','planned','documented'):
        if any(r.get('confidence') == status for r in rows):
            return status
    return 'inferred' if rows else 'unknown'
