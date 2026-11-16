"""Who owns an NFT, read from the chain (Spellpower's entry proof).

An indexer says who holds an NFT; only the chain says whether that is still true.
The owner of an NFT is read from the spend that created its current coin: the
parent spend is uncurried, its inner p2 puzzle run with its own solution, and the
owner is the puzzle hash of the odd CREATE_COIN it makes -- the same condition the
NFT's ownership layer wraps into the child's puzzle. That is stricter than chia's
get_metadata_and_phs, which reads the memo (a hint anyone can write).

Two ways to find the coin:

  current  {launchers: [{launcher, from?}]}  -> walk the singleton forward from its
           launcher (or from a coin a previous walk ended on, which the router keeps),
           one node call per transfer, to the unspent coin, then read its owner. The
           gate's way: it asks about the one NFT the indexer says a proven address
           holds, so a whale's 300-NFT address costs no more than anyone's.
  owned    {phs: [hex32]}                    -> every NFT an address owns: unspent
           one-mojo coins hinted to it, each parent read. The page's way, later. A
           hint only nominates a coin; the owner read decides.

A coin whose parent is the launcher itself (an eve coin nobody has spent yet) has no
inner spend to read; it is reported with owner null rather than guessed.

stdin:  {network, mode, node_url?, ...}
stdout: current: {success, nfts: {launcher: {coin_id, height, owner, did, hops, spent_to?, error?}}}
        owned:   {success, owned: {ph: [{launcher, coin_id, height, did}]}, unresolved: {ph: n}, checked: {ph: n}}
"""

from __future__ import annotations

import json
import pathlib
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.wallet.nft_wallet.uncurry_nft import UncurriedNFT

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from multisig_tool import MultisigError, Node, network_config, strip0x  # noqa: E402

MAX_PHS = 20
MAX_LAUNCHERS = 20
# Hinted one-mojo coins looked at per address in `owned`. CATs and XCH hinted to the
# address are skipped before any spend is fetched.
MAX_COINS_PER_PH = 300
# Transfers walked per NFT in `current`. A walk that runs out reports where it stopped,
# and the router resumes from there next time.
MAX_HOPS = 150
WORKERS = 6
CREATE_COIN = 51


def hex32(value: Any) -> bytes32:
    text = strip0x(str(value or "")).lower()
    if len(text) != 64:
        raise MultisigError(f"{value!r} is not a 32-byte hex value")
    return bytes32.fromhex(text)


def record_coin_id(record: dict[str, Any]) -> bytes32:
    coin = record["coin"]
    return Coin(hex32(coin["parent_coin_info"]), hex32(coin["puzzle_hash"]), uint64(int(coin["amount"]))).name()


def nft_owner(puzzle: Program, solution: Program) -> tuple[bytes32, bytes32, bytes32 | None] | None:
    """(launcher id, new owner p2 puzzle hash, owner DID) for an NFT spend, or None."""
    try:
        unft = UncurriedNFT.uncurry(*puzzle.uncurry())
    except Exception:  # noqa: BLE001 -- not an NFT
        return None
    if unft is None:
        return None
    conditions = unft.p2_puzzle.run(unft.get_innermost_solution(solution))
    owner: bytes32 | None = None
    for condition in conditions.as_iter():
        if condition.list_len() < 3 or condition.first().as_int() != CREATE_COIN:
            continue
        if condition.at("rrf").as_int() % 2 == 1:
            if owner is not None:
                return None  # two odd outputs: the singleton layer refuses this spend
            owner = bytes32(condition.at("rf").as_atom())
    if owner is None:
        return None
    did = unft.owner_did if isinstance(unft.owner_did, bytes) and len(unft.owner_did) == 32 else None
    return bytes32(unft.singleton_launcher_id), owner, did


def parent_spend(node: Node, record: dict[str, Any]) -> tuple[Program, Program] | None:
    try:
        return node.puzzle_and_solution(hex32(record["coin"]["parent_coin_info"]), int(record.get("confirmed_block_index") or 0))
    except Exception:  # noqa: BLE001 -- an unreadable parent proves nothing
        return None


# ── current: walk one NFT forward ──────────────────────────────────────────────


