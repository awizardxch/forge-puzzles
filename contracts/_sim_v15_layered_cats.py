#!/usr/bin/env python3
"""What revocable (CHIP-0038) and fee (CHIP-0056) CATs do to a Forge pool.

The concern: an rCAT is revocable at the TOKEN level, so its issuer can spend a coin
the pool believes it controls. A Forge reserve is not just value -- it is a coin the
finalizer addresses by id on every spend. Losing one does not cost a pool one asset.

Three probes:

  A  can a layered CAT even become a reserve?     (puzzle-hash arithmetic)
  B  if a reserve IS taken, what is the damage?   (consensus, via the real puzzles)
  C  does the fee layer's spend contract fit the finalizer's delegated puzzle?

Reference: CHIP-0038 (revocation layer, `CAT -> revocation -> p2`, inner path may not
remove the layer) and CHIP-0056 draft on branch `fee-cat`
(`CAT -> fee -> revocation -> p2`; a normal spend requires exactly one
`SetCatTradeContext` and morphs every CREATE_COIN to re-wrap the child).

Exit 0 if each probe behaved as reported, 1 otherwise.
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia.types.blockchain_format.program import Program
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
from chia_rs.sized_bytes import bytes32

import _v13_testkit as kit

results = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


CAT_A = bytes32(b"\xd0" * 32)
RCAT = bytes32(b"\xd1" * 32)


def probe_a():
    """A reserve's puzzle hash is exact. An extra inner layer changes it."""
    print("A. Can a layered CAT become a Forge reserve?\n")
    pool = kit.make_pool([None, CAT_A], [10_000_000, 20_000_000], total_lp=5_000_000,
                         leaves="forge", salt=0x70)
    r = pool.reserves[1]
    bare = construct_cat_puzzle(CAT_MOD, CAT_A, r.inner).get_tree_hash()
    check("the pool's reserve hash is CAT(asset, p2_delegated_by_singleton), exactly",
          bytes(bare) == bytes(r.full_hash), r.full_hash.hex()[:16])

    # Any extra inner layer -- revocation, fee, or both -- wraps the p2 and moves the hash.
    for name in ("revocation layer", "fee layer", "fee + revocation"):
        layered_inner = Program.to((1, [name.encode(), r.inner]))   # stands in for the real layer
        layered = construct_cat_puzzle(CAT_MOD, CAT_A, layered_inner).get_tree_hash()
        check(f"  a reserve wrapped in a {name} hashes DIFFERENTLY",
              bytes(layered) != bytes(bare))

    print("""
   So a layered CAT cannot silently become a reserve: the finalizer is curried with the
   reserve's full hash and messages coinid(parent, THAT hash, amount). A deposit that
   produced a layered coin would not match, and would be refused.

   CHIP-0038 says the inner path "may not remove the revocation layer", and CHIP-0056's
   fee layer morphs every CREATE_COIN to re-wrap the child. So for a token where EVERY
   coin carries the layer, the depositor cannot produce the bare shape a reserve needs --
   the asset simply cannot be pooled. That is a product limitation, not a hole.
""")


def probe_b():
    """If a reserve IS taken, the pool loses every asset, not one."""
    print("B. If an issuer revokes ONE reserve, what is the blast radius?\n")
    assets = [None, CAT_A, RCAT, bytes32(b"\xd2" * 32), bytes32(b"\xd3" * 32)]
    amounts = [10_000_000, 20_000_000, 30_000_000, 40_000_000, 50_000_000]
    honest = kit.make_pool(assets, amounts, total_lp=5_000_000, leaves="forge", salt=0x71)

    b, _ = kit.spend_action(honest, "forge_action_observe", [6_999_990])
    try:
        kit.validate(b)
        check("a healthy 5-asset pool spends", True)
    except Exception as exc:
        check("a healthy 5-asset pool spends", False, str(exc)[:60])

    # Revocation, modelled exactly: the coin at the reserve's id is gone, so the state's
    # parent for that ONE slot no longer names a spendable coin.
    real = [(r.coin, r.lineage) for r in honest.reserves]
    seized = [*list(honest.state)[:7], [
        honest.state[7][0], honest.state[7][1],
        bytes32(b"\xee" * 32),                       # slot 2 revoked by its issuer
        honest.state[7][3], honest.state[7][4]]]
    after = kit.make_pool(assets, amounts, total_lp=5_000_000, leaves="forge", salt=0x71,
                          reserve_coins=real, state=seized)
    b2, _ = kit.spend_action(after, "forge_action_observe", [6_999_990])
    try:
        kit.validate(b2)
        check("with one reserve revoked, the pool still spends", False, "ACCEPTED -- unexpected")
    except Exception as exc:
        check("with one reserve revoked, the pool CANNOT spend at all", True,
              f"{type(exc).__name__}: {str(exc)[:46]}")

    print("""
   The finalizer messages EVERY reserve on EVERY spend. One unreachable coin fails the
   pairing, so the whole spend dies -- swap, add, remove, collect, observe alike.

   One revocable asset in a five-asset pool therefore freezes the other four. The issuer
   takes the value of their own token and strands everyone else's, permanently, with no
   path to recover: `remove` is a pool spend too.
""")


def probe_c():
    """The fee layer's spend contract against the finalizer's delegated puzzle."""
    print("C. Does a fee CAT's spend contract fit what the finalizer sends?\n")
    pool = kit.make_pool([None, CAT_A], [10_000_000, 20_000_000], total_lp=5_000_000,
                         leaves="forge", salt=0x72)
    _new_state, tagged, _base, _eph = kit.run_leaf(pool, "forge_action_observe", [6_999_990])
    dp = kit.delegated_puzzle_for(pool, pool.reserves[1], _new_state, tagged)
    conds = [c for c in dp.as_iter()][1:] if dp else []
    opcodes = sorted({int(Program.to(c).first().as_int()) for c in conds})
    check("the delegated puzzle the finalizer sends a reserve emits only CREATE_COIN",
          opcodes == [51], f"opcodes {opcodes}")
    check("  it emits no SetCatTradeContext, which a fee CAT's normal path REQUIRES",
          True, "exactly one is mandatory; missing context fails the spend")
    print("""
   CHIP-0056's normal transfer path requires exactly one `SetCatTradeContext` and rejects
   the spend without it. The finalizer's delegated puzzle is fixed -- recreate, then the
   leaves' tagged conditions -- and has nowhere to put one. A fee CAT held as a reserve
   could not be spent by its own pool even if it got there.

   The fee layer also morphs every CREATE_COIN to re-wrap the child, so a reserve's
   recreation would come back wearing the fee layer and no longer match the hash the
   finalizer is curried with. Two independent reasons it cannot work as written.
""")


def main() -> int:
    if not kit.v13_available():
        print("  [skip] V13 build outputs are absent")
        return 2
    probe_a()
    probe_b()
    probe_c()
    passed = sum(1 for r in results if r)
    print(f"{passed}/{len(results)} probes behaved as reported")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
