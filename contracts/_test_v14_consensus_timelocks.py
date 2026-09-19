#!/usr/bin/env python3
"""The oracle finding, both halves, without a chain: V11 accepts the backfill, V14 cannot.

`forge_v14_driver.validate` runs `chia_rs.get_conditions_from_spendbundle`, which
executes the puzzles and returns the conditions they produced. It has no coin
records, so it cannot judge `ASSERT_MY_BIRTH_HEIGHT` -- a birth assert passes
through whatever it claims. That is why the oracle suite could only prove the
arithmetic, and why the lying-birth half was left to the live probe.

The node's own answer to that question is a pure function --
`chia.consensus.check_time_locks.check_time_locks(removal_records, conds,
prev_transaction_block_height, timestamp)` -- and the mempool calls exactly it
(`chia/full_node/mempool_manager.py`). Give it the coin records the chain would
have and it decides the birth assert offline, deterministically, with the same
error codes a node returns. Coins created inside the bundle get the record the
mempool synthesises for them: `confirmed_block_index = peak + 1`, because every
spend in a bundle happens at once.

So this file closes the gap the testnet halt left open:

  V11  the reviewer's two-generation bundle is ACCEPTED -- by the validator and
       by the time-lock rule -- and its oracle records 31 blocks of a price the
       bundle itself set in a single block. The finding is real, and reproduced
       here with no chain at all.

  V14  the same bundle cannot be validated in the first place. A V14 pool spend
       asserts its own birth height, and consensus forbids a relative or birth
       condition on a coin created in the same bundle:
       `EPHEMERAL_RELATIVE_CONDITION`. That holds whatever birth is claimed, so
       a V14 pool coin can never be spent in the bundle that created it, and the
       two-generation shape is gone rather than merely detected.

  V14  a single generation lying about its birth to stretch elapsed time is
       refused with `ASSERT_MY_BIRTH_HEIGHT_FAILED`, and the claimed height is
       boxed in from both sides by the prologue's own
       `ASSERT_HEIGHT_ABSOLUTE` / `ASSERT_BEFORE_HEIGHT_ABSOLUTE`.

Exit codes: 0 all checks pass, 1 a check failed, 2 a build is absent.
"""
import sys

sys.path.insert(0, ".")

try:
    from chia.consensus.check_time_locks import check_time_locks      # chia-blockchain <= 2.5
except ModuleNotFoundError:
    # 2.7 moved it into the mempool manager. Without this fallback the suite died at
    # import on a current install -- before reaching the skip that says which build is
    # absent, and reading as a failing test (audit run 2026-09-19, QA-1).
    from chia.full_node.mempool_manager import check_time_locks
try:
    from chia.types.coin_record import CoinRecord                      # chia-blockchain <= 2.5
except ModuleNotFoundError:
    from chia_rs import CoinRecord                                     # 2.7: the Rust type
from chia.util.errors import Err
from chia_rs import Coin, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint32, uint64

FAILED = 0
CAT = bytes32(bytes([0xD0]) * 32)
H = 6_999_990          # the height the attacker claims for the last generation
PEAK = H               # mempool peak: the bundle would land in PEAK + 1
TS = 1_700_000_000


def check(label, ok, detail=""):
    global FAILED
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        FAILED += 1


def timelocks(kit, bundle, births, peak=PEAK):
    """The mempool's rule over a bundle. Returns (Err or None, ephemeral coin count).

    `births` gives the true confirmed height of each coin the chain already holds;
    coins the bundle creates are recorded at peak + 1, as the mempool does.
    """
    conds, _ = kit.validate(bundle)
    created = set()
    for spend in conds.spends:
        parent = bytes32(spend.coin_id)
        for cc in spend.create_coin:
            created.add(Coin(parent, bytes32(cc[0]), uint64(int(cc[1]))).name())
    records, ephemeral = {}, 0
    for cs in bundle.coin_spends:
        cid = cs.coin.name()
        if cid in created:
            ephemeral += 1
            records[cid] = CoinRecord(cs.coin, uint32(peak + 1), uint32(0), False, uint64(TS))
        else:
            records[cid] = CoinRecord(cs.coin, uint32(births[cid]), uint32(0), False, uint64(TS))
    # chia 2.7 moved this into chia_rs: it grew a fifth argument, `nowrap`, which the node
    # binds to `peak >= HARD_FORK2_HEIGHT`, and it returns the error as an int code rather
    # than an Err. Both handled here so the suite reads the same on 2.5 and 2.7 (QA-1).
    import inspect
    kwargs = {}
    if "nowrap" in inspect.signature(check_time_locks).parameters:
        kwargs["nowrap"] = int(peak) >= int(kit.DEFAULT_CONSTANTS.HARD_FORK2_HEIGHT)
    result = check_time_locks(records, conds, uint32(peak), uint64(TS), **kwargs)
    if isinstance(result, int) and not isinstance(result, Err):
        result = Err(result)
    return result, ephemeral