def odd_child(node: Node, coin_id: bytes32) -> dict[str, Any] | None:
    body = node.rpc("get_coin_records_by_parent_ids", {"parent_ids": ["0x" + coin_id.hex()], "include_spent_coins": True})
    if body.get("success") is False:
        raise MultisigError(f"get_coin_records_by_parent_ids failed: {body.get('error')}")
    odd = [r for r in body.get("coin_records") or [] if isinstance(r, dict) and int((r.get("coin") or {}).get("amount") or 0) % 2 == 1]
    return odd[0] if len(odd) == 1 else None


def current_nft(node: Node, launcher: bytes32, start: bytes32 | None) -> dict[str, Any]:
    record = node.coin_record(start or launcher)
    if record is None:
        return {"error": "the chain has no such coin"}
    hops = 0
    while record.get("spent"):
        if hops >= MAX_HOPS:
            return {"error": "the walk ran out of hops", "spent_to": record_coin_id(record).hex(), "hops": hops}
        child = odd_child(node, record_coin_id(record))
        if child is None:
            return {"error": "a spent coin with no single odd child (melted, or not a singleton)", "hops": hops}
        record = child
        hops += 1
    coin_id = record_coin_id(record)
    out: dict[str, Any] = {"coin_id": coin_id.hex(), "height": int(record.get("confirmed_block_index") or 0), "hops": hops, "owner": None, "did": None}
    if coin_id == launcher or hex32(record["coin"]["parent_coin_info"]) == launcher:
        return out  # an unspent launcher or eve coin: nothing says who owns it yet
    spend = parent_spend(node, record)
    found = nft_owner(*spend) if spend else None
    if found is None:
        out["error"] = "the parent spend is not an NFT spend"
        return out
    found_launcher, owner, did = found
    if found_launcher != launcher:
        out["error"] = "the coin belongs to another singleton"
        return out
    out["owner"] = owner.hex()
    out["did"] = did.hex() if did else None
    return out


def _records_by_names(node: Node, ids: list[bytes32]) -> dict[bytes32, dict[str, Any]]:
    """Coin records for many ids, 100 a call, spent ones included."""
    out: dict[bytes32, dict[str, Any]] = {}
    unique = list(dict.fromkeys(ids))
    for start in range(0, len(unique), 100):
        body = node.rpc("get_coin_records_by_names", {"names": ["0x" + i.hex() for i in unique[start:start + 100]], "include_spent_coins": True})
        if body.get("success") is False:
            raise MultisigError(f"get_coin_records_by_names failed: {body.get('error')}")
        for record in body.get("coin_records") or []:
            if isinstance(record, dict):
                out[record_coin_id(record)] = record
    return out


def _children(node: Node, ids: list[bytes32]) -> dict[bytes32, list[dict[str, Any]]]:
    """Every child of each of these coins, 100 parents a call, spent ones included."""
    out: dict[bytes32, list[dict[str, Any]]] = {i: [] for i in ids}
    unique = list(dict.fromkeys(ids))
    for start in range(0, len(unique), 100):
        body = node.rpc("get_coin_records_by_parent_ids", {"parent_ids": ["0x" + i.hex() for i in unique[start:start + 100]], "include_spent_coins": True})
        if body.get("success") is False:
            raise MultisigError(f"get_coin_records_by_parent_ids failed: {body.get('error')}")
        for record in body.get("coin_records") or []:
            if isinstance(record, dict):
                out.setdefault(hex32(record["coin"]["parent_coin_info"]), []).append(record)
    return out


