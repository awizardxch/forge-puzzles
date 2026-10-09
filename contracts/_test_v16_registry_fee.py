"""V16: the creation fee lives in registry STATE and the treasury moves it (owner, 2026-10-07).

V14 curried the fee into the registry, so changing it meant a registry rollover. V16 seeds
`RegistryState.creation_fee` from the launch constant in `init`, charges whatever the state
holds in `register`, and moves it only through the `set_fee` leaf, which a coin at the
treasury's puzzle hash authorizes by a mode-23 message to THIS registry coin -- the DAO-fee
handshake turned to the treasury. Zero makes registration free (no settlement asserted).

Each case is run through the real validator (chia_rs, mempool rules). Exit 0 all pass, 1 a
failure, 2 nothing exercised (build outputs absent).
"""
import sys
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")
from chia.types.blockchain_format.program import Program
from chia_rs.sized_bytes import bytes32
import _v16_testkit as kit
from _test_v16_registry import CATS, registration

results = []
FEE = kit.DEFAULT_CREATION_FEE
TREASURY = Program.to(1)                       # the treasury's coin: an identity puzzle, so its spend is its conditions
TREASURY_PH = bytes32(TREASURY.get_tree_hash())
OTHER = Program.to([3, [], 1, 1])              # `(i () 1 1)`: the same behavior under a different puzzle hash


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def refuses(label, thunk):
    try:
        thunk()
    except kit.Rejected as exc:
        return check(label, True, str(exc)[:60])
    except Exception as exc:  # noqa: BLE001 -- a leaf that raises locally is a refusal too
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


def set_fee(reg, new_fee, sender=TREASURY, message_fee=None, with_message=True):
    extra = []
    if with_message:
        _, spend = kit.treasury_fee_spend(reg, new_fee if message_fee is None else message_fee, sender)
        extra.append(spend)
    return kit.registry_spend(reg, "forge_registry_set_fee", [new_fee], extra_spends=extra)


def state_of(program) -> list:
    return [x.as_int() for x in program.as_iter()]


