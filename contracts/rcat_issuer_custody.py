#!/usr/bin/env python3
"""rCAT issuer custody: keep the issuer's hidden puzzle H out of the trading wallet.

An rCAT (CHIP-0038) is (asset id, H). Whoever can spend a coin AT H, or reveal H's puzzle and sign
for it, can claw back every coin of the asset through the revocation layer's hidden path, and (V17+)
send the mode-23 message that lets a pool's reserve be rebased. On testnet11 H1 and H2 were
derivations 70 and 71 of the TRADING wallet (Sage "Hearts on Fire"), and Sage spent the coins parked
at H1/H2 as ordinary change -- the issuer's authority sat in a hot wallet that trades.

This module is revision-independent (no driver import), so every revision's scripts can share it:

  * `trading_derivation_of(h, master_pk)` -- is H the standard puzzle of an unhardened derivation of
    a key? Derived offline from the master public key (Sage `get_keys` returns it), so no wallet call.
  * `check_issuer_h(...)` -- the guard: refuse an H that belongs to the connected TRADING key; on a
    test network an explicit override lets the legacy setup (R1/R2) be driven, on mainnet nothing does.
  * `issuer_message_spends(...)` -- an issuer action funded from the ISSUER'S OWN coin: the issuer's
    coin creates an ephemeral 1-mojo coin AT H in the same bundle, and that coin (H's puzzle, signed by
    H's key) sends the message. No coin has to sit at H between actions, so there is nothing for a
    wallet to spend as change and nothing to lose.
  * `issuer_fee_spend(...)` -- the network fee of a hidden-path spend (clawback), from the issuer's coin.

See docs/RCAT_ISSUER_CUSTODY.md for the runbook.
"""
from __future__ import annotations

from dataclasses import dataclass

from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend
from chia.wallet.derive_keys import master_pk_to_wallet_pk_unhardened
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_pk, solution_for_conditions
from chia_rs import Coin, G1Element
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

DEFAULT_SCAN = 2000                 # unhardened derivations checked; Sage hands out deep receive addresses
TEST_NETWORKS = ("testnet11", "testnet10", "simulator0", "simulator")
MAINNET = "mainnet"


class IssuerCustodyError(ValueError):
    """An issuer's H is held by the trading wallet (or the custody rules are otherwise broken)."""


def master_pk_of(raw) -> G1Element:
    if isinstance(raw, G1Element):
        return raw
    return G1Element.from_bytes(bytes.fromhex(str(raw).lower().removeprefix("0x")))


def standard_puzzle(master_pk, index: int) -> Program:
    """The standard (p2_delegated_puzzle_or_hidden_puzzle) puzzle of unhardened derivation `index`."""
    return puzzle_for_pk(master_pk_to_wallet_pk_unhardened(master_pk_of(master_pk), int(index)))


def trading_derivation_of(h, master_pk, scan: int = DEFAULT_SCAN, hardened_phs=()) -> int | str | None:
    """The derivation of `master_pk` whose standard puzzle hashes to `h`: an int for an unhardened one,
    "hardened" when `h` is among `hardened_phs` (Sage `get_derivations hardened:true`; hardened keys
    cannot be derived from a public key), else None."""
    h = bytes32(h)
    pk = master_pk_of(master_pk)
    for i in range(int(scan)):
        if standard_puzzle(pk, i).get_tree_hash() == h:
            return i
    if any(bytes32(x) == h for x in hardened_phs):
        return "hardened"
    return None


@dataclass(frozen=True)
class Verdict:
    ok: bool                 # the guard lets the action proceed
    trading: bool            # H IS a derivation of the trading key
    derivation: object       # which one (int, "hardened") or None
    overridden: bool         # proceeding only because of the explicit test-network override
    message: str


def check_issuer_h(h, trading_master_pk, network: str, allow_trading_issuer: bool = False,
                   scan: int = DEFAULT_SCAN, hardened_phs=(), what: str = "issuer action") -> Verdict:
    """The custody guard. Raises IssuerCustodyError when H is a standard-puzzle derivation of the
    trading key, unless `allow_trading_issuer` on a TEST network (never on mainnet)."""
    found = trading_derivation_of(h, trading_master_pk, scan, hardened_phs)
    if found is None:
        return Verdict(True, False, None, False, f"{what}: H {bytes32(h).hex()[:16]} is not a derivation of the trading key "
                                                 f"(first {scan} unhardened{' + listed hardened' if hardened_phs else ''} checked)")
    msg = (f"{what}: H {bytes32(h).hex()[:16]} is the standard puzzle of the TRADING key's derivation {found}. "
           "The issuer's hidden puzzle must live in its own wallet that never trades (a separate fingerprint, "
           "ideally a vault): a trading wallet spends coins at H as change, and its key can claw back every coin "
           "of the asset. See docs/RCAT_ISSUER_CUSTODY.md.")
    if not allow_trading_issuer:
        raise IssuerCustodyError(msg)
    if str(network) == MAINNET or str(network) not in TEST_NETWORKS:
        raise IssuerCustodyError(msg + f" The override is refused on {network}: it exists for the legacy testnet assets only.")
    return Verdict(True, True, found, True, msg + f" OVERRIDDEN on {network} (legacy testnet setup).")


