"""arsenal.py - the war chest: an index of everything the harness can reach for, so a model does not have to carry it all.

Tools, whole repos, single symbols (functions/classes with their own score), named prompts, past solutions and skills are entered ONCE, with a plain summary and a
score. When a request arrives, search(query) returns the closest entries ("I need something that does this" -> this repo, this symbol, this prompt) with their
similarity, their stored score, and, for a repo, the licence class and the use card that says whether it may be used in a public build.

  Arsenal(path=None, embed=None)       path: a JSON file the index is kept in. embed: optional text -> list of numbers (a real embedding model); without it,
                                       a built-in TF-IDF cosine over the summaries is used (no network, no dependency, same answer every time)
  add(kind, name, summary, ...)        enter or replace one entry (same kind + name + path = same entry)
  search(query, n, kind)               closest entries, similarity first, stored score breaks ties
  manifest()                           compact deterministic text of what the arsenal holds, for putting in front of a model at the start
  index_hash()                         hash of the whole index: proves which version answered
  as_dependency(...)                   the callable the reasoning pipeline's stage 12 takes

HONEST LIMITS: similarity is retrieval, not truth: a close match is a candidate to check, not an answer. The TF-IDF default matches words, not meaning; pass `embed`
for meaning. The score is whatever the caller entered; the arsenal does not compute it. The index is only as complete as what was added to it. Nothing is truncated."""
import math
import os
import re
import tempfile

from vega_harness import use_gate as ug
from vega_harness.records import canon, h

KINDS = ("tool", "repo", "symbol", "prompt", "solution", "skill")
_STOP = frozenset("a an and are as at be by for from has have in is it its of on or that the this to was were will with i need want do does what which how".split())
_TOKEN = re.compile(r"[a-z0-9]+")


class ArsenalError(ValueError):
    pass


def _tokens(text):
    return [t for t in _TOKEN.findall(text.lower()) if len(t) > 1 and t not in _STOP]


