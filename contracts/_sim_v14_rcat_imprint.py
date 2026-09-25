#!/usr/bin/env python3
"""The rCAT imprint: a CAT wearing a revocation layer it was never issued with.

Raised at the 2026-09-24 AMA: a plain CAT can be made to LOOK like a revocable CAT
(CHIP-0038). If a pool ever decides "this asset is an rCAT, revocable by H" from what a
coin looks like, an attacker who imprints the pattern chooses H -- and with it who can
revoke the pool's reserve. Everything here runs the REAL revocation layer (chia_puzzles_py
REVOCATION_LAYER, the one chia's RCATWallet uses) through the REAL V14 puzzles and the
mempool's validator; nothing is a stand-in.

  A  what an rCAT is on chain, and what a fake is      (the layer, the CAT layer, chia's own classifier)
  B  every lane into a V14 pool, with each kind of layered coin      (the shipped puzzles)
  C  a pool that DID accept the layer: where the fake enters, and why revocation bricks it
  D  stock splits and merges: what happens when the pool is not a party to them

The layer, read off the disassembly (args MOD_HASH, HIDDEN_PUZZLE_HASH, INNER_PUZZLE_HASH;
solution (hidden puzzle solution)):
  hidden path   sha256tree(puzzle) == HIDDEN_PUZZLE_HASH; conditions pass through UNTOUCHED
  inner path    sha256tree(puzzle) == INNER_PUZZLE_HASH;  every CREATE_COIN is rewritten to
                revocation(HIDDEN_PUZZLE_HASH, ph), so the owner can never remove the layer

Exit 0 if every probe behaved as reported, 1 otherwise, 2 if the V14 build is absent.
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia.types.blockchain_format.program import Program
from chia.wallet.cat_wallet.cat_utils import (
    CAT_MOD, CAT_MOD_HASH_HASH, QUOTED_CAT_MOD_HASH, SpendableCAT, construct_cat_puzzle,
    unsigned_spend_bundle_for_spendable_cats,
)
from chia.wallet.util.curry_and_treehash import curry_and_treehash, shatree_atom
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH
from chia.wallet.uncurried_puzzle import uncurry_puzzle
from chia.wallet.vc_wallet.vc_drivers import REVOCATION_LAYER, create_revocation_layer, match_revocation_layer
from chia_rs import Coin, G1Element, G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import _v14_testkit as kit
import _test_v14_registry as regsuite
import forge_math

results = []
X = bytes32(b"\xd0" * 32)                 # one asset id throughout: a plain CAT, as issued
H0 = 6_999_990
IDENTITY = Program.to(1)                  # stands in for a holder's p2: whoever holds it spends it
CREATE_COIN = 51


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def verdict(thunk):
    """('ACCEPTED', result) or ('refused <code>', None). A harness failure is NOT a refusal."""
    try:
        return "ACCEPTED", thunk()
    except kit.Rejected as exc:
        return "refused " + str(exc).split(": ")[-1].split(" ")[0], None
    except Exception as exc:
        reason = kit.refusal_reason(exc)
        if reason is None:
            raise
        return "refused locally (clvm)", None


# ---- the hidden puzzles --------------------------------------------------------------------
# A revoker's hidden puzzle: an AGG_SIG_UNSAFE on its key, then whatever conditions the solution
# names. The offline validator collects the signature requirement without checking it; on chain
# only the key holder can satisfy it. That is all "revocable by H" means.

def hidden_puzzle(tag: bytes) -> Program:
    pk = bytes(G1Element.generator())
    return Program.to([4, (1, [49, pk, tag]), 1])     # (c (q 49 pk tag) 1)


ISSUER = hidden_puzzle(b"issuer")         # what the real issuer committed to at issuance
ATTACKER = hidden_puzzle(b"attacker")     # what an imprinter chooses
ANYONE = IDENTITY                         # H = sha256tree(1): the solution IS the conditions -- no key at all
H_ISSUER, H_ATTACKER, H_ANYONE = (bytes32(p.get_tree_hash()) for p in (ISSUER, ATTACKER, ANYONE))


def rev(h: bytes32, inner_hash: bytes32) -> Program:
    return create_revocation_layer(h, inner_hash)


def _cat_ph(inner_hash: bytes32) -> bytes32:
    """CAT(X, inner) by hash, so no inner reveal is needed."""
    return bytes32(curry_and_treehash(QUOTED_CAT_MOD_HASH, CAT_MOD_HASH_HASH, shatree_atom(X), inner_hash))


def fabricated_cat(inner: Program, amount: int, salt: int):
    """A coin of X at CAT(X, inner), with a lineage the CAT layer accepts: not an issuance."""
    grandparent = bytes32(bytes([salt]) * 32)
    parent = kit.coin_id(grandparent, _cat_ph(IDENTITY.get_tree_hash()), amount)
    coin = Coin(parent, _cat_ph(inner.get_tree_hash()), uint64(amount))
    return coin, LineageProof(grandparent, IDENTITY.get_tree_hash(), uint64(amount))


def run_cats(*spendables):
    return kit.validate(SpendBundle(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, list(spendables)).coin_spends,
                                    G2Element()))


def classify(full_puzzle: Program):
    """chia's RCATWallet classifier, exactly as wallet_state_manager runs it: the puzzle is a
    CAT, and its inner puzzle uncurries to REVOCATION_LAYER. The hidden hash comes FROM THE COIN."""
    u = uncurry_puzzle(full_puzzle)
    if u.mod != CAT_MOD:
        return None
    inner = uncurry_puzzle(u.args.at("rrf"))
    return ("rCAT", bytes32(match_revocation_layer(inner)[0])) if inner.mod == REVOCATION_LAYER else ("CAT", None)


def name_of(h):
    return {H_ISSUER: "issuer", H_ATTACKER: "ATTACKER", H_ANYONE: "ANYONE"}.get(h, h.hex()[:12] if h else "-")


# ---- A: what an rCAT is, and what a fake is ------------------------------------------------------

def part_a():
    print("=" * 92)
    print("A. WHAT AN rCAT IS ON CHAIN, AND WHAT A FAKE ONE IS")
    print("=" * 92)
    amount = 1_000
    p2 = IDENTITY.get_tree_hash()

    print("\nA1. A plain CAT holder imprints the pattern, with no issuer involved:")
    coin, lin = fabricated_cat(IDENTITY, amount, 0xA1)
    imprinted = rev(H_ATTACKER, p2)
    v, out = verdict(lambda: run_cats(SpendableCAT(coin, X, IDENTITY, Program.to([[CREATE_COIN, imprinted.get_tree_hash(), amount]]),
                                                   lineage_proof=lin)))
    check(f"a plain X coin re-issues itself inside a revocation layer: {v}", v == "ACCEPTED")
    check("  the child is at CAT(X, revocation(H_attacker, p2)) -- same asset id, no TAIL ran",
          out is not None and (_cat_ph(imprinted.get_tree_hash()), amount) in out[1])
    full = construct_cat_puzzle(CAT_MOD, X, imprinted)
    c = classify(full)
    check(f"  chia's RCATWallet classifier calls it: {c[0]}, revocable by {name_of(c[1])}",
          c == ("rCAT", H_ATTACKER))

    print("\nA2. The genuine article, beside it:")
    genuine = construct_cat_puzzle(CAT_MOD, X, rev(H_ISSUER, p2))
    g = classify(genuine)
    check(f"  a coin the issuer minted classifies as: {g[0]}, revocable by {name_of(g[1])}", g == ("rCAT", H_ISSUER))
    ug, uf = uncurry_puzzle(uncurry_puzzle(genuine).args.at("rrf")), uncurry_puzzle(uncurry_puzzle(full).args.at("rrf"))
    same_but_h = (ug.mod == uf.mod and ug.args.at("f") == uf.args.at("f") and ug.args.at("rrf") == uf.args.at("rrf")
                  and ug.args.at("rf") != uf.args.at("rf"))
    check("  the two puzzles are the same program, the same asset, the same p2 -- they differ ONLY in the curried H",
          same_but_h)
    print("""
   So there is no such thing as "a genuine rCAT coin" to detect. The layer is a public
   puzzle any holder can put on any coin of any CAT. What differs between the issuer's coin
   and the imprint is ONE value, H, and a coin cannot tell you which H is the right one.
   Genuineness is a claim about (asset id, H), and only the issuer can make it.""")

    print("\nA3. The inner path cannot remove the layer:")
    coin3, lin3 = fabricated_cat(rev(H_ATTACKER, p2), amount, 0xA3)
    v, out = verdict(lambda: run_cats(SpendableCAT(
        coin3, X, rev(H_ATTACKER, p2), Program.to([0, IDENTITY, [[CREATE_COIN, p2, amount]]]), lineage_proof=lin3)))
    check(f"  the owner asks for a PLAIN child: {v}", v == "ACCEPTED")
    check("  ...and gets revocation(H, plain) -- the layer re-wraps every CREATE_COIN",
          out is not None and (_cat_ph(rev(H_ATTACKER, p2).get_tree_hash()), amount) in out[1]
          and (_cat_ph(p2), amount) not in out[1])

    print("\nA4. The hidden path can remove it -- and can do anything else:")
    v, out = verdict(lambda: run_cats(SpendableCAT(
        coin3, X, rev(H_ATTACKER, p2), Program.to([1, ATTACKER, [[CREATE_COIN, p2, amount]]]), lineage_proof=lin3)))
    check(f"  the H holder spends it with no p2 signature: {v}", v == "ACCEPTED")
    check("  the child is a PLAIN CAT X coin: the 'rCAT' was always fungible with plain X",
          out is not None and (_cat_ph(p2), amount) in out[1])

    print("\nA5. A layer nobody holds the key to -- H = sha256tree(1):")
    coin5, lin5 = fabricated_cat(rev(H_ANYONE, p2), amount, 0xA5)
    thief = bytes32(b"\x7e" * 32)
    v, out = verdict(lambda: run_cats(SpendableCAT(
        coin5, X, rev(H_ANYONE, p2), Program.to([1, ANYONE, [[CREATE_COIN, thief, amount]]]), lineage_proof=lin5)))
    check(f"  a STRANGER takes the coin through the hidden path, no key, no signature: {v}", v == "ACCEPTED")
    check(f"  and chia's classifier still calls it: {classify(construct_cat_puzzle(CAT_MOD, X, rev(H_ANYONE, p2)))[0]}",
          classify(construct_cat_puzzle(CAT_MOD, X, rev(H_ANYONE, p2)))[0] == "rCAT")

    print("\nA6. A genuine outer layer does not vouch for what is inside it:")
    nested_p2 = rev(H_ATTACKER, p2)
    coin6, lin6 = fabricated_cat(rev(H_ISSUER, p2), amount, 0xA6)
    v, out = verdict(lambda: run_cats(SpendableCAT(
        coin6, X, rev(H_ISSUER, p2), Program.to([0, IDENTITY, [[CREATE_COIN, nested_p2.get_tree_hash(), amount]]]),
        lineage_proof=lin6)))
    stacked = construct_cat_puzzle(CAT_MOD, X, rev(H_ISSUER, nested_p2.get_tree_hash()))
    check(f"  the owner of a genuine coin puts a SECOND layer inside it: {v}",
          v == "ACCEPTED" and out is not None and (stacked.get_tree_hash(), amount) in out[1])
    s = classify(stacked)
    check(f"  the classifier reports only the outer one: {s[0]}, revocable by {name_of(s[1])} -- the attacker's is invisible",
          s == ("rCAT", H_ISSUER))
    print("""
   What an rCAT is, then: (asset id, H), where H is the one the ISSUER committed to. Not the
   layer's presence, which anyone can add; not the layer's H, which anyone can choose; not
   even a correct outer H, which can wrap a second revoker. The chain records the issuer's
   choice in exactly one place -- the coins the TAIL's issuance spend created -- and
   nowhere after it.""")


# ---- B: V14 as shipped, against every kind of layered coin ---------------------------------------

LAYERS = [("the issuer's genuine H", H_ISSUER, ISSUER), ("an imprinted attacker H", H_ATTACKER, ATTACKER),
          ("H = anyone-can-revoke", H_ANYONE, ANYONE)]


def part_b():
    print("\n" + "=" * 92)
    print("B. EVERY LANE INTO A V14 POOL, WITH EVERY KIND OF LAYERED COIN (the shipped puzzles)")
    print("=" * 92)
    pool = kit.make_pool([None, X], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x40)
    r, w = pool.state[0], pool.weights
    gross = 400_000
    out_amt = forge_math.swap_output(r[1], r[0], gross, pool.fee_bps, w[1], w[0])

    def swap_in_x(settlement_coin, spendable):
        return kit.spend_action(pool, "forge_action_swap", [H0, 1, 0, gross, out_amt, *kit.settlement_ref(settlement_coin)],
                                extra_cats={X: [spendable]})[0]

    print("\nB1. The swap/add lane -- the only lane a stranger can reach. X in, XCH out:")
    c, c_cat = kit.offer_settlement_cat(X, gross, salt=0xC8)
    v, _ = verdict(lambda: kit.validate(swap_in_x(c, c_cat)))
    check(f"  a plain X offer settlement: {v}", v == "ACCEPTED")
    for label, h, _hp in LAYERS:
        # An rCAT holder's offer settlement comes out of the inner path wrapped: revocation(H, OFFER_MOD).
        inner = rev(h, bytes32(OFFER_MOD_HASH))
        grand = bytes32(b"\xc9" * 32)
        parent = kit.coin_id(grand, _cat_ph(inner.get_tree_hash()), gross)
        sc = Coin(parent, _cat_ph(inner.get_tree_hash()), uint64(gross))
        spendable = SpendableCAT(sc, X, inner, Program.to([0, OFFER_MOD, [[sc.name()]]]),
                                 lineage_proof=LineageProof(grand, inner.get_tree_hash(), uint64(gross)))
        v, _ = verdict(lambda: kit.validate(swap_in_x(sc, spendable)))
        check(f"  an X settlement behind {label}: {v} (132 = the derived plain settlement was never spent)",
              v == "refused 132")
    print("""
   The leaf derives the settlement id as coinid(parent, CAT(X, OFFER_MOD), amount). A layered
   settlement is at CAT(X, revocation(H, OFFER_MOD)), so its id is not the derived one,
   whatever H is. The CAT ring still balances -- the refusal is the leaf's, not conservation's.""")

    print("B2. The creation lane -- a reserve launcher that creates a layered reserve:")
    reg0 = kit.make_registry(salt=0x21)
    reg = reg0.advance([1, 0])
    slots = {kit.MIN_KEY: (reg0.coin, reg0.inner_hash), kit.MAX_KEY: (reg0.coin, reg0.inner_hash)}
    left, right = (kit.MIN_KEY, kit.ZERO_32, kit.MIN_KEY), (kit.MAX_KEY, kit.ZERO_32, kit.MAX_KEY)
    gpool = kit.make_pool([None, X], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x50)
    base = regsuite.registration(reg, gpool, left, right, slots, with_reserves=False)[0]
    for label, h, _hp in LAYERS:
        layered = rev(h, gpool.reserves[1].inner_hash).get_tree_hash()
        spends = [*base.coin_spends, *kit.reserve_launcher_spends(gpool, created={1: layered})]
        v, _ = verdict(lambda: kit.validate(SpendBundle(spends, G2Element())))
        check(f"  a genesis reserve behind {label}: {v} (12 = register's announcement never made)", v == "refused 12")

    print("B3. A layered coin substituted for a live reserve (same parent, same amount):")
    res = pool.reserves[1]
    for label, h, _hp in LAYERS:
        v, _ = verdict(lambda: kit.validate(spend_layered(pool, "forge_action_observe", [H0], {1: (h, None)})[0]))
        check(f"  a reserve coin behind {label}: {v} (147 = the finalizer's message has no receiver)", v == "refused 147")
    print("""
   The finalizer messages coinid(parent, CAT(X, p2_delegated_by_singleton), amount) and no
   other id; a layered coin is a different id with the same parent and amount.""")

    print("B4. A plain coin that USED to be an rCAT (the issuer stripped it via the hidden path):")
    check("  it is at CAT(X, p2) exactly, so it is an ordinary reserve -- and has no hidden path left to revoke",
          _cat_ph(res.inner_hash) == res.full_hash)
    # The issuer's only remaining route is to spend the reserve like anyone else: as its p2.
    rogue = Program.to((1, [[CREATE_COIN, bytes32(b"\x7e" * 32), int(res.coin.amount)]]))
    v, _ = verdict(lambda: run_cats(SpendableCAT(res.coin, X, res.inner, Program.to([pool.inner_hash, rogue]),
                                                 lineage_proof=res.lineage)))
    check(f"  the issuer spends that reserve without the pool: {v} (the p2 needs the pool singleton's message)",
          v == "refused 147")
    print("""
   VERDICT FOR V14: the pool never interprets any coin as an rCAT. It admits exactly one
   shape per asset -- CAT(asset, p2_delegated_by_singleton) -- through every lane, and a
   layered coin of any H, genuine or imprinted or keyless, is a different puzzle hash and is
   refused. The AMA's brick is not reachable in the shipped protocol. It becomes reachable
   the moment someone teaches the pool the layer, which is what part C measures.""")


def spend_layered(pool, name, solution, layered: dict, extra_cats=None):
    """spend_action, with reserve i's coin replaced by one behind revocation(H, p2) and spent
    through the layer's INNER path. layered = {index: (H, Coin | None[, LineageProof])}; a None
    coin means same parent and amount as the reserve it replaces (and so the same lineage)."""
    new_state, tagged_conds, _base, _eph = kit.run_leaf(pool, name, solution)
    inner_solution = Program.to([[pool.leaves[name]], [[kit.SINGLE_LEAF_SELECTOR, *pool.leaf_proof(name)]],
                                 [kit.with_birth(pool, solution)]])
    spends = [kit.make_spend(pool.coin, kit.puzzle_for_singleton(pool.launcher_id, pool.inner),
                             kit.solution_for_singleton(pool.lineage, uint64(1), inner_solution))]
    cats = {k: list(v) for k, v in (extra_cats or {}).items()}
    for r in pool.reserves:
        dp = kit.delegated_puzzle_for(pool, r, new_state, tagged_conds)
        p2_solution = Program.to([pool.inner_hash, dp])
        if r.index in layered:
            h, coin, *lineage = layered[r.index]
            wrapped = rev(h, r.inner_hash)
            # the reserve's OWN asset, not the module's X: a live pool's asset is not X
            coin = coin or Coin(r.coin.parent_coin_info,
                                bytes32(construct_cat_puzzle(CAT_MOD, r.asset_id, wrapped).get_tree_hash()), r.coin.amount)
            cats.setdefault(r.asset_id, []).append(
                SpendableCAT(coin, r.asset_id, wrapped, Program.to([0, r.inner, p2_solution]),
                             lineage_proof=lineage[0] if lineage else r.lineage))
        elif r.asset_id is None:
            spends.append(kit.make_spend(r.coin, r.inner, p2_solution))
        else:
            cats.setdefault(r.asset_id, []).append(SpendableCAT(r.coin, r.asset_id, r.inner, p2_solution, lineage_proof=r.lineage))
    for spendables in cats.values():
        spends.extend(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, spendables).coin_spends)
    return SpendBundle(spends, G2Element()), new_state


# ---- C: a pool that accepted the layer -----------------------------------------------------------

def layered_pool(h: bytes32, salt: int):
    """What 'adding rCAT support' costs in the finalizer: nothing but its curried full hash."""
    plain = kit.make_pool([None, X], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=salt)
    full = _cat_ph(rev(h, plain.reserves[1].inner_hash).get_tree_hash())
    pool = kit.make_pool([None, X], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=salt,
                         reserve_full_hashes=[plain.reserves[0].full_hash, full])
    return pool, full


def part_c():
    print("\n" + "=" * 92)
    print("C. A POOL THAT ACCEPTED THE LAYER: WHERE THE FAKE ENTERS, AND WHAT REVOCATION DOES")
    print("=" * 92)

    print("\nC1. The finalizer, curried with a layered full hash and otherwise untouched:")
    pool, full = layered_pool(H_ATTACKER, 0x60)
    v, _ = verdict(lambda: kit.validate(spend_layered(pool, "forge_action_observe", [H0], {1: (H_ATTACKER, None)})[0]))
    check(f"  observe, with the X reserve behind revocation(H, p2): {v}", v == "ACCEPTED")
    r, w = pool.state[0], pool.weights
    gross = 250_000
    out_amt = forge_math.swap_output(r[0], r[1], gross, pool.fee_bps, w[0], w[1])
    s, s_spend = kit.offer_settlement_xch(gross, salt=0xC0)
    _st, tagged_conds, _b, _e = kit.run_leaf(pool, "forge_action_swap", [H0, 0, 1, gross, out_amt, *kit.settlement_ref(s)])
    payout_ph = [bytes32(c.rest().first().as_atom()) for i, c in tagged_conds if i == 1 and c.first().as_int() == CREATE_COIN][0]
    b, _ = spend_layered(pool, "forge_action_swap", [H0, 0, 1, gross, out_amt, *kit.settlement_ref(s)], {1: (H_ATTACKER, None)})
    b = SpendBundle([*b.coin_spends, s_spend], G2Element())
    v, out = verdict(lambda: kit.validate(b))
    check(f"  a swap XCH -> X through that reserve: {v}", v == "ACCEPTED")
    check("  the recreated reserve came back wearing the layer (the inner path re-wrapped the finalizer's CREATE_COIN)",
          out is not None and any(ph == full for ph, _a in out[1]))
    check("  and the trader's payout did too: they received revocation(H_attacker, settlement), not plain X",
          out is not None and any(ph == _cat_ph(rev(H_ATTACKER, payout_ph).get_tree_hash()) for ph, _a in out[1]))
    print("""
   So rCAT "support" is one curried hash away, and the puzzles would carry it without a
   complaint. That is exactly why V14 pins the refusal with a test: the arithmetic would let
   it through. The question it leaves is the AMA's -- WHICH H gets curried.""")

    print("C2. Where the fake enters -- a pool that takes H from the coin it was shown:")
    print("""   If H comes from the creator's coin (what chia's RCATWallet classifier would hand you), an
   imprinter creates "the X pool" with H = theirs. Plain X holders cannot even deposit into
   it without first wearing the attacker's layer -- and every coin that leaves it as a payout
   wears it too, as C1 just showed. Then:""")
    res = pool.reserves[1]
    lay_coin = Coin(res.coin.parent_coin_info, full, res.coin.amount)
    wrapped = rev(H_ATTACKER, res.inner_hash)
    thief = bytes32(b"\x7e" * 32)
    v, _ = verdict(lambda: run_cats(SpendableCAT(lay_coin, X, wrapped,
                                                 Program.to([1, ATTACKER, [[CREATE_COIN, thief, int(lay_coin.amount)]]]),
                                                 lineage_proof=res.lineage)))
    check(f"  the imprinter revokes the pool's X reserve with no pool spend at all: {v}", v == "ACCEPTED")
    # "Put it back": the revoker recreates a coin at the SAME puzzle hash and amount. Its parent is
    # the revoked reserve, not the parent the pool's state records -- so the finalizer cannot see it.
    v, _ = verdict(lambda: run_cats(SpendableCAT(lay_coin, X, wrapped,
                                                 Program.to([1, ATTACKER, [[CREATE_COIN, wrapped.get_tree_hash(), int(lay_coin.amount)]]]),
                                                 lineage_proof=res.lineage)))
    check(f"  or revokes it and puts an identical coin straight back: {v}", v == "ACCEPTED")
    back = Coin(lay_coin.name(), full, lay_coin.amount)
    back_lineage = LineageProof(lay_coin.parent_coin_info, wrapped.get_tree_hash(), lay_coin.amount)
    # Control: the replacement is a sound CAT coin in its own right, so whatever refuses below is the finalizer.
    v, _ = verdict(lambda: run_cats(SpendableCAT(back, X, wrapped,
                                                 Program.to([1, ATTACKER, [[CREATE_COIN, thief, int(back.amount)]]]),
                                                 lineage_proof=back_lineage)))
    check(f"  (control) the replacement coin and its lineage are valid on their own: {v}", v == "ACCEPTED")
    v, _ = verdict(lambda: kit.validate(spend_layered(pool, "forge_action_observe", [H0], {1: (H_ATTACKER, back, back_lineage)})[0]))
    check(f"  the pool, offered the identical replacement coin (with its own valid lineage): {v} -- frozen, XCH side included",
          v == "refused 147")
    print("""
   Revocation does not have to steal to brick. The finalizer names a reserve by (parent,
   hash, amount) with the parent read from state; after ANY hidden-path spend -- theft,
   burn, or a faithful put-back -- the coin state names is spent and nothing can replace it.
   A pool cannot rewrite its own state without spending, and it cannot spend.""")

    print("C3. The defence against the FAKE (not against the issuer):")
    genuine_pool, gfull = layered_pool(H_ISSUER, 0x61)
    check("  the same X pool curried with the issuer's H and with an imprinter's H are different puzzle hashes",
          genuine_pool.coin.puzzle_hash != pool.coin.puzzle_hash and gfull != full)
    v, _ = verdict(lambda: kit.validate(spend_layered(genuine_pool, "forge_action_observe", [H0], {1: (H_ATTACKER, None)})[0]))
    check(f"  an imprinted coin substituted into the issuer-H pool: {v}", v == "refused 147")
    v, _ = verdict(lambda: kit.validate(spend_layered(genuine_pool, "forge_action_observe", [H0], {1: (H_ISSUER, None)})[0]))
    check(f"  the genuine coin in the same pool: {v}", v == "ACCEPTED")
    print("""
   If H is a GENESIS PARAMETER -- curried, part of the pool's identity, never read from a
   deposit -- the imprint cannot get in: a coin with any other H is a different puzzle hash,
   and the exact-hash rule does the rest, as it does today. The same discipline as lpRatio and
   the config binding: never accept an identifier you can derive or verify.

   What that does NOT solve is the issuer.""")

    print("C4. A fake riding with a real one, in the issuer-H pool (the AMA's combination concern):")
    gr = genuine_pool.reserves[1]
    rider_inner = rev(H_ATTACKER, IDENTITY.get_tree_hash())
    rider, rider_lin = fabricated_cat(rider_inner, 777, 0xC4)
    rider_spend = SpendableCAT(rider, X, rider_inner, Program.to([0, IDENTITY, [[CREATE_COIN, IDENTITY.get_tree_hash(), 777]]]),
                               lineage_proof=rider_lin)
    r, w = genuine_pool.state[0], genuine_pool.weights
    g_out = forge_math.swap_output(r[0], r[1], gross, genuine_pool.fee_bps, w[0], w[1])
    s4, s4_spend = kit.offer_settlement_xch(gross, salt=0xC4)
    b, _ = spend_layered(genuine_pool, "forge_action_swap", [H0, 0, 1, gross, g_out, *kit.settlement_ref(s4)],
                         {1: (H_ISSUER, None)}, extra_cats={X: [rider_spend]})
    v, out = verdict(lambda: kit.validate(SpendBundle([*b.coin_spends, s4_spend], G2Element())))
    check(f"  a swap whose X ring carries the genuine reserve AND an imprinted coin: {v}", v == "ACCEPTED")
    check("  the reserve came back under the ISSUER's H -- the rider's H reached nothing of the pool's",
          out is not None and any(ph == gfull for ph, _a in out[1])
          and not any(ph == _cat_ph(rev(H_ATTACKER, gr.inner_hash).get_tree_hash()) for ph, _a in out[1]))
    check("  the rider's value left under its own layer, to its own owner",
          out is not None and (_cat_ph(rider_inner.get_tree_hash()), 777) in out[1])
    # A twin wearing the ISSUER's H at the pool's own p2, same amount as the reserve: anyone can
    # make one, and it is a genuine-shaped coin at the reserve's exact puzzle hash, with a lineage
    # the CAT layer accepts. (That it cannot disturb the pool while it merely EXISTS is a coin-store
    # question, answered on the local chain: scripts/sim-v14-rcat-imprint.py part 5.)
    twin, twin_lin = fabricated_cat(rev(H_ISSUER, gr.inner_hash), int(gr.coin.amount), 0xC5)
    check("  anyone can build a twin at the issuer-H reserve's exact puzzle hash", twin.puzzle_hash == gfull)
    v, _ = verdict(lambda: kit.validate(spend_layered(genuine_pool, "forge_action_observe", [H0], {1: (H_ISSUER, twin, twin_lin)})[0]))
    check(f"  and it cannot stand in for the reserve: {v} (the finalizer names the reserve by the parent in state)",
          v == "refused 147")
    print("""
   Mixing is not a lever. A CAT ring balances by asset id, so a fake and a real coin can ride
   in one bundle -- but the pool never reads H from a coin, the finalizer recreates the reserve
   itself under the genesis H, and a coin with another H can only pay its value to its own
   outputs. There is no runtime rCAT flag for a fake to set; that is the design rule, not luck.
   What WOULD be trippable is any design that decides revocability at run time from what it
   is shown -- an "is this an rCAT?" check on deposits, a mode switch, an auto-quarantine.""")
    print("""
   None of that protects against the issuer: H_issuer can do everything C2 did. So the genesis
   H settles WHO can brick the pool; it cannot stop them from doing it. That needs part D's
   design -- revocation that goes through the pool rather than around it.""")