def current(node: Node, launchers: list[dict[str, Any]]) -> dict[str, Any]:
    """Every NFT walked forward together: one call per hop for all of them.

    Walking each NFT alone cost one node call per hop per NFT, serially: 20 NFTs
    were 169 calls and 20 s from a desk, past the 90 s limit from the router where
    a call costs over a second (owner, 2026-10-24: "check spellpower as that seems
    to be slow"). Now the starts are one batch, each hop is one
    ``get_coin_records_by_parent_ids`` for every NFT still moving, and the parent
    spends that name the owners are fetched side by side. The answer per NFT is
    exactly :func:`current_nft`'s.
    """
    items = [(hex32(item.get("launcher")), hex32(item["from"]) if item.get("from") else None) for item in launchers[:MAX_LAUNCHERS]]
    nfts: dict[str, Any] = {}
    try:
        starts = _records_by_names(node, [start or launcher for launcher, start in items])
    except MultisigError as exc:
        return {"nfts": {launcher.hex(): {"error": str(exc)} for launcher, _ in items}}

    walking: dict[bytes32, tuple[dict[str, Any], int]] = {}
    for launcher, start in items:
        record = starts.get(start or launcher)
        if record is None:
            nfts[launcher.hex()] = {"error": "the chain has no such coin"}
        else:
            walking[launcher] = (record, 0)

    while True:
        moving = [launcher for launcher, (record, _) in walking.items() if record.get("spent")]
        if not moving:
            break
        for launcher in [l for l in moving if walking[l][1] >= MAX_HOPS]:
            record, hops = walking.pop(launcher)
            nfts[launcher.hex()] = {"error": "the walk ran out of hops", "spent_to": record_coin_id(record).hex(), "hops": hops}
        moving = [launcher for launcher in moving if launcher in walking]
        try:
            children = _children(node, [record_coin_id(walking[launcher][0]) for launcher in moving])
        except MultisigError as exc:
            for launcher in moving:
                walking.pop(launcher)
                nfts[launcher.hex()] = {"error": str(exc)}
            continue
        for launcher in moving:
            record, hops = walking[launcher]
            odd = [r for r in children.get(record_coin_id(record), []) if int((r.get("coin") or {}).get("amount") or 0) % 2 == 1]
            if len(odd) != 1:
                walking.pop(launcher)
                nfts[launcher.hex()] = {"error": "a spent coin with no single odd child (melted, or not a singleton)", "hops": hops}
            else:
                walking[launcher] = (odd[0], hops + 1)

    # Each NFT's current coin; the owner is read from the spend that made it.
    pending: list[tuple[bytes32, dict[str, Any], dict[str, Any]]] = []
    for launcher, (record, hops) in walking.items():
        coin_id = record_coin_id(record)
        out: dict[str, Any] = {"coin_id": coin_id.hex(), "height": int(record.get("confirmed_block_index") or 0), "hops": hops, "owner": None, "did": None}
        nfts[launcher.hex()] = out
        if coin_id == launcher or hex32(record["coin"]["parent_coin_info"]) == launcher:
            continue  # an unspent launcher or eve coin: nothing says who owns it yet
        pending.append((launcher, record, out))

    # Worker threads only fetch text: chia's Program objects are bound to the thread
    # that made them, so every puzzle is built and run here.
    def fetch(item: tuple[bytes32, dict[str, Any], dict[str, Any]]) -> dict[str, Any] | None:
        record = item[1]
        try:
            body = node.rpc("get_puzzle_and_solution", {
                "coin_id": "0x" + strip0x(str(record["coin"]["parent_coin_info"])),
                "height": int(record.get("confirmed_block_index") or 0),
            })
        except Exception:  # noqa: BLE001 -- an unreadable parent proves nothing
            return None
        spend = body.get("coin_solution") or body.get("coin_spend")
        return spend if isinstance(spend, dict) and spend.get("puzzle_reveal") else None

    if pending:
        with ThreadPoolExecutor(max_workers=min(WORKERS, len(pending))) as pool:
            spends = list(pool.map(fetch, pending))
    else:
        spends = []
    for (launcher, _, out), spend in zip(pending, spends):
        found = None
        if spend is not None:
            puzzle = Program.from_bytes(bytes.fromhex(strip0x(str(spend["puzzle_reveal"]))))
            solution = Program.from_bytes(bytes.fromhex(strip0x(str(spend.get("solution") or "80"))))
            found = nft_owner(puzzle, solution)
        if found is None:
            out["error"] = "the parent spend is not an NFT spend"
            continue
        found_launcher, owner, did = found
        if found_launcher != launcher:
            out["error"] = "the coin belongs to another singleton"
            continue
        out["owner"] = owner.hex()
        out["did"] = did.hex() if did else None
    # The caller's order, as before.
    return {"nfts": {launcher.hex(): nfts[launcher.hex()] for launcher, _ in items}}


