#!/usr/bin/env python3
"""Pool names on chain: the genesis memo, and renames only the deployer can make.

A V11 pool's puzzles carry no label (a label inside the leaves would be a
revision), so the name lives in the chain's metadata instead, in two places:

* **genesis** -- the memo on the launcher's creation, written by the coin that
  paid for the pool (`create-pool --emoji`); immutable, like the launcher;
* **renames** -- any later coin the SAME address creates for itself, hinted with
  the pool's launcher id and carrying the new name as its second memo and the
  symbol as its third. The
  address is the launcher parent's puzzle hash: whoever paid for the launcher.
  A rename is authenticated by that address's own signature on the spend, so
  the router (which holds no key) cannot rename anything, and nobody else can
  either. The newest rename by height is the pool's name.

Resolution is a hint query (`get_coin_records_by_hint(launcher_id)`): the
pool's own singleton and reserves come back too, but they sit at other puzzle
hashes and are ignored; only coins AT the deployer's puzzle hash whose parent
also sat there count, and the memo is read from that parent's spend.

The work is bounded (external review 2026-11-16, "hinted dust can stall pool adoption").
Anyone can send a 1-mojo coin to the deployer's address hinted with the launcher id, and
each one used to cost a parent lookup of its own, so enough dust pushed the index script
past the router's 240 s kill and a new pool was never listed. Now:

* parents are looked up in batches (`get_coin_records_by_names`, NAMES_PER_BATCH a call);
  one call per coin only if the node refuses the batch, and then for MAX_SINGLE_LOOKUPS;
* at most MAX_HINTED_COINS hinted coins at the deployer's address are considered, newest
  first, and at most MAX_RENAME_SPENDS spends are read for memos;
* every pool has a time budget (FORGE_NAMES_POOL_BUDGET_S, default 20 s) and the whole run
  one more (FORGE_NAMES_BUDGET_S, default 120 s; api/_indexReconcile.js sets it well inside
  its kill). An exhausted budget raises NameBudgetExceeded; the index scripts catch it and
  the record's name stands in, so the pool is still listed.

Dust can therefore at worst hide a rename older than the newest MAX_HINTED_COINS coins at
the deployer's address (the name falls back to an older rename or the genesis memo); it can
no longer keep a pool out of the index.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from typing import Any

import forge_network as _forge_network  # noqa: E402
from untrusted_clvm import parse_untrusted_hex, run_capped  # noqa: E402

# The node of the network this runs against (FORGE_NETWORK / FORGE_NODE_URL).
DEFAULT_NODE = _forge_network.node_url()
CREATE_COIN = 51
LAUNCHER_HASH_HEX = "eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9"

# The bounds (see the module docstring).
MAX_HINTED_COINS = 1_000
NAMES_PER_BATCH = 100
MAX_SINGLE_LOOKUPS = 50
MAX_RENAME_SPENDS = 32
RPC_TIMEOUT_S = 60.0


def _budget_s(env: str, default: float) -> float:
    try:
        value = float(os.environ.get(env, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


POOL_BUDGET_S = _budget_s("FORGE_NAMES_POOL_BUDGET_S", 20.0)
# One deadline for the whole process, from the first import: the index scripts import this
# module once and resolve every pool of a record in turn.
RUN_DEADLINE = time.monotonic() + _budget_s("FORGE_NAMES_BUDGET_S", 120.0)


class NameBudgetExceeded(RuntimeError):
    """Name resolution ran out of its time budget; the caller's record name stands in."""


def _strip(v: Any) -> str:
    return str(v or "").removeprefix("0x")


