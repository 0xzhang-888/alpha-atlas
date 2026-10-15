import unittest

from src.chain_evidence import text_evidence, follow_evidence, evidence_status, own_x_source


class EarlyChainTests(unittest.TestCase):
    def chains(self, value, kind='bio'):
        return {e['chain'] for e in text_evidence(value, 'https://x.com/Early', kind, 100)}

    def test_early_project_needs_no_ca(self):
        self.assertEqual(self.chains('The lending protocol built on @base'), {'base'})
        self.assertEqual(self.chains('Building on NEAR Protocol'), {'near'})
        self.assertEqual(self.chains('基于 GIWA 的借贷协议'), {'giwa'})
        self.assertEqual(self.chains('Lucrari是Upbit旗下GIWA链上的代币发射台。'), {'giwa'})
        self.assertEqual(self.chains('Built on Ethereum and Base'), {'ethereum','base'})
        self.assertEqual(self.chains('项目将在 Robinhood Chain 上线'), {'robinhood'})

    def test_plans_not_deployment_verification(self):
        rows = text_evidence('We will launch on Arc next month', 'https://x.com/Early/status/123', 'official_post', 100)
        self.assertEqual(rows[0]['chain'], 'arc')
        self.assertEqual(rows[0]['relation'], 'planned_launch')
        self.assertEqual(evidence_status(rows), 'planned')

    def test_denials_ordinary_words_and_others_are_not_claims(self):
        for value in ('Growing our user base, near launch, story arc', 'Not building on Base',
                      'Solana is faster than Ethereum', 'Wallet supports Ethereum, Solana and Base',
                      'Our investor built on Base', 'An EVM compatible protocol',
                      'RT @Other: We are launching on Base'):
            self.assertEqual(self.chains(value), set(), value)
        self.assertEqual(self.chains('Not building on Base; launching on Monad'), {'monad'})
        self.assertEqual(self.chains('Not live yet. We are launching on Base next month'), {'base'})
        self.assertEqual(self.chains('Our protocol is inspired by Ethereum and built on Monad', 'document'), {'monad'})
        self.assertEqual(self.chains('The protocol supports Ethereum and Solana wallets', 'document'), set())
        self.assertEqual(self.chains('Built on Monad, inspired by Ethereum and Base', 'document'), {'monad'})

    def test_follow_direction_and_official_registry(self):
        self.assertEqual(follow_evidence('early', [{'follower':'base','followed':'early','ts':100}]), [])
        self.assertEqual(follow_evidence('early', [{'follower':'someone','followed':'base','ts':100}]), [])
        self.assertEqual(follow_evidence('early', [{'follower':'early','followed':'fake_base','ts':100}]), [])
        rows = follow_evidence('early', [{'follower':'early','followed':'base','ts':100}, {'follower':'early','followed':'arc','ts':101}])
        self.assertEqual({r['chain'] for r in rows}, {'base','arc'})
        self.assertTrue(all(r['source'] is None and r['confidence'] == 'inferred' for r in rows))

    def test_own_original_post(self):
        self.assertTrue(own_x_source('https://x.com/Early/status/123', 'early', post=True))
        self.assertFalse(own_x_source('https://x.com/Other/status/123', 'early', post=True))
        self.assertFalse(own_x_source('https://x.com/Early', 'early', post=True))
