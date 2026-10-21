"""Arsenal: entry validation, retrieval, ranking, licence attachment, persistence, manifest stability, pipeline stage 12 wiring."""
import os
import tempfile
import unittest

from vega_harness import arsenal as ar
from vega_harness import conductor as cd
from vega_harness import reasoning_pipeline as rp

MATRIX = {"left-pad-ish": {"repo": "left-pad-ish", "license_class": "PERMISSIVE", "personal_build": "ok", "public_build": "ok", "taint_if_used": False},
          "gpl-thing": {"repo": "gpl-thing", "license_class": "STRONG_COPYLEFT", "personal_build": "quarantine", "public_build": "no", "taint_if_used": True}}


def mk():
    a = ar.Arsenal(use_matrix=MATRIX)
    a.add("repo", "gpl-thing", "Parses invoices from scanned PDF files and extracts totals", path="gpl-thing", score=0.9)
    a.add("repo", "left-pad-ish", "Pads strings on the left with a fill character", path="left-pad-ish", score=0.6)
    a.add("symbol", "chain_verify", "Verify a hash chain end to end and report the first broken link", path="custody/dag.py:Chain.verify", score=0.95)
    a.add("tool", "secret_scan", "Scan a staged tree for credential shaped strings before a push", score=0.8)
    a.add("prompt", "split_test", "Prompt template that splits a claim into testable parts", score=0.7)
    return a


class TestEntry(unittest.TestCase):
    def test_refuses_bad_entries(self):
        a = ar.Arsenal()
        for args in [("nope", "n", "s"), ("tool", "", "s"), ("tool", "n", "  "), ("tool", 5, "s")]:
            with self.assertRaises(ar.ArsenalError):
                a.add(*args)
        for sc in (True, 1.5, -0.1, float("nan"), "0.5"):
            with self.assertRaises(ar.ArsenalError):
                a.add("tool", "n", "s", score=sc)
        with self.assertRaises(ar.ArsenalError):
            a.add("tool", "n", "s", tags="abc")        # a bare string would be read letter by letter

    def test_same_kind_name_path_replaces(self):
        a = ar.Arsenal()
        i1 = a.add("tool", "t", "first summary", score=0.1)
        i2 = a.add("tool", "t", "second summary", score=0.9)
        self.assertEqual(i1, i2)
        self.assertEqual(len(a.items), 1)
        self.assertEqual(a.items[i1]["summary"], "second summary")

    def test_no_truncation(self):
        a = ar.Arsenal()
        long = "word " * 5000
        iid = a.add("prompt", "big", long)
        self.assertEqual(a.items[iid]["summary"], long)


class TestSearch(unittest.TestCase):
    def test_finds_the_right_entry(self):
        a = mk()
        self.assertEqual(a.search("check that my hash chain is intact")[0]["name"], "chain_verify")
        self.assertEqual(a.search("extract totals from scanned invoice pdf")[0]["name"], "gpl-thing")
        self.assertEqual(a.search("find credentials before I push")[0]["name"], "secret_scan")

    def test_kind_filter(self):
        a = mk()
        hits = a.search("verify chain", kind="symbol")
        self.assertTrue(hits and all(h["kind"] == "symbol" for h in hits))
        self.assertEqual(a.search("pads left fill character", kind="tool"), [])       # that text belongs to a repo, not a tool

    def test_repo_hit_carries_licence_and_card(self):
        a = mk()
        hit = a.search("parse invoices pdf", kind="repo", show_use_cards=True)[0]
        self.assertEqual(hit["license_class"], "STRONG_COPYLEFT")
        self.assertIn("USE CARD for gpl-thing", hit["use_card"])
        self.assertNotIn("use_card", a.search("parse invoices pdf", kind="repo")[0])

    def test_unknown_repo_is_strict(self):
        a = ar.Arsenal(use_matrix={})
        a.add("repo", "mystery", "Does a thing with widgets", score=0.5)
        self.assertEqual(a.search("widgets")[0]["license_class"], "NO_LICENSE_FILE")

    def test_nothing_matches_gives_empty_not_a_guess(self):
        self.assertEqual(mk().search("quantum chromodynamics lattice"), [])

    def test_score_breaks_ties_not_similarity(self):
        a = ar.Arsenal()
        a.add("tool", "a_low", "alpha beta", score=0.1)
        a.add("tool", "b_high", "alpha beta", score=0.9)
        hits = a.search("alpha beta")
        self.assertEqual([h["name"] for h in hits], ["b_high", "a_low"])
        self.assertEqual(hits[0]["similarity"], hits[1]["similarity"])

    def test_bad_queries_refused(self):
        a = mk()
        for q in ("", "   ", None, 5):
            with self.assertRaises(ar.ArsenalError):
                a.search(q)
        for n in (0, -1, True, 2.5):
            with self.assertRaises(ar.ArsenalError):
                a.search("verify", n=n)

    def test_empty_index_returns_empty(self):
        self.assertEqual(ar.Arsenal().search("anything"), [])

    def test_embedding_function_is_used_and_checked(self):
        vocab = ["chain", "invoice", "secret"]
        emb = lambda t: [float(t.lower().count(w)) + 0.001 for w in vocab]
        a = ar.Arsenal(embed=emb)
        a.add("symbol", "chain_verify", "verify the chain", score=0.9)
        a.add("tool", "invoice_parser", "parse invoice", score=0.9)
        self.assertEqual(a.search("chain chain")[0]["name"], "chain_verify")
        bad = ar.Arsenal(embed=lambda t: [])
        bad.add("tool", "x", "y")
        with self.assertRaises(ar.ArsenalError):
            bad.search("y")
        crash = ar.Arsenal(embed=lambda t: 1 / 0)
        crash.add("tool", "x", "y")
        with self.assertRaises(ar.ArsenalError):
            crash.search("y")