def _rpc(node: str, route: str, body: dict, timeout: float = RPC_TIMEOUT_S) -> dict:
    req = urllib.request.Request(f"{node.rstrip('/')}/{route}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "aWizard-Forge/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _call(node: str, route: str, body: dict, deadline: float | None) -> dict:
    """_rpc under a deadline (monotonic seconds): refused once it has passed, and never
    waiting past it. With no deadline it is _rpc exactly as before."""
    if deadline is None:
        return _rpc(node, route, body)
    left = deadline - time.monotonic()
    if left <= 0:
        raise NameBudgetExceeded(f"name resolution ran out of time before {route}")
    return _rpc(node, route, body, timeout=min(RPC_TIMEOUT_S, max(left, 0.5)))


def _coin_id_hex(coin: dict) -> str:
    """sha256(parent || puzzle_hash || amount) of a node's coin JSON, as Coin.name() hashes
    it (the amount in CLVM's minimal signed encoding); '' when malformed."""
    try:
        parent = bytes.fromhex(_strip(coin["parent_coin_info"]))
        ph = bytes.fromhex(_strip(coin["puzzle_hash"]))
        amount = int(coin["amount"])
    except (KeyError, TypeError, ValueError):
        return ""
    if len(parent) != 32 or len(ph) != 32 or amount < 0:
        return ""
    raw = amount.to_bytes((amount.bit_length() + 8) // 8, "big", signed=True) if amount else b""
    return hashlib.sha256(parent + ph + raw).hexdigest()


def _parents_by_id(node: str, parent_ids: list[str], deadline: float | None) -> dict[str, dict]:
    """coin id hex -> coin record, for each parent the node knows. Batched; one call per coin
    only if the node refuses the batch route, and then for the first MAX_SINGLE_LOOKUPS."""
    try:
        out: dict[str, dict] = {}
        for start in range(0, len(parent_ids), NAMES_PER_BATCH):
            chunk = parent_ids[start:start + NAMES_PER_BATCH]
            body = _call(node, "get_coin_records_by_names",
                         {"names": ["0x" + c for c in chunk], "include_spent_coins": True}, deadline)
            if body.get("success") is False:
                raise ValueError(body.get("error") or "get_coin_records_by_names refused")
            wanted = set(chunk)
            for rec in body.get("coin_records") or []:
                # Keyed by the id recomputed from the record, never by position: a record the
                # node returns for a coin we did not ask about is dropped.
                cid = _coin_id_hex((rec or {}).get("coin") or {})
                if cid in wanted:
                    out[cid] = rec
        return out
    except NameBudgetExceeded:
        raise
    except Exception:  # noqa: BLE001 -- a node without the batch route: one by one, capped
        out = {}
        for parent_hex in parent_ids[:MAX_SINGLE_LOOKUPS]:
            rec = _call(node, "get_coin_record_by_name", {"name": "0x" + parent_hex}, deadline).get("coin_record")
            if rec:
                out[parent_hex] = rec
        return out


def _memos_of(node: str, coin_id_hex: str, spent_height: int, target_ph_hex: str, target_amount: int,
              deadline: float | None = None) -> list[bytes]:
    """The memos on the CREATE_COIN in `coin_id`'s spend that made (target_ph, target_amount)."""
    cs = _call(node, "get_puzzle_and_solution", {"coin_id": "0x" + coin_id_hex, "height": int(spent_height)}, deadline)["coin_solution"]
    # The node answered with somebody's spend: no back-references, and a cost cap (audit U3).
    puzzle = parse_untrusted_hex(cs["puzzle_reveal"])
    solution = parse_untrusted_hex(cs["solution"])
    for cond in run_capped(puzzle, solution).as_iter():
        items = list(cond.as_iter())
        if items[0].atom is None or items[0].as_int() != CREATE_COIN or len(items) < 4:
            continue
        if items[1].as_atom().hex() == target_ph_hex and items[2].as_int() == target_amount:
            return [m.as_atom() for m in items[3].as_iter()]
    return []


def _text(memo: bytes) -> str | None:
    if len(memo) == 32 or not memo:
        return None
    try:
        return memo.decode("utf-8")
    except UnicodeDecodeError:
        return None


def deployer_puzzle_hash(launcher_parent_hex: str, node: str = DEFAULT_NODE, deadline: float | None = None) -> str:
    """The address that paid for the launcher: the launcher parent's puzzle hash."""
    rec = _call(node, "get_coin_record_by_name", {"name": "0x" + launcher_parent_hex}, deadline)["coin_record"]
    return _strip(rec["coin"]["puzzle_hash"])


def genesis_name(launcher_parent_hex: str, node: str = DEFAULT_NODE,
                 deadline: float | None = None) -> tuple[str | None, str | None]:
    """(name, symbol) from the launcher's creation memos, either may be None."""
    rec = _call(node, "get_coin_record_by_name", {"name": "0x" + launcher_parent_hex}, deadline)["coin_record"]
    if not rec.get("spent"):
        return None, None
    memos = _memos_of(node, launcher_parent_hex, rec["spent_block_index"], LAUNCHER_HASH_HEX, 1, deadline)
    texts = [t for t in (_text(m) for m in memos) if t]
    return (texts[0] if texts else None), (texts[1] if len(texts) > 1 else None)


def renames(launcher_id_hex: str, deployer_ph_hex: str, node: str = DEFAULT_NODE,
            deadline: float | None = None) -> list[tuple[int, str, str | None]]:
    """[(height, name, symbol)] of the renames the deployer made, oldest first -- among the
    newest MAX_HINTED_COINS hinted coins at the deployer's address (module docstring)."""
    recs = _call(node, "get_coin_records_by_hint",
                 {"hint": "0x" + launcher_id_hex, "include_spent_coins": True}, deadline).get("coin_records", [])
    # Only coins AT the deployer's address can be renames; the newest first, capped.
    at_deployer = [r for r in recs or [] if isinstance(r, dict)
                   and _strip((r.get("coin") or {}).get("puzzle_hash")) == deployer_ph_hex]
    at_deployer.sort(key=lambda r: int(r.get("confirmed_block_index") or 0), reverse=True)
    at_deployer = at_deployer[:MAX_HINTED_COINS]
    parent_ids = list(dict.fromkeys(_strip(r["coin"].get("parent_coin_info")) for r in at_deployer))
    parents = _parents_by_id(node, parent_ids, deadline)
    found: list[tuple[int, str, str | None]] = []
    reads = 0
    for r in at_deployer:
        coin = r["coin"]
        parent_hex = _strip(coin["parent_coin_info"])
        parent = parents.get(parent_hex)
        # A parent at the deployer's address is the deployer's own signed spend: dust cannot
        # make one. Only those cost a memo read, and at most MAX_RENAME_SPENDS of them.
        if not parent or _strip(parent["coin"]["puzzle_hash"]) != deployer_ph_hex or not parent.get("spent"):
            continue
        if reads >= MAX_RENAME_SPENDS:
            break
        reads += 1
        memos = _memos_of(node, parent_hex, parent["spent_block_index"], deployer_ph_hex, int(coin["amount"]), deadline)
        if len(memos) >= 2 and memos[0].hex() == launcher_id_hex:
            text = _text(memos[1])
            if text:
                found.append((int(r["confirmed_block_index"]), text, _text(memos[2]) if len(memos) > 2 else None))
    found.sort(key=lambda t: t[0])
    return found


def resolve(launcher_id_hex: str, launcher_parent_hex: str, node: str = DEFAULT_NODE,
            deadline: float | None = None) -> dict[str, Any]:
    """{name, source ('rename'|'genesis'|None), deployer_ph, genesis, renames}.
    Bounded by the pool's budget and the run's (module docstring): an exhausted budget
    raises NameBudgetExceeded, and the caller's record name stands in."""
    if deadline is None:
        deadline = min(RUN_DEADLINE, time.monotonic() + POOL_BUDGET_S)
    deployer = deployer_puzzle_hash(launcher_parent_hex, node, deadline)
    history = renames(launcher_id_hex, deployer, node, deadline)
    genesis, genesis_symbol = genesis_name(launcher_parent_hex, node, deadline)
    if history:
        height, name, symbol = history[-1]
        return {"name": name, "symbol": symbol, "source": "rename", "height": height, "deployer_ph": deployer,
                "genesis": genesis, "genesis_symbol": genesis_symbol, "renames": history}
    if genesis:
        return {"name": genesis, "symbol": genesis_symbol, "source": "genesis", "deployer_ph": deployer,
                "genesis": genesis, "genesis_symbol": genesis_symbol, "renames": []}
    return {"name": None, "symbol": None, "source": None, "deployer_ph": deployer, "genesis": None,
            "genesis_symbol": genesis_symbol, "renames": []}