def main() -> int:
    if not (kit.v16_available() and kit.registry_available()):
        print("  [skip] V16 build outputs are absent; run scripts/build-v16.py")
        return 2
    if "forge_registry_set_fee" not in kit.REGISTRY_LEAF_ORDER:
        print("  [skip] the driver carries no set_fee leaf")
        return 2

    print("init seeds the fee from the launch constant:")
    reg0 = kit.make_registry(salt=0x31, treasury_ph=TREASURY_PH, creation_fee=FEE)
    check("  before init the state holds no fee and current_fee is 0", reg0.state == [0, 0, 0] and reg0.current_fee == 0)
    bundle, st = kit.registry_spend(reg0, "forge_registry_init", [])
    accepts("init validates", lambda: kit.validate(bundle))
    check("  state becomes (1, 0, launch fee)", state_of(st) == [1, 0, FEE])
    reg1 = reg0.advance(state_of(st))
    check("  current_fee reads the state", reg1.current_fee == FEE and reg1.creation_fee == FEE)
    slots = {kit.MIN_KEY: (reg0.coin, reg0.inner_hash), kit.MAX_KEY: (reg0.coin, reg0.inner_hash)}
    left, right = (kit.MIN_KEY, kit.ZERO_32, kit.MIN_KEY), (kit.MAX_KEY, kit.ZERO_32, kit.MAX_KEY)

    print("set_fee:")
    refuses("set_fee before init is refused", lambda: set_fee(reg0, 5))
    refuses("set_fee with no treasury message is refused", lambda: kit.validate(set_fee(reg1, FEE * 2, with_message=False)[0]))
    refuses("set_fee with a message from a puzzle that is not the treasury is refused",
            lambda: kit.validate(set_fee(reg1, FEE * 2, sender=OTHER)[0]))
    refuses("set_fee whose message names another fee is refused",
            lambda: kit.validate(set_fee(reg1, FEE * 2, message_fee=FEE * 3)[0]))
    refuses("a negative fee is refused", lambda: set_fee(reg1, -1))
    bundle, st = set_fee(reg1, FEE * 2)
    out = accepts("the treasury raises the fee", lambda: kit.validate(bundle))
    check("  state becomes (1, 0, 2x fee); pool_count and initialized carried", state_of(st) == [1, 0, FEE * 2])
    if out:
        _, additions = out
        check("  successor registry singleton at the new state", (reg1.successor_puzzle_hash([1, 0, FEE * 2]), 1) in additions)
        check("  no slot is spent or created by set_fee", not any(a == 0 for _, a in additions))
    reg2 = reg1.advance(state_of(st))
    # Audit F7 for the registry: a leading-zero spelling of the fee is stored canonically.
    bundle, st = kit.registry_spend(reg2, "forge_registry_set_fee", [Program.to(b"\x00" + (FEE * 2).to_bytes(3, "big"))],
                                    extra_spends=[kit.treasury_fee_spend(reg2, FEE * 2, TREASURY)[1]])
    check("  a leading-zero spelling of the same fee stores the canonical atom (F7)", bytes(st.rest().rest().first().as_atom()) == Program.to(FEE * 2).as_atom())

    print("register charges the fee in STATE:")
    pool_a = kit.make_pool([None, CATS[0]], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x50,
                           protocol_ph=reg2.protocol_ph)
    refuses("paying the launch constant after a raise is refused",
            lambda: kit.validate(registration(reg2, pool_a, left, right, slots, fee=FEE)[0]))
    refuses("paying one mojo under the raised fee is refused",
            lambda: kit.validate(registration(reg2, pool_a, left, right, slots, fee=FEE * 2 - 1)[0]))
    bundle, st = registration(reg2, pool_a, left, right, slots)       # fee_settlement reads current_fee
    out = accepts("paying the raised fee registers", lambda: kit.validate(bundle))
    if out:
        _, additions = out
        check("  the treasury received the raised fee", (TREASURY_PH, FEE * 2) in additions)
    check("  the fee is carried through register, pool_count is 1", state_of(st) == [1, 1, FEE * 2])
    reg3 = reg2.advance(state_of(st))

    print("a zero fee makes registration free:")
    bundle, st = set_fee(reg3, 0)
    accepts("the treasury sets the fee to zero", lambda: kit.validate(bundle))
    check("  state becomes (1, 1, 0)", state_of(st) == [1, 1, 0])
    reg4 = reg3.advance(state_of(st))
    key_a = kit.pool_key(pool_a.config())
    slots4 = {kit.MIN_KEY: (reg2.coin, reg2.inner_hash), key_a: (reg2.coin, reg2.inner_hash)}
    pool_b = kit.make_pool([None, CATS[1]], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x51,
                           protocol_ph=reg4.protocol_ph)
    key_b = kit.pool_key(pool_b.config())
    if key_b < key_a:
        lb, rb = (kit.MIN_KEY, kit.ZERO_32, kit.MIN_KEY), (key_a, pool_a.launcher_id, kit.MAX_KEY)
    else:
        slots4 = {key_a: (reg2.coin, reg2.inner_hash), kit.MAX_KEY: (reg2.coin, reg2.inner_hash)}
        lb, rb = (key_a, pool_a.launcher_id, kit.MIN_KEY), (kit.MAX_KEY, kit.ZERO_32, kit.MAX_KEY)
    bundle, st = registration(reg4, pool_b, lb, rb, slots4, with_fee=False)
    accepts("a pool registers with no fee settlement at all", lambda: kit.validate(bundle))
    check("  pool_count is 2, fee still 0", state_of(st) == [1, 2, 0])
    reg5 = reg4.advance(state_of(st))
    bundle, st = set_fee(reg5, FEE)
    accepts("and the treasury can raise it again from zero", lambda: kit.validate(bundle))
    check("  state becomes (1, 2, fee)", state_of(st) == [1, 2, FEE])

    print("the message binds to this registry coin:")
    other = kit.make_registry(salt=0x32, treasury_ph=TREASURY_PH, creation_fee=FEE).advance([1, 0, FEE])
    refuses("a treasury message addressed to another registry coin does not authorize this one",
            lambda: kit.validate(kit.registry_spend(reg5, "forge_registry_set_fee", [7],
                                                    extra_spends=[kit.treasury_fee_spend(other, 7, TREASURY)[1]])[0]))

    passed = sum(results)
    print(f"\n{passed}/{len(results)} registry fee checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
