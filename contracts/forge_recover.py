#!/usr/bin/env python3
"""Rebuild a V16, V17, V18, V19 or V20 registry's record -- the registry at its tip and every pool it
admitted -- from the chain alone, given nothing but the registry's launcher id.

Why (owner, 2026-10-21): Railway went down while the only fresh copy of the mainnet router's
state was on Railway's volume. "we are suppose to be decentralized in the manner of being able
to be recreated from chain." Everything a router needs to list, quote and settle the pools, and
to create new ones, is on chain; this reads it back:

  * the registry: its launcher's parent, and from its first spend the constants curried into
    its leaves (creation fee, treasury, protocol recipient, price scale, oracle window; V17 also
    the pinned dev fee rate). The registry rebuilt from those must be the eve coin the launcher
    created.
  * every spend of the registry, in order, replayed through the registry's own leaves
    (`init`, `register`, `set_fee`). Each replayed state must curry to the puzzle hash of the
    coin the chain actually created next, or the walk stops: nothing here is guessed.
  * each `register` solution carries the pool's whole configuration, its genesis reserves,
    LP supply and DAO fee, and the grandparent of every reserve. The pool is rebuilt exactly as
    creation built it (forge_v16_create.plan / forge_v17_create.plan), and must hash to the
    pool eve coin its launcher created. Its slot updates are applied as creation's
    `record_patch` applies them.

The revision is read from the chain, not assumed: the registry's first spend runs one of its
leaves, and whichever build's registry leaves that module hash belongs to (V16 = protocol 17,
V17 = protocol 18) is the build every later step uses -- its driver, replay, snapshot and index.
V17 adds to what a registration carries: PoolConfig's tenth field (one hidden puzzle hash H per
asset; an rCAT reserve is born under CHIP-0038's revocation layer with that H), the pinned dev
fee rate as the registry's sixteenth constant, the eight-leaf pool root, and an issuer-proof list
(per rCAT: attested or not). A V16 registry is rebuilt exactly as before.

The output is a record in the deploy script's shape (`v16-<network>.json` / `v17-<network>.json`:
`registry` with its current coin, lineage, state and slots, and `pools` at genesis), which the
existing tooling already consumes: `forge_registry_record.py` checks it against the chain before
a router keeps it, `forge_v16_index.py` / `forge_v17_index.py` turn it into the deployment index
(names from the chain's memos), and the router replays each pool from genesis to its tip with
the verified replay (forge_v16_resync / forge_v17_resync). `--tips` does that replay here too,
so a recovery can be compared with a backup without a router.

Not on chain, so not recovered: the operator's name and logo overlay, the multisig board,
offers filed with the router, fee-ladder rows and API keys, and which registry succeeded which
(a rollover is an operator decision). See docs/FORGE_RECOVERY.md.

usage:
  python contracts/forge_recover.py --registry <launcher id> [--registry <retired launcher id> ...]
                                    [--network mainnet] [--node URL]
                                    [--out record.json] [--tips tips.json] [--cache DIR]

A network that rolled its registry over has pools under each: name the active registry first and
every registry it succeeded after it; those land under `retired_registries`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.wallet.lineage_proof import LineageProof  # noqa: E402
from chia_rs import Coin  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

import forge_v16_driver as drv  # noqa: E402  -- V16, and the helpers both builds share
try:   # optional: the public repository ships V18 and not the frozen V17
    import forge_v17_driver as drv17  # noqa: E402
except ImportError:
    drv17 = None
import forge_v18_driver as drv18  # noqa: E402
import forge_v19_driver as drv19  # noqa: E402
try:   # optional while V20 is unpublished (the public repository does not ship it yet)
    import forge_v20_driver as drv20  # noqa: E402
except ImportError:
    drv20 = None
from untrusted_clvm import run_capped  # noqa: E402

NODES = {"mainnet": "https://api.coinset.org", "testnet11": "https://testnet11.api.coinset.org"}
ZERO = bytes32(b"\x00" * 32)
TOP = bytes32(b"\xff" * 32)
MAX_REGISTRY_SPENDS = 100_000


class RecoverError(Exception):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


# ---- revisions -------------------------------------------------------------------------------------

class Revision:
    """One build's registry leaves and the modules that rebuild, replay, snapshot and index its
    pools. The lane modules are imported when first used, as the V16-only tool always did."""

    def __init__(self, tag: str, driver, config_fields: int) -> None:
        self.tag, self.drv = tag, driver
        self.protocol = int(driver.PROTOCOL_VERSION)
        self.config_fields = config_fields          # PoolConfig: 9 fields in V16, 10 in V17 (the H list)
        self.registry_leaves = ({bytes(driver.REGISTRY_LEAF_MODS[n].get_tree_hash()): n for n in driver.REGISTRY_LEAF_ORDER}
                                if driver.REGISTRY_LEAF_MODS else {})

    @property
    def is_v17(self) -> bool:
        """V17-shaped: the ten-field PoolConfig (H list), the pinned dev fee constant, the eight-leaf
        root and issuer proofs. True for V17 AND V18 (V18 = V17 plus one register rule); the
        name is kept from the V17 port. Never use it to pick a module: `_lane` keys those by tag."""
        return self.tag in ("v17", "v18", "v19", "v20")   # V19 (protocol 20) and V20 (21): the same shape again

    def _lane(self, kind: str):
        import importlib
        return importlib.import_module(f"forge_{self.tag}_{kind}")

    def replay_spend(self, pool, solution: Program):
        return self._lane("resync").replay_spend(pool, solution)

    def pool_to_snapshot(self, pool) -> dict:
        return self._lane("offer").pool_to_snapshot(pool)

    def snapshot_to_pool(self, snapshot: dict):
        return self._lane("offer").snapshot_to_pool(snapshot)

    def index_entries(self, record: dict, resolve: bool):
        return self._lane("index").entries(record, resolve=resolve)


V16 = Revision("v16", drv, config_fields=9)
V17 = Revision("v17", drv17, config_fields=10) if drv17 is not None else None
V18 = Revision("v18", drv18, config_fields=10)     # V17 plus one register rule (2026-11-13): same shape, own leaves
V19 = Revision("v19", drv19, config_fields=10)     # V18 plus review 2026-11-16 findings 08/09: same shape, own leaves
V20 = Revision("v20", drv20, config_fields=10) if drv20 is not None else None   # V19 plus a positive effective term on every add: same shape, own leaves
REVISIONS = tuple(rev for rev in (V16, V17, V18, V19, V20) if rev is not None)


def revision_of_pool(pool) -> Revision:
    if drv20 is not None and isinstance(pool, drv20.V20Pool):
        return V20
    if isinstance(pool, drv19.V19Pool):
        return V19
    if isinstance(pool, drv18.V18Pool):
        return V18
    return V17 if drv17 is not None and isinstance(pool, drv17.V17Pool) else V16


def snapshot_of(pool) -> dict:
    """A recovered pool's snapshot, written by its own revision's lane."""
    return revision_of_pool(pool).pool_to_snapshot(pool)


# ---- reading the chain ----------------------------------------------------------------------

class Reader:
    """Node RPC with a disk cache for what the chain can never change: a spent coin's record
    and any spend's puzzle and solution. An unspent coin is always asked again."""

    def __init__(self, node_url: str, cache_dir: Path | None = None) -> None:
        self.node = node_url.rstrip("/")
        self.cache = cache_dir
        self.calls = 0
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

    def _rpc(self, route: str, body: dict) -> dict:
        self.calls += 1
        req = urllib.request.Request(f"{self.node}/{route}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "aWizard-Forge-recover/1.0"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as response:
                    out = json.loads(response.read())
                break
            except Exception:  # noqa: BLE001
                if attempt == 3:
                    raise
                import time
                time.sleep(2 * (attempt + 1))
        if out.get("success") is False:
            raise RecoverError(f"node {route}: {out.get('error')}", "NODE_ERROR")
        return out

    def _cached(self, key: str, fetch, keep) -> Any:
        path = self.cache / f"{key}.json" if self.cache else None
        if path and path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        value = fetch()
        if path and keep(value):
            path.write_text(json.dumps(value), encoding="utf-8")
        return value

    def record(self, coin_id: bytes) -> dict | None:
        return self._cached(f"rec-{coin_id.hex()}",
                            lambda: self._rpc("get_coin_record_by_name", {"name": "0x" + coin_id.hex()}).get("coin_record"),
                            lambda r: bool(r and r.get("spent")))

    def children(self, coin_id: bytes) -> list[dict]:
        rec = self.record(coin_id)
        return self._cached(f"kids-{coin_id.hex()}",
                            lambda: self._rpc("get_coin_records_by_parent_ids",
                                              {"parent_ids": ["0x" + coin_id.hex()], "include_spent_coins": True})["coin_records"],
                            lambda _v: bool(rec and rec.get("spent")))

    def spend(self, coin_id: bytes, height: int) -> tuple[Program, Program]:
        value = self._cached(f"spend-{coin_id.hex()}",
                             lambda: self._rpc("get_puzzle_and_solution", {"coin_id": "0x" + coin_id.hex(), "height": height})["coin_solution"],
                             lambda _v: True)
        return Program.fromhex(_strip(value["puzzle_reveal"])), Program.fromhex(_strip(value["solution"]))


def _strip(text: str) -> str:
    return text[2:] if str(text).lower().startswith("0x") else str(text)


def _coin(record: dict) -> Coin:
    c = record["coin"]
    return Coin(bytes32.fromhex(_strip(c["parent_coin_info"])), bytes32.fromhex(_strip(c["puzzle_hash"])), uint64(int(c["amount"])))


def _coin_json(coin: Coin) -> dict:
    return {"parent_coin_info": coin.parent_coin_info.hex(), "puzzle_hash": coin.puzzle_hash.hex(), "amount": int(coin.amount)}


def _singleton_child(reader: Reader, coin_id: bytes, puzzle_hash: bytes | None = None) -> dict:
    """The odd-amount child of a spent singleton coin (the next generation)."""
    kids = [k for k in reader.children(coin_id) if int(k["coin"]["amount"]) % 2 == 1]
    if puzzle_hash is not None:
        kids = [k for k in kids if bytes.fromhex(_strip(k["coin"]["puzzle_hash"])) == bytes(puzzle_hash)]
    if len(kids) != 1:
        raise RecoverError(f"coin {coin_id.hex()[:12]} has {len(kids)} matching singleton children", "LINEAGE_BROKEN")
    return kids[0]


# ---- the action layer's solution --------------------------------------------------------------

def actions(inner_solution: Program) -> list[tuple[Program, Program]]:
    """(leaf program, leaf solution) per action, in execution order.

    The action layer picks each leaf by RUNNING its selector with the revealed `puzzles` as the
    environment, so this does the same (forge_v16_resync and
    forge_v17_resync.replay_spend do likewise). The driver writes selectors as the paths 2, 5,
    11, ... over a flat list, but the puzzle enforces neither that nor the list's shape: a
    selector `(q . leaf)` returns the leaf whatever `puzzles` holds, and consensus accepts it.
    Decoding the driver's convention instead left such a spend unreadable. Callers identify the
    leaf by its module hash, never by its position."""
    puzzles, selectors, solutions = list(inner_solution.as_iter())[:3]
    try:
        selector_list = [entry.first() for entry in selectors.as_iter()][::-1]
    except Exception as exc:  # noqa: BLE001 -- an entry that is not a pair cannot have run on chain
        raise RecoverError("a spend has a selector entry that is not a pair", "UNKNOWN_LEAF") from exc
    solution_list = list(solutions.as_iter())
    if len(selector_list) != len(solution_list):
        raise RecoverError("a spend's selectors and solutions do not match", "UNKNOWN_LEAF")
    out = []
    for selector, solution in zip(selector_list, solution_list):
        try:
            leaf = run_capped(selector, puzzles, drv.MAX_COST)
        except Exception as exc:  # noqa: BLE001 -- a selector the action layer could not have run either
            raise RecoverError("a spend names a leaf selector that does not resolve", "UNKNOWN_LEAF") from exc
        out.append((leaf, solution))
    return out


def _mod_hash(leaf: Program) -> bytes | None:
    try:
        return bytes(leaf.uncurry()[0].get_tree_hash())
    except Exception:  # noqa: BLE001 -- a selector may return something that is not a curried program
        return None


def revisions_of_leaf(leaf: Program) -> list[Revision]:
    """Every build whose registry commits to this leaf's module (a leaf two builds share names both)."""
    mod_hash = _mod_hash(leaf)
    return [rev for rev in REVISIONS if mod_hash is not None and mod_hash in rev.registry_leaves]


REGISTRY_LEAVES = V16.registry_leaves      # the V16-only tool's table, kept for its callers


# ---- the registry --------------------------------------------------------------------------------

def registry_genesis(reader: Reader, launcher_id: bytes32) -> tuple[Any, dict, Revision]:
    """The registry's eve, rebuilt from its launcher and the constants its first spend reveals,
    and the revision whose registry leaves that spend runs."""
    launcher = reader.record(launcher_id)
    if not launcher:
        raise RecoverError(f"launcher {launcher_id.hex()} is not on chain", "LAUNCHER_NOT_FOUND")
    launcher_parent = bytes32.fromhex(_strip(launcher["coin"]["parent_coin_info"]))
    eve = _singleton_child(reader, launcher_id)
    if not eve.get("spent"):
        raise RecoverError("the registry was never initialized (its eve is unspent)", "NOT_INITIALIZED")
    _puzzle, solution = reader.spend(_coin(eve).name(), int(eve["spent_block_index"]))
    leaf, _sol = actions(list(solution.as_iter())[2])[0]
    # `init` is byte-identical in V16 and V17 (only register and set_fee changed), so the leaf's
    # module names candidates, not the revision: the revision is the one whose registry, rebuilt
    # from these constants, IS the eve the launcher created.
    candidates = revisions_of_leaf(leaf)
    if not candidates:
        raise RecoverError("the registry's first spend runs no registry leaf of any build this tool knows "
                           f"({', '.join(r.tag for r in REVISIONS)}; another revision's registry?)", "WRONG_REVISION")
    _mod, args = leaf.uncurry()
    constants = list(list(args.as_iter())[0].as_iter())
    creation_fee = constants[7].as_int()
    treasury_ph = bytes32(constants[8].as_atom())
    protocol_ph = bytes32(constants[11].as_atom())
    price_scale, oracle_window = constants[12].as_int(), constants[13].as_int()
    rev, reg, extra = None, None, {}
    for candidate in candidates:
        extra = {}
        if candidate.is_v17:
            if len(constants) < 16:
                continue
            # V17 (pinned dev fee): the dev fee rate every pool must carry is the registry's sixteenth constant
            extra["protocol_fee_bps"] = constants[15].as_int()
        built = candidate.drv.make_registry(creation_fee=creation_fee, treasury_ph=treasury_ph, launcher_parent=launcher_parent,
                                            state=[0, 0, 0], protocol_ph=protocol_ph, price_scale=price_scale,
                                            oracle_window=oracle_window, **extra)
        if built.launcher_id != launcher_id:
            raise RecoverError("the launcher's parent does not derive this launcher id", "LAUNCHER_MISMATCH")
        if built.coin == _coin(eve):
            rev, reg = candidate, built
            break
    earlier_leaves = False
    if rev is None:
        # An EARLIER build of a revision's registry: testnet's first V17 registries predate later
        # changes to `register` (DAO consent, issuer proofs), so their root is not this build's.
        # Their pools are this build's pools all the same -- the registry pins the pool build in
        # its constants -- so the registry is accepted when (a) every constant that names the
        # POOL build (action layer, reserve, slot, finalizer, amount and TAIL mods, the pool leaf
        # mods, the reserve launcher) is this build's, and (b) the registry is this build's
        # finalizer and action layer over the root the eve's own puzzle reveals, which must hash
        # to the eve. Its spends then run the leaves they reveal (consensus proved each is in that
        # root), and every replayed state is still checked against the coin the chain created.
        for candidate in candidates:
            extra = {"protocol_fee_bps": constants[15].as_int()} if candidate.is_v17 and len(constants) >= 16 else {}
            if candidate.is_v17 and not extra:
                continue
            built = candidate.drv.make_registry(creation_fee=creation_fee, treasury_ph=treasury_ph,
                                                launcher_parent=launcher_parent, state=[0, 0, 0], protocol_ph=protocol_ph,
                                                price_scale=price_scale, oracle_window=oracle_window, **extra)
            mine = list(Program.to(built.constants).as_iter())
            if len(mine) != len(constants) or any(mine[i] != constants[i] for i in (0, 1, 2, 3, 4, 5, 6, 14)):
                continue
            root = _revealed_root(_puzzle, candidate)
            if root is None:
                continue
            inner = candidate.drv.ACTION_LAYER.curry(built.finalizer, root, Program.to([0, 0, 0]))
            from chia.wallet.puzzles.singleton_top_layer_v1_1 import puzzle_for_singleton
            if puzzle_for_singleton(launcher_id, inner).get_tree_hash() != _coin(eve).puzzle_hash:
                continue
            rev, earlier_leaves = candidate, True
            reg = replace(built, tree=_Root(root), inner=inner, coin=_coin(eve))
            break
    if rev is None:
        raise RecoverError("the registry rebuilt from its curried constants is not the eve the launcher created "
                           f"with any of {', '.join(r.tag for r in candidates)} (another revision's registry?)", "WRONG_REVISION")
    facts = {"launcher_parent": launcher_parent.hex(), "launcher_id": launcher_id.hex(), "creation_fee": creation_fee,
             "treasury_ph": treasury_ph.hex(), "protocol_ph": protocol_ph.hex(), "price_scale": price_scale,
             "oracle_window": oracle_window, **extra, "genesis": {"height": int(launcher["confirmed_block_index"])}}
    if earlier_leaves:
        # Registry facts the record never held: kept out of `facts`, reported through the walk
        reg = _EarlierBuild(reg)
    return reg, facts, rev


class _Root:
    """A merkle root known without its leaves (an earlier build's registry): Registry reads `.root`."""

    def __init__(self, root: bytes) -> None:
        self.root = bytes(root)


class _EarlierBuild:
    """Marks a registry whose leaves are an earlier build's: its spends run the leaves they reveal."""

    def __init__(self, reg) -> None:
        self.reg = reg


def _revealed_root(puzzle: Program, rev: Revision) -> bytes | None:
    """The merkle root curried into a registry coin's revealed singleton puzzle, if its inner is
    this build's action layer."""
    try:
        _mod, args = puzzle.uncurry()
        inner = list(args.as_iter())[1]
        layer, inner_args = inner.uncurry()
        if layer.get_tree_hash() != rev.drv.ACTION_LAYER.get_tree_hash():
            return None
        return bytes(list(inner_args.as_iter())[1].as_atom())
    except Exception:  # noqa: BLE001
        return None


def _slot(key: bytes, launcher: bytes, left: bytes, right: bytes, parent: Coin, parent_inner: bytes) -> dict:
    return {"key": key.hex(), "launcher_id": launcher.hex(), "left": left.hex(), "right": right.hex(),
            "parent": _coin_json(parent), "parent_inner_hash": parent_inner.hex()}


def walk_registry(reader: Reader, launcher_id: bytes32, progress=None) -> tuple[Any, dict, dict, list]:
    """Replay every registry spend. Returns (registry at tip, facts, slots, registrations). Each
    registration carries the registry's revision under "revision"."""
    reg, facts, rev = registry_genesis(reader, launcher_id)
    earlier = isinstance(reg, _EarlierBuild)
    facts = {**facts, "_earlier_build": earlier}     # popped by recover(); never written to a record
    if earlier:
        reg = reg.reg
        if progress:
            progress(f"registry {launcher_id.hex()[:12]} is an earlier {rev.tag} registry build: replaying the leaves it reveals")
    slots: dict[str, dict] = {}
    registrations: list[dict] = []
    for _ in range(MAX_REGISTRY_SPENDS):
        rec = reader.record(reg.coin.name())
        if rec is None:
            raise RecoverError(f"registry coin {reg.coin.name().hex()[:12]} is not on chain", "LINEAGE_BROKEN")
        if not rec.get("spent"):
            return reg, facts, slots, registrations
        height = int(rec["spent_block_index"])
        _puzzle, solution = reader.spend(reg.coin.name(), height)
        state, spent = reg.state, reg.coin
        names = []
        for leaf, leaf_solution in actions(list(solution.as_iter())[2]):
            name = rev.registry_leaves.get(_mod_hash(leaf))
            if earlier:
                # consensus proved the revealed leaf is in this registry's root; run it, and name an
                # earlier build's leaf by what it did to RegistryState (initialized, pool_count, fee)
                out = run_capped(leaf, Program.to(((None, state), leaf_solution)), drv.MAX_COST)
                new_state = [int(x.as_int()) for x in out.first().rest().as_iter()]
                if name is None:
                    if state[0] == 0 and new_state[0] == 1:
                        name = "forge_registry_init"
                    elif new_state[1] == state[1] + 1:
                        name = "forge_registry_register"
                    elif new_state[1] == state[1]:
                        name = "forge_registry_set_fee"
                    else:
                        raise RecoverError("an earlier-build registry leaf changed the state in no known way", "UNKNOWN_LEAF")
            else:
                if name is None:
                    raise RecoverError("a registry spend runs a leaf this registry does not commit to", "UNKNOWN_LEAF")
                # the registry's OWN curried leaf runs, not the revealed one: consensus proved the
                # revealed leaf is in this registry's root, so the two are the same program
                out = run_capped(reg.leaves[name], Program.to(((None, state), leaf_solution)), drv.MAX_COST)
                new_state = [int(x.as_int()) for x in out.first().rest().as_iter()]
            names.append(name)
            if name == "forge_registry_init":
                pass   # the sentinel slots are written below, against the coin this spend consumed
            elif name == "forge_registry_register":
                registrations.append({"solution": leaf_solution, "height": height, "registry_coin": spent,
                                      "registry_inner": reg.inner_hash, "revision": rev, "earlier_registry": earlier})
            state = new_state
        after = reg.advance(state)
        child = _singleton_child(reader, spent.name())
        if _coin(child) != after.coin:
            raise RecoverError(f"replaying registry spend at {height} gives a coin the chain did not create", "REPLAY_DIVERGED")
        # Slots, as creation's record_patch writes them: each points at the registry coin whose
        # spend created it, and that coin's inner hash.
        if "forge_registry_init" in names:
            slots[ZERO.hex()] = _slot(ZERO, ZERO, ZERO, TOP, spent, reg.inner_hash)
            slots[TOP.hex()] = _slot(TOP, ZERO, ZERO, TOP, spent, reg.inner_hash)
        for item in [r for r in registrations if r["registry_coin"] == spent]:
            sol = list(item["solution"].as_iter())
            config = list(sol[1].as_iter())
            # V16 keys its first nine config fields; V17's key adds the H list (field 10), and V17's
            # register refuses a config of any other length
            key = bytes32(rev.drv.pool_key([config[i] for i in range(rev.config_fields)]))
            left_key, right_key = bytes32(sol[9].as_atom()), bytes32(sol[10].as_atom())
            pool_launcher = drv.coin_id(bytes32(sol[0].as_atom()), drv.SINGLETON_LAUNCHER_HASH, 1)
            if left_key.hex() not in slots or right_key.hex() not in slots:
                raise RecoverError("a registration brackets slots the replay has not seen", "SLOTS_DIVERGED")
            slots[key.hex()] = _slot(key, pool_launcher, left_key, right_key, spent, reg.inner_hash)
            slots[left_key.hex()] = {**slots[left_key.hex()], "right": key.hex(), "parent": _coin_json(spent), "parent_inner_hash": reg.inner_hash.hex()}
            slots[right_key.hex()] = {**slots[right_key.hex()], "left": key.hex(), "parent": _coin_json(spent), "parent_inner_hash": reg.inner_hash.hex()}
            item["key"] = key
        reg = after
        if progress:
            progress(f"registry spend at {height}: {'+'.join(n or '?' for n in names)}")
    raise RecoverError("the registry has more spends than this tool walks", "WALK_TOO_DEEP")


# ---- the pools ------------------------------------------------------------------------------------

def _atom32(p: Program) -> bytes32:
    return bytes32(p.as_atom())


def pool_from_registration(reader: Reader, item: dict) -> tuple[Any, dict]:
    """The pool at genesis, rebuilt as creation built it (forge_v16_create.plan /
    forge_v17_create.plan), proven against its eve coin."""
    rev: Revision = item.get("revision") or V16
    pdrv = rev.drv
    sol = list(item["solution"].as_iter())
    launcher_parent = _atom32(sol[0])
    config = list(sol[1].as_iter())
    asset_ids = [None if bytes(a.as_atom()) == bytes(ZERO) else bytes32(a.as_atom()) for a in config[0].as_iter()]
    weights = [w.as_int() for w in config[1].as_iter()]
    fee_bps, protocol_fee_bps = config[2].as_int(), config[3].as_int()
    protocol_ph, lp_asset_id, dao_ph = _atom32(config[4]), _atom32(config[5]), _atom32(config[8])
    reserves = [r.as_int() for r in sol[2].as_iter()]
    total_lp, dao_fee_bps = sol[3].as_int(), sol[4].as_int()
    grandparents = [_atom32(g) for g in sol[5].as_iter()]
    common = dict(total_lp=total_lp, leaves="forge", weights=weights, fee_bps=fee_bps, protocol_fee_bps=protocol_fee_bps,
                  protocol_ph=protocol_ph, dao_ph=dao_ph, dao_fee_bps=dao_fee_bps, launcher_parent=launcher_parent)
    hiddens: list = []
    if rev.is_v17:
        # V17 (rCAT): PoolConfig field 10, one hidden puzzle hash per asset (zero = a plain asset)
        hiddens = [_atom32(h) for h in config[9].as_iter()]
        common["hidden_puzzle_hashes"] = hiddens
    shape = pdrv.make_pool(asset_ids, reserves, **common)
    reserve_coins = []
    for i, asset in enumerate(asset_ids):
        if asset is None:
            launcher = Coin(grandparents[i], pdrv.RESERVE_LAUNCHER_HASH, uint64(reserves[i]))
            reserve_coins.append((Coin(launcher.name(), shape.reserves[i].inner_hash, uint64(reserves[i])), None, grandparents[i]))
        elif rev.is_v17:
            # V17 (rCAT): the launcher sits under the revocation layer with the asset's H, so its
            # puzzle hash and the reserve's lineage name the LAYERED launcher (forge_v17_create.plan);
            # for a plain CAT (H zero) `layered` is the launcher itself, as in V16
            h = hiddens[i]
            launcher = Coin(grandparents[i], pdrv.reserve_launcher_full_hash(asset, h), uint64(reserves[i]))
            reserve_coins.append((Coin(launcher.name(), shape.reserves[i].full_hash, uint64(reserves[i])),
                                  LineageProof(grandparents[i], pdrv.layered(h, pdrv.RESERVE_LAUNCHER).get_tree_hash(),
                                               uint64(reserves[i])), grandparents[i]))
        else:
            launcher = Coin(grandparents[i], pdrv.reserve_launcher_full_hash(asset), uint64(reserves[i]))
            reserve_coins.append((Coin(launcher.name(), shape.reserves[i].full_hash, uint64(reserves[i])),
                                  LineageProof(grandparents[i], pdrv.RESERVE_LAUNCHER_HASH, uint64(reserves[i])), grandparents[i]))
    pool = pdrv.make_pool(asset_ids, reserves, reserve_coins=reserve_coins, **common)
    if pool.lp_asset_id != lp_asset_id:
        raise RecoverError(f"pool {pool.launcher_id.hex()[:12]}: rebuilt LP id differs from the registered config", "POOL_MISMATCH")
    eve = _singleton_child(reader, pool.launcher_id)
    if _coin(eve) != pool.coin:
        raise RecoverError(f"pool {pool.launcher_id.hex()[:12]}: rebuilt genesis is not the eve its launcher created", "POOL_MISMATCH")
    birth = int(eve["confirmed_block_index"])
    pool = replace(pool, birth=birth)
    st = pool.state
    v17_fields = {} if not rev.is_v17 else {
        # V17 (rCAT): as forge_v17_create.record_patch writes them -- the H list (left out, the
        # index would rebuild a plain pool) and, per asset, None (plain) / True (registered with
        # an issuer proof, which register enforces in full) / False (registered without one)
        "hidden_puzzle_hashes": [bytes(h).hex() for h in pool.hiddens],
        # an earlier V17 registry build's register took no issuer proofs (eleven fields)
        "issuer_attested": pdrv.issuer_attested(pool.hiddens, sol[11] if len(sol) > 11 else [None] * len(pool.hiddens)),
        "protocol_version": rev.protocol,
    }
    record = {
        "label": pool.launcher_id.hex()[:12], "emoji": "", "symbol": "",
        "key": item["key"].hex(), "lp_recipient_ph": lp_recipient(reader, sol, pool),
        "registry_launcher_id": None,   # filled by the caller
        "launcher_parent": pool.launcher_parent.hex(), "launcher_id": pool.launcher_id.hex(), "lp_asset_id": pool.lp_asset_id.hex(),
        "asset_ids": [None if a is None else a.hex() for a in pool.asset_ids], "weights": pool.weights,
        "fee_bps": pool.fee_bps, "protocol_fee_bps": pool.protocol_fee_bps, "protocol_ph": pool.protocol_ph.hex(),
        "dao_ph": pool.dao_ph.hex(), "dao_fee_bps": int(st[5]),
        **v17_fields,
        "state": [st[0], st[1], st[2], st[3], bytes(st[4]).hex(), int(st[5]), list(st[6]), [bytes(x).hex() for x in st[7]]],
        "birth": birth,
        "coin": _coin_json(pool.coin),
        "lineage": {"parent_name": pool.lineage.parent_name.hex(), "inner_puzzle_hash": None, "amount": 1},
        "reserves": [{"coin": _coin_json(r.coin),
                      "lineage": None if r.lineage is None else {"parent_name": r.lineage.parent_name.hex(),
                                                                 "inner_puzzle_hash": r.lineage.inner_puzzle_hash.hex(),
                                                                 "amount": int(r.lineage.amount)}}
                     for r in pool.reserves],
        "history": [{"height": item["height"], "via": "chain-recover"}], "created_by": "chain-recover",
    }
    return pool, record


SKIP_HINTS = {bytes(drv.OFFER_MOD_HASH), bytes.fromhex("00" * 30 + "dead")}


def _hints_created(reader: Reader, coin_id: bytes32) -> list[bytes]:
    """The first memo (the hint) of every CREATE_COIN the coin's spend made."""
    rec = reader.record(coin_id)
    if not rec or not rec.get("spent"):
        return []
    puzzle, solution = reader.spend(coin_id, int(rec["spent_block_index"]))
    out = []
    for cond in run_capped(puzzle, solution, drv.MAX_COST).as_iter():
        items = list(cond.as_iter())
        if items and items[0].as_int() == 51 and len(items) > 3:
            memos = list(items[3].as_iter())
            if memos and len(memos[0].as_atom()) == 32:
                out.append(memos[0].as_atom())
    return out


def lp_recipient(reader: Reader, sol: list, pool) -> str | None:
    """Who the genesis LP was minted to. Display only; None when it cannot be read.

    The LP eve does not pay the creator directly: it creates a settlement (OFFER_MOD) coin, and
    burns the floor to 0x...dead, and the settlement coin pays the recipient
    (drv.genesis_lp_settlement_spends). Reading the eve's own hint gave the settlement puzzle
    hash for every pool (graded against Railway's record, 2026-10-22), so the walk goes one hop
    further: the first hint that is neither the settlement puzzle nor the burn. A pool whose
    whole genesis LP was burned (18 of mainnet's 21) has no other recipient: that is the burn."""
    burn = bytes.fromhex("00" * 30 + "dead")
    try:
        eve_id = _atom32(sol[6])
        seen = list(_hints_created(reader, eve_id))
        for kid in reader.children(eve_id):
            seen += _hints_created(reader, _coin(kid).name())
        found = next((h for h in seen if h not in SKIP_HINTS), None)
        if found is not None:
            return found.hex()
        if burn in seen:
            return burn.hex()
    except Exception:  # noqa: BLE001
        return None
    return None


def replay_to_tip(reader: Reader, pool, rev: Revision | None = None) -> tuple[Any, int]:
    """Walk the pool from genesis to its unspent tip through its own leaves (its revision's
    resync replay), every step checked against the coin the chain created."""
    rev = rev or revision_of_pool(pool)
    steps = 0
    while True:
        rec = reader.record(pool.coin.name())
        if rec is None:
            raise RecoverError(f"pool coin {pool.coin.name().hex()[:12]} is not on chain", "TIP_UNKNOWN")
        if not rec.get("spent"):
            return replace(pool, birth=int(rec["confirmed_block_index"])), steps
        _puzzle, solution = reader.spend(pool.coin.name(), int(rec["spent_block_index"]))
        try:
            pool, _names = rev.replay_spend(pool, solution)
        except RecoverError:
            raise
        except Exception as exc:  # noqa: BLE001 -- the resync module's own refusal, kept with its code
            raise RecoverError(f"the pool spend at {rec['spent_block_index']} does not replay: {exc}",
                               getattr(exc, "code", "REPLAY_FAILED")) from exc
        steps += 1


def recover_pool(reader: Reader, registry_ids: list[bytes32], pool_launcher: bytes32) -> tuple[Any, int]:
    """One pool at its unspent tip, rebuilt from the chain alone: its registration in one of
    `registry_ids` (the router's registry and any it rolled over from), proven against its eve,
    then replayed through every spend to the tip. V16 and V17 registries alike: the pool comes
    back as its own revision's pool (snapshot it with `snapshot_of`).

    This is how a router's index recovers a pool whose indexed coin never existed -- a push
    the mempool accepted and then dropped (mainnet ❤️ XCH, 2026-10-23): the replay a resync runs
    starts from the coin the router holds, and there was no such coin to start from."""
    for registry_id in registry_ids:
        _reg, _facts, _slots, registrations = walk_registry(reader, registry_id)
        for item in registrations:
            sol = list(item["solution"].as_iter())
            if drv.coin_id(bytes32(sol[0].as_atom()), drv.SINGLETON_LAUNCHER_HASH, 1) != pool_launcher:
                continue
            pool, _record = pool_from_registration(reader, item)
            return replay_to_tip(reader, pool, item.get("revision"))
    raise RecoverError(f"pool {pool_launcher.hex()[:12]} is registered in none of the router's registries", "NOT_REGISTERED")


# ---- a router-ready index ---------------------------------------------------------------------------

def tip_index(record: dict, tips: dict, resolve_names: bool = True, rev: Revision | None = None) -> tuple[dict, list]:
    """The deployment index a router lists and settles from, at each pool's recovered TIP.

    The revision's index module (forge_v16_index / forge_v17_index) builds the entries from the
    record (genesis state; names from the chain's launch memos); each entry's snapshot is then
    replaced by the tip the replay reached, proven again through JSON exactly as a router holds
    it. A pool without a tip keeps its genesis entry and the router's reconciler walks it forward."""
    rev = rev or V16
    plans, skipped = rev.index_entries(record, resolve_names)
    for plan in plans.values():
        batch = plan["batches"]["0"]
        tip = (tips.get(batch["launcherCoinId"]) or {}).get("snapshot")
        if not tip:
            continue
        rev.snapshot_to_pool(json.loads(json.dumps(tip)))   # the tip must prove itself as stored
        state_json = json.loads(batch["poolStateJson"])
        for asset, reserve in zip(state_json["assets"], tip["state"]["reserves"]):
            asset["reserve"] = str(reserve)
        state_json["total_lp"] = str(tip["total_lp"])
        batch.update({
            "poolSnapshot": tip,
            "currentCoinId": tip["pool_coin_id"], "currentPuzzleHash": tip["pool_coin"]["puzzle_hash"],
            "bootstrapTargetCoinId": tip["pool_coin_id"], "bootstrapTargetPuzzleHash": tip["pool_coin"]["puzzle_hash"],
            "lpOut": str(tip["total_lp"]), "poolStateJson": json.dumps(state_json), "txId": "chain-recover",
        })
    return plans, skipped


def as_backup(network: str, plans: dict, launcher_id: str) -> dict:
    """The index wrapped as a router backup (api/admin/backup.js), so a router loads it with
    POST /api/admin/backup {action: "restore", snapshot}: the same normalization, revision
    check and safety backup every restore gets. No metadata overlay, no multisig board: the
    chain does not hold them."""
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return {"kind": "forge-responder-backup", "version": 1, "id": f"{stamp}-{launcher_id[:6]}",
            "createdAt": now.isoformat().replace("+00:00", "Z"), "reason": "chain-recover",
            "note": f"rebuilt from chain, registry {launcher_id[:12]}", "network": network,
            "pools": len(plans), "deploymentIndex": plans, "poolMetadata": None, "multisigIndex": None}


# ---- putting it together --------------------------------------------------------------------------

def _registry_json(reg, facts: dict, slots: dict) -> dict:
    return {**facts, "state": list(reg.state), "coin": _coin_json(reg.coin),
            "lineage": {"parent_name": reg.lineage.parent_name.hex(),
                        "inner_puzzle_hash": None if reg.lineage.inner_puzzle_hash is None else reg.lineage.inner_puzzle_hash.hex(),
                        "amount": int(reg.lineage.amount)},
            "slots": slots}


def recover(reader: Reader, launcher_id: bytes32 | list, network: str, tips: bool = False, progress=None) -> dict:
    """`launcher_id`: one registry, or a list -- the active registry first, then the registries it
    succeeded (their pools are recovered too; the registries land under `retired_registries`).
    Every registry must be of one revision: a record is read with one revision's tools."""
    launcher_ids = list(launcher_id) if isinstance(launcher_id, (list, tuple)) else [launcher_id]
    pools, tip_snapshots, problems, registries, revision = [], {}, [], [], None
    registration_count, earlier_builds = 0, []
    for registry_id in launcher_ids:
        reg, facts, slots, registrations = walk_registry(reader, registry_id, progress)
        if facts.pop("_earlier_build", False):
            earlier_builds.append(registry_id.hex())
        rev = registrations[0]["revision"] if registrations else registry_genesis(reader, registry_id)[2]
        if revision is not None and rev is not revision:
            raise RecoverError(f"registry {registry_id.hex()[:12]} is {rev.tag}, the first is {revision.tag}: "
                               "recover each revision's registries into their own record", "MIXED_REVISIONS")
        revision = rev
        registries.append(_registry_json(reg, facts, slots))
        registration_count += len(registrations)
        for item in registrations:
            try:
                pool, record = pool_from_registration(reader, item)
            except RecoverError as exc:
                problems.append({"registry": registry_id.hex(), "height": item["height"], "code": exc.code, "error": str(exc)})
                continue
            record["registry_launcher_id"] = registry_id.hex()
            pools.append(record)
            if progress:
                progress(f"pool {pool.launcher_id.hex()[:12]} recovered (registered at {item['height']})")
            if tips:
                try:
                    tip, steps = replay_to_tip(reader, pool, rev)
                    tip_snapshots[pool.launcher_id.hex()] = {"steps": steps, "snapshot": rev.pool_to_snapshot(tip)}
                except Exception as exc:  # noqa: BLE001
                    problems.append({"launcher_id": pool.launcher_id.hex(), "code": getattr(exc, "code", "REPLAY_FAILED"),
                                     "error": f"{type(exc).__name__}: {exc}"})
    now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    record = {"network": network, "registry": registries[0], "pools": pools,
              "log": [{"step": "recover", "at": now, "launcher_id": launcher_ids[0].hex(), "pools": len(pools),
                       "registry_spends": registration_count, "node_calls": reader.calls}]}
    if revision is not None and revision is not V16:
        record["log"][0]["protocol_version"] = revision.protocol
    if len(registries) > 1:
        record["retired_registries"] = registries[1:]
        record["log"][0]["retired_registries"] = [r.hex() for r in launcher_ids[1:]]
    if earlier_builds:
        record["log"][0]["earlier_build_registries"] = earlier_builds
    return {"record": record, "tips": tip_snapshots, "problems": problems, "revision": revision,
            "earlier_build_registries": earlier_builds}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--registry", required=True, action="append",
                    help="a registry's launcher id (hex); repeat for the registries the first one succeeded")
    ap.add_argument("--network", default=os.environ.get("FORGE_NETWORK", "mainnet"), choices=sorted(NODES))
    ap.add_argument("--node", default=None)
    ap.add_argument("--out", default=None, help="write the recovered record here")
    ap.add_argument("--tips", default=None, help="also replay every pool to its tip and write the snapshots here")
    ap.add_argument("--cache", default=None, help="cache immutable chain reads in this directory")
    ap.add_argument("--backup", default=None, help="write the tip index as a router backup (restorable) here; implies tips")
    args = ap.parse_args()
    if args.backup and not args.tips:
        args.tips = str(Path(args.backup).with_suffix(".tips.json"))
    reader = Reader(args.node or os.environ.get("FORGE_NODE_URL") or NODES[args.network], Path(args.cache) if args.cache else None)
    launchers = [bytes32.fromhex(_strip(r)) for r in args.registry]
    try:
        out = recover(reader, launchers, args.network, tips=bool(args.tips), progress=lambda m: print(m, file=sys.stderr))
    except RecoverError as exc:
        print(json.dumps({"success": False, "code": exc.code, "error": str(exc)}))
        return 1
    if args.out:
        Path(args.out).write_text(json.dumps(out["record"], indent=1), encoding="utf-8")
    if args.tips:
        Path(args.tips).write_text(json.dumps(out["tips"], indent=1), encoding="utf-8")
    indexed, skipped = None, []
    if args.backup:
        plans, skipped = tip_index(out["record"], out["tips"], rev=out["revision"])
        indexed = len(plans)
        Path(args.backup).write_text(json.dumps(as_backup(args.network, plans, launchers[0].hex())), encoding="utf-8")
        out["problems"].extend({"code": "INDEX_SKIPPED", **s} for s in skipped)
    reg = out["record"]["registry"]
    print(json.dumps({"success": not out["problems"], "network": args.network, "registry": reg["launcher_id"],
                      "revision": out["revision"].tag, "protocol_version": out["revision"].protocol,
                      "registry_tip": Coin(bytes32.fromhex(reg["coin"]["parent_coin_info"]), bytes32.fromhex(reg["coin"]["puzzle_hash"]),
                                           uint64(1)).name().hex(),
                      "registry_state": reg["state"], "pools": len(out["record"]["pools"]),
                      "retired_registries": [r["launcher_id"] for r in out["record"].get("retired_registries", [])],
                      "earlier_build_registries": out["earlier_build_registries"],
                      "slots": len(reg["slots"]), "tips": len(out["tips"]), "indexed": indexed, "problems": out["problems"],
                      "node_calls": reader.calls}, indent=1))
    return 0 if not out["problems"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
