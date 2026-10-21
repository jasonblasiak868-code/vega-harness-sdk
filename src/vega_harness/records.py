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
import types

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


def _code_digest(code, seen=0):
    parts = [code.co_code, repr(code.co_names).encode(), repr(code.co_varnames).encode(), repr(code.co_freevars).encode(), repr(code.co_argcount).encode()]
    for c in code.co_consts:
        parts.append(_code_digest(c, seen + 1) if isinstance(c, types.CodeType) and seen < 20 else repr(c).encode())
    return hashlib.sha256(b"\x1e".join(parts)).digest()


def _value_id(v, depth):
    """A stable text for a value a function captured or defaults to: plain JSON data by its canonical form, a function by its own identity, anything else (an object
    whose state we cannot read safely) by its type alone. Never a memory address."""
    try:
        return "j:" + canon(v)
    except RecordRefused:
        pass
    if isinstance(v, float):
        return "f:" + repr(v)
    if callable(v) and depth < 3 and (hasattr(v, "__code__") or hasattr(v, "func")):
        return "fn:" + code_hash(v, depth + 1)
    return "t:%s.%s" % (type(v).__module__, type(v).__qualname__)


def code_hash(fn, depth=0):
    """Identity of a callable: its compiled instructions, names, nested code, AND the values it captured from its surroundings (closure cells), its default
    arguments, and (for a partial) the wrapped function and bound arguments. Two functions with the same body but different captured values are different. A captured
    object that is not plain data is identified by its type only, so a change inside such an object is NOT seen (keep step state in plain data). Bytecode differs
    between Python versions, so an identity made on one version does not match on another; that fails safe."""
    code = getattr(fn, "__code__", None)
    state = None
    if code is None and hasattr(fn, "func") and hasattr(fn, "args") and hasattr(fn, "keywords"):                      # functools.partial
        parts = [b"partial", code_hash(fn.func, depth + 1).encode()] + [_value_id(a, depth).encode() for a in fn.args] + \
                [("%s=%s" % (k, _value_id(v, depth))).encode() for k, v in sorted(fn.keywords.items())]
        return hashlib.sha256(b"\x1e".join(parts)).hexdigest()
    if code is None:
        code = getattr(getattr(fn, "__call__", None), "__code__", None)
        state = getattr(fn, "__dict__", None)
    if code is None:
        return hashlib.sha256(f"{type(fn).__module__}.{type(fn).__qualname__}".encode()).hexdigest()
    parts = [_code_digest(code)]
    cells = []
    for cell in (getattr(fn, "__closure__", None) or ()):
        try:
            cells.append(_value_id(cell.cell_contents, depth))
        except ValueError:                                                                                              # an empty cell
            cells.append("empty")
    parts.append(("closure:" + "\x1f".join(cells)).encode())
    parts.append(("defaults:" + _value_id(list(getattr(fn, "__defaults__", None) or ()), depth)).encode())
    parts.append(("kwdefaults:" + _value_id(dict(getattr(fn, "__kwdefaults__", None) or {}), depth)).encode())
    if state is not None:
        parts.append(("state:" + "\x1f".join("%s=%s" % (k, _value_id(v, depth)) for k, v in sorted(state.items(), key=lambda kv: str(kv[0])))).encode())
    return hashlib.sha256(b"\x1e".join(parts)).hexdigest()


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