class TestPersistenceAndManifest(unittest.TestCase):
    def test_round_trip_and_hash(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "arsenal.json")
            a = mk()
            a.path = p
            a.save()
            b = ar.Arsenal(path=p, use_matrix=MATRIX)
            self.assertEqual(a.index_hash(), b.index_hash())
            self.assertEqual(b.search("hash chain intact")[0]["name"], "chain_verify")

    def test_hash_changes_when_an_entry_changes(self):
        a = mk()
        h0 = a.index_hash()
        a.add("tool", "secret_scan", "Scan a staged tree for credential shaped strings before a push", score=0.81)
        self.assertNotEqual(h0, a.index_hash())

    def test_manifest_is_deterministic_and_insertion_order_free(self):
        a, b = mk(), ar.Arsenal(use_matrix=MATRIX)
        for it in reversed(list(a.items.values())):
            b.add(it["kind"], it["name"], it["summary"], it["path"], it["tags"], it["score"])
        self.assertEqual(a.manifest(), b.manifest())
        self.assertIn("symbol (1):", a.manifest())

    def test_manifest_says_how_many_more(self):
        a = ar.Arsenal()
        for i in range(8):
            a.add("tool", "t%d" % i, "does thing %d" % i, score=0.5)
        self.assertIn("...and 3 more tool entries", a.manifest(per_kind=5))


class TestPipelineWiring(unittest.TestCase):
    def plan(self, deps):
        with tempfile.TemporaryDirectory() as d:
            c = cd.Conductor(os.path.join(d, "runs.json"), os.path.join(d, "reg.json"))
            return rp.plan({"goal": "verify a hash chain"}, c, deps=deps)

    def test_empty_arsenal_stays_not_built(self):
        out = self.plan({"arsenal": ar.Arsenal().as_dependency()})
        self.assertIn("arsenal", out["blueprint"]["unbuilt"])

    def test_stage_12_carries_hits_and_hash(self):
        a = mk()
        out = self.plan({"arsenal": a.as_dependency(kind="symbol")})
        stage = out["blueprint"]["arsenal"]
        self.assertEqual(stage["status"], "BUILT")
        self.assertEqual(stage["index_hash"], a.index_hash())
        self.assertEqual(stage["hits"][0]["name"], "chain_verify")
        self.assertTrue(stage["confident"])
        self.assertNotIn("arsenal", out["blueprint"]["unbuilt"])

    def test_not_confident_when_best_hit_is_weak(self):
        a = mk()
        stage = a.as_dependency(threshold=0.99)("hash chain")
        self.assertFalse(stage["confident"])


if __name__ == "__main__":
    unittest.main()