# ---- D: splits and merges ------------------------------------------------------------------------

def arbitrage_to(res, target_price_num, target_price_den, fee_bps):
    """Trade the pool until its spot price of X (XCH per X = res[0]/res[1]) reaches the target,
    through the real curve. Returns the reserves after."""
    x, y = res
    lo, hi, best = 0, 0, None
    if x * target_price_den > target_price_num * y:          # X too expensive in the pool: sell X in
        lo, hi = 1, y * 4
        while lo <= hi:
            m = (lo + hi) // 2
            out = forge_math.swap_output(y, x, m, fee_bps)
            if (x - out) * target_price_den >= target_price_num * (y + m):
                best, lo = (x - out, y + m), m + 1
            else:
                hi = m - 1
    else:                                                    # X too cheap: buy X with XCH
        lo, hi = 1, x * 4
        while lo <= hi:
            m = (lo + hi) // 2
            out = forge_math.swap_output(x, y, m, fee_bps)
            if (x + m) * target_price_den <= target_price_num * (y - out):
                best, lo = (x + m, y - out), m + 1
            else:
                hi = m - 1
    return best or res


def part_d():
    print("\n" + "=" * 92)
    print("D. STOCK SPLITS AND MERGES: WHAT HAPPENS WHEN THE POOL IS NOT A PARTY")
    print("=" * 92)
    x0, y0, fee = 100 * 10**12, 200_000_000, 30         # 100 XCH : 200,000 X  ->  0.0005 XCH per X
    print(f"\n   Pool: {x0/1e12:.0f} XCH + {y0/1e3:,.0f} X, fee {fee} bps. LP value measured in XCH at the TRUE price.\n")
    print(f"   {'corporate action':<22} {'pool treated as':<34} {'LP value after':>15} {'vs correct':>11}")
    rows = []
    for label, k_num, k_den in (("2:1 split", 2, 1), ("10:1 split", 10, 1), ("1:2 merge", 1, 2), ("1:10 merge", 1, 10)):
        # The true price per NEW unit after a k:1 split is p/k; a holder of y old units holds y*k new ones.
        p_num, p_den = x0 * k_den, y0 * k_num               # new true price, XCH per new X
        correct = x0 + (y0 * k_num // k_den) * p_num // p_den
        # 1. the pool is a party: its X reserve is re-denominated with everyone else's
        rows.append((label, "a party (reserve re-denominated)", correct, correct))
        # 2. not a party: its reserve count stays, the market reprices, arbitrage closes the gap
        after = arbitrage_to((x0, y0), p_num, p_den, fee)
        v = after[0] + after[1] * p_num // p_den
        rows.append((label, "not a party (arbitraged)", v, correct))
    for label, how, v, correct in rows:
        print(f"   {label:<22} {how:<34} {v/1e12:>12.4f} XCH {v/correct - 1:>+10.2%}")
    split_loss = rows[1][2] / rows[1][3] - 1
    merge_gap = rows[5][2] / rows[5][3] - 1
    check("a 2:1 split the pool is not a party to costs LPs more than a quarter of the pool", split_loss < -0.25,
          f"{split_loss:+.2%}")
    check("a 1:2 merge the pool is not a party to leaves the pool holding un-merged supply", merge_gap > 0.25,
          f"{merge_gap:+.2%}")
    print("""
   A split or merge the pool does not take part in is an instant, permanent price move of
   k:1 on one side of the pool. A split, and arbitrage collects it from the LPs. A merge
   "gains" only because the pool's reserve was never merged: those units are now worth the
   post-merge price, so the pool is issuing supply the merge meant to retire, diluting every
   other holder, and arbitrageurs sell it off. Neither direction is neutral: the pool must be
   re-denominated in the same instant as everyone else.

   On an rCAT, the only tool an issuer has for a merge is the hidden path -- revoke and
   reissue -- and part C showed what the hidden path does to a Forge reserve. So corporate
   actions and Forge pools are compatible ONLY through a pool lane built for them.""")


def main() -> int:
    if not kit.v14_available():
        print("  [skip] V14 build outputs are absent; run scripts/build-v14.py")
        return 2
    part_a()
    part_b()
    part_c()
    part_d()
    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} probes behaved as reported")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
