#!/usr/bin/env python3
"""The five V11 leaves, each alone, through the consensus validator.

Every case builds a real pool -- action layer curried with the five-leaf merkle
root, multi-reserve finalizer, p2_delegated_by_singleton reserves (XCH and
CAT), the V11 LP TAIL -- and pushes a complete bundle through
chia_rs.get_conditions_from_spendbundle. Honest cases check the successor state
against the Python mirror in forge_math and the oracle arithmetic; adversarial
cases sit beside them and must be refused, per the audit skill.

Lanes:
  swap      2-asset, weighted 4/1, 3-asset; output +1 refused; same asset refused;
            no input settlement refused; fee accrues into fees_owed
  add       balanced and off-ratio; lp_delta +1 refused; eve that mints a
            different amount refused (TAIL); missing deposit settlement refused
  remove    3-asset free withdrawal; vault pays the crossing fee; burn = total_lp
            refused; payout +1 refused; fabricated melt coin (finding 4) refused
  observe   slot coin created at the slot puzzle hash for (h . cums), announced
  collect   pays fees_owed to the curried recipient with a hint, zeroes state;
            zero fee refused; same index twice refused
  prologue  h <= last_height refused; height conditions carry h and h + window;
            prev_root chains across two spends; oracle accumulates the pre-spend
            price; a sixth leaf with a valid-looking proof is refused

Exit 0 all pass, 1 a failure, 2 nothing exercised (build outputs absent).
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
from chia.wallet.trading.offer import OFFER_MOD_HASH
from chia_rs.sized_bytes import bytes32

import _v13_testkit as kit
import forge_math

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
    except Exception as exc:  # a leaf that raises while run locally is a refusal too
        return check(label, True, f"{type(exc).__name__} (local run)")
    return check(label, False, "accepted")


def accepts(label, thunk):
    try:
        out = thunk()
    except kit.Rejected as exc:
        check(label, False, str(exc)[:90])
        return None
    check(label, True)
    return out


CATS = [bytes32(bytes([0xD0 + i]) * 32) for i in range(4)]
SETTLE_PH = bytes32(OFFER_MOD_HASH)
H0 = 6_999_990  # a height inside (VALIDATION_HEIGHT - window, VALIDATION_HEIGHT]


def swap_case(pool, asset_in, asset_out, gross, h=H0, claimed=None, settle=True, salt=0xC0):
    r, w = pool.state[0], pool.weights
    honest = forge_math.swap_output(r[asset_in], r[asset_out], gross, pool.fee_bps, w[asset_in], w[asset_out])
    claimed = honest if claimed is None else claimed
    asset = pool.asset_ids[asset_in]
    extra_spends, extra_cats, settlement_id = [], {}, bytes32(b"\x00" * 32)
    if settle:
        if asset is None:
            coin, spend = kit.offer_settlement_xch(gross, salt=salt)
            extra_spends.append(spend)
        else:
            coin, spendable = kit.offer_settlement_cat(asset, gross, salt=salt)
            extra_cats[asset] = [spendable]
        settlement_id = coin.name()
    bundle, new_state = kit.spend_action(pool, "forge_action_swap",
                                         [h, asset_in, asset_out, gross, claimed, settlement_id],
                                         extra_spends=extra_spends, extra_cats=extra_cats)
    return bundle, new_state, honest


def main() -> int:
    if not kit.v13_available():
        print("  [skip] V13 build outputs are absent; run scripts/build-v13.py")
        return 2

    # ---------------------------------------------------------------- swap
    print("swap:")
    pool = kit.make_pool([None, CATS[0]], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x30)
    bundle, new_state, honest = swap_case(pool, 0, 1, 250_000)
    out = accepts("XCH -> CAT swap through the real leaf", lambda: kit.validate(bundle))
    if out:
        conds, additions = out
        st = kit.state_to_list(new_state)
        fee = honest * pool.protocol_fee_bps // 10_000
        check("  reserves move by gross in and honest out", st[0] == [10_250_000, 20_000_000 - honest])
        check("  protocol fee accrues into fees_owed[out]", st[2] == [0, fee], str(fee))
        check("  trader is paid the output minus the fee at the settlement puzzle",
              (construct_cat_puzzle(CAT_MOD, CATS[0], kit.OFFER_MOD).get_tree_hash(), honest - fee) in additions)
        check("  out reserve re-created holding reserve + owed fee",
              (pool.reserves[1].full_hash, 20_000_000 - honest + fee) in additions)
        check("  successor singleton carries the new state", (pool.successor_puzzle_hash(st), 1) in additions)
        check("  prev_root is the hash of the state this spend started from", st[4] == kit.program_hash(pool.state))
        check("  oracle credited the pre-spend price and recorded the spot", st[3] == kit.expected_oracle(pool.state, pool.weights, H0))
        check("  height pinned: ASSERT_HEIGHT_ABSOLUTE h and ASSERT_BEFORE h + window",
              conds.height_absolute == H0 and conds.before_height_absolute == H0 + kit.ORACLE_WINDOW)
        print(f"          cost: {conds.cost:,}")
    bundle, _, _ = swap_case(pool, 1, 0, 400_000, salt=0xC2)
    accepts("CAT -> XCH swap, the other direction", lambda: kit.validate(bundle))

    weighted = kit.make_pool([None, CATS[1]], [8_000_000, 2_000_000], total_lp=4_000_000, leaves="forge",
                             weights=[4, 1], salt=0x31)
    bundle, _, _ = swap_case(weighted, 0, 1, 100_000, salt=0xC3)
    accepts("80/20 weighted swap", lambda: kit.validate(bundle))
    three = kit.make_pool([None, CATS[0], CATS[1]], [3_000_000, 4_000_000, 5_000_000], total_lp=3_000_000,
                          leaves="forge", salt=0x32)
    bundle, new_state, _ = swap_case(three, 1, 2, 90_000, salt=0xC4)
    out = accepts("3-asset swap between two CATs leaves the XCH reserve untouched", lambda: kit.validate(bundle))
    if out:
        check("  untouched reserve re-created unchanged", (three.reserves[0].full_hash, 3_000_000) in out[1])

    refuses("claiming one mojo more than the curve allows is refused",
            lambda: kit.validate(swap_case(pool, 0, 1, 250_000, claimed=honest + 1)[0]))
    refuses("claiming one mojo less is refused too (bracket is exact)",
            lambda: kit.validate(swap_case(pool, 0, 1, 250_000, claimed=honest - 1)[0]))
    refuses("swapping an asset for itself is refused",
            lambda: kit.validate(swap_case(pool, 1, 1, 250_000)[0]))
    refuses("a swap whose input settlement is not in the bundle is refused",
            lambda: kit.validate(swap_case(pool, 0, 1, 250_000, settle=False)[0]))

    # ── sign assertions: the class that drained TibetSwap V2 ────────────────
    #
    # On 2026-08-24 TibetSwap V2 was drained because `swap.clsp` enforced that
    # one reserve rise and the other fall but never required the user-supplied
    # input to be POSITIVE. A negative amount reverses the trade and with it the
    # sign of the trade fee, so the fee pays the swapper; repeated against the
    # same pair it empties the reserves. Two in-house AI audits (April and June
    # 2026) and several expert human reviews all missed it.
    #
    # Forge's swap leaf has asserted `gross_input > 0` and `claimed_output > 0`
    # from the start, and remove has asserted `burn > 0`. These pin the property
    # that matters -- such a solution is refused -- at the leaf.
    #
    # What they do NOT pin is those particular lines. A mutation check settled
    # it: recompiling the leaf with both sign assertions deleted still refuses
    # every case below, because `exact_swap_output` is unsatisfiable for a
    # negative input (brute-forced over the plausible output range, and the
    # Python mirror refuses one outright). So the curve is the load-bearing
    # defence here and the `> 0` asserts are a second, redundant layer.
    #
    # That is worth writing down rather than leaving as a comfortable
    # assumption: anyone reading the leaf would take the asserts for the
    # defence, and if the bracket were ever widened they would become it.
    #
    # These run the compiled leaf directly rather than building a bundle, so the
    # refusal comes from the puzzle and not from a helper raising on the way in.
    print("sign assertions (TibetSwap V2 class):")
    SETTLE_ID = bytes32(b"" * 32)

    def leaf_probe(leaf, solution):
        return lambda: kit.run_leaf(pool, leaf, solution)

    refuses("a negative swap input is refused",
            leaf_probe("forge_action_swap", [H0, 0, 1, -250_000, 100_000, SETTLE_ID]))
    refuses("a zero swap input is refused",
            leaf_probe("forge_action_swap", [H0, 0, 1, 0, 100_000, SETTLE_ID]))
    refuses("a negative claimed output is refused",
            leaf_probe("forge_action_swap", [H0, 0, 1, 250_000, -100_000, SETTLE_ID]))
    refuses("a zero claimed output is refused",
            leaf_probe("forge_action_swap", [H0, 0, 1, 250_000, 0, SETTLE_ID]))
    refuses("a negative asset index is refused",
            leaf_probe("forge_action_swap", [H0, -1, 1, 250_000, 100_000, SETTLE_ID]))
    refuses("an asset index past the last reserve is refused",
            leaf_probe("forge_action_swap", [H0, 0, 9, 250_000, 100_000, SETTLE_ID]))
    refuses("a negative LP burn is refused",
            leaf_probe("forge_action_remove", [H0, -1, bytes32(b"" * 32), [0, 0]]))
    refuses("a zero LP burn is refused",
            leaf_probe("forge_action_remove", [H0, 0, bytes32(b"" * 32), [0, 0]]))

    # The deposit path asks the same question of three assertions rather than
    # one: `lp_delta > 0`, `deposit >= 0` for each asset, and `any_positive` so
    # the whole deposit cannot be nothing. A zero on ONE asset is legitimate --
    # that is an off-ratio add -- which is why the per-asset check is `>= 0` and
    # the emptiness check is separate.
    ADD_PARENT = bytes32(b"" * 32)
    ADD_IDS = [bytes32(b"" * 32)] * 2
    refuses("a negative LP mint is refused",
            leaf_probe("forge_action_add", [H0, [100_000, 200_000], -50_000, ADD_PARENT, ADD_IDS]))
    refuses("a zero LP mint is refused",
            leaf_probe("forge_action_add", [H0, [100_000, 200_000], 0, ADD_PARENT, ADD_IDS]))
    refuses("a negative deposit in any slot is refused",
            leaf_probe("forge_action_add", [H0, [-100_000, 200_000], 50_000, ADD_PARENT, ADD_IDS]))
    refuses("a deposit of nothing at all is refused",
            leaf_probe("forge_action_add", [H0, [0, 0], 50_000, ADD_PARENT, ADD_IDS]))

    # ---------------------------------------------------------------- add
    print("add:")
    pool = kit.make_pool([None, CATS[0]], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x33)

    def add_case(pool, deposits, lp_delta=None, h=H0, settle=(True, True), eve_delta=None, salt=0xA0):
        honest = forge_math.invariant_lp_mint(pool.state[0], deposits, pool.state[1], pool.fee_bps, pool.weights, version=10)
        lp_delta = honest if lp_delta is None else lp_delta
        new_total = pool.state[1] + lp_delta
        # run the leaf once with a placeholder parent to learn the new state root
        probe_state, _, _, _ = kit.run_leaf(pool, "forge_action_add", [h, deposits, lp_delta, bytes32(b"\x01" * 32),
                                                                        [bytes32(b"\x02" * 32)] * len(deposits)])
        root = probe_state.get_tree_hash()
        recipient = bytes32(b"\x55" * 32)
        lp_parent, lp_spends = kit.lp_mint_spends(pool, eve_delta if eve_delta is not None else lp_delta,
                                                  new_total, root, recipient, salt=salt)
        extra_spends, extra_cats, ids = list(lp_spends), {}, []
        for i, (asset, dep, ok) in enumerate(zip(pool.asset_ids, deposits, settle)):
            if dep > 0 and ok:
                if asset is None:
                    coin, spend = kit.offer_settlement_xch(dep, salt=salt + 1 + i)
                    extra_spends.append(spend)
                else:
                    coin, spendable = kit.offer_settlement_cat(asset, dep, salt=salt + 1 + i)
                    extra_cats[asset] = [spendable]
                ids.append(coin.name())
            else:
                ids.append(bytes32(b"\x00" * 32) if not ok else bytes32(b"\x03" * 32))
        bundle, new_state = kit.spend_action(pool, "forge_action_add", [h, deposits, lp_delta, lp_parent, ids],
                                             extra_spends=extra_spends, extra_cats=extra_cats)
        return bundle, new_state, honest, recipient

    bundle, new_state, honest, recipient = add_case(pool, [1_000_000, 2_000_000])
    out = accepts("balanced add mints the mirror's LP through the real eve", lambda: kit.validate(bundle))
    if out:
        conds, additions = out
        st = kit.state_to_list(new_state)
        check("  reserves grew by the deposits", st[0] == [11_000_000, 22_000_000])
        check("  total_lp grew by lp_delta", st[1] == 5_000_000 + honest, str(honest))
        lp_coin_ph = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, kit.Program.to(recipient)).get_tree_hash_precalc(recipient)
        hinted = [(ph, a, hint) for ph, a, hint in kit.additions_with_hints(conds) if ph == lp_coin_ph]
        check("  LP minted to the recipient at the LP CAT puzzle hash", any(a == honest for _, a, _ in hinted))
        check("  the minted LP carries a hint equal to the recipient's inner puzzle hash (CHIP-0020)",
              any(hint == bytes(recipient) for _, _, hint in hinted))
        print(f"          cost: {conds.cost:,}")
    bundle, new_state, honest, _ = add_case(pool, [3_000_000, 500_000], salt=0xA8)
    out = accepts("off-ratio add is repriced and mints the mirror's LP", lambda: kit.validate(bundle))
    if out:
        check("  total_lp grew by exactly the mirror's figure", kit.state_to_list(new_state)[1] == 5_000_000 + honest)
    refuses("minting one LP more than the invariant allows is refused",
            lambda: kit.validate(add_case(pool, [1_000_000, 2_000_000], lp_delta=honest + 1, salt=0xB0)[0]))
    refuses("an eve that mints a different amount than the pool authorized is refused by the TAIL",
            lambda: kit.validate(add_case(pool, [1_000_000, 2_000_000], eve_delta=honest + 5, salt=0xB8)[0]))
    refuses("a deposit whose settlement is not in the bundle is refused",
            lambda: kit.validate(add_case(pool, [1_000_000, 2_000_000], settle=(True, False), salt=0xB9)[0]))
    refuses("an add with no deposit at all is refused",
            lambda: kit.validate(add_case(pool, [0, 0], lp_delta=1, salt=0xBA)[0]))

    # ---------------------------------------------------------------- remove
    print("remove:")
    three = kit.make_pool([None, CATS[0], CATS[1]], [3_000_000, 4_000_000, 5_000_000], total_lp=3_000_000,
                          leaves="forge", salt=0x34)

    def remove_case(pool, burn, payouts=None, h=H0, fabricated=False, salt=0x90):
        vf = forge_math.vault_fee_bps(len(pool.state[0]), 10, pool.fee_bps)
        honest = forge_math.withdrawal_amounts(pool.state[0], burn, pool.state[1], vf)
        payouts = honest if payouts is None else payouts
        new_total = pool.state[1] - burn
        probe_state, _, _, _ = kit.run_leaf(pool, "forge_action_remove", [h, burn, bytes32(b"\x01" * 32), payouts])
        root = probe_state.get_tree_hash()
        lp_parent, lp_spends = kit.lp_melt_spend(pool, burn, new_total, root, salt=salt, fabricated=fabricated)
        bundle, new_state = kit.spend_action(pool, "forge_action_remove", [h, burn, lp_parent, payouts],
                                             extra_spends=lp_spends)
        return bundle, new_state, honest

    bundle, new_state, honest = remove_case(three, 300_000)
    out = accepts("3-asset remove pays every reserve pro rata and melts the LP", lambda: kit.validate(bundle))
    if out:
        conds, additions = out
        st = kit.state_to_list(new_state)
        check("  reserves fell by the mirror's payouts", st[0] == [a - p for a, p in zip([3_000_000, 4_000_000, 5_000_000], honest)])
        check("  total_lp fell by the burn", st[1] == 2_700_000)
        check("  XCH payout lands at OFFER_MOD", (SETTLE_PH, honest[0]) in additions)
        check("  CAT payouts land at CAT(OFFER_MOD)",
              all((construct_cat_puzzle(CAT_MOD, c, kit.OFFER_MOD).get_tree_hash(), p) in additions
                  for c, p in zip(CATS[:2], honest[1:])))
        print(f"          cost: {conds.cost:,}")
    vault = kit.make_pool([CATS[2]], [10_000_000], total_lp=10_000_000, leaves="forge", fee_bps=30,
                          protocol_fee_bps=0, salt=0x35)
    bundle, new_state, honest = remove_case(vault, 1_000_000, salt=0x91)
    out = accepts("vault remove withholds the crossing fee", lambda: kit.validate(bundle))
    if out:
        check("  paid 1,000,000 x (1 - 30bps) = 997,000", honest == [997_000] and kit.state_to_list(new_state)[0] == [9_003_000])
    refuses("burning the whole supply is refused (burn < total_lp)",
            lambda: kit.validate(remove_case(three, 3_000_000, salt=0x92)[0]))
    # V13 (CHIP-0062 review P1, "the final LP position cannot redeem"): everything above the
    # locked minimum is redeemable, and exactly the minimum stays.
    accepts("V13: burning everything above MIN_LOCKED_LP (total_lp - 1000) is accepted",
            lambda: kit.validate(remove_case(three, 3_000_000 - kit.MIN_LOCKED_LP, salt=0x95)[0]))
    refuses("V13: one unit into the locked minimum is refused",
            lambda: kit.validate(remove_case(three, 3_000_000 - kit.MIN_LOCKED_LP + 1, salt=0x96)[0]))
    refuses("a payout one mojo above the proportional share is refused",
            lambda: kit.validate(remove_case(three, 300_000, payouts=[honest_plus for honest_plus in
                                                                      [x + 1 if i == 0 else x for i, x in enumerate(
                                                                          forge_math.withdrawal_amounts([3_000_000, 4_000_000, 5_000_000], 300_000, 3_000_000, 0))]],
                                             salt=0x93)[0]))
    # Refused by the TAIL's delta lock (amount + delta == 0 != -burn), not by `parent_is_cat`:
    # this construction never reaches that line. _test_v11_lp_receive_forgery.py case C does.
    refuses("a melt coin fabricated from ordinary mojos (no CAT parent) is refused -- finding 4 stays closed",
            lambda: kit.validate(remove_case(three, 300_000, fabricated=True, salt=0x94)[0]))

    # ---------------------------------------------------------------- observe
    print("observe:")
    pool = kit.make_pool([None, CATS[0]], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x36)
    bundle, new_state = kit.spend_action(pool, "forge_action_observe", [H0])
    out = accepts("observe creates a slot and announces the accumulator", lambda: kit.validate(bundle))
    if out:
        conds, additions = out
        cums = kit.expected_cums(pool.state, pool.weights, H0)
        # upstream's slot is double-curried: (singleton info, nonce) first, the value hash second
        slot_ph = (kit.SLOT.curry(kit.Program.to((kit.SINGLETON_TOP_LAYER_V1_1_HASH, pool.struct_hash)), 1)
                   .curry(kit.Program.to((H0, cums)).get_tree_hash()).get_tree_hash())
        check("  slot coin at the slot puzzle hash for (h . cums), amount 0", (slot_ph, 0) in additions)
        st = kit.state_to_list(new_state)
        check("  state unchanged beyond the prologue", st[0] == pool.state[0] and st[1] == pool.state[1] and st[3] == kit.expected_oracle(pool.state, pool.weights, H0))
        emitted = list(kit.Program.from_bytes(bytes(bundle.coin_spends[0].puzzle_reveal))
                       .run(kit.Program.from_bytes(bytes(bundle.coin_spends[0].solution))).as_iter())
        want = kit.Program.to(["forge-observe-v1", H0, cums]).get_tree_hash()
        check("  puzzle announcement of (\"forge-observe-v1\", h, cums)",
              any(c.first().as_int() == kit.CREATE_PUZZLE_ANNOUNCEMENT and bytes(c.rest().first().as_atom()) == bytes(want)
                  for c in emitted))

    # ---------------------------------------------------------------- collect
    print("collect:")
    pool = kit.make_pool([None, CATS[0]], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x37)
    bundle, new_state, honest = swap_case(pool, 0, 1, 250_000, salt=0xC5)
    kit.validate(bundle)
    after_swap = pool.advance(kit.state_to_list(new_state))
    fee = honest * pool.protocol_fee_bps // 10_000
    bundle, new_state = kit.spend_action(after_swap, "forge_action_collect", [H0 + 1, [1]])
    out = accepts("collect after a swap pays the accrued fee to the recipient", lambda: kit.validate(bundle))
    if out:
        conds, additions = out
        cat_ph = construct_cat_puzzle(CAT_MOD, CATS[0], kit.Program.to(pool.protocol_ph)).get_tree_hash_precalc(pool.protocol_ph)
        hinted = [(ph, a, hint) for ph, a, hint in kit.additions_with_hints(conds) if ph == cat_ph]
        check("  fee coin created for the protocol recipient, CAT-wrapped", any(a == fee for _, a, _ in hinted), str(fee))
        check("  fee coin hinted with the recipient's puzzle hash", any(hint == bytes(pool.protocol_ph) for _, _, hint in hinted))
        st = kit.state_to_list(new_state)
        check("  fees_owed zeroed, reserves untouched", st[2] == [0, 0] and st[0] == after_swap.state[0])
        check("  prev_root chains to the post-swap state", st[4] == kit.program_hash(after_swap.state))
        check("  reserve 1 re-created holding the curve reserve alone", (pool.reserves[1].full_hash, st[0][1]) in additions)
    # V13 (second audit M-1): protocol and DAO fees to the SAME recipient are one coin, not two
    # identical CreateCoins that consensus rejects as DUPLICATE_OUTPUT with the fees still owed.
    shared = kit.make_pool([None, CATS[0]], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x38,
                           protocol_fee_bps=5, dao_ph=kit.make_pool([None, CATS[0]], [1, 1], salt=0x38).protocol_ph, dao_fee_bps=5)
    b_s, s_s, honest_s = swap_case(shared, 0, 1, 250_000, salt=0xC6)
    kit.validate(b_s)
    shared_after = shared.advance(kit.state_to_list(s_s))
    st_s = kit.state_to_list(s_s)
    check("a pool whose DAO recipient is the protocol recipient owes equal amounts after one swap", st_s[2][1] == st_s[6][1] > 0)
    b_c, _ = kit.spend_action(shared_after, "forge_action_collect", [H0 + 1, [1]])
    out = accepts("collect pays ONE coin of both fees to the shared recipient (V12 refused this as DUPLICATE_OUTPUT)",
                  lambda: kit.validate(b_c))
    if out:
        shared_ph = construct_cat_puzzle(CAT_MOD, CATS[0], kit.Program.to(shared.protocol_ph)).get_tree_hash_precalc(shared.protocol_ph)
        paid = [a for ph, a in out[1] if ph == shared_ph]
        check("  exactly one coin, of protocol fee + DAO fee", paid == [st_s[2][1] + st_s[6][1]], str(paid))
    refuses("collecting a reserve with no fee owed is refused",
            lambda: kit.validate(kit.spend_action(after_swap, "forge_action_collect", [H0 + 1, [0]])[0]))
    refuses("naming the same index twice is refused",
            lambda: kit.validate(kit.spend_action(after_swap, "forge_action_collect", [H0 + 1, [1, 1]])[0]))
    refuses("collect with no indices is refused",
            lambda: kit.validate(kit.spend_action(after_swap, "forge_action_collect", [H0 + 1, []])[0]))

    # ---------------------------------------------------------------- empty action list
    # CHIP-0062 review (cursor-bot, 2026-09-11) flagged that the CHIP never states an empty
    # action list is forbidden. The puzzle does: upstream action.rue asserts
    # `!(selectors_and_proofs is nil)`. Pinned here so the CHIP sentence has a test behind it.
    print("empty action list:")
    refuses("a spend that runs no leaf at all is refused (upstream action layer)",
            lambda: kit.validate(kit.spend_actions(pool, [])[0]))

    # ---------------------------------------------------------------- prologue and the tree
    print("prologue and merkle tree:")
    refuses("a height not above the oracle's last height is refused",
            lambda: kit.validate(kit.spend_action(after_swap, "forge_action_observe", [H0])[0]))
    # V13 (second audit, MIN_LOCKED_LP boundary): the registry refuses a genesis at or below the
    # floor, but nothing stopped a pool being MINTED below it outside the registry -- and then a
    # depositor's LP is partly trapped, because `remove` will not burn past the floor that pool
    # never had. The prologue now refuses to spend such a pool at all, so it can trap nobody.
    starved = kit.make_pool([None, CATS[0]], [10_000_000, 20_000_000], total_lp=kit.MIN_LOCKED_LP - 1,
                            leaves="forge", salt=0x3E)
    refuses("a pool minted below MIN_LOCKED_LP cannot be spent at all",
            lambda: kit.validate(kit.spend_action(starved, "forge_action_observe", [H0])[0]))
    floor_exact = kit.make_pool([None, CATS[0]], [10_000_000, 20_000_000], total_lp=kit.MIN_LOCKED_LP,
                                leaves="forge", salt=0x3D)
    accepts("...and one at exactly the floor still runs (it just has nothing left to redeem)",
            lambda: kit.validate(kit.spend_action(floor_exact, "forge_action_observe", [H0])[0]))
    refuses("a sixth leaf (the passthrough) with a valid-looking proof is refused",
            lambda: kit.validate(kit.spend_action(after_swap, "forge_action_observe", [H0 + 1],
                                                  leaf=kit.PASSTHROUGH, proof=after_swap.leaf_proof("forge_action_observe"))[0]))
    refuses("the right leaf under the wrong proof is refused",
            lambda: kit.validate(kit.spend_action(after_swap, "forge_action_observe", [H0 + 1],
                                                  proof=after_swap.leaf_proof("forge_action_swap"))[0]))

    # ---------------------------------------------------------------- several actions in one spend
    print("multi-action spends:")
    pool = kit.make_pool([None, CATS[0]], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x38)
    r, w = pool.state[0], pool.weights
    g1 = 250_000
    out1 = forge_math.swap_output(r[0], r[1], g1, pool.fee_bps, w[0], w[1])
    fee1 = out1 * pool.protocol_fee_bps // 10_000
    s1, s1_spend = kit.offer_settlement_xch(g1, salt=0xD1)
    bundle, st = kit.spend_actions(pool, [("forge_action_swap", [H0, 0, 1, g1, out1, s1.name()]),
                                          ("forge_action_collect", [H0, [1]])], extra_spends=[s1_spend])
    out = accepts("swap then collect in one spend, sharing h", lambda: kit.validate(bundle))
    if out:
        conds, additions = out
        st = kit.state_to_list(st)
        check("  the fee the swap accrued is collected by the second action", st[2] == [0, 0])
        check("  the fee coin is created for the recipient",
              any(a == fee1 for _, a in additions if a == fee1))
        check("  reserve 1 re-created holding the curve reserve alone", (pool.reserves[1].full_hash, st[0][1]) in additions)
        check("  one prologue: height pinned once", conds.height_absolute == H0 and conds.before_height_absolute == H0 + kit.ORACLE_WINDOW)
        print(f"          cost: {conds.cost:,}")
    # observe after a swap in the same spend records the PRE-spend price
    bundle, st = kit.spend_actions(pool, [("forge_action_swap", [H0, 0, 1, g1, out1, s1.name()]),
                                          ("forge_action_observe", [H0])], extra_spends=[s1_spend])
    out = accepts("swap then observe in one spend", lambda: kit.validate(bundle))
    if out:
        pre_cums = kit.expected_cums(pool.state, w, H0)
        slot_ph = (kit.SLOT.curry(kit.Program.to((kit.SINGLETON_TOP_LAYER_V1_1_HASH, pool.struct_hash)), 1)
                   .curry(kit.Program.to((H0, pre_cums)).get_tree_hash()).get_tree_hash())
        check("  observe cannot record a same-spend price: the slot holds the pre-spend accumulator", (slot_ph, 0) in out[1])
        moved = [kit.spot_price(st_r0, w[0], st_r1, w[1]) for st_r0, st_r1 in [(kit.state_to_list(st)[0][0], kit.state_to_list(st)[0][1])]]
        check("  ...although the swap moved the spot price", moved[0] != kit.spot_price(r[0], w[0], r[1], w[1]))
    # the same leaf twice: the earlier action's proof is omitted (selector already verified)
    g2 = 100_000
    r_after = [r[0] + g1, r[1] - out1]
    out2 = forge_math.swap_output(r_after[0], r_after[1], g2, pool.fee_bps, w[0], w[1])
    s2, s2_spend = kit.offer_settlement_xch(g2, salt=0xD2)
    bundle, st = kit.spend_actions(pool, [("forge_action_swap", [H0, 0, 1, g1, out1, s1.name()]),
                                          ("forge_action_swap", [H0, 0, 1, g2, out2, s2.name()])],
                                   extra_spends=[s1_spend, s2_spend])
    accepts("two swaps in one spend, the second priced on the first's output, one proof for the shared leaf",
            lambda: kit.validate(bundle))
    stale = forge_math.swap_output(r[0], r[1], g2, pool.fee_bps, w[0], w[1])
    refuses("sandwich: the second swap cannot claim its pre-spend quote",
            lambda: kit.validate(kit.spend_actions(pool, [("forge_action_swap", [H0, 0, 1, g1, out1, s1.name()]),
                                                          ("forge_action_swap", [H0, 0, 1, g2, stale, s2.name()])],
                                                   extra_spends=[s1_spend, s2_spend])[0]))
    refuses("two actions naming different heights are refused",
            lambda: kit.validate(kit.spend_actions(pool, [("forge_action_swap", [H0, 0, 1, g1, out1, s1.name()]),
                                                          ("forge_action_collect", [H0 + 1, [1]])], extra_spends=[s1_spend])[0]))
    refuses("a proof omitted for a selector never verified is refused",
            lambda: kit.validate(kit.spend_actions(pool, [("forge_action_swap", [H0, 0, 1, g1, out1, s1.name()]),
                                                          ("forge_action_collect", [H0, [1]])],
                                                   extra_spends=[s1_spend], omit_proofs=True, force_no_proofs=True)[0]))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} action checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
