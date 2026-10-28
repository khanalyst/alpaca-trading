"""Golden equivalence: every registered rule arm evaluates exactly as before.

The signal-contract fixes of the October 2026 review are opt-in.  This test
replays a deterministic synthetic market through every arm already registered
(the 24-arm diagnostic shadow cohort, mechanism cohorts v1, v2,
cost-compatible, deadline-compatible and refresh, and the subject and mirror
arms of both sealed preregistered hypotheses) and compares each arm's signals,
bracket geometry, thesis deadlines and exit transitions with a fingerprint
computed on the code before those fixes (commit 352fb70).  A difference in a
single emitted field of a single arm changes that arm's digest.
"""

import unittest

from tests.research.rule_golden_harness import fingerprint, registered_arms


# variant_id: (signals, bracket exits, sha256 of every signal and exit record)
ORIGINAL_FINGERPRINT = {
    "rule.cross-sectional-residual.2969f7f47855f157": (
        353, 217,
        "13dd0105ffb13277a7528ac5db17570f02d6214ecd5ef3952cfa2f9d1eec1069"),
    "rule.cross-sectional-residual.374375e771a64864": (
        492, 297,
        "671d7f980b5de18a5f14279764c9277a9c72ed325f2c313697972ced100e0f9c"),
    "rule.cross-sectional-residual.63a5380d5e0b6762": (
        329, 0,
        "1c236e2a06664afff8f411b912d6d0246335b4e2ca042a03d4a0fd59c316b68c"),
    "rule.cross-sectional-residual.a4c9876c220419b0": (
        269, 0,
        "23b4aabe87473c75bac08409a89b108806c988b164495bec901f3a9a046e8288"),
    "rule.cross-sectional-residual.fb2ccaeb9c54907c": (
        269, 86,
        "4f85b2bf275356646c936d929efe7b78bb16c78a78fa3b928915f988728ef528"),
    "rule.mean-reversion.022db7a0df31f106": (
        50, 2,
        "f3ba0c4a2608f861cef94470dc7254787bf791b595a06917aedd68e2ee5a4230"),
    "rule.mean-reversion.264ff151b4541339": (
        50, 2,
        "106d5aeb5ab1a25400b77208d3aecfce25d0362ab01ff8a299ccb3ee0cc18a52"),
    "rule.mean-reversion.75a110f082d304d1": (
        18, 11,
        "b695f71308e2dd7cdd88b346eb6d66bc88402650b6b6789db3be902e8b69d8e9"),
    "rule.mean-reversion.78f80534776d6a4b": (
        50, 33,
        "49023e839ad84bff417f6f9178edd6be5499a23c2279f29e409b52dddb01337b"),
    "rule.mean-reversion.b4e231565af56f03": (
        20, 13,
        "a38e035641bf54cb5c65e9fc5cf56db3dcc3cdd135d8468b9f1b2ea5e57ebd58"),
    "rule.mean-reversion.d20edba4c678e1d8": (
        31, 19,
        "95ff10459e0dcbef920f3f5e2c0be36158d74b50aff894f1a21856a0fc3cf0d9"),
    "rule.momentum-continuation.0e8fed23b39eb8b2": (
        25, 11,
        "75a43434b8b2b993db6f960893ee29198a4967a0ebc302bf788ed6f574aa0870"),
    "rule.momentum-continuation.984cbcedd4a2846a": (
        27, 19,
        "80098817217bb194cc7b356ec8d3491c12aeb6788514080fafdaac214b6d4f7f"),
    "rule.momentum-continuation.bdc87e262eb6fb8a": (
        25, 0,
        "6026be9564709875445c8af6bfadc631fe72fb8a12852020f9d0ea73e3743754"),
    "rule.momentum-continuation.c47501171d61ad81": (
        27, 0,
        "f74031e3e3851eda9cc1455a8a7f83da228631cb170ae5626dc77bb9c133a75b"),
    "rule.momentum-continuation.fe74f73cfc7d082a": (
        57, 36,
        "83e299d5d19c45ad8a99e393299e2e9f75044748859e77463b13347c66a41e52"),
    "rule.opening-drive.0bad7c9529bb2e5e": (
        232, 0,
        "7ae551de80e88f18708974b8230c786456836c0b4598eca1ef7a8a5bb6b40e7e"),
    "rule.opening-drive.0d82e82b09466fa9": (
        237, 0,
        "52eda76cf6928137070a1cedc3943932ddb125798c75da40e5bb6e9a4afd9103"),
    "rule.opening-drive.393e7229056fc663": (
        158, 88,
        "01a8700440fb244a3a36dda4104babd4d01b41a288c712545666813437b45bdf"),
    "rule.opening-drive.560520e6c8c85b1e": (
        158, 88,
        "5104bd1585019857b114cb6fa5e99263ef7d743177bd0bd0952795829589d028"),
    "rule.opening-drive.a083492d6437b241": (
        237, 59,
        "df2c6ebbdca8afb8f1a9a4812bfeb7ee103d70bb46b9472da3d48332060d93b6"),
    "rule.opening-range-breakout.026e91aa282aba27": (
        525, 113,
        "4c5accae14ea672c366f0ea7ed208bba7057f239870d78cd6ce8e102d0a77d7f"),
    "rule.opening-range-breakout.0eb200d3136d80ee": (
        304, 168,
        "10c0764e5aa19840427735b769cadd479679110de1f7c2385e0a123a71e0d890"),
    "rule.opening-range-breakout.2aa6eecce57d9c56": (
        525, 0,
        "18e25b391930f7e543edeea53c6d97f3d2b06a73d8a448f9b22ca2c2b7189d65"),
    "rule.opening-range-breakout.3578223ed48d29e5": (
        1009, 493,
        "06b8b0453b1045f5027ea9a130d82a13117b8d0d9b481cf719ccd374058ec994"),
    "rule.opening-range-breakout.4b10682772f81284": (
        300, 165,
        "1a5db25e0d33118e273a66c0b0549043876dee2464d3da872bc79426987bdaa9"),
    "rule.opening-range-breakout.4d812cf5660e0c25": (
        623, 270,
        "c4c61d67f12512718de2620af23463b4e1c712234190c8a378005292385487da"),
    "rule.opening-range-breakout.511ac0622f1dc0bc": (
        525, 236,
        "1e79a4a80daeeeaadfb43d454ba1879a2aa4100d2545adc0acf6bb4426c90ac2"),
    "rule.opening-range-breakout.5bba32e8cbb1aa60": (
        517, 0,
        "893c86afa5c91ec2ca3f80f3ba3c8a1a709f57f7bf12aab21aff0b6a84ac8ddc"),
    "rule.opening-range-breakout.e7bf7cf599d2212e": (
        525, 130,
        "68b3f4ddf78ccc0b4f6ac8966f8dbd74d67e67797f21ae14b88049debda4c1be"),
    "rule.opening-range-fade.41ae8632f2b73a00": (
        1, 1,
        "3c9043e306adb4e8865da4978fbd32b9e11814e89297bb297f53732e77ae5929"),
    "rule.opening-range-fade.8a6efdff14783e2b": (
        2, 0,
        "80bbc66b9c156e47056b43fd8a5d66f02a6bb51807cebb633c09e467a34affa0"),
    "rule.opening-range-fade.ab1c2b7f9fa16248": (
        2, 2,
        "5632460e965ca7bb9bbf48b594eee6e71d42f316f467d530ce44432f11b7b5af"),
    "rule.opening-range-fade.be61630d346f0923": (
        1, 1,
        "12697252444b54a9ad902fcb873d51f51a58623954f2d10ba4328f7212d1102b"),
    "rule.opening-range-fade.d5785d9e70b56def": (
        3, 3,
        "889be08342db675f6c7d206cd60055f5dd35eef7b52efc9eaf36b74a5d33134a"),
    "rule.opening-range-fade.ddabf6b55dc8617c": (
        2, 0,
        "3e1b0f2666de2f0815ac117292c87ed524b1e05f120e7c5d8e11074546324587"),
    "rule.range-expansion.0e890d6aa42e0560": (
        24, 14,
        "df2fbc341f93106638d42c7361c72a4113da5dd966b6d4978690826d63845cfe"),
    "rule.range-expansion.4b22b60b28b8c96d": (
        24, 0,
        "4eaa5e09fefa14ea42cd86e7ca7c0775f4dc8d58f81698db85cfbb6bbc006308"),
    "rule.range-expansion.a622774ea1d73ea8": (
        22, 0,
        "afe7a5f2a94b155a0157fb90e1cf4efd6d92cada76ee49bc272c91df61b086d2"),
    "rule.range-expansion.a9871756f22463a5": (
        65, 35,
        "96c0922bfb600895dd04840554d964cd86245b0f085684e778289ddb30e628bf"),
    "rule.range-expansion.f7fd1b25f2f95ba6": (
        41, 24,
        "01d1b9ed90613b85645df9b47c62974510b9d3f101e5ad242235a379fd586a61"),
    "rule.trend-pullback.1afa4bc3e16ea5dd": (
        5, 1,
        "f2fffb247d80c2831cdd2a53d569d2e240240b0d1d3e64ceb89bd7ab7dffd8eb"),
    "rule.trend-pullback.203b7d4417ea9bda": (
        9, 0,
        "a5129493c8d22a4d8bb66f33e05b63780747b299af5b3c07374a3037f880915f"),
    "rule.trend-pullback.2ffd1a88404dd020": (
        390, 165,
        "6e846e8a5b009c60ff98fcb6c397a5ad47b6cec40c281bf35730b099773ad405"),
    "rule.trend-pullback.54a4944ddb2cc60e": (
        498, 202,
        "f19ecceb28d4e97773c5ef73e99163cc454d2eca8665215e94afd9c75d9e1063"),
    "rule.trend-pullback.8627ac15537e638f": (
        5, 1,
        "51417db6d42a92541f4c5c31f63a649b9c6b4fc8e1c9916c4c52f814c8cbc1db"),
    "rule.trend-pullback.9140fd797a16059c": (
        9, 5,
        "9425ba6306aede050e9430938d7c081715e44deef9961b6a38ea0ac1c88c7c02"),
    "rule.trend-pullback.eaf3e32977d88904": (
        261, 114,
        "72405b7f5925cf38ac78a3e0841293f86a859ab7cf35788211558eaf7aca845b"),
    "rule.trend-pullback.ed9b7c13994d48c1": (
        8, 0,
        "dbaf9d6e0aeb6db233fcbb0e95118c19eb91958b0340811deaf1271bc545bc11"),
    "rule.trend-pullback.f7f19e02b4d866be": (
        9, 3,
        "500eb857e0532aaadcc93ce62c5237cfde0d303f6ccd93897025dc1b1fd2b9a1"),
    "rule.volatility-breakout.0a3e27f02d6c32e2": (
        8, 0,
        "df1793c1996181d57ff96126e95245770723f833061f6ed5d536d0cfa465f517"),
    "rule.volatility-breakout.2aee6d9740f509c7": (
        4, 0,
        "c2fb2b565c24b1a4f12d093bc3ca29c5b59bce14cc942b0e41c711698cad8379"),
    "rule.volatility-breakout.5e9c9b9924ce2284": (
        8, 3,
        "25e5cbf5f8d437fc36ec5c6b5c32da88259d6af5984129055044aeec5b65e7e2"),
    "rule.volatility-breakout.ba43d3a496ff37d8": (
        9, 6,
        "5bb8d9795d848e010b5f3d07032ae2a2eba0eb190545d2b0841a45d86ffd5bf8"),
    "rule.volatility-breakout.cc1b851a285825c5": (
        4, 3,
        "21eba52b91e4ecd732ba1a2f9bb24fc7e1f8c7e11844d3109718fa65fea3fecc"),
    "rule.volume-breakout.30fece6e3f9b9fd6": (
        14, 9,
        "44c8a7a9fbd91926a50f59bde6d61b2e7696881e3383896c244358fa8820d4ad"),
    "rule.volume-breakout.3f366a2a931587ad": (
        7, 0,
        "f5a394ba0d9ab86e4a4693e6180ffc94dc2c0d9194720958439a0711833bfc52"),
    "rule.volume-breakout.465d223d5ec94f9e": (
        7, 4,
        "0d2d241fa4d1582bacdd752f65afabefae137c7e9609246fcf32dea74db87568"),
    "rule.volume-breakout.a68b65c3c8e96ff4": (
        10, 8,
        "b38495f281dec34a7198b9e1c84c65c28fe85329931d7537b4ff10848f4eb182"),
    "rule.volume-breakout.c553bb320f77c948": (
        6, 0,
        "5e15e5ecc048b7337e96412b8fb30ad0ee6b9f98a95e1f5967c2f10344ec313e"),
    "rule.vwap-reversion.348e64e69600cac7": (
        66, 13,
        "2e7b562b048efcb6521f413aabc6c265d957d2d7c304cae9e699909dae81b613"),
    "rule.vwap-reversion.36845f38771a5266": (
        214, 73,
        "7e9cdf5852456b38998b7109e9d2b4fb56f1b3c30c3b4da73d722eb6c88af159"),
    "rule.vwap-reversion.4128a7b49bb1a8d1": (
        64, 2,
        "6b3a192c81b3c8fe32ee1d224f0a6027bac5445014fca2f3449340e9de2cb3ba"),
    "rule.vwap-reversion.4e86280f99dfcb13": (
        57, 1,
        "bca4acaf4881f1f36b383fb80fab15c4ce55b400ac1d8c7918b0a22263d72adf"),
    "rule.vwap-reversion.6907a109eaf5f882": (
        64, 0,
        "d69561f78cb0c29fe9951cec773daa620a709a38bab646883d29e52d2ee1a406"),
    "rule.vwap-reversion.8679c8c06a7b7a1e": (
        64, 0,
        "ace79c673d014bf8fc7231f4cb20e6d8f3d8bf53e268203b93b2a93d8b99a240"),
    "rule.vwap-reversion.918ecc939194518f": (
        910, 301,
        "e30fbcb3db63d302007e4450f24dc7aefeae74253f00d80f35eebf0e5d0d5e4f"),
    "rule.vwap-reversion.ab97bfe87ed566de": (
        1090, 738,
        "73e9291b2eec328f56c432b5d4a1209af20f77c601f94d6bf900efaf1f14fa0e"),
    "rule.vwap-reversion.cf8b38b12c9bf952": (
        873, 608,
        "967fc702118ed2ab2586983e4f2ba65899075d5c0b2e0aa62c474b048c7a6def"),
    "rule.vwap-reversion.e982e7a55f7d8ed1": (
        66, 7,
        "3cae0f517ddebb1cbab5d39602e519f5dd6b4c4d403d8dae7820868c5afa400e"),
    "rule.vwap-trend.23428d749bda8288": (
        159, 0,
        "65a1aead312b299552f219b49f2c55236f7278cea181b5efe282b63c8ef35f87"),
    "rule.vwap-trend.349de232c3d7a18c": (
        18, 9,
        "9cbad87132426d0331915ef13d4e23d0d38068aee2ad65b9e76c88136fbb8a96"),
    "rule.vwap-trend.588bc47ed64d051e": (
        4, 3,
        "79d1d8c8be7ca68c8c4002f6e3720fffa9ea09c4c64f6e38bd01b8eede7ef9a5"),
    "rule.vwap-trend.5dc4fee859b70d55": (
        223, 0,
        "936a7fe228c4fee4a3819123e17d5d9a56ca46435f97867df2125acf1120fef6"),
    "rule.vwap-trend.b4fbbeeedf5dd011": (
        159, 65,
        "a2cac5214461baa54eb6923d19dfdd367e2289a21299e16aeb080598d63d16b4"),
}


class RegisteredArmGoldenEquivalenceTests(unittest.TestCase):
    def test_registered_inventory_is_the_pinned_inventory(self):
        self.assertEqual(sorted(dict(registered_arms())),
                         sorted(ORIGINAL_FINGERPRINT))

    def test_every_registered_arm_matches_the_pre_fix_fingerprint(self):
        actual = fingerprint()
        self.assertEqual(sorted(actual), sorted(ORIGINAL_FINGERPRINT))
        # The harness is only meaningful if every arm actually trades in it.
        self.assertTrue(all(signals > 0 for signals, _exits, _digest
                            in ORIGINAL_FINGERPRINT.values()))
        for variant_id, (signals, exits, digest) in sorted(
                ORIGINAL_FINGERPRINT.items()):
            with self.subTest(variant_id=variant_id):
                self.assertEqual(
                    (actual[variant_id]["signals"], actual[variant_id]["exits"],
                     actual[variant_id]["digest"]),
                    (signals, exits, digest))


if __name__ == "__main__":
    unittest.main()
