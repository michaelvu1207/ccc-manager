import json
import unittest

from cccswitcher import remote, remote_parse

try:
    from .fakes import FIXTURES, REGISTRY
except ImportError:  # `unittest discover -s tests` imports test modules top-level
    from fakes import FIXTURES, REGISTRY


def load(name):
    return json.loads((FIXTURES / name).read_text())


class CswapListParsing(unittest.TestCase):
    def test_real_026_sample(self):
        claude = remote_parse.parse_cswap_list(load("cswap-0.26-list.json"))
        accounts = claude["accounts"]
        self.assertEqual([a["num"] for a in accounts], ["1", "2", "3"])
        self.assertEqual(sum(a["active"] for a in accounts), 1)
        self.assertTrue(all(a["needs_login"] for a in accounts))
        stale = accounts[2]  # no live usage, but lastGoodUsage with a Fable cap
        self.assertTrue(stale["usage_stale"])
        self.assertEqual([w["id"] for w in stale["windows"]], ["5h", "wk", "fable"])

    def test_live_disabled_alias_scoped(self):
        alice, bob = remote_parse.parse_cswap_list(load("cswap-live-disabled.json"))["accounts"]
        self.assertTrue(alice["disabled"])
        self.assertEqual(alice["label"], "alice@example.com [work]")
        self.assertFalse(alice["needs_login"])
        self.assertFalse(alice["usage_stale"])
        self.assertEqual([(w["id"], w["pct_used"]) for w in alice["windows"]],
                         [("5h", 12.0), ("wk", 40.0), ("fable", 75.0)])
        rendered = remote_parse.render_provider({"accounts": [alice]})["accounts"][0]["windows"]
        self.assertTrue(all(w["resets_in"] for w in rendered))
        self.assertTrue(bob["active"])
        self.assertFalse(bob["needs_login"])
        self.assertEqual(bob["status_message"], "keychain locked")

    def test_active_falls_back_to_active_number(self):
        payload = {"schemaVersion": 1, "activeAccountNumber": 2,
                   "accounts": [{"number": 1, "email": "a@x"}, {"number": 2, "email": "b@x"}]}
        a, b = remote_parse.parse_cswap_list(payload)["accounts"]
        self.assertFalse(a["active"])
        self.assertTrue(b["active"])

    def test_errors(self):
        for bad in ({"schemaVersion": 1, "error": {"type": "X", "message": "boom"}}, [1], {"schemaVersion": 1}):
            with self.assertRaises(ValueError):
                remote_parse.parse_cswap_list(bad)


class CodexParsing(unittest.TestCase):
    def test_projection_and_windows(self):
        rows = remote_parse.project_codex_registry(REGISTRY)
        self.assertNotIn("secret-id-1", json.dumps(rows))
        c1, c2 = remote_parse.codex_provider(rows)["accounts"]
        self.assertEqual([w["id"] for w in c1["windows"]], ["5h", "wk"])
        self.assertEqual([(w["id"], w["pct_used"]) for w in c2["windows"]], [("wk", 90)])
        self.assertTrue(c2["active"])
        self.assertEqual(c2["label"], "c2@example.com [main]")


class ProbeParsing(unittest.TestCase):
    def test_cswap_engine_sections(self):
        out = "\n".join([
            "@@ccc platform", "Linux", "@@ccc fingerprint", "abc", "/home/u",
            "@@ccc cswap", json.dumps(load("cswap-0.26-list.json")), "", "@@ccc cswap_rc 0",
            "@@ccc codex", json.dumps(REGISTRY), "", "@@ccc end",
        ])
        record = remote.parse_probe(out)
        self.assertEqual(record["platform"], "Linux")
        self.assertEqual(len(record["fingerprint"]), 16)
        claude, codex = record["providers"]
        self.assertEqual((len(claude["accounts"]), len(codex["accounts"])), (3, 2))

    def test_missing_tools_failures_and_truncation(self):
        out = "@@ccc platform\nDarwin\n@@ccc cswap\n@@ccc cswap_missing\n@@ccc codex\n@@ccc codex_missing\n@@ccc end\n"
        claude, codex = remote.parse_probe(out)["providers"]
        self.assertFalse(claude["available"])
        self.assertFalse(codex["available"])
        out = "@@ccc platform\nLinux\n@@ccc cswap\nTraceback\n@@ccc cswap_rc 1\n@@ccc codex\n@@ccc codex_missing\n@@ccc end\n"
        self.assertEqual(remote.parse_probe(out)["providers"][0]["error"], "cswap list failed (exit 1)")
        with self.assertRaises(ValueError):
            remote.parse_probe("@@ccc platform\nLinux\n@@ccc cswap\n{")

    def test_ccc_engine(self):
        snap = {"version": 2, "providers": [{"id": "claude", "available": True, "accounts": []},
                                            {"id": "codex", "available": True, "accounts": []}]}
        out = f"@@ccc platform\nLinux\n@@ccc ccc\n{json.dumps(snap)}\n@@ccc ccc_rc 0\n@@ccc end\n"
        self.assertEqual([p["id"] for p in remote.parse_probe(out)["providers"]], ["claude", "codex"])
        out = "@@ccc platform\nLinux\n@@ccc ccc\n@@ccc ccc_missing\n@@ccc end\n"
        self.assertEqual(remote.parse_probe(out)["providers"][0]["error"], "ccc not installed")


if __name__ == "__main__":
    unittest.main()
