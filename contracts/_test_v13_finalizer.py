#!/usr/bin/env python3
"""The multi-reserve finalizer: every reserve re-created, every message bound.

Drives the real compiled action layer, Forge's multi-reserve finalizer and
reserve amount program, and upstream's p2_delegated_by_singleton reserves
through chia_rs's consensus validator -- the code the mempool runs, which
enforces SEND_MESSAGE / RECEIVE_MESSAGE pairing.

Honest lane: a two-asset pool (XCH + CAT) and a ten-asset pool where one reserve
pays out, one grows from a settlement coin, and the rest are untouched; every
successor lands at the puzzle hash and amount the state says.

Adversarial lane, each beside the honest case per the audit skill: a tag index
outside the asset count; a missing, wrong, or swapped parent id; a reserve run
with a delegated puzzle the singleton never sent; an action whose new state
does not match what it pays out; a truth that lies about the pre-spend amount;
an untagged payout from the singleton; and pool B's singleton, curried with pool
A's reserve hashes, trying to move pool A's reserves.

Exit 0 all pass, 1 a failure, 2 nothing exercised (build outputs absent).
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia.wallet.trading.offer import OFFER_MOD_HASH
from chia_rs.sized_bytes import bytes32

import _v13_testkit as kit

results = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def refuses(label, thunk):
    try:
        thunk()
    except kit.Rejected as exc:
        return check(label, True, str(exc)[:60])
    return check(label, False, "accepted")


def accepts(label, thunk):
    try:
        out = thunk()
    except kit.Rejected as exc:
        check(label, False, str(exc)[:90])
        return None
    check(label, True)
    return out


CATS = [bytes32(bytes([0xD0 + i]) * 32) for i in range(9)]
PAYOUT_PH = bytes32(OFFER_MOD_HASH)


def honest_case(n: int, salt: int):
    """Reserve 0 (XCH) pays out, reserve 1 (CAT) grows, others untouched."""
    assets = [None, *CATS[: n - 1]]
    reserves = [5_000_000 + 1_000 * i for i in range(n)]
    fees = [7 * i for i in range(n)]
    pool = kit.make_pool(assets, reserves, total_lp=2_000_000, fees=fees, salt=salt)
    payout, growth = 123_456, 77_777
    new_reserves = list(reserves)
    new_reserves[0] -= payout
    new_reserves[1] += growth
    new_state = kit.forge_state(new_reserves, 2_000_000, fees, prev_root=bytes32(b"\x11" * 32))
    tagged = [(0, [kit.CREATE_COIN, PAYOUT_PH, payout])]
    bundle = kit.spend_pool(pool, new_state, tagged,
                            extra_cats={CATS[0]: [kit.cat_settlement(CATS[0], growth, salt=salt + 1)]})
    return pool, new_state, bundle


def main() -> int:
    if not kit.v13_available():
        print("  [skip] V13 build outputs are absent; run scripts/build-v13.py")
        return 2

    print("honest spends through the consensus validator:")
    for n in (2, 10):
        pool, new_state, bundle = honest_case(n, salt=0x40 + n)
        out = accepts(f"N={n}: one payout, one growth, {n - 2} untouched reserves", lambda: kit.validate(bundle))
        if out is None:
            continue
        conds, additions = out
        want_singleton = (pool.successor_puzzle_hash(new_state), 1)
        check(f"N={n}: successor singleton at the action layer curried with the new state",
              want_singleton in additions)
        amounts = kit.reserve_amounts(new_state)
        for r in pool.reserves:
            check(f"N={n}: reserve {r.index} re-created at its own puzzle hash holding reserve + fees_owed",
                  (r.full_hash, amounts[r.index]) in additions, str(amounts[r.index]))
        check(f"N={n}: payout coin created from reserve 0", (PAYOUT_PH, 123_456) in additions)
        singleton_spend = bundle.coin_spends[0]
        emitted = list(kit.Program.from_bytes(bytes(singleton_spend.puzzle_reveal))
                       .run(kit.Program.from_bytes(bytes(singleton_spend.solution))).as_iter())
        sent = [c for c in emitted if c.first().as_int() == kit.SEND_MESSAGE]
        check(f"N={n}: exactly {n} messages sent by the singleton, one per reserve", len(sent) == n, str(len(sent)))
        check(f"N={n}: every message uses mode 23 (sender puzzle, receiver coin)",
              all(c.rest().first().as_int() == 0b010_111 for c in sent))
        print(f"          cost of the N={n} spend: {conds.cost:,}")

    print("adversarial, beside the honest case above:")
    pool, new_state, _ = honest_case(2, salt=0x60)
    tagged = [(0, [kit.CREATE_COIN, PAYOUT_PH, 123_456])]
    grow = {CATS[0]: [kit.cat_settlement(CATS[0], 77_777, salt=0x61)]}
    parents = [r.coin.parent_coin_info for r in pool.reserves]

    refuses("tag index equal to the asset count fails the spend",
            lambda: kit.validate(kit.spend_pool(pool, new_state, tagged + [(2, [kit.CREATE_COIN, PAYOUT_PH, 1])],
                                                extra_cats=grow)))
    refuses("negative tag index fails the spend",
            lambda: kit.validate(kit.spend_pool(pool, new_state, tagged + [(-1, [kit.CREATE_COIN, PAYOUT_PH, 1])],
                                                extra_cats=grow)))
    # V13 (CHIP-0062 review P1, "reserve receiver derivation trusts solution data"): the four
    # V11 cases that fed parent ids through the SOLUTION no longer have a solution to feed. The
    # attack they guarded against is now built directly: a DECOY coin at reserve 0's puzzle hash,
    # holding exactly the pre-spend amount, offered in place of the real reserve.
    honest = kit.spend_pool(pool, new_state, tagged, extra_cats=grow)
    real0 = pool.reserves[0].coin
    decoy = kit.Coin(bytes32(b"\x99" * 32), real0.puzzle_hash, real0.amount)
    swapped = []
    for cs in honest.coin_spends:
        if cs.coin.name() == real0.name():
            swapped.append(kit.make_spend(decoy, kit.Program.from_bytes(bytes(cs.puzzle_reveal)),
                                          kit.Program.from_bytes(bytes(cs.solution))))
        else:
            swapped.append(cs)
    check("the decoy has the reserve's puzzle hash and amount, but not its id",
          decoy.puzzle_hash == real0.puzzle_hash and decoy.amount == real0.amount and decoy.name() != real0.name())
    refuses("a decoy reserve at the same puzzle hash and amount cannot stand in: the receiver comes from state",
            lambda: kit.validate(kit.SpendBundle(swapped, honest.aggregated_signature)))
    successor = pool.advance(new_state)
    check("the successor state records the spent reserves as the next parents",
          successor.state[7] == [r.coin.name() for r in pool.reserves])
    check("  ...and every successor reserve coin's parent is the reserve it came from",
          [r.coin.parent_coin_info for r in successor.reserves] == [r.coin.name() for r in pool.reserves])

    honest_dp = kit.delegated_puzzle_for(pool, pool.reserves[0], new_state, tagged)
    greedy_dp = kit.Program.to((1, [*list(honest_dp.rest().as_iter()), [kit.CREATE_COIN, PAYOUT_PH, 1]]))
    refuses("a reserve run with a delegated puzzle the singleton did not send is refused",
            lambda: kit.validate(kit.spend_pool(pool, new_state, tagged, reserve_delegated={0: greedy_dp}, extra_cats=grow)))
    refuses("a reserve naming a different sender inner puzzle hash is refused",
            lambda: kit.validate(kit.spend_pool(pool, new_state, tagged, reserve_sender_inner_hash=bytes32(b"\x42" * 32),
                                                extra_cats=grow)))

    # The action's state must match its payouts: claiming reserve 0 unchanged
    # while paying 123,456 out of it asks the reserve to create more than it holds.
    lying_state = kit.forge_state([r for r in pool.state[0]], pool.state[1], pool.state[2])
    lying_state[0][1] += 77_777
    refuses("an action whose new state omits its own payout is refused (reserve would mint)",
            lambda: kit.validate(kit.spend_pool(pool, lying_state, tagged, extra_cats=grow)))
    # And a payout with no settlement to fund the growth is refused too.
    refuses("a growing CAT reserve with no settlement coin in its ring is refused",
            lambda: kit.validate(kit.spend_pool(pool, new_state, tagged)))
    xch_grow = kit.forge_state([pool.state[0][0] + 5_000, pool.state[0][1]], pool.state[1], pool.state[2])
    refuses("a growing XCH reserve with no settlement coin in the bundle is refused (would mint)",
            lambda: kit.validate(kit.spend_pool(pool, xch_grow)))
    accepts("...and is accepted once a settlement coin funds the growth",
            lambda: kit.validate(kit.spend_pool(pool, xch_grow, extra_spends=[kit.xch_settlement(5_000, salt=0x66)])))

    # A truth that lies about the pre-spend amount points the message at a coin
    # that is not the reserve. Build the pool at one amount, present a coin at another.
    liar = kit.make_pool([None, CATS[0]], [5_000_000, 5_001_000], total_lp=2_000_000, salt=0x62)
    liar.reserves[0].coin = kit.Coin(liar.reserves[0].coin.parent_coin_info, liar.reserves[0].full_hash, kit.uint64(4_000_000))
    # The new state matches the coin, so the only inconsistency left is the receiver id.
    liar_new = kit.forge_state([4_000_000, 5_001_000], 2_000_000)
    refuses("a curried state that misstates a reserve's pre-spend amount cannot move it",
            lambda: kit.validate(kit.spend_pool(liar, liar_new)))

    refuses("an untagged payout from the singleton itself is refused (singleton holds one mojo)",
            lambda: kit.validate(kit.spend_pool(pool, new_state, tagged, base_conditions=[[kit.CREATE_COIN, PAYOUT_PH, 2]],
                                                extra_cats=grow)))

    # Pool B curries pool A's reserve hashes into its finalizer and tries to spend them.
    victim = kit.make_pool([None, CATS[0]], [5_000_000, 5_001_000], total_lp=2_000_000, salt=0x63)
    attacker = kit.make_pool([None, CATS[0]], [5_000_000, 5_001_000], total_lp=2_000_000, salt=0x64,
                             reserve_full_hashes=[r.full_hash for r in victim.reserves],
                             reserve_inner_hashes=[r.inner_hash for r in victim.reserves])
    attacker.reserves = victim.reserves  # the coins it is trying to move
    drain = kit.forge_state([1, 1], 2_000_000)
    steal = [(0, [kit.CREATE_COIN, PAYOUT_PH, 4_999_999]), (1, [kit.CREATE_COIN, PAYOUT_PH, 5_000_999])]
    refuses("pool B's singleton, curried with pool A's reserves, cannot move them (sender is A's singleton)",
            lambda: kit.validate(kit.spend_pool(attacker, drain, steal)))
    refuses("...even when the reserves are told B's inner puzzle hash as the sender",
            lambda: kit.validate(kit.spend_pool(attacker, drain, steal, reserve_sender_inner_hash=attacker.inner_hash)))

    # Untouched pool, no conditions at all: every reserve still re-created.
    quiet = kit.make_pool([None, CATS[1], CATS[2]], [10, 20, 30], total_lp=5, salt=0x65)
    out = accepts("a spend with no tagged conditions still re-creates every reserve",
                  lambda: kit.validate(kit.spend_pool(quiet, quiet.state)))
    if out:
        _, additions = out
        for r in quiet.reserves:
            check(f"  quiet reserve {r.index} re-created unchanged", (r.full_hash, [10, 20, 30][r.index]) in additions)

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} finalizer checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
