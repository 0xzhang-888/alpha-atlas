import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

// Load the buildless browser module without requiring a package manager.
const source=await readFile(new URL('../web/data.js',import.meta.url),'utf8');
const {chains,demoRecords,snapshotRecords}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const chainIds=new Set(chains.map(c=>c.id));
assert.equal(chainIds.size,chains.length);
for(const id of ['ethereum','bsc','robinhood','arc','near','unknown'])assert.ok(chainIds.has(id));
assert.equal(new Set([...demoRecords,...snapshotRecords].map(r=>r.id)).size,demoRecords.length+snapshotRecords.length);
for(const r of [...demoRecords,...snapshotRecords]){
  assert.ok(['project','crypto_dev'].includes(r.category));
  assert.ok(Array.isArray(r.chains));
  assert.equal(new Set(r.chains).size,r.chains.length);
  assert.ok(r.followers===null||(Number.isInteger(r.followers)&&r.followers>=0));
  assert.ok(r.distinctKols===null||(Number.isInteger(r.distinctKols)&&r.distinctKols>=0));
  if(r.signal3m)assert.ok(r.distinctKols>=2);
  for(const id of r.chains)assert.ok(chainIds.has(id));
  for(const s of [r.website,r.whitepaper])if(s.url)assert.equal(new URL(s.url).protocol,'https:');
}
assert.ok(demoRecords.every(r=>r.demo===true));
assert.ok(snapshotRecords.every(r=>r.demo===false&&r.followers===null&&r.distinctKols===null));
assert.ok(demoRecords.some(r=>r.category==='crypto_dev'));
assert.ok(snapshotRecords.some(r=>r.chains.length>1));
assert.ok(snapshotRecords.some(r=>r.chains.length===0));
console.log('Passed: unique identities, chain registry, required ecosystems, missing values, signal thresholds, safe source URLs, demo/snapshot isolation, multichain and unknown-chain records.');