def _is_score(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and 0 <= x <= 1


def _cos(a, b):
    num = sum(x * y for x, y in zip(a, b))
    da, db = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return 0.0 if da == 0 or db == 0 else num / (da * db)


class Arsenal:
    def __init__(self, path=None, embed=None, use_matrix=None):
        self.path, self.embed, self.use_matrix = path, embed, use_matrix
        self.items = {}
        self._vec = None
        if path and os.path.exists(path):
            self._load()

    # ---- entry -------------------------------------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def item_id(kind, name, path=""):
        return h({"kind": kind, "name": name, "path": path})[:16]

    def add(self, kind, name, summary, path="", tags=(), score=0.5, license_class=None):
        if kind not in KINDS:
            raise ArsenalError("kind must be one of %s" % ", ".join(KINDS))
        if not isinstance(name, str) or not name.strip() or not isinstance(summary, str) or not summary.strip():
            raise ArsenalError("an entry needs a name and a summary, both non-empty text")
        if not isinstance(path, str):
            raise ArsenalError("path must be text")
        if isinstance(tags, (str, bytes)) or any(not isinstance(t, str) for t in tags):
            raise ArsenalError("tags must be a list of text")
        if not _is_score(score):
            raise ArsenalError("score must be a number from 0 to 1 (not a boolean)")
        if license_class is not None and not isinstance(license_class, str):
            raise ArsenalError("license_class must be text or absent")
        iid = self.item_id(kind, name, path)
        self.items[iid] = {"id": iid, "kind": kind, "name": name, "path": path, "summary": summary, "tags": list(tags), "score": score,
                           "license_class": license_class}
        self._vec = None
        return iid

    def remove(self, iid):
        existed = self.items.pop(iid, None) is not None
        self._vec = None
        return existed

    # ---- persistence -------------------------------------------------------------------------------------------------------------------------------------------
    def _load(self):
        import json
        with open(self.path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ArsenalError("the arsenal file is an object with a list of items")
        for it in data["items"]:
            self.add(it["kind"], it["name"], it["summary"], it.get("path", ""), it.get("tags", ()), it.get("score", 0.5), it.get("license_class"))

    def save(self):
        if not self.path:
            raise ArsenalError("this arsenal has no file path")
        body = canon({"items": [self.items[k] for k in sorted(self.items)]})
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(self.path)) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(body)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def index_hash(self):
        return h([self.items[k] for k in sorted(self.items)])

    # ---- retrieval ---------------------------------------------------------------------------------------------------------------------------------------------
    def _text_of(self, it):
        return " ".join([it["name"].replace("_", " "), it["summary"], " ".join(it["tags"])])

    def _build(self):
        ids = sorted(self.items)
        docs = [self._text_of(self.items[i]) for i in ids]
        if self.embed:
            vecs = [self._embed_one(d) for d in docs]
            self._vec = {"ids": ids, "mode": "embed", "vecs": vecs}
            return
        toks = [_tokens(d) for d in docs]
        df = {}
        for t in toks:
            for w in set(t):
                df[w] = df.get(w, 0) + 1
        n = len(toks)
        idf = {w: math.log((1 + n) / (1 + c)) + 1 for w, c in df.items()}
        vocab = sorted(idf)
        pos = {w: i for i, w in enumerate(vocab)}
        vecs = []
        for t in toks:
            v = [0.0] * len(vocab)
            for w in t:
                v[pos[w]] += 1
            vecs.append([x * idf[vocab[i]] for i, x in enumerate(v)])
        self._vec = {"ids": ids, "mode": "tfidf", "vecs": vecs, "idf": idf, "pos": pos, "size": len(vocab)}

    def _embed_one(self, text):
        try:
            v = self.embed(text)
        except Exception as exc:
            raise ArsenalError("the embedding function failed: %s" % type(exc).__name__) from exc
        if not isinstance(v, (list, tuple)) or not v or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in v):
            raise ArsenalError("the embedding function must return a non-empty list of finite numbers")
        return list(v)

    def _query_vec(self, query):
        if self._vec["mode"] == "embed":
            return self._embed_one(query)
        v = [0.0] * self._vec["size"]
        for w in _tokens(query):
            if w in self._vec["pos"]:
                v[self._vec["pos"][w]] += self._vec["idf"][w]
        return v

    def search(self, query, n=5, kind=None, show_use_cards=False):
        if not isinstance(query, str) or not query.strip():
            raise ArsenalError("a query is non-empty text")
        if isinstance(n, bool) or not isinstance(n, int) or n <= 0:
            raise ArsenalError("n is a positive whole number")
        if kind is not None and kind not in KINDS:
            raise ArsenalError("kind must be one of %s" % ", ".join(KINDS))
        if not self.items:
            return []
        if self._vec is None:
            self._build()
        q = self._query_vec(query)
        rows = []
        for iid, vec in zip(self._vec["ids"], self._vec["vecs"]):
            it = self.items[iid]
            if kind and it["kind"] != kind:
                continue
            sim = _cos(q, vec)
            if sim > 0:
                rows.append((sim, it))
        rows.sort(key=lambda r: (-r[0], -r[1]["score"], r[1]["name"]))
        out = []
        for sim, it in rows[:n]:
            hit = {k: it[k] for k in ("id", "kind", "name", "path", "summary", "tags", "score")}
            hit["similarity"] = round(sim, 4)
            if it["kind"] == "repo":
                hit["license_class"] = ug.klass_of(it["license_class"]) if it["license_class"] else ug.klass(it["name"], self.use_matrix)
                if show_use_cards:
                    hit["use_card"] = ug.use_card(it["name"], self.use_matrix)
            elif it["license_class"]:
                hit["license_class"] = ug.klass_of(it["license_class"])
            out.append(hit)
        return out

    # ---- context for a model ----------------------------------------------------------------------------------------------------------------------------------
    def manifest(self, per_kind=5):
        """Deterministic: the same index gives byte-identical text, so it can sit in a cached prompt prefix."""
        if not self.items:
            return "ARSENAL: empty."
        lines = ["ARSENAL (search it before rebuilding, re-deriving or asking a model to invent):"]
        for k in KINDS:
            its = sorted((i for i in self.items.values() if i["kind"] == k), key=lambda i: (-i["score"], i["name"]))
            if not its:
                continue
            lines.append("%s (%d):" % (k, len(its)))
            for i in its[:per_kind]:
                lines.append("- %s [%.2f]: %s" % (i["name"], i["score"], i["summary"].split(". ")[0].rstrip(".") + "."))
            if len(its) > per_kind:
                lines.append("- ...and %d more %s entries: search to reach them." % (len(its) - per_kind, k))
        return "\n".join(lines)

    def as_dependency(self, n=5, kind=None, threshold=0.35, show_use_cards=False):
        """The callable stage 12 of the reasoning pipeline takes: goal -> what the arsenal offers. An empty arsenal reports NOT_BUILT, so the blueprint stays honest.
        `confident` marks whether the best hit reaches `threshold` (untuned: calibrate it on your own index)."""
        def dep(goal):
            if not self.items:
                return {"status": "NOT_BUILT", "why": "no entries have been indexed"}
            hits = self.search(goal, n=n, kind=kind, show_use_cards=show_use_cards)
            return {"status": "BUILT", "index_hash": self.index_hash(), "mode": (self._vec or {}).get("mode"), "threshold": threshold, "hits": hits,
                    "confident": bool(hits) and hits[0]["similarity"] >= threshold, "note": "retrieval, not verification: each hit is a candidate to check"}
        return dep
