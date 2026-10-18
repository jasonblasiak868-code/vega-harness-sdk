"""records.py - the harness's record keeping, on the custody chain, plus single-use commitment burning.

Every harness record (a Conductor run, a registered workflow, a frozen prediction, a step outcome, an incident) is one entry in a vega_custody Chain, so it is
append-only, hash-chained, canonical JSON (NaN refused), replica-checked, credential-shaped values refused, and cross-process safe. Records hold hashes, names,
verdicts and counts, never secrets.

  RecordChain.append(kind, payload)            one record; two writers at once are safe (a stale writer re-reads and retries)
  RecordChain.read()                           payload dicts (with seq, hash, prev_hash added)
  RecordChain.append_unique(kind, field, ...)  atomic 'first writer wins': used to burn a commitment so one signature authorises one thing, once
  make_ledger_check(ledger)                    a commitment checker for the human-only gates: a minted, unexpired commitment for exactly this action"""
import hashlib
import json
import time

from vega_custody.dag import dag as dagmod


class RecordRefused(ValueError):
    pass


class Duplicate(RecordRefused):
    pass


def canon(obj) -> str:
    """Strict canonical JSON. No default= fallback: an object that is not plain JSON data is refused, never turned into text containing a memory address."""
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise RecordRefused(f"value is not plain JSON data: {type(exc).__name__}: {exc}") from exc


def h(obj) -> str:
    return hashlib.sha256(canon(obj).encode("utf-8")).hexdigest()


def _view(entry):
    r = dict(entry["payload"])
    r.update(seq=entry["seq"] + 1, hash=entry["entry_hash"], prev_hash=entry["parents"][0] if entry["parents"] else "0" * 64)
    return r


class RecordChain:
    def __init__(self, path, mirrors=()):
        self.paths = [str(path)] + [str(m) for m in mirrors]

    def _chain(self):
        return dagmod.Chain(self.paths)

    def read(self):
        return [_view(e) for e in self._chain().entries()]

    def verify(self, expected_head=None, min_length=None):
        try:
            ok, problems = self._chain().verify(expected_head=expected_head, min_length=min_length)
        except dagmod.DagError as exc:
            return False, [str(exc)]
        return (True, None) if ok else (False, problems)

    def append(self, kind, payload, attempts=8):
        return self._append(kind, payload, None, None, attempts)

    def append_unique(self, kind, field, value, payload, attempts=8):
        """Record only if no earlier record of this kind has payload[field] == value. Raises Duplicate otherwise. Atomic across writers: the chain refuses a stale writer,
        and the retry re-checks against the fresh view."""
        return self._append(kind, {**payload, field: value}, field, value, attempts)

    def _append(self, kind, payload, uniq_field, uniq_value, attempts):
        if not isinstance(payload, dict):
            raise RecordRefused("a record is an object")
        body = {"kind": kind, **payload}
        canon(body)
        for attempt in range(max(1, attempts)):
            chain = self._chain()
            if uniq_field is not None and any(e["payload"].get("kind") == kind and e["payload"].get(uniq_field) == uniq_value for e in chain.entries()):
                raise Duplicate(f"{kind} with {uniq_field}={uniq_value!r} was already recorded")
            try:
                return _view(chain.record(kind, body))
            except dagmod.RecordRefused as exc:
                if "changed underneath" not in str(exc) or attempt == attempts - 1:
                    raise RecordRefused(f"record refused by the chain: {exc}") from exc
                time.sleep(0.02 * (attempt + 1))
            except dagmod.DagError as exc:
                raise RecordRefused(f"record refused by the chain: {exc}") from exc


def burn_commitment(chain: RecordChain, commitment, purpose: str) -> str:
    """Mark a commitment as spent for `purpose`. Returns its id. Raises Duplicate if the same commitment was already used for anything: one signature, one use."""
    cid = h(commitment)
    chain.append_unique("COMMITMENT_USED", "commitment_id", cid, {"purpose": purpose})
    return cid


def make_ledger_check(ledger, clock=time.time):
    """checker(commitment, action) -> bool: the commitment is a WCOMMIT for exactly `action`, has not expired, and verifies in the ledger. Single use is enforced separately
    by burn_commitment, which the Conductor and predicted_path call before any effect."""
    def check(commitment, action):
        try:
            return (isinstance(commitment, dict) and commitment.get("kind") == "WCOMMIT" and commitment.get("action") == action
                    and int(commitment["expiry"]) > int(clock()) and bool(ledger.verify(commitment)))
        except Exception:  # reviewed: any failure while checking means the commitment is not accepted; the caller refuses the step
            return False
    return check
