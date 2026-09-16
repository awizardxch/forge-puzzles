#!/usr/bin/env python3
"""Answer the two findings of the automated review of CHIP-0062 revision 8, on a node.

Cursor Bugbot raised two against the CHIP text on 2026-09-16. Both are valid against the
prose and neither is a defect in the puzzles, and the difference is worth proving rather
than asserting -- so this builds each attack against the real compiled V14 puzzles and
lets an in-process full node judge it.

  MEDIUM, "settlement coinid omits puzzle hash". The claim: a leaf given only parent and
          amount cannot reconstruct the spent coin, because a coin id needs a puzzle hash
          too and parent-plus-amount is not unique.
          The answer: the puzzle hash is the third input and it is DERIVED from the asset
          the leaf credits -- which is why it is not in the solution. Below: one parent and
          one amount produce two different ids for two different assets, and a leaf handed
          the other asset's pair derives a coin that does not exist.

  HIGH,   "reserve proof skips child puzzle". The claim: register derives P_i and asserts
          an announcement from it, but nothing pins what the launcher CREATED, so a
          launcher may fund a coin the finalizer will not recognise and take the key anyway.
          The answer: the announcement's message commits to the created puzzle hash, the
          amount and the launcher id, and `register` builds the expected message from the
          reserve hash IT derives. Below: a launcher that creates a decoy hash, a launcher
          that creates the right hash for the wrong amount, and a launcher that announces
          another pool's launcher id -- each refused by the node, with the honest control
          accepted in the same run.

Every creation here runs `sim_v14.create_pool`, the same lane the honest one uses, with
the launcher knobs turned. A probe that builds its own bundle proves something about the
probe.

    python scripts/sim-v14-review-derivations.py
"""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "contracts"))
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

_spec = importlib.util.spec_from_file_location("sim_v14", ROOT / "scripts" / "sim-v14.py")
sim_v14 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sim_v14)

import _sim_harness as harness  # noqa: E402
import forge_v14_driver as drv  # noqa: E402
import forge_v14_offer as offer  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402

PASSED = FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {label}" + (f"  {detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {label}" + (f"  {detail}" if detail else ""))


# --------------------------------------------------------------------------- MEDIUM
def settlement_ids_need_the_puzzle_hash(cat: bytes32) -> None:
    """One parent, one amount, two assets: two ids. The puzzle hash is an input."""
    print("\nMEDIUM -- 'a leaf given parent and amount cannot reconstruct the coin'")
    parent = bytes32(b"\x11" * 32)
    amount = 250_000

    xch_ph = offer.settle_ph(None)
    cat_ph = offer.settle_ph(cat)
    check("the settlement puzzle hash differs by asset, and neither comes from a solution",
          xch_ph != cat_ph, f"XCH {xch_ph.hex()[:12]}  CAT {cat_ph.hex()[:12]}")

    xch_id = drv.coin_id(parent, xch_ph, amount)
    cat_id = drv.coin_id(parent, cat_ph, amount)
    check("the SAME parent and amount derive two different coin ids",
          xch_id != cat_id, f"{xch_id.hex()[:12]} vs {cat_id.hex()[:12]}")
    from chia.types.blockchain_format.coin import Coin as _Coin
    real = _Coin(parent, cat_ph, amount)
    check("and the derived id is the real coin's id: the leaf reconstructs the coin",
          cat_id == real.name() and drv.settlement_ref(real) == (parent, amount),
          f"derived {cat_id.hex()[:12]} == coin {real.name().hex()[:12]}")
    print("       so the id is not derived from two values; it is derived from three, and the")
    print("       third is fixed by the asset the leaf credits rather than supplied with it.")


# ------------------------------------------------------------------------------ HIGH
async def reserve_message_binds_what_was_created(sim, client, wallet, reg, cat: bytes32) -> None:
    print("\nHIGH -- 'the reserve proof skips the child puzzle'")
    assets = [None, cat]
    reserves = [8_000_000, 16_000_000]

    async def refused(label: str, knobs: dict, salt: int) -> str:
        """Build the SAME creation lane with a launcher knob turned, and push it.

        The registry bookkeeping AND the wallet are copied: a refused bundle spends
        nothing on chain, but building it still consumes coins from this script's own
        wallet, and the control has to be funded from the same purse.
        """
        bundle, _pool = await sim_v14.create_pool(
            sim, client, copy.deepcopy(wallet), copy.deepcopy(reg), assets, reserves, [1, 1],
            label=f"probe-{salt}", push_now=False, launcher_knobs=knobs)
        return await harness.expect_refusal(client, bundle, label)

    err = await refused("decoy created hash", {"created": {1: bytes32(b"\x99" * 32)}}, 1)
    check("a launcher that creates a DECOY puzzle hash is refused by the node", True, err)
    print("       this is the finding's exact construction: the coin is funded, the launcher")
    print("       announces it truthfully, and the announcement still does not match the one")
    print("       register computes from the reserve hash it derives.")

    err = await refused("wrong amount", {"amounts": {0: reserves[0] - 1}}, 2)
    check("a launcher that creates the right hash for the WRONG amount is refused", True, err)

    err = await refused("foreign launcher id", {"launcher_id": bytes32(b"\x98" * 32)}, 3)
    check("a launcher that announces ANOTHER pool's launcher id is refused", True, err)

    # the control, in the same run, through the same lane
    pool = await sim_v14.create_pool(sim, client, wallet, reg, assets, reserves, [1, 1],
                                     label="honest control")
    check("the honest creation, same lane and no knobs, is ACCEPTED and registered",
          pool is not None and pool.birth > 0, f"birth {pool.birth}")
    return pool


async def main() -> int:
    print("V14 derivation probes -- the two findings of the automated review, judged by a node")
    print("=" * 78)
    async with harness.sim_and_client() as (sim, client):
        coins = await harness.farm_to_identity(sim, client, blocks=6)
        wallet = harness.Wallet(xch=list(coins), cats={})
        funding = wallet.take_xch(400_000_000)
        pieces, split = harness.split_xch(funding, [180_000_000])
        await harness.push(client, sim, split, "split for issuance")
        cat, minted, lineage, mint = harness.issue_cat(pieces[0], 60_000_000, salt=0xC1)
        await harness.push(client, sim, mint, "issue the probe token")
        wallet.cats.setdefault(cat, []).append((minted, lineage))
        settlement_ids_need_the_puzzle_hash(cat)
        reg = await sim_v14.mint_registry(sim, client, wallet)
        await reserve_message_binds_what_was_created(sim, client, wallet, reg, cat)

    print("=" * 78)
    print(f"{PASSED}/{PASSED + FAILED} checks passed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
