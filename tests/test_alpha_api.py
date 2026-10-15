import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from src.alpha_api import create_api, read_snapshot, public_url
from src.storage import Storage


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "test.db")
        self.storage = Storage(self.path)
        self.now = 2_000_000_000
        with sqlite3.connect(self.path) as c:
            c.execute("CREATE TABLE handle_meta(handle TEXT PRIMARY KEY,payload TEXT,ts INTEGER)")

    def tearDown(self):
        self.temp.cleanup()

    def add_meta(self, handle, data):
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT INTO handle_meta VALUES (?,?,?)", (handle.lower(), json.dumps(data), self.now))

    def test_real_counts_missing_values_and_read_only(self):
        self.storage.add_event(" Private KOL ", "Example", 1, self.now-100, followers=0)
        self.storage.add_event("private kol", "example", 2, self.now-90)
        self.storage.add_event("Other Private", "example", 3, self.now-50)
        self.storage.add_event("Private KOL", "Ignored", 4, self.now-20)
        self.add_meta("Example", {"category": "project", "summary": "Research", "user_id": "secret-id"})
        before = Path(self.path).read_bytes()
        data = read_snapshot(self.path, self.now)
        self.assertEqual(Path(self.path).read_bytes(), before)
        self.assertEqual(data["totals"]["unclassified"], 1)
        r = data["records"][0]
        self.assertEqual(r["distinctKols"], 2)
        self.assertEqual(r["distinctKols3m"], 2)
        self.assertTrue(r["signal3m"])
        self.assertEqual(r["followers"], 0)
        self.assertEqual(r["chains"], [])
        self.assertEqual(r["researchStatus"], "unavailable")
        encoded = json.dumps(data)
        for private in ("Private KOL", "Other Private", "secret-id", "message_id", "announced_kols"):
            self.assertNotIn(private, encoded)

    def test_stale_followers_and_profile_does_not_override_identity(self):
        self.storage.add_event("K1", "Builder", 1, self.now-18000, followers=34)
        self.add_meta("Builder", {"category": "crypto_dev", "summary": "Builder"})
        with sqlite3.connect(self.path) as c:
            c.execute("UPDATE profile_followers SET ts=?", (self.now-90000,))
            c.execute("INSERT INTO research_cache VALUES (?,?,?)", ("deepseek:v3:builder:profile", json.dumps({"category":"other","followers":99,"followers_source":"https://x.com/Builder","_researched_at":self.now}), self.now))
        r = read_snapshot(self.path, self.now)["records"][0]
        self.assertEqual(r["category"], "crypto_dev")
        self.assertEqual(r["followers"], 99)
        self.assertFalse(r["signal3m"])

    def test_chain_is_evidence_not_verified_deployment(self):
        self.storage.add_event("K", "Example", 1, self.now)
        self.add_meta("Example", {"category": "project"})
        report = {"category":"project","_verified":True,"_mode":"project","_researched_at":self.now,
                  "official_ca":[{"address":"0x123", "chain":"Base", "source":"https://x.com/Example/status/1"}]}
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT INTO research_cache VALUES (?,?,?)", ("deepseek:v3:example:project", json.dumps(report),self.now))
        r = read_snapshot(self.path,self.now)["records"][0]
        self.assertEqual(r["chains"],["base"])
        self.assertEqual(r["chainStatus"],"inferred")
        self.assertEqual(r["chainEvidence"][0]["relation"],"reported_token_chain")

    def test_promoted_is_not_verified_project_and_broken_json_is_safe(self):
        self.storage.add_event("K", "Unknown", 1, self.now)
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT INTO account_watch VALUES ('unknown',1,'[]')")
            c.execute("INSERT INTO handle_meta VALUES ('unknown','not-json',?)", (self.now,))
        r=read_snapshot(self.path,self.now)["records"][0]
        self.assertTrue(r["watchlisted"])
        self.assertEqual(r["originalCategory"],"unclassified")
        self.assertEqual(r["category"],"project")

    def test_bio_website_plan_and_directional_follow_without_ca(self):
        self.storage.add_event('K1', 'Early', 1, self.now, bio='The lending protocol built on @base')
        self.storage.add_event('K2', 'Launch', 2, self.now)
        self.storage.add_event('K3', 'Follower', 3, self.now)
        self.storage.add_event('Follower Display', 'arc', 4, self.now, follower_handle='Follower')
        self.storage.add_event('Chain official', 'Early', 5, self.now, follower_handle='ethereum')
        for handle in ('Early','Launch','Follower'):
            self.add_meta(handle, {'category':'project'})
        report = {'category':'project','_verified':True,'_mode':'project','_researched_at':self.now,
                  'website':{'status':'read','url':'https://demo.example','finding':'该项目将在 GIWA 链上推出测试网。'}}
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT INTO research_cache VALUES (?,?,?)', ('deepseek:v3:launch:project',json.dumps(report),self.now))
        before = Path(self.path).read_bytes()
        records = {r['id']:r for r in read_snapshot(self.path,self.now)['records']}
        self.assertEqual(Path(self.path).read_bytes(),before)
        self.assertEqual(records['early']['chains'],['base'])
        self.assertEqual(records['early']['chainStatus'],'declared')
        self.assertEqual(records['launch']['chains'],['giwa'])
        self.assertEqual(records['launch']['chainStatus'],'planned')
        self.assertEqual(records['follower']['chains'],['arc'])
        self.assertEqual(records['follower']['chainStatus'],'inferred')
        self.assertTrue(all(r['officialCa'] == [] for r in records.values()))

    def test_old_or_ambiguous_bios_not_guessed_and_no_display_name_follows(self):
        self.storage.add_event('Early', 'base', 1, self.now)
        self.storage.add_event('K', 'Early', 2, self.now, bio='user base and near launch')
        self.add_meta('Early', {'category':'project'})
        self.assertEqual(read_snapshot(self.path,self.now)['records'][0]['chains'],[])
        with sqlite3.connect(self.path) as c:
            c.execute('UPDATE source_profiles SET bio=?,ts=?', ('Built on Ethereum',self.now-8*86400))
        self.assertEqual(read_snapshot(self.path,self.now)['records'][0]['chains'],[])

    def test_missing_database_is_not_created(self):
        missing=str(Path(self.temp.name)/"missing.db")
        with self.assertRaises(sqlite3.OperationalError):
            read_snapshot(missing,self.now)
        self.assertFalse(Path(missing).exists())

    def test_ai_chain_preflight_without_ca_and_private_fields(self):
        self.storage.add_event('K', 'Early', 1, self.now, bio='Our home is Base.')
        self.add_meta('Early', {'category':'project'})
        check={'version':2,'handle':'early','status':'complete','checked_at':self.now,'ai_checked':True,
               'model':'deepseek-v4-pro','signature':'private-cache-signature',
               'chain_evidence':[{'chain':'base','kind':'bio','relation':'ecosystem','source':'https://x.com/early','quote':'Our home is Base.'}]}
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT INTO research_cache VALUES (?,?,?)', ('chain-check:v2:early',json.dumps(check),self.now))
        result=read_snapshot(self.path,self.now)
        record=result['records'][0]
        self.assertEqual(record['chains'],['base'])
        self.assertTrue(record['chainCheck']['aiChecked'])
        self.assertEqual(record['chainCheck']['status'],'complete')
        self.assertNotIn('private-cache-signature',json.dumps(result))
        self.assertEqual(record['officialCa'],[])
        old=read_snapshot(self.path,self.now+21601)['records'][0]
        self.assertEqual(old['chainCheck']['status'],'not_checked')
        self.assertEqual(old['chains'],[])

    def test_source_urls(self):
        for u in ("javascript:alert(1)","http://localhost/x","http://127.0.0.1/x","http://10.0.0.1/x","http://[::1]/x","https://example.com/?api_key=private","https://user:pw@example.com","https://example.com/?token=private","https://example.com/?api%5Fkey=private","https://example.com/#access_token=private"):
            self.assertIsNone(public_url(u))
        self.assertEqual(public_url("https://docs.example.com/a"),"https://docs.example.com/a")

    def test_new_identity_wins_over_old_report_and_research_keeps_original_time(self):
        self.storage.add_event("K", "Builder", 1, self.now)
        self.add_meta("Builder", {"category":"crypto_dev", "summary":"New identity"})
        report={"category":"project", "_researched_at":self.now-90000, "_verified":True, "followers":777,
                "followers_source":"https://x.com/Builder", "website":{"status":"read","url":"https://example.com","finding":"未部署 Ethereum，仅支持 Solana"}}
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT INTO research_cache VALUES (?,?,?)",("deepseek:v3:builder:project",json.dumps(report),self.now))
        r=read_snapshot(self.path,self.now)["records"][0]
        self.assertEqual(r["category"],"crypto_dev")
        self.assertEqual(r["summary"],"New identity")
        self.assertEqual(r["researchStatus"],"stale")
        self.assertIsNone(r["followers"])
        self.assertEqual(r["chains"],[])


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=str(Path(self.temp.name)/"data.db")
        Storage(self.path)
        self.token="a"*40
        self.client=TestClient(TestServer(create_api(self.path,self.token)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_auth_methods_and_empty_database(self):
        self.assertEqual((await self.client.get('/healthz')).status,200)
        self.assertEqual((await self.client.get('/api/alpha')).status,401)
        self.assertEqual((await self.client.get('/api/alpha',headers={'Authorization':'Bearer wrong'})).status,401)
        headers={'Authorization':'Bearer '+self.token}
        r=await self.client.get('/api/alpha',headers=headers)
        self.assertEqual(r.status,200)
        self.assertEqual((await r.json())['records'],[])
        self.assertEqual(r.headers['Cache-Control'],'private, no-store')
        self.assertEqual((await self.client.post('/api/alpha',headers=headers)).status,405)
        self.assertEqual((await self.client.get('/api/alpha?token=no',headers=headers)).status,400)

    async def test_concurrent_reads_do_not_write(self):
        before=Path(self.path).read_bytes()
        responses=await asyncio.gather(*(self.client.get('/api/alpha',headers={'Authorization':'Bearer '+self.token}) for _ in range(8)))
        self.assertTrue(all(r.status==200 for r in responses))
        self.assertEqual(Path(self.path).read_bytes(),before)

    async def test_no_token_fails_closed(self):
        with self.assertRaises(ValueError):
            create_api(self.path,'')