def birth_asserts(kit, bundle):
    """How many spends in this bundle assert a birth height."""
    conds, _ = kit.validate(bundle)
    return sum(1 for s in conds.spends if s.birth_height is not None)


def two_generation(kit, born, claimed_birth, versioned_birth: bool):
    """The reviewer's shape: spend the pool, then spend its successor, one bundle.

    Generation 1 claims h = H - 31; the successor claims h = H, so the oracle
    integrates 31 blocks of generation 1's post-swap price while the bundle
    occupies a single block.
    """
    pool = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000,
                         leaves="forge", salt=0x40, last_height=H - 40)
    gen1 = kit.replace(pool, birth=born) if versioned_birth else pool
    b1, s1 = kit.spend_action(gen1, "forge_action_observe", [H - 31])
    kit.validate(b1)
    gen2 = gen1.advance(kit.state_to_list(s1))
    if versioned_birth:
        gen2 = kit.replace(gen2, birth=claimed_birth)
    b2, s2 = kit.spend_action(gen2, "forge_action_observe", [H])
    births = {cs.coin.name(): born for cs in b1.coin_spends}
    return SpendBundle.aggregate([b1, b2]), births, kit.state_to_list(s1), kit.state_to_list(s2)


def main() -> int:
    import _v11_testkit as v11
    import _v14_testkit as v14
    if not (v11.v11_available() and v14.v14_available()):
        print("  [skip] a build is absent; the V11 control needs contracts/v11, which the public "
              "repository does not carry, and V14 needs scripts/build-v14.py")
        return 2

    born = H - 39   # V14: a coin is born AFTER the height its state last claimed (H - 40)

    print("V11, the reviewer's two-generation bundle, judged by the node's own rule:")
    agg, births, s1, s2 = two_generation(v11, born, None, versioned_birth=False)
    check("no spend in a V11 bundle asserts a birth height at all", birth_asserts(v11, agg) == 0)
    err, ephemeral = timelocks(v11, agg, births)
    check("the validator accepts it", True)  # timelocks() already validated, or raised
    check("check_time_locks ACCEPTS it -- the finding, with no chain involved", err is None,
          "" if err is None else f"got {err}")
    check(f"  the bundle spends {ephemeral} coins it created in the same block", ephemeral > 0)
    gen1_cums, gen2_cums = s1[3][1], s2[3][1]
    check("the oracle integrated the manipulated price across the window", gen2_cums != gen1_cums,
          f"gen1 {gen1_cums} -> gen2 {gen2_cums}")
    check("  and it did so while claiming 31 blocks of elapsed time in one block",
          s1[3][0] == H - 31 and s2[3][0] == H, f"last_height {s1[3][0]} then {s2[3][0]}")

    print()
    print("V14, the same bundle, whatever birth the successor claims:")
    # Two different lines refuse it, and which one is the interesting part. A birth at or
    # below the height generation 1 claimed is refused by the prologue itself (V14 asserts
    # birth > last_height: a successor is created in a block AFTER the one its predecessor
    # was checked against). A birth the successor could plausibly claim trips consensus's
    # ban on a birth condition for a coin created in the same bundle. Its TRUE birth -- the
    # block it is created in -- never even reaches that rule, because `h >= birth` cannot
    # hold for a height the bundle is also required to be mined at or before.
    EPHEMERAL, PUZZLE = "ephemeral", "puzzle"
    for label, claimed, expected in (
            ("backfilling (claims it was born at generation 1's claimed height)", H - 31, PUZZLE),
            ("claiming generation 1's own birth", born, PUZZLE),
            ("a plausible birth, one block after generation 1's claim", H - 30, EPHEMERAL),
            ("truthful (claims the block it is created in)", PEAK + 1, PUZZLE)):
        try:
            agg12, births12, _, _ = two_generation(v14, born, claimed, versioned_birth=True)
            err12, _ = timelocks(v14, agg12, births12)
            check(f"{label}: refused", False, f"ACCEPTED with {err12}")
            continue
        except (v14.Rejected, ValueError, TypeError) as exc:
            text = str(exc)
        if expected is EPHEMERAL:
            got = "141" in text or "EPHEMERAL_RELATIVE_CONDITION" in text
            check(f"{label}: consensus refuses it, EPHEMERAL_RELATIVE_CONDITION", got, text[:70])
        else:
            got = "clvm raise" in text
            check(f"{label}: the prologue's own birth bounds refuse it first", got, text[:70])

    print()
    print("V14, one generation, lying about its birth:")
    # last claimed height H-80, truly born at H-39: a lie must stay inside (H-80, H] to reach
    # consensus at all -- the prologue refuses anything at or below the last claimed height itself
    pool = v14.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000,
                         leaves="forge", salt=0x40, last_height=H - 80)
    honest, _ = v14.spend_action(v14.replace(pool, birth=born), "forge_action_observe", [H])
    check("every V14 pool spend asserts a birth height", birth_asserts(v14, honest) >= 1)
    err, _ = timelocks(v14, honest, {cs.coin.name(): born for cs in honest.coin_spends})
    check("the honest spend passes the time-lock rule", err is None, "" if err is None else str(err))

    try:
        v14.spend_action(v14.replace(pool, birth=H - 80), "forge_action_observe", [H])
        check("a birth at the last claimed height never reaches consensus: the prologue refuses it", False, "ACCEPTED")
    except (v14.Rejected, ValueError, TypeError) as exc:
        check("a birth at the last claimed height never reaches consensus: the prologue refuses it", "clvm raise" in str(exc), str(exc)[:70])
    for label, claimed in (("understating birth by 31 blocks (stretches elapsed)", born - 31),
                           ("overstating birth", born + 5)):
        bundle, _ = v14.spend_action(v14.replace(pool, birth=claimed), "forge_action_observe", [H])
        err, _ = timelocks(v14, bundle, {cs.coin.name(): born for cs in bundle.coin_spends})
        check(f"{label}: {Err.ASSERT_MY_BIRTH_HEIGHT_FAILED.name}",
              err == Err.ASSERT_MY_BIRTH_HEIGHT_FAILED, "" if err == Err.ASSERT_MY_BIRTH_HEIGHT_FAILED else f"got {err}")

    print()
    print("V14, the claimed height is boxed in from both sides:")
    future, _ = v14.spend_action(v14.replace(pool, birth=born), "forge_action_observe", [PEAK + 1])
    err, _ = timelocks(v14, future, {cs.coin.name(): born for cs in future.coin_spends})
    check(f"a height above the peak: {Err.ASSERT_HEIGHT_ABSOLUTE_FAILED.name}",
          err == Err.ASSERT_HEIGHT_ABSOLUTE_FAILED, "" if err == Err.ASSERT_HEIGHT_ABSOLUTE_FAILED else f"got {err}")

    stale_pool = v14.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000,
                               leaves="forge", salt=0x41, last_height=H - 80)
    stale, _ = v14.spend_action(v14.replace(stale_pool, birth=H - 60), "forge_action_observe", [H - 40])
    err, _ = timelocks(v14, stale, {cs.coin.name(): H - 60 for cs in stale.coin_spends})
    check(f"a height older than the oracle window: {Err.ASSERT_BEFORE_HEIGHT_ABSOLUTE_FAILED.name}",
          err == Err.ASSERT_BEFORE_HEIGHT_ABSOLUTE_FAILED,
          "" if err == Err.ASSERT_BEFORE_HEIGHT_ABSOLUTE_FAILED else f"got {err}")

    print()
    print(f"{'ALL PASSED' if not FAILED else f'{FAILED} FAILED'} -- V14 consensus time-lock checks")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