def sage_trading_key(sage) -> tuple[int, G1Element]:
    """(fingerprint, master public key) of the key Sage is logged into: the trading wallet."""
    key = sage.call("get_key")["key"]
    return int(key["fingerprint"]), master_pk_of(key["public_key"])


def sage_key(sage, fingerprint: int) -> G1Element:
    """The master public key of any key Sage holds, without logging into it (get_keys)."""
    for k in sage.call("get_keys").get("keys", []):
        if int(k["fingerprint"]) == int(fingerprint):
            return master_pk_of(k["public_key"])
    raise IssuerCustodyError(f"Sage holds no key with fingerprint {fingerprint}")


def sage_hardened_phs(sage, limit: int = 500) -> list:
    """Puzzle hashes of the active key's first `limit` hardened derivations, as Sage reports them."""
    from chia.util.bech32m import decode_puzzle_hash
    try:
        page = sage.call("get_derivations", {"hardened": True, "offset": 0, "limit": int(limit)}).get("derivations") or []
    except Exception:  # noqa: BLE001 -- a Sage without hardened derivations generated has none to match
        return []
    return [bytes32(decode_puzzle_hash(d["address"])) for d in page]


def check_with_sage(h, sage, network: str, allow_trading_issuer: bool = False, scan: int = DEFAULT_SCAN,
                    what: str = "issuer action") -> Verdict:
    """check_issuer_h against the key Sage is logged into (unhardened by derivation, hardened as Sage lists them)."""
    _fp, pk = sage_trading_key(sage)
    return check_issuer_h(h, pk, network, allow_trading_issuer, scan, sage_hardened_phs(sage), what)


def issuer_message_spends(funding_coin: Coin, funding_puzzle: Program, hidden_puzzle: Program,
                          h_conditions: list, fee: int, change_ph: bytes32) -> tuple[list, Coin]:
    """An issuer action's message coin, funded from the issuer's own coin in the same bundle.

    `funding_coin` (the issuer's wallet, standard puzzle `funding_puzzle`) creates a 1-mojo coin AT H,
    pays `fee` and returns the rest to `change_ph`; that ephemeral coin is spent with `hidden_puzzle`
    (whose hash is H) emitting `h_conditions` (e.g. the rebase's [66, 23, message, receiver]) and
    nothing else, so its mojo joins the fee. Both spends are AGG_SIG_ME by the issuer's keys."""
    h = bytes32(hidden_puzzle.get_tree_hash())
    if bytes32(funding_puzzle.get_tree_hash()) != bytes32(funding_coin.puzzle_hash):
        raise IssuerCustodyError("the issuer's funding puzzle does not hash to its coin")
    if bytes32(funding_coin.puzzle_hash) == h:
        raise IssuerCustodyError("fund the message from an issuer coin that is NOT at H; nothing should sit at H")
    change = int(funding_coin.amount) - 1 - int(fee)
    if change < 0:
        raise IssuerCustodyError(f"the issuer's coin holds {funding_coin.amount}, the action needs {int(fee) + 1}")
    conds = [[51, h, 1], [52, int(fee) + 1]]
    if change:
        conds.append([51, change_ph, change, [change_ph]])
    eph = Coin(funding_coin.name(), h, uint64(1))
    return [make_spend(funding_coin, funding_puzzle, solution_for_conditions(Program.to(conds))),
            make_spend(eph, hidden_puzzle, solution_for_conditions(Program.to(list(h_conditions))))], eph


def issuer_fee_spend(funding_coin: Coin, funding_puzzle: Program, fee: int, change_ph: bytes32):
    """The network fee of an issuer spend that creates no message coin (a clawback), from the issuer's coin."""
    change = int(funding_coin.amount) - int(fee)
    if change < 0:
        raise IssuerCustodyError(f"the issuer's coin holds {funding_coin.amount}, the fee is {fee}")
    conds = [[52, int(fee)]] + ([[51, change_ph, change, [change_ph]]] if change else [])
    return make_spend(funding_coin, funding_puzzle, solution_for_conditions(Program.to(conds)))