# ── owned: every NFT at an address ─────────────────────────────────────────────


def owned_nfts(node: Node, phs: list[bytes32]) -> dict[str, Any]:
    owned: dict[str, list[dict[str, Any]]] = {}
    unresolved: dict[str, int] = {}
    checked: dict[str, int] = {}
    work: list[tuple[bytes32, dict[str, Any]]] = []
    # Each address's hint read side by side: the records are plain JSON, so they
    # can cross threads.
    with ThreadPoolExecutor(max_workers=max(1, min(WORKERS, len(phs)))) as pool:
        hinted = list(pool.map(node.coin_records_by_hint, phs))
    for ph, found in zip(phs, hinted):
        records = [r for r in found
                   if not r.get("spent") and int((r.get("coin") or {}).get("amount") or 0) == 1]
        records = records[:MAX_COINS_PER_PH]
        checked[ph.hex()] = len(records)
        owned.setdefault(ph.hex(), [])
        work.extend((ph, record) for record in records)

    # Worker threads only fetch text: chia's Program objects are bound to the thread
    # that made them, so every puzzle is built and run here.
    def fetch(item: tuple[bytes32, dict[str, Any]]) -> tuple[bytes32, dict[str, Any], dict[str, Any] | None]:
        ph, record = item
        coin = record["coin"]
        try:
            body = node.rpc("get_puzzle_and_solution", {
                "coin_id": "0x" + strip0x(str(coin["parent_coin_info"])),
                "height": int(record.get("confirmed_block_index") or 0),
            })
        except Exception:  # noqa: BLE001 -- an unreadable parent proves nothing
            return ph, record, None
        spend = body.get("coin_solution") or body.get("coin_spend")
        return ph, record, spend if isinstance(spend, dict) and spend.get("puzzle_reveal") else None

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        fetched = list(pool.map(fetch, work))

    for ph, record, spend in fetched:
        if spend is None:
            unresolved[ph.hex()] = unresolved.get(ph.hex(), 0) + 1
            continue
        puzzle = Program.from_bytes(bytes.fromhex(strip0x(str(spend["puzzle_reveal"]))))
        solution = Program.from_bytes(bytes.fromhex(strip0x(str(spend.get("solution") or "80"))))
        found = nft_owner(puzzle, solution)
        if found is None:
            continue  # a one-mojo coin that is not an NFT child (or an eve coin)
        launcher, owner, did = found
        if owner != ph:
            continue  # hinted to this address, owned by another: the memo lied
        owned[ph.hex()].append({
            "launcher": launcher.hex(),
            "coin_id": record_coin_id(record).hex(),
            "height": int(record.get("confirmed_block_index") or 0),
            "did": did.hex() if did else None,
        })
    return {"owned": owned, "unresolved": unresolved, "checked": checked}


def main() -> int:
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="strict")
        except (AttributeError, ValueError):
            pass
    raw = sys.stdin.read().strip()
    try:
        payload = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            raise MultisigError("stdin payload must be a JSON object")
        network = str(payload.get("network") or "testnet11")
        node = Node(str(payload.get("node_url") or network_config(network)["node_url"]))
        mode = str(payload.get("mode") or "current")
        if mode == "current":
            launchers = payload.get("launchers")
            if not isinstance(launchers, list) or not launchers:
                raise MultisigError("launchers must be a non-empty list")
            out = current(node, [l if isinstance(l, dict) else {"launcher": l} for l in launchers])
        elif mode == "owned":
            phs = payload.get("phs")
            if not isinstance(phs, list) or not phs:
                raise MultisigError("phs must be a non-empty list")
            out = owned_nfts(node, list(dict.fromkeys(hex32(p) for p in phs))[:MAX_PHS])
        else:
            raise MultisigError(f"unknown mode {mode!r}; expected current or owned")
        print(json.dumps({"success": True, "network": network, **out}))
        return 0
    except MultisigError as exc:
        print(json.dumps({"success": False, "error": str(exc)}))
        return 1
    except Exception as exc:  # noqa: BLE001 -- one JSON line either way
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
