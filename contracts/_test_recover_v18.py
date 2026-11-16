#!/usr/bin/env python3
"""forge_recover.py and the V18 replay it uses: what the chain recovery must read on V18 (protocol 19),
with a V17 registry beside it recovered as V17 (V17 and V18 share `init` and the pool shape).

Offline (no node): every chain read goes through a fake Reader holding the spends this suite
builds with the real drivers and checks with kit.validate (chia_rs consensus conditions).

  1. Revision detection. `init` is byte-identical in V16 and V18, so a registry's first spend
     names two candidate builds; the revision is the one whose rebuild IS the eve. A V16 and a
     V18 registry each come back as their own revision, with V18's sixteenth constant (the pinned
     dev fee rate) read back.
  2. An EARLIER build of a V18 registry (testnet's first V18 registries predate later `register`
     changes): rebuilt from the root its eve reveals, accepted only because every pool-build
     constant is this build's, and its spends replayed through the leaves they reveal.
  3. Selectors in the recovery's own decoder: a selector written as `(q . leaf)` resolves to the leaf,
     as it does on chain; the driver's 2, 5, 11 ... paths still resolve as before.
  4. rCAT `refund`: the finalizer adopts a replacement reserve (coinid(new_parent, the reserve's
     full hash, the recorded amount)) and writes ITS id as the reserve parent. forge_v18_resync
     .replay_spend must derive the same successor the driver built; with the adopt step removed
     it must not (the control that shows the step is load-bearing; R1-R5 on testnet11 diverged
     exactly there before the fix).
  5. A V18 pool rebuilt from a `register` solution (rCAT reserve under the revocation layer, the
     H list in config, issuer proofs absent) hashes to the pool the driver built.

Exit 0 all pass, 1 a failure.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

import forge_recover as fr  # noqa: E402
import forge_v16_driver as k16  # noqa: E402
try:   # optional: the public repository ships V18 and not the frozen V17; the V17 row skips there
    import forge_v17_driver as k17  # noqa: E402  -- V17 is live beside V18: the two must be told apart
except ImportError:
    k17 = None
import forge_v18_driver as kit  # noqa: E402
import forge_v18_resync as rs17  # noqa: E402
from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle  # noqa: E402
from chia.wallet.lineage_proof import LineageProof  # noqa: E402
from chia_rs import Coin  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

results: list[bool] = []


def check(label: str, good: bool, detail: str = "") -> None:
    results.append(bool(good))
    print(f"  [{'PASS' if good else 'FAIL'}] {label}{'  -- ' + detail if detail and not good else ''}")


def _coin_json(c: Coin) -> dict:
    return {"parent_coin_info": "0x" + c.parent_coin_info.hex(), "puzzle_hash": "0x" + c.puzzle_hash.hex(), "amount": int(c.amount)}


class FakeReader:
    """forge_recover.Reader over coins and spends held in memory."""

    def __init__(self) -> None:
        self.coins: dict[bytes, dict] = {}
        self.spends: dict[bytes, tuple[Program, Program]] = {}
        self.calls = 0

    def add(self, coin: Coin, spent_with=None, height: int = 10) -> None:
        self.coins[bytes(coin.name())] = {"coin": _coin_json(coin), "spent": spent_with is not None,
                                          "spent_block_index": height + 1 if spent_with is not None else 0,
                                          "confirmed_block_index": height}
        if spent_with is not None:
            self.spends[bytes(coin.name())] = spent_with

    def record(self, coin_id: bytes):
        return self.coins.get(bytes(coin_id))

    def children(self, coin_id: bytes):
        return [r for r in self.coins.values() if fr._strip(r["coin"]["parent_coin_info"]) == bytes(coin_id).hex()]

    def spend(self, coin_id: bytes, height: int):
        return self.spends[bytes(coin_id)]


def registry_on_chain(reg, kit_module) -> FakeReader:
    """A launched registry whose eve ran `init`: the launcher, the eve (spent) and its child."""
    bundle, new_state = kit_module.registry_spend(reg, "forge_registry_init", [])
    kit_module.validate(bundle)
    eve_spend = next(s for s in bundle.coin_spends if s.coin == reg.coin)
    reader = FakeReader()
    reader.add(Coin(reg.launcher_parent, kit_module.SINGLETON_LAUNCHER_HASH, uint64(1)), spent_with=(Program.to(0), Program.to(0)), height=5)
    reader.add(reg.coin, spent_with=(Program.from_bytes(bytes(eve_spend.puzzle_reveal)), Program.from_bytes(bytes(eve_spend.solution))))
    after = reg.advance([int(x.as_int()) for x in new_state.as_iter()])
    reader.add(after.coin, height=11)
    return reader


def case_revision_detection() -> None:
    print("\n1. revision detection: init is shared, the rebuild decides")
    check("setup: forge_registry_init is byte-identical in V16 and V18",
          k16.REGISTRY_LEAF_MODS["forge_registry_init"].get_tree_hash() == kit.REGISTRY_LEAF_MODS["forge_registry_init"].get_tree_hash())
    for tag, module, extra in (("v16", k16, {}), ("v17", k17, {"protocol_fee_bps": 7}), ("v18", kit, {"protocol_fee_bps": 7})):
        if module is None:
            print(f"  {tag}: skipped (not in this tree)")
            continue
        reg = module.make_registry(salt=0x31, creation_fee=123, **extra)
        reader = registry_on_chain(reg, module)
        got, facts, rev = fr.registry_genesis(reader, reg.launcher_id)
        check(f"a {tag} registry is recovered as {tag}", rev.tag == tag, rev.tag)
        check(f"  its eve rebuilt exactly (coin {reg.coin.name().hex()[:12]})", got.coin == reg.coin)
        if tag in ("v17", "v18"):
            check("  the pinned dev fee rate (constant 16) is read back", facts.get("protocol_fee_bps") == 7, str(facts.get("protocol_fee_bps")))
        else:
            check("  a V16 registry's facts gain no new field", "protocol_fee_bps" not in facts)
        tip, _f, _slots, regs = fr.walk_registry(reader, reg.launcher_id)
        check(f"  walked to its tip: state {list(tip.state)}", list(tip.state) == [1, 0, 123])


def case_earlier_build() -> None:
    print("\n2. an earlier V18 registry build (register differs, pool build the same)")
    real = dict(kit.REGISTRY_LEAF_MODS)
    try:
        # stand-in for an earlier `register`: any other module gives the registry another root
        kit.REGISTRY_LEAF_MODS["forge_registry_register"] = k16.REGISTRY_LEAF_MODS["forge_registry_register"]
        old = kit.make_registry(salt=0x32, creation_fee=55)
    finally:
        kit.REGISTRY_LEAF_MODS.clear()
        kit.REGISTRY_LEAF_MODS.update(real)
    current = kit.make_registry(salt=0x32, creation_fee=55)
    check("setup: the earlier build's eve is not this build's", old.coin != current.coin)
    reader = registry_on_chain(old, kit)
    got, facts, rev = fr.registry_genesis(reader, old.launcher_id)
    check("recovered as v18, marked an earlier build", rev.tag == "v18" and isinstance(got, fr._EarlierBuild))
    tip, facts, _slots, _regs = fr.walk_registry(reader, old.launcher_id)
    check("  walked through the leaves it reveals to the coin the chain created", list(tip.state) == [1, 0, 55]
          and tip.coin.name() == Coin(old.coin.name(), old.advance([1, 0, 55]).coin.puzzle_hash, uint64(1)).name())
    check("  flagged for the log, kept out of the registry record", facts.get("_earlier_build") is True)

    # a registry whose POOL build differs (another action layer constant) is refused, not adopted
    bad = kit.make_registry(salt=0x33, creation_fee=55)
    consts = list(bad.constants)
    consts[6] = [bytes32(b"\x11" * 32)] * len(consts[6])          # pool leaf mods of some other build
    curried = {n: kit.REGISTRY_LEAF_MODS[n].curry(Program.to(consts)) for n in kit.REGISTRY_LEAF_ORDER}
    tree = kit.MerkleTree([curried[n].get_tree_hash() for n in kit.REGISTRY_LEAF_ORDER])
    inner = kit.ACTION_LAYER.curry(bad.finalizer, bytes32(tree.root), Program.to([0, 0, 0]))
    from chia.wallet.puzzles.singleton_top_layer_v1_1 import puzzle_for_singleton
    bad = replace(bad, constants=consts, leaves=curried, tree=tree, inner=inner,
                  coin=Coin(bad.launcher_id, puzzle_for_singleton(bad.launcher_id, inner).get_tree_hash(), uint64(1)))
    reader = registry_on_chain(bad, kit)
    try:
        fr.registry_genesis(reader, bad.launcher_id)
        check("a registry pinning another POOL build is refused", False, "accepted")
    except fr.RecoverError as exc:
        check("a registry pinning another POOL build is refused", exc.code == "WRONG_REVISION", exc.code)


def case_selectors() -> None:
    print("\n3. the recovery's decoder runs selectors as the action layer does")
    reg = kit.make_registry(salt=0x34)
    init, register = reg.leaves["forge_registry_init"], reg.leaves["forge_registry_register"]
    honest = Program.to([[init, register], [[5], [2]], [[], []]])        # reverse order: register(5) ran second
    got = fr.actions(honest)
    check("driver paths 2, 5 resolve to init, register in execution order",
          [fr._mod_hash(x) for x, _ in got] == [fr._mod_hash(init), fr._mod_hash(register)])
    quoted = Program.to([[], [[(1, init)]], [[]]])                        # (q . leaf), `puzzles` empty
    got = fr.actions(quoted)
    check("a (q . leaf) selector with no puzzles resolves to the leaf", got[0][0] == init)
    try:
        fr.actions(Program.to([[init], [[(1, init)], [2]], [[]]]))
        check("a selector count its solutions do not match is refused", False)
    except fr.RecoverError as exc:
        check("a selector count its solutions do not match is refused", exc.code == "UNKNOWN_LEAF")


PLAIN, R = bytes32(b"\xd0" * 32), bytes32(b"\xd5" * 32)
ISSUER_PUZZLE = Program.to([5, [4, 1, []]])
H = bytes32(ISSUER_PUZZLE.get_tree_hash())
H0 = 6_999_990


def rcat_pool(salt=0x71):
    return kit.make_pool([None, PLAIN, R], [10_000_000, 20_000_000, 30_000_000], total_lp=5_000_000, leaves="forge",
                         salt=salt, protocol_ph=bytes32(b"\x7a" * 32), dao_ph=bytes32(b"\x66" * 32), dao_fee_bps=50,
                         hidden_puzzle_hashes=[kit.ZERO_32, kit.ZERO_32, H])


def replacement(pool, index, salt=0xA9):
    r = pool.reserves[index]
    amount = int(r.coin.amount)
    funder_inner = kit.layered(r.hidden, kit.IDENTITY)
    gp = bytes32(bytes([salt]) * 32)
    parent = kit.coin_id(gp, construct_cat_puzzle(CAT_MOD, r.asset_id, funder_inner).get_tree_hash(), amount)
    coin = Coin(parent, r.full_hash, uint64(amount))
    lin = LineageProof(gp, funder_inner.get_tree_hash(), uint64(amount))
    return replace(pool, reserves=[replace(x, coin=coin, lineage=lin) if x.index == index else x for x in pool.reserves]), parent


def case_refund() -> None:
    print("\n4. rCAT refund: the replay adopts the replacement reserve")
    pool = rcat_pool()
    swapped, parent = replacement(pool, 2)
    bundle, st = kit.spend_action(swapped, "forge_action_refund", [H0 + 1, 2, parent])
    kit.validate(bundle)
    want = Coin(pool.coin.name(), swapped.successor_puzzle_hash(kit.state_to_list(st)), uint64(1))
    spend = next(s for s in bundle.coin_spends if s.coin == pool.coin)
    solution = Program.from_bytes(bytes(spend.solution))
    # the replay starts from the pool as indexed BEFORE the refund: its reserve 2 is the clawed coin
    after, names = rs17.replay_spend(pool, solution)
    check("the bundle is accepted by consensus (validate)", True)
    check("replay names the leaf", names == ["forge_action_refund"], str(names))
    check("replayed successor is the coin the driver built", after.coin == want,
          f"{after.coin.name().hex()[:12]} vs {want.name().hex()[:12]}")
    check("the successor records the replacement as reserve 2's parent",
          list(after.state)[7][2] == swapped.reserves[2].coin.name())
    check("  and the next reserve 2 is the replacement's child",
          after.reserves[2].coin.parent_coin_info == swapped.reserves[2].coin.name())
    saved = rs17._adopts
    try:
        rs17._adopts = lambda _base: {}
        without, _ = rs17.replay_spend(pool, solution)
    finally:
        rs17._adopts = saved
    check("CONTROL without the adopt step the replay derives a coin the chain never created", without.coin != want)


def case_register_rebuild() -> None:
    print("\n5. a V18 rCAT pool rebuilt from its register solution")
    pool = rcat_pool(salt=0x72)
    sol = Program.to(kit.register_solution(pool, (bytes32(b"\x00" * 32), bytes32(b"\x00" * 32), bytes32(b"\xff" * 32)),
                                           (bytes32(b"\xff" * 32), bytes32(b"\x00" * 32), bytes32(b"\x00" * 32))))
    reader = FakeReader()
    reader.add(pool.coin)
    item = {"solution": sol, "height": 12, "registry_coin": None, "registry_inner": None, "revision": fr.V18,
            "key": kit.pool_key(pool.config())}
    got, record = fr.pool_from_registration(reader, item)
    check("rebuilt pool hashes to the driver's eve", got.coin == pool.coin)
    check("rCAT reserve rebuilt under the revocation layer", got.reserves[2].full_hash == pool.reserves[2].full_hash
          and got.reserves[2].lineage == pool.reserves[2].lineage)
    check("record carries the H list, attestation and protocol 19",
          record["hidden_puzzle_hashes"][2] == H.hex() and record["issuer_attested"] == [None, None, False]
          and record["protocol_version"] == 19)
    eleven = Program.to(list(sol.as_iter())[:11])
    item["solution"] = eleven
    _got, record = fr.pool_from_registration(reader, item)
    check("an earlier register's eleven-field solution (no issuer proofs) reads as unattested",
          record["issuer_attested"] == [None, None, False])


def main() -> int:
    if not (kit.v18_available() and kit.registry_available()):
        print("V18 puzzles are not compiled; skipping")
        return 0
    case_revision_detection()
    case_earlier_build()
    case_selectors()
    case_refund()
    case_register_rebuild()
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
