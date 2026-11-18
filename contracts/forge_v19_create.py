#!/usr/bin/env python3
"""Create and register a V11 pool from a creator's own coins, keylessly (phase 8.1).

The operator flow (scripts/deploy-v11-testnet.py create-pool) builds the creation
bundle with the operator's wallet. This is the same bundle split along the key
boundary so a **creator** can make a pool through the interface while the router
holds no key:

  creator side (signed by the creator's wallet, e.g. Sage `sign_coin_spends`):
    * one XCH coin spent to create the launcher (memos: name, symbol), the LP
      eve, the XCH reserve if the pool holds XCH, the fee settlement, and change;
      it also ASSERTS the announcement of the genesis LP payment to the creator,
      so a bundle that mints the genesis LP anywhere else fails the creator's
      own spend -- and one announcement of each router spend that binds the pool
      (the launcher, every reserve launcher, the fee settlement, the registry's
      `register`), so none of them can be dropped from the bundle it signed for;
    * one CAT coin per CAT asset, spent to that asset's reserve puzzle hash
      (with change back to the creator);

  router side (no key):
    * the launcher spend (kv [total_lp, eve_coin_id]) creating the eve singleton;
    * the LP eve ring minting the genesis supply to OFFER_MOD, whose spend pays
      the creator under nonce = launcher id (the payment the creator asserted);
    * the fee settlement spend paying the treasury under nonce = launcher id;
    * the two neighbour slot spends and the registry's `register` action.

Why the launcher's parent is the creator's XCH coin: the launcher id, and from it
the LP asset id, is then known to the creator BEFORE signing, which is what lets
the creator bind the genesis LP to their own address without a guard puzzle.
The creator picks the coin, so the coin id is theirs to know.

  issuer side (V19, rCAT only; signed by the rCAT's ISSUER, never the creator):
    * per revocable asset, one spend of the issuer singleton L -- the singleton whose
      everything_with_singleton (CHIP-0038 EWS) TAIL minted the asset -- that recreates
      itself and sends `issuer_attestation_condition`: a mode-23 message to the registry
      coin naming (asset, H, pool launcher). `register` refuses an rCAT without it, and
      without the proof (L, TAIL nonce, the singleton's inner puzzle hash) that the asset
      id IS that EWS curry. See `IssuerAttestation` for how a router obtains the spend.

No RPC, no signing. `plan()` returns the unsigned creator spends, the issuer spends it
was given and the router spends; `finalize()` accepts the creator's signature (and,
optionally, the creator spends as the wallet actually signed them, and the issuer's
signature) and produces the bundle.
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.types.coin_spend import CoinSpend, make_spend  # noqa: E402
from chia.wallet.cat_wallet.cat_utils import (  # noqa: E402
    CAT_MOD, SpendableCAT, construct_cat_puzzle, unsigned_spend_bundle_for_spendable_cats,
)
from chia.wallet.lineage_proof import LineageProof  # noqa: E402
from chia.wallet.puzzles.singleton_top_layer_v1_1 import SINGLETON_LAUNCHER, SINGLETON_LAUNCHER_HASH  # noqa: E402
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH  # noqa: E402
from chia_rs import Coin, G2Element, SpendBundle  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

import forge_v19_driver as drv  # noqa: E402
from forge_offer import ZERO_32  # noqa: E402
from forge_v19_offer import OfferRejected, pool_to_snapshot  # noqa: E402

OFFER_PH = bytes32(OFFER_MOD_HASH)
CREATE_COIN = 51
AGG_SIG_UNSAFE = 49     # the creation gate's key proof
CREATE_COIN_ANNOUNCEMENT, ASSERT_COIN_ANNOUNCEMENT = 60, 61
CREATE_PUZZLE_ANNOUNCEMENT, ASSERT_PUZZLE_ANNOUNCEMENT = 62, 63


def announcement_binds(spend: CoinSpend) -> list:
    """The asserts a signed spend adds to require `spend` in its bundle: one per announcement
    the spend makes, read off the spend as it will run on chain, so nothing here can drift
    from what the puzzle announces. The CAT layer's own ring announcement -- a 33-byte COIN
    announcement, 0xcb then the ring hash, emitted by the CAT mod itself -- is the ring's
    business and is left out. Only that one: a 32-byte hash announced by a non-CAT spend
    (the registry singleton's) begins 0xcb one time in 256, and dropping it left the
    creator with nothing to require of that spend."""
    puzzle, solution = Program.from_bytes(bytes(spend.puzzle_reveal)), Program.from_bytes(bytes(spend.solution))
    _, conditions = puzzle.run_with_cost(drv.MAX_COST, solution)
    is_cat = puzzle.uncurry()[0] == CAT_MOD
    binds = []
    for condition in conditions.as_iter():
        if condition.first().atom is None:
            continue
        opcode = condition.first().as_int()
        if opcode not in (CREATE_COIN_ANNOUNCEMENT, CREATE_PUZZLE_ANNOUNCEMENT):
            continue
        message = bytes(condition.rest().first().as_atom())
        if is_cat and opcode == CREATE_COIN_ANNOUNCEMENT and len(message) == 33 and message[:1] == b"\xcb":
            continue
        anchor = spend.coin.name() if opcode == CREATE_COIN_ANNOUNCEMENT else spend.coin.puzzle_hash
        binds.append([opcode + 1, bytes32(hashlib.sha256(bytes(anchor) + message).digest())])
    if not binds:
        raise OfferRejected(f"spend {spend.coin.name().hex()[:8]} announces nothing: the creator cannot require it")
    return binds


@dataclass(frozen=True)
class CreatorXch:
    coin: Coin
    puzzle: Program                 # the creator's own p2 puzzle for this coin


@dataclass(frozen=True)
class CreatorCat:
    asset_id: bytes32
    coin: Coin
    inner: Program                  # the creator's inner p2 puzzle
    lineage: LineageProof
    # More coins of the same CAT, spent in one ring with `coin` when no single coin holds the
    # reserve (owner, 2026-10-29: "can't you simply use a combine first"): `coin` still creates
    # the reserve's launcher and the change, so the reserve's parent is unchanged; these only
    # add their amounts to the ring. Each is (coin, inner p2 puzzle, lineage proof).
    extra: tuple = ()
    # V19 (rCAT, CHIP-0038): the hidden puzzle hash H of these coins (zero for a plain CAT). An
    # rCAT coin is CAT(asset, rev(H, inner)) and is spent through the layer's owner path, which
    # re-wraps every coin it creates with H -- the reserve launcher is born layered, the change
    # comes back to the creator layered. Every coin of the ring has the same H.
    hidden: bytes32 = ZERO_32

    @property
    def total(self) -> int:
        return int(self.coin.amount) + sum(int(c.amount) for c, _inner, _lineage in self.extra)

    @property
    def coins(self) -> list:
        return [self.coin, *(c for c, _inner, _lineage in self.extra)]


@dataclass(frozen=True)
class CreationConfig:
    asset_ids: list                 # None is XCH
    reserves: list                  # genesis reserves, same order
    weights: list
    fee_bps: int
    protocol_fee_bps: int
    protocol_ph: bytes32
    total_lp: int
    name: str = ""
    symbol: str = ""
    dao_ph: bytes32 = ZERO_32       # V11.1: the DAO recipient (immutable) and the opening rate
    dao_fee_bps: int = 0
    # V19 (rCAT): one H per asset in `asset_ids` order (PoolConfig field 10); empty = all plain
    hidden_puzzle_hashes: list = field(default_factory=list)

    @property
    def hiddens(self) -> list:
        return list(self.hidden_puzzle_hashes) or [ZERO_32] * len(self.asset_ids)


@dataclass(frozen=True)
class DaoConsentCoin:
    """A coin AT the pool's DAO address that consents to the registration: `plan` spends it with
    [the consent condition, CREATE_COIN back to its own puzzle hash for its whole amount], and the
    wallet signs that spend with the rest. `puzzle` is the coin's own (standard p2) puzzle."""
    coin: Coin
    puzzle: Program


# `dao_consent=FROM_CREATOR_XCH`: the creator's paying XCH coin IS at the DAO address, and its own
# spend carries the consent condition.
FROM_CREATOR_XCH = "from_creator_xch"
# `dao_consent=FROM_LOCK` (V19, the V18 -> V19 batch move, owner 2026-11-17): the DAO is a multisig
# lock, whose own proposal sends the consent; nothing in this bundle carries it. The condition the
# lock must send is in details["dao_consent"], and `register` refuses the bundle without it.
FROM_LOCK = "from_lock"


@dataclass(frozen=True)
class IssuerAttestation:
    """V19 (rCAT issuer binding, OPTIONAL): an rCAT reserve's proof that H is its issuer's own.

    `launcher_id`, `nonce`: the issuer singleton L and the EWS TAIL nonce N, with
    asset_id == drv.ews_asset_id(L, N) -- checked here, recomputed by `register`.
    `inner_puzzle_hash`: the issuer singleton's CURRENT inner puzzle hash; `register` expects
    the message from curry(SINGLETON_MOD, [struct(L), inner_puzzle_hash]).
    `spend`: the issuer singleton's spend that recreates itself and outputs
    `issuer_attestation_condition(registry, pool_launcher_id, asset_id, H)`. The ISSUER signs
    it (its signature joins the bundle through `finalize(issuer_signature=...)`); the creator's
    wallet never sees it as its own.

    How a router obtains the spend: the message names the pool launcher id, which is
    `creation_launcher_id(creator's XCH coin)`, and the registry coin, the live one in the
    record -- both known before the plan. Plan once without `spend` (prepare-create): the
    plan's details["issuer_attestations"] give each issuer the exact condition to emit, the
    singleton puzzle hash it must come from, and `provided: false`. The issuer spends its
    singleton with that condition and signs; plan again with `spend` set. `finalize` refuses
    a bundle with any attestation still missing. Another registration first moves the
    registry coin, which voids every attestation built for the old one: re-attest."""
    asset_id: bytes32
    launcher_id: bytes32
    nonce: bytes
    inner_puzzle_hash: bytes32
    spend: CoinSpend | None = None


def creation_launcher_id(xch_coin: Coin) -> bytes32:
    """The pool launcher id a creation paid from `xch_coin` will have (the launcher's parent is that coin)."""
    return drv.coin_id(xch_coin.name(), SINGLETON_LAUNCHER_HASH, 1)


def issuer_attestation_condition(registry: drv.Registry, pool_launcher_id: bytes32, asset_id: bytes32, hidden: bytes32) -> list:
    """The condition the issuer singleton's spend must output for `register` to list (asset, H)
    in the pool `pool_launcher_id`: a mode-23 message to THIS registry coin."""
    return [drv.SEND_MESSAGE, 0b010_111, drv.rcat_issuer_message(asset_id, hidden, pool_launcher_id), registry.coin.name()]


def _check_issuer_spend(att: IssuerAttestation, registry: drv.Registry, pool_launcher_id: bytes32, hidden: bytes32) -> None:
    """Refuse, by name, an issuer spend `register` would not accept: not the issuer singleton's
    coin at the named inner puzzle hash, or not sending the exact attestation."""
    from chia.wallet.singleton import create_singleton_puzzle_hash
    spend, tag = att.spend, att.asset_id.hex()[:8]
    if spend.coin.puzzle_hash != create_singleton_puzzle_hash(att.inner_puzzle_hash, att.launcher_id):
        raise OfferRejected(f"the issuer spend for {tag} is not a coin of the issuer singleton {att.launcher_id.hex()[:12]} "
                            f"at inner puzzle hash {att.inner_puzzle_hash.hex()[:12]}")
    puzzle = Program.from_bytes(bytes(spend.puzzle_reveal))
    if puzzle.get_tree_hash() != spend.coin.puzzle_hash:
        raise OfferRejected(f"the issuer spend for {tag}: its puzzle does not hash to its coin's puzzle hash")
    want = issuer_attestation_condition(registry, pool_launcher_id, att.asset_id, hidden)
    try:
        _cost, out = puzzle.run_with_cost(GATE_PROOF_MAX_COST, Program.from_bytes(bytes(spend.solution)))
        sent = any(c.first().atom is not None and c.first().as_int() == drv.SEND_MESSAGE
                   and Program.to(c).get_tree_hash() == Program.to(want).get_tree_hash() for c in out.as_iter())
    except Exception as exc:  # noqa: BLE001 -- the issuer's spend is the caller's: a failure is a refusal
        raise OfferRejected(f"the issuer spend for {tag} does not run: {str(exc)[:80]}") from exc
    if not sent:
        raise OfferRejected(f"the issuer spend for {tag} does not send the attestation to this registry coin for this pool "
                            f"(issuer_attestation_condition; the registry coin moves with every registration)")


@dataclass
class CreationPlan:
    pool: drv.V19Pool
    registry_after: drv.Registry
    key: bytes32
    left: tuple
    right: tuple
    recipient_ph: bytes32
    creator_spends: list            # unsigned CoinSpends the creator's wallet signs
    router_spends: list             # CoinSpends nobody signs
    network_fee: int
    details: dict[str, Any] = field(default_factory=dict)
    # V19 (rCAT): the issuer singletons' attestation spends, signed by each ISSUER (not the creator)
    issuer_spends: list = field(default_factory=list)
    # how many attestations the creation needs; finalize refuses until len(issuer_spends) == this
    issuer_needed: int = 0
    # the register leaf's issuer-proof list, one entry per asset (None = no proof): record_patch's issuer_attested
    register_proofs: tuple = ()

    def unsigned_bundle(self) -> SpendBundle:
        return SpendBundle([*self.creator_spends, *self.issuer_spends, *self.router_spends], G2Element())


def canonical(config: CreationConfig) -> CreationConfig:
    """Assets ascending by id with XCH first, as the puzzle requires."""
    order = sorted(range(len(config.asset_ids)), key=lambda i: bytes(32) if config.asset_ids[i] is None else bytes(config.asset_ids[i]))
    hiddens = config.hiddens
    return replace(config, asset_ids=[config.asset_ids[i] for i in order], reserves=[int(config.reserves[i]) for i in order],
                   weights=[int(config.weights[i]) for i in order], hidden_puzzle_hashes=[hiddens[i] for i in order])


def lp_payment_announcement(lp_asset_id: bytes32, launcher_id: bytes32, recipient_ph: bytes32, amount: int) -> bytes32:
    """The announcement the genesis LP settlement makes when it pays the creator:
    sha256(CAT(lp, OFFER_MOD) puzzle hash + tree_hash((launcher_id, [[recipient, amount, [recipient]]])))."""
    settle_ph = construct_cat_puzzle(CAT_MOD, lp_asset_id, OFFER_MOD).get_tree_hash()
    return bytes32(drv.hashlib.sha256(bytes(settle_ph) + bytes(Program.to((launcher_id, [[recipient_ph, amount, [recipient_ph]]])).get_tree_hash())).digest())


def gate_proof_digest(network_id: str, coin_ids: list) -> bytes:
    """What an approved key signs to admit a creation paid from any coin of its wallet:
    the network and this creation's own creator coins, sorted. Domain-separated from the
    fee ladder's digest (forge_fee_identity.fee_proof_digest)."""
    import hashlib
    return hashlib.sha256(b"forge-create-gate|" + network_id.encode() + b"|"
                          + b"".join(sorted(bytes(c) for c in coin_ids))).digest()


def dao_consent_condition(registry: drv.Registry, pool: drv.V19Pool) -> list:
    """V19 (DAO consent): the condition a coin AT the pool's DAO address must output for `register`
    to list a pool that names that DAO -- a mode-23 message to this registry coin naming the
    pool's launcher, config and opening rate. A router puts it in the delegated solution of a
    coin the DAO's wallet (or lock) controls at that address."""
    msg = drv.dao_consent_message(pool.launcher_id, Program.to(pool.config()).get_tree_hash(), int(pool.state[5]))
    return [drv.SEND_MESSAGE, 0b010_111, msg, registry.coin.name()]


def refuse_pre_v19_lp(asset_ids, pre_v19_lp) -> None:
    """V19 lane rule (owner, 2026-11-17): a V19 pool holds only V19 assets -- XCH, plain CATs and
    V19 LP -- never the LP of a pre-V19 Forge pool (V14..V18). The puzzle cannot tell an LP's
    revision, so the lane refuses it here, before anything is signed, from the LP asset ids of the
    older registries' records (forge_stdin.pre_v19_lp_assets)."""
    bad = [a for a in asset_ids if a is not None and bytes(a).hex() in pre_v19_lp]
    if bad:
        raise OfferRejected(f"{bad[0].hex()[:12]}... is the LP of a pre-V19 Forge pool ({pre_v19_lp[bytes(bad[0]).hex()]}); a "
                            "V19 pool holds only XCH, plain CATs and V19 LP. Remove that LP from its pool, add the assets "
                            "to the market's V19 pool, and use the V19 LP instead")


def plan(registry: drv.Registry, slots: dict, config: CreationConfig, xch: CreatorXch, cats: dict,
         recipient_ph: bytes32, network_fee: int, gate_proof_keys: tuple = (),
         dao_consent_spends: tuple = (), dao_consent=None, issuer_attestations: tuple = (),
         pre_v19_lp: dict | None = None) -> CreationPlan:
    """Build the creation bundle around the creator's coins.

    `slots` is the registry's live slot list: key hex -> {key, launcher_id, left, right,
    parent (coin json), parent_inner_hash}, as the registry driver records it.
    `cats` maps asset id -> CreatorCat. The XCH coin must cover: 2 mojos (launcher, eve),
    the XCH reserve, the creation fee, total_lp - 1 of LP backing, and the network fee.
    `dao_consent_spends` (V19, DAO consent): when the config names a DAO, the spend(s) of a coin at
    the DAO address outputting `dao_consent_condition`; they join the wallet-signed spends.
    `dao_consent` builds that consent HERE, where the launcher id it names first exists: a
    DaoConsentCoin (a coin at the DAO address, spent back to itself with the condition), or
    FROM_CREATOR_XCH (the paying coin is at the DAO address; its own conditions carry it).
    `issuer_attestations` (V19, rCAT): OPTIONAL, at most one IssuerAttestation per revocable
    asset, never one for a plain asset. An rCAT without one registers unattested (the gate checks
    its H against the mint); one with a proof is enforced on chain, and its issuer's spend may
    follow (see IssuerAttestation). details["rcat_assets"] says which rCATs are attested.
    """
    config = canonical(config)
    refuse_pre_v19_lp(config.asset_ids, pre_v19_lp or {})
    n = len(config.asset_ids)
    if not (1 <= n <= 10) or len(config.reserves) != n or len(config.weights) != n:
        raise OfferRejected("a pool has 1 to 10 assets with one reserve and one weight each")
    if any(r <= 0 for r in config.reserves) or config.total_lp <= 0:
        raise OfferRejected("genesis reserves and total_lp must be positive")
    # V19 (genesis floor): the genesis supply floor `register` enforces, refused here with a reason.
    if config.total_lp < drv.MIN_GENESIS_LP:
        raise OfferRejected(f"the genesis supply must be at least {drv.MIN_GENESIS_LP:,} LP units "
                            f"(got {config.total_lp:,}); raise the LP ratio")
    # V19 (external review 2026-11-16, finding 09): refused here, before anything is signed, by
    # the same bounds `register` enforces.
    why = drv.genesis_bound_reason(list(config.asset_ids), list(config.reserves), int(config.total_lp))
    if why:
        raise OfferRejected(why)
    # V19 (DAO consent): a named DAO must consent, or `register` refuses the whole creation.
    if config.dao_ph != ZERO_32 and not dao_consent_spends and dao_consent is None:
        raise OfferRejected("a pool that names a DAO needs that DAO's consent: a spend of a coin at the DAO "
                            "address that sends the registration message (see dao_consent_condition)")
    if dao_consent is not None:
        if config.dao_ph == ZERO_32:
            raise OfferRejected("a DAO consent was given for a pool that names no DAO")
        if dao_consent == FROM_LOCK:
            pass   # the lock's proposal sends it (details["dao_consent"])
        elif dao_consent == FROM_CREATOR_XCH:
            if xch.coin.puzzle_hash != config.dao_ph:
                raise OfferRejected("the creator's XCH coin is not at the DAO address, so its spend cannot consent for the DAO")
        elif isinstance(dao_consent, DaoConsentCoin):
            if dao_consent.coin.puzzle_hash != config.dao_ph:
                raise OfferRejected("the consenting coin is not at the pool's DAO address")
            if dao_consent.puzzle.get_tree_hash() != dao_consent.coin.puzzle_hash:
                raise OfferRejected("the consenting coin's puzzle does not hash to its puzzle hash")
            if dao_consent.coin.name() == xch.coin.name() or any(dao_consent.coin.name() == c.name() for cat in cats.values() for c in cat.coins):
                raise OfferRejected("the consenting coin is one of the creator's funding coins; name another, or from_creator_xch")
        else:
            raise OfferRejected("dao_consent is a coin at the DAO address or from_creator_xch")
    hiddens = [bytes32(h) for h in config.hiddens]
    if len(hiddens) != n:
        raise OfferRejected("hidden_puzzle_hashes names one hash per asset (zero for a plain asset)")
    for asset, h in zip(config.asset_ids, hiddens):
        if asset is None and h != ZERO_32:
            raise OfferRejected("XCH has no hidden puzzle hash; only a CAT may be revocable")
    for asset in config.asset_ids:
        if asset is not None and asset not in cats:
            raise OfferRejected(f"the creator must provide a coin of {asset.hex()[:8]} for its reserve")
    # V19 (rCAT): the market is (asset, H) from the CONFIG. The creator's coins must be of exactly
    # that market -- a plain coin cannot fund an rCAT reserve, nor an rCAT coin a plain one.
    for asset, cat in cats.items():
        if asset not in config.asset_ids:
            continue
        want = hiddens[config.asset_ids.index(asset)]
        if bytes(cat.hidden) != bytes(want):
            raise OfferRejected(f"the creator's {asset.hex()[:8]} coins are under hidden puzzle hash "
                                f"{bytes(cat.hidden).hex()[:8]}, the pool names {bytes(want).hex()[:8]}")
    # V19 (rCAT issuer binding): an attestation is OPTIONAL. When given, the rCAT must be CHIP-0038's
    # everything_with_singleton TAIL of an issuer singleton that attests (asset, H, pool); a bad one
    # is refused here by name, before the registry leaf would raise on it.
    attest: dict = {}
    for att in issuer_attestations:
        if att.asset_id not in config.asset_ids:
            raise OfferRejected(f"an issuer attestation names {att.asset_id.hex()[:8]}, which the pool does not hold")
        if att.asset_id in attest:
            raise OfferRejected(f"two issuer attestations name {att.asset_id.hex()[:8]}")
        attest[att.asset_id] = att
    proofs = []
    for asset, h in zip(config.asset_ids, hiddens):
        att = attest.get(asset)
        if h == ZERO_32:
            if att is not None:
                raise OfferRejected(f"{asset.hex()[:8]} is a plain asset in this pool; it takes no issuer attestation")
            proofs.append(None)
            continue
        if att is None:
            # Attestation is OPTIONAL (owner decision 2026-11-12): an unattested rCAT registers with
            # no issuer condition; the router/site gate checks its H against the asset's mint.
            proofs.append(None)
            continue
        if drv.ews_asset_id(att.launcher_id, bytes(att.nonce)) != asset:
            raise OfferRejected(f"{asset.hex()[:8]} is not the everything_with_singleton TAIL of singleton "
                                f"{att.launcher_id.hex()[:12]} with nonce {bytes(att.nonce).hex() or '(empty)'}: V19 lists an rCAT "
                                f"only under the CHIP-0038 EWS TAIL, whose issuer singleton attests its H")
        proofs.append(drv.issuer_proof(att.launcher_id, bytes(att.nonce), att.inner_puzzle_hash))
    xch_index =config.asset_ids.index(None) if None in config.asset_ids else None
    xch_reserve = config.reserves[xch_index] if xch_index is not None else 0
    fee = registry.current_fee     # V19: the fee in registry state, set by the treasury
    need = 2 + xch_reserve + fee + (config.total_lp - 1) + network_fee
    if int(xch.coin.amount) < need:
        raise OfferRejected(f"the creator's XCH coin holds {xch.coin.amount}, the creation needs {need} "
                            f"(launcher, eve, XCH reserve {xch_reserve}, fee {fee}, LP backing {config.total_lp - 1}, network fee {network_fee})")
    for asset, cat in cats.items():
        i = config.asset_ids.index(asset)
        if cat.total < config.reserves[i]:
            raise OfferRejected(f"the creator's {asset.hex()[:8]} coins hold {cat.total}, the reserve needs {config.reserves[i]}")

    # the pool, with the creator's XCH coin as the launcher's parent
    shape = drv.make_pool(config.asset_ids, config.reserves, total_lp=config.total_lp, leaves="forge", weights=config.weights,
                          fee_bps=config.fee_bps, protocol_fee_bps=config.protocol_fee_bps, protocol_ph=config.protocol_ph,
                          dao_ph=config.dao_ph, dao_fee_bps=config.dao_fee_bps,
                          launcher_parent=xch.coin.name(), hidden_puzzle_hashes=hiddens)
    reserve_coins = []
    for i, asset in enumerate(config.asset_ids):
        if asset is None:
            # V19: the creator's coin creates a reserve LAUNCHER, and the launcher creates the reserve.
            # The creator's coin is the grandparent `register` derives the reserve parent from.
            launcher = Coin(xch.coin.name(), drv.RESERVE_LAUNCHER_HASH, uint64(config.reserves[i]))
            reserve_coins.append((Coin(launcher.name(), shape.reserves[i].inner_hash, uint64(config.reserves[i])), None,
                                  xch.coin.name()))
        else:
            cat = cats[asset]
            h = hiddens[i]
            # V19 (rCAT): the launcher is layered with the asset's H, so its lineage names the
            # LAYERED launcher, and the creator coin's lineage names the creator's layered inner.
            launcher = Coin(cat.coin.name(), drv.reserve_launcher_full_hash(asset, h), uint64(config.reserves[i]))
            reserve_coins.append((Coin(launcher.name(), shape.reserves[i].full_hash, uint64(config.reserves[i])),
                                  LineageProof(cat.coin.name(), drv.layered(h, drv.RESERVE_LAUNCHER).get_tree_hash(),
                                               uint64(config.reserves[i])),
                                  cat.coin.name(),
                                  LineageProof(cat.coin.parent_coin_info, drv.layered(h, cat.inner).get_tree_hash(),
                                               cat.coin.amount)))
    pool = drv.make_pool(config.asset_ids, config.reserves, total_lp=config.total_lp, leaves="forge", weights=config.weights,
                         fee_bps=config.fee_bps, protocol_fee_bps=config.protocol_fee_bps, protocol_ph=config.protocol_ph,
                          dao_ph=config.dao_ph, dao_fee_bps=config.dao_fee_bps,
                         launcher_parent=xch.coin.name(), reserve_coins=reserve_coins, hidden_puzzle_hashes=hiddens)
    launcher_id = pool.launcher_id
    key = drv.pool_key(pool.config())
    if key.hex() in slots:
        raise OfferRejected("a pool with this configuration is already registered")
    keys = sorted(bytes32.fromhex(k) for k in slots)
    left_key = max((k for k in keys if k < key), default=None)
    right_key = min((k for k in keys if k > key), default=None)
    if left_key is None or right_key is None:
        raise OfferRejected("the registry's slot list has no bracket for this key (is it initialized?)")
    left_rec, right_rec = slots[left_key.hex()], slots[right_key.hex()]
    left = (left_key, bytes32.fromhex(left_rec["launcher_id"]), bytes32.fromhex(left_rec["left"]))
    right = (right_key, bytes32.fromhex(right_rec["launcher_id"]), bytes32.fromhex(right_rec["right"]))

    eve_ph = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, drv.LP_MINT_INNER).get_tree_hash()
    memos = [m.encode("utf-8") for m in (config.name, config.symbol) if m]
    change = int(xch.coin.amount) - need

    # router side -- built first: the creator's spend asserts what these announce
    launcher = Coin(xch.coin.name(), SINGLETON_LAUNCHER_HASH, uint64(1))
    eve = Coin(xch.coin.name(), eve_ph, uint64(1))
    pool.extra["eve_coin_id"] = eve.name()   # V19: register_solution reads it
    launcher_spend = make_spend(launcher, SINGLETON_LAUNCHER, Program.to([pool.coin.puzzle_hash, 1, [config.total_lp, eve.name()]]))  # V19: names the one eve
    eve_spends = drv.lp_eve_ring(pool, eve, OFFER_PH, config.total_lp, drv.genesis_action(pool))
    lp_pay = drv.genesis_lp_settlement_spends(pool, eve, recipient_ph, config.total_lp)   # V19: burns the floor
    # A fee of zero is a free registration (forge_registry_register.rue asserts no settlement), so no
    # fee coin is made: a 0-mojo settlement would pay nothing (deploy-v19-testnet.py does the same).
    fee_coin = Coin(xch.coin.name(), OFFER_PH, uint64(fee))
    fee_spend = (make_spend(fee_coin, OFFER_MOD, Program.to([[launcher_id, [registry.treasury_ph, fee, [registry.treasury_ph]]]]))
                 if fee > 0 else None)
    slot_spends = []
    for rec, value in ((left_rec, drv.slot_value(left[0], left[1], left[2], right[0])),
                       (right_rec, drv.slot_value(right[0], right[1], left[0], right[2]))):
        parent = Coin(bytes32.fromhex(rec["parent"]["parent_coin_info"]), bytes32.fromhex(rec["parent"]["puzzle_hash"]),
                      uint64(int(rec["parent"]["amount"])))
        _, spend = drv.slot_spend(registry, value, parent, bytes32.fromhex(rec["parent_inner_hash"]))
        slot_spends.append(spend)
    reserve_launchers = drv.reserve_launcher_spends(pool)
    # V19 (rCAT): each issuer's spend, when given, must send exactly the attestation `register` receives
    issuer_spends, issuer_details = [], []
    rcat_assets = [{"asset_id": asset.hex(), "hidden_puzzle_hash": bytes(h).hex(), "attested": asset in attest}
                   for asset, h in zip(config.asset_ids, hiddens) if h != ZERO_32]
    for asset, h in zip(config.asset_ids, hiddens):
        att = attest.get(asset)
        if att is None:
            continue
        if att.spend is not None:
            _check_issuer_spend(att, registry, launcher_id, h)
            issuer_spends.append(att.spend)
        from chia.wallet.singleton import create_singleton_puzzle_hash
        cond = issuer_attestation_condition(registry, launcher_id, asset, h)
        issuer_details.append({
            "asset_id": asset.hex(), "hidden_puzzle_hash": bytes(h).hex(), "launcher_id": att.launcher_id.hex(),
            "nonce": bytes(att.nonce).hex(), "inner_puzzle_hash": att.inner_puzzle_hash.hex(),
            "singleton_puzzle_hash": create_singleton_puzzle_hash(att.inner_puzzle_hash, att.launcher_id).hex(),
            "pool_launcher_id": launcher_id.hex(), "registry_coin_id": registry.coin.name().hex(),
            "message": bytes(cond[2]).hex(), "condition": [cond[0], cond[1], bytes(cond[2]).hex(), bytes(cond[3]).hex()],
            "provided": att.spend is not None})
    reg_bundle, reg_state = drv.registry_spend(registry, "forge_registry_register",
                                               drv.register_solution(pool, left, right, issuer_proofs=proofs),
                                               extra_spends=[])
    router_spends = [*reserve_launchers, launcher_spend, *eve_spends, *lp_pay, *([fee_spend] if fee_spend else []),
                     *slot_spends, *reg_bundle.coin_spends]

    # creator side
    conditions = [
        [CREATE_COIN, SINGLETON_LAUNCHER_HASH, 1, memos] if memos else [CREATE_COIN, SINGLETON_LAUNCHER_HASH, 1],
        [CREATE_COIN, eve_ph, 1],
    ]
    if xch_index is not None:
        conditions.append([CREATE_COIN, drv.RESERVE_LAUNCHER_HASH, xch_reserve])   # V19: the launcher, not the reserve
    if fee > 0:
        conditions.append([CREATE_COIN, OFFER_PH, fee])
    if dao_consent == FROM_CREATOR_XCH:
        conditions.append(dao_consent_condition(registry, pool))      # V19 (DAO consent): the paying coin is the DAO's
    if change > 0:
        conditions.append([CREATE_COIN, xch.puzzle.get_tree_hash(), change, [xch.puzzle.get_tree_hash()]])
    # The creator's bind: the genesis LP must be paid to recipient_ph, or this spend fails.
    # V19: the creator receives everything above the locked floor; the floor is burned by the same
    # settlement, and `register` asserts that burn.
    conditions.append([ASSERT_PUZZLE_ANNOUNCEMENT, lp_payment_announcement(pool.lp_asset_id, launcher_id, recipient_ph,
                                                                          config.total_lp - drv.LOCKED_BURN)])
    # The bundle's bind (F1, 2026-10-07 review, confirmed on the simulator): the router's spends
    # hold no key, so until here whoever put the bundle in a block could drop the registry's
    # `register` and slot spends -- the only spends that checked the reserve launchers and the
    # fee -- point those at itself, and reuse the creator's signed spends byte for byte. The
    # creator's spend therefore asserts an announcement of every router spend that binds the
    # pool: the singleton launcher's, each reserve launcher's, the fee settlement's and the
    # registry's own `forge-registered-v19`. A bundle missing any of them fails this spend.
    for spend in (launcher_spend, *reserve_launchers, *([fee_spend] if fee_spend else []), *reg_bundle.coin_spends):
        conditions.extend(announcement_binds(spend))
    # The creation gate's key proof (owner, 2026-10-09): a wallet that controls an approved
    # address may pay from ANY of its coins, provided that address's key signs inside this
    # spend. AGG_SIG_UNSAFE over a digest of this creation's own creator coins: consensus
    # refuses the whole bundle unless the key's holder signed, so naming a public key one
    # does not hold proves nothing, and the signature cannot be lifted onto other coins.
    digest = None
    if gate_proof_keys:
        import forge_network
        digest = gate_proof_digest(forge_network.network_id(), [xch.coin.name(), *(coin.name() for c in cats.values() for coin in c.coins)])
        for proof_key in gate_proof_keys:
            conditions.append([AGG_SIG_UNSAFE, bytes(proof_key), digest])
    creator_spends = [make_spend(xch.coin, xch.puzzle, drv.p2_delegated_solution(conditions)), *dao_consent_spends]
    if isinstance(dao_consent, DaoConsentCoin):
        # back to itself, whole: the spend only carries the DAO's message
        own = dao_consent.coin.puzzle_hash
        creator_spends.append(make_spend(dao_consent.coin, dao_consent.puzzle, drv.p2_delegated_solution(
            [dao_consent_condition(registry, pool), [CREATE_COIN, own, int(dao_consent.coin.amount), [own]]])))
    # The proof only binds if the paying coin's puzzle actually OUTPUTS those conditions.
    # Its puzzle comes from the caller: one that runs the delegated conditions but drops
    # opcode 49 would pass the gate on a public approved key and never need its signature
    # (2026-10-15 security review, forge-ui U-04). Run it and require every pair.
    if gate_proof_keys and not emits_gate_proofs(creator_spends[0], gate_proof_keys, digest):
        raise OfferRejected("the paying coin's puzzle does not emit the creation gate's key proof; pay from a standard wallet coin")
    for asset, cat in cats.items():
        i = config.asset_ids.index(asset)
        conds = [[CREATE_COIN, drv.RESERVE_LAUNCHER_HASH, config.reserves[i]]]   # V19: CAT-wrapped by the layer
        if cat.total > config.reserves[i]:
            conds.append([CREATE_COIN, cat.inner.get_tree_hash(), cat.total - config.reserves[i], [cat.inner.get_tree_hash()]])
        # One ring: the first coin creates the launcher and the change; the others create
        # nothing and only add their amounts (the CAT layer balances the ring).
        # V19 (rCAT): through the layer's owner path, which re-wraps the launcher and the change with H
        h = cat.hidden
        ring = [SpendableCAT(cat.coin, asset, drv.layered(h, cat.inner),
                             drv.layered_solution(h, cat.inner, drv.p2_delegated_solution(conds)), lineage_proof=cat.lineage)]
        ring.extend(SpendableCAT(coin, asset, drv.layered(h, inner), drv.layered_solution(h, inner, drv.p2_delegated_solution([])),
                                 lineage_proof=lineage)
                    for coin, inner, lineage in cat.extra)
        creator_spends.extend(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, ring).coin_spends)
    registry_after = registry.advance([x.as_int() for x in reg_state.as_iter()])     # RegistryState is [initialized, pool_count, creation_fee]
    return CreationPlan(pool, registry_after, key, left, right, recipient_ph, creator_spends, router_spends, network_fee, {
        "launcher_id": launcher_id.hex(), "lp_asset_id": pool.lp_asset_id.hex(), "key": key.hex(),
        **({"dao_consent": dao_consent_condition(registry, pool)} if dao_consent == FROM_LOCK else {}),
        "need_xch": need, "change": change, "genesis_lp": config.total_lp, "burned_lp": drv.LOCKED_BURN,
        "creator_lp": config.total_lp - drv.LOCKED_BURN, "name": config.name, "symbol": config.symbol,
        **({"issuer_attestations": issuer_details} if issuer_details else {}),
        **({"rcat_assets": rcat_assets} if rcat_assets else {}),
    }, issuer_spends=issuer_spends, issuer_needed=len(issuer_details), register_proofs=tuple(proofs))


GATE_PROOF_MAX_COST = 11_000_000_000     # one spend's share of a block: the caller's puzzle is untrusted


def emits_gate_proofs(spend: CoinSpend, proof_keys, digest: bytes) -> bool:
    """Whether `spend`, run as consensus would run it, outputs AGG_SIG_UNSAFE(key, digest)
    for every key in `proof_keys`. A puzzle that fails, runs over the cost cap, or leaves
    any pair out does not."""
    try:
        _cost, out = Program.from_bytes(bytes(spend.puzzle_reveal)).run_with_cost(
            GATE_PROOF_MAX_COST, Program.from_bytes(bytes(spend.solution)))
        found = set()
        for condition in out.as_iter():
            parts = list(condition.as_iter())
            if len(parts) >= 3 and parts[0].atom is not None and parts[0].as_int() == AGG_SIG_UNSAFE:
                found.add((parts[1].as_atom(), parts[2].as_atom()))
    except Exception:  # noqa: BLE001 -- anything that does not run cleanly proves nothing
        return False
    return all((bytes(key), bytes(digest)) in found for key in proof_keys)


def finalize(plan_: CreationPlan, signature: G2Element, signed_creator_spends: list | None = None,
             issuer_signature: G2Element | None = None) -> SpendBundle:
    """The bundle: the creator's spends (as signed), the issuers' attestation spends and the
    router's, under the creator's aggregate signature (aggregated with `issuer_signature`, the
    issuers' own, when given). If the wallet returned the spends it signed, they replace the
    planned ones -- but they must be the same spends, or the plan is void. A creation with a
    SUPPLIED attestation still missing its issuer's spend is refused (`register` would refuse
    it on chain); an rCAT planned without an attestation needs no issuer spend."""
    if len(plan_.issuer_spends) != plan_.issuer_needed:
        missing = [d["asset_id"][:8] for d in plan_.details.get("issuer_attestations", []) if not d.get("provided")]
        raise OfferRejected(f"the rCAT creation still needs the issuer singleton's attestation spend for {', '.join(missing)}: "
                            f"the issuer spends its singleton with the condition in details.issuer_attestations, then re-plan with it")
    creator = signed_creator_spends if signed_creator_spends is not None else plan_.creator_spends
    planned = {(cs.coin.name(), bytes(cs.puzzle_reveal), bytes(cs.solution)) for cs in plan_.creator_spends}
    got = {(cs.coin.name(), bytes(cs.puzzle_reveal), bytes(cs.solution)) for cs in creator}
    if planned != got:
        raise OfferRejected("the signed creator spends differ from the planned ones; re-plan before signing")
    if issuer_signature is not None:
        from chia_rs import AugSchemeMPL
        signature = AugSchemeMPL.aggregate([signature, issuer_signature])
    bundle = SpendBundle([*creator, *plan_.issuer_spends, *plan_.router_spends], signature)
    try:
        drv.validate(bundle)
    except drv.Rejected as exc:
        raise OfferRejected(f"creation bundle fails consensus validation: {exc}") from exc
    return bundle


def record_patch(plan_: CreationPlan, registry_record: dict[str, Any]) -> dict[str, Any]:
    """What the registry driver's record must learn once the bundle confirms: the pool
    record (as scripts/deploy-v11-testnet.py writes it) and the registry's new coin,
    state and slot list. Applied by `commit_record`."""
    pool = plan_.pool
    reg_after = plan_.registry_after
    key = plan_.key.hex()
    left_key, right_key = plan_.left[0].hex(), plan_.right[0].hex()
    reg_coin_json = {"parent_coin_info": registry_record["coin"]["parent_coin_info"], "puzzle_hash": registry_record["coin"]["puzzle_hash"],
                     "amount": int(registry_record["coin"]["amount"])}
    reg_inner = reg_after.lineage.inner_puzzle_hash.hex()
    slots = dict(registry_record["slots"])
    slots[key] = {"key": key, "launcher_id": pool.launcher_id.hex(), "left": left_key, "right": right_key,
                  "parent": reg_coin_json, "parent_inner_hash": reg_inner}
    slots[left_key] = {**slots[left_key], "right": key, "parent": reg_coin_json, "parent_inner_hash": reg_inner}
    slots[right_key] = {**slots[right_key], "left": key, "parent": reg_coin_json, "parent_inner_hash": reg_inner}
    st = pool.state
    return {
        "pool": {
            "label": plan_.details.get("symbol") or plan_.details["launcher_id"][:12],
            "emoji": plan_.details.get("name", ""), "symbol": plan_.details.get("symbol", ""),
            "key": key, "lp_recipient_ph": plan_.recipient_ph.hex(),
            # Which registry admitted it: a network may roll its registry over (forge_v19_index.pool_registry_id).
            "registry_launcher_id": registry_record.get("launcher_id"),
            "launcher_parent": pool.launcher_parent.hex(), "launcher_id": pool.launcher_id.hex(), "lp_asset_id": pool.lp_asset_id.hex(),
            "asset_ids": [None if a is None else a.hex() for a in pool.asset_ids], "weights": pool.weights,
            "fee_bps": pool.fee_bps, "protocol_fee_bps": pool.protocol_fee_bps, "protocol_ph": pool.protocol_ph.hex(),
            # The DAO recipient is curried into the pool's config: without it the index rebuilds a
            # different puzzle and drops the pool ("snapshot state does not hash to its pool coin's
            # puzzle"). The first mainnet pair, which has a DAO fee, was left out of the index this way
            # (2026-10-10); the deploy script's records always carried it.
            "dao_ph": pool.dao_ph.hex(), "dao_fee_bps": int(st[5]),
            # V19 (rCAT): PoolConfig field 10, curried like the DAO recipient -- left out, the index
            # would rebuild a plain pool and drop every pool holding an rCAT.
            "hidden_puzzle_hashes": [bytes(h).hex() for h in pool.hiddens],
            # V19 (rCAT, attestation optional 2026-11-12): per asset None (plain) / True (registered
            # with an issuer proof, enforced on chain) / False (unattested; the gate vetted its H).
            # Derived from the register solution this plan built (drv.issuer_attested).
            "issuer_attested": drv.issuer_attested(pool.hiddens, plan_.register_proofs or [None] * len(pool.hiddens)),
            "state": [st[0], st[1], st[2], st[3], bytes(st[4]).hex(), int(st[5]), list(st[6]),
                      [bytes(x).hex() for x in st[7]]],   # V19: reserve parents live in state
            "birth": int(pool.birth),
            "coin": {"parent_coin_info": pool.coin.parent_coin_info.hex(), "puzzle_hash": pool.coin.puzzle_hash.hex(), "amount": 1},
            "lineage": {"parent_name": pool.lineage.parent_name.hex(), "inner_puzzle_hash": None, "amount": 1},
            "reserves": [{"coin": {"parent_coin_info": r.coin.parent_coin_info.hex(), "puzzle_hash": r.coin.puzzle_hash.hex(), "amount": int(r.coin.amount)},
                          "lineage": None if r.lineage is None else {"parent_name": r.lineage.parent_name.hex(),
                                                                     "inner_puzzle_hash": r.lineage.inner_puzzle_hash.hex(), "amount": int(r.lineage.amount)}}
                         for r in pool.reserves],
            "history": [], "created_by": "keyless-create",
        },
        "registry": {
            "state": list(reg_after.state),
            "coin": {"parent_coin_info": reg_after.coin.parent_coin_info.hex(), "puzzle_hash": reg_after.coin.puzzle_hash.hex(), "amount": 1},
            "lineage": {"parent_name": reg_after.lineage.parent_name.hex(), "inner_puzzle_hash": reg_inner, "amount": 1},
            "slots": slots,
        },
    }


def commit_record(record_path: str, patch: dict[str, Any], result: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply a `record_patch` to the registry driver's record once the bundle confirmed."""
    import json
    path = Path(record_path)
    state = json.loads(path.read_text(encoding="utf-8"))
    pool = dict(patch["pool"])
    if result:
        pool["created"] = result
        pool["history"] = [result]
    if any(p["launcher_id"] == pool["launcher_id"] for p in state["pools"]):
        return {"success": True, "committed": False, "reason": "already recorded"}
    state["pools"].append(pool)
    state["registry"].update(patch["registry"])
    state.setdefault("log", []).append({"step": "create-pool", "label": pool["label"], "emoji": pool["emoji"], "launcher_id": pool["launcher_id"],
                                        "lp_asset_id": pool["lp_asset_id"], "via": "keyless-create", **(result or {})})
    path.write_text(json.dumps(state, indent=1), encoding="utf-8")
    return {"success": True, "committed": True, "pools": len(state["pools"])}


def plan_json(plan_: CreationPlan) -> dict[str, Any]:
    """What an API returns to a creator: the spends to sign and the pool they will own."""
    return {
        "creator_spends": [cs.to_json_dict() for cs in plan_.creator_spends],
        "router_spends": [cs.to_json_dict() for cs in plan_.router_spends],
        # V19 (rCAT): signed by the issuers, not the creator's wallet -- never send these to it
        "issuer_spends": [cs.to_json_dict() for cs in plan_.issuer_spends],
        "pool": pool_to_snapshot(plan_.pool),
        "registry": {"key": plan_.key.hex(), "left_key": plan_.left[0].hex(), "right_key": plan_.right[0].hex(),
                     "coin_after": {"parent_coin_info": plan_.registry_after.coin.parent_coin_info.hex(),
                                    "puzzle_hash": plan_.registry_after.coin.puzzle_hash.hex(), "amount": 1},
                     "state_after": plan_.registry_after.state},
        **plan_.details,
    }


# ---- the V16 -> V19 and V17 -> V19 mirrors (owner, 2026-11-12: "1 for 1 equivalent LP") ------------

def market_of(pool) -> tuple:
    """What makes two pools of DIFFERENT revisions the same market: assets, weights, LP fee and
    DAO recipient. `pool_key` cannot say it -- it hashes the config, whose LP asset id is the
    launcher's -- so a mirror is matched on these. A V19 pool holding an rCAT is never a V16
    pool's mirror (V16 has no rCAT)."""
    hiddens = getattr(pool, "hiddens", None) or [ZERO_32] * len(pool.asset_ids)
    return (tuple(bytes(ZERO_32 if a is None else a) for a in pool.asset_ids), tuple(int(w) for w in pool.weights),
            int(pool.fee_bps), bytes(pool.dao_ph), tuple(bytes(h) for h in hiddens))


def find_mirror(source_pool, v19_pools) -> drv.V19Pool | None:
    """The V19 pool that mirrors `source_pool` (market_of equal), or None. A V16 source has no
    rCAT, so its mirror is all plain; a V17 source's mirror carries the SAME hidden puzzle hash
    per asset (an rCAT reserve keeps its issuer's H across the migration)."""
    want = market_of(source_pool)
    if getattr(source_pool, "hiddens", None) is None:
        want = (*want[:4], tuple(bytes(ZERO_32) for _ in source_pool.asset_ids))
    for pool in v19_pools:
        if market_of(pool) == want:
            return pool
    return None


def mirror_config_from_v16(snapshot: dict[str, Any], *, protocol_fee_bps: int | None = None, registry=None,
                           existing_v19: list | tuple = (), reserves: list | None = None,
                           total_lp: int | None = None, name: str = "", symbol: str = "") -> CreationConfig:
    """A protocol-18 CreationConfig mirroring a protocol-17 (V16) pool: the same asset ids,
    weights, LP fee, DAO recipient and current DAO rate; every hidden puzzle hash zero; the dev
    fee the V19 registry pins (its constant 15, or `protocol_fee_bps`); and a genesis supply at
    or above MIN_GENESIS_LP.

    The V19 pool opens at the V16 pool's price and LP value: by default its reserves are the
    V16 reserves scaled to `total_lp` (MIN_GENESIS_LP unless given) over the V16 supply, each
    rounded UP, so one V19 LP unit holds at least what one V16 unit does and a migration
    (forge_v19_route.migrate_v16_to_v19) trades about one for one. Explicit `reserves` set the
    scale instead; the supply then follows at the V16 LP value. Refused when `existing_v19`
    already holds the mirror (snapshots or pools).
    """
    import forge_v16_offer as v16
    src = v16.snapshot_to_pool(snapshot)          # proves the snapshot against its pool coin
    return _mirror_config(src, "V16", protocol_fee_bps=protocol_fee_bps, registry=registry, existing_v19=existing_v19,
                          reserves=reserves, total_lp=total_lp, name=name, symbol=symbol,
                          hiddens=[ZERO_32] * len(src.asset_ids))


def _mirror_config(src, rev: str, *, protocol_fee_bps, registry, existing_v19, reserves, total_lp, name, symbol,
                   hiddens) -> CreationConfig:
    others = [snapshot_to_pool_any(p) for p in existing_v19]
    found = find_mirror(src, others)
    if found is not None:
        raise OfferRejected(f"a V19 pool for this market already exists ({found.launcher_id.hex()[:12]}); migrate into it")
    if protocol_fee_bps is None:
        protocol_fee_bps = int(registry.constants[15]) if registry is not None and len(registry.constants) > 15 else 5
    r16, l16 = [int(x) for x in src.state[0]], int(src.state[1])
    if l16 <= 0 or any(r <= 0 for r in r16):
        raise OfferRejected(f"the {rev} pool is empty; there is no price to mirror")
    if reserves is None:
        total = max(int(total_lp or 0), drv.MIN_GENESIS_LP)
        reserves = [-(-r * total // l16) for r in r16]          # ceil: never below the source LP value
    else:
        reserves = [int(x) for x in reserves]
        if len(reserves) != len(r16) or any(r <= 0 for r in reserves):
            raise OfferRejected(f"reserves name one positive amount per asset of the {rev} pool")
        total = int(total_lp) if total_lp is not None else min(r * l16 // r0 for r, r0 in zip(reserves, r16))
    if total < drv.MIN_GENESIS_LP:
        raise OfferRejected(f"the mirror's genesis supply {total:,} is below MIN_GENESIS_LP {drv.MIN_GENESIS_LP:,}; "
                            "give larger reserves")
    why = drv.genesis_bound_reason(list(src.asset_ids), reserves, total)   # V19 (finding 09)
    if why:
        raise OfferRejected(f"the {rev} pool's LP value cannot be mirrored on V19: {why}")
    return CreationConfig(list(src.asset_ids), reserves, [int(w) for w in src.weights], int(src.fee_bps),
                          int(protocol_fee_bps), registry.protocol_ph if registry is not None else src.protocol_ph,
                          total, name, symbol, dao_ph=src.dao_ph, dao_fee_bps=int(src.state[5]),
                          hidden_puzzle_hashes=list(hiddens))


def check_mirror(config: CreationConfig, source_pool) -> None:
    """A creation that says it mirrors an older pool (`config.from_pool`: a V16 or a V17 pool)
    must BE that market: the same assets, weights, LP fee, DAO recipient and current DAO rate;
    every asset plain for a V16 source, each asset's hidden puzzle hash equal to the V17
    source's. Refused by name on the first field that differs."""
    c = canonical(config)
    proto = int(getattr(sys.modules.get(type(source_pool).__module__), "PROTOCOL_VERSION", 0) or 0)
    rev = {19: "V18", 18: "V17"}.get(proto) or ("V17" if getattr(source_pool, "hiddens", None) is not None else "V16")
    if list(c.asset_ids) != list(source_pool.asset_ids):
        raise OfferRejected(f"the mirror's assets are not the {rev} pool's")
    if [int(w) for w in c.weights] != [int(w) for w in source_pool.weights]:
        raise OfferRejected(f"the mirror's weights are not the {rev} pool's")
    if int(c.fee_bps) != int(source_pool.fee_bps):
        raise OfferRejected(f"the mirror's LP fee {c.fee_bps} bps is not the {rev} pool's {source_pool.fee_bps}")
    if bytes(c.dao_ph) != bytes(source_pool.dao_ph):
        raise OfferRejected(f"the mirror's DAO recipient is not the {rev} pool's")
    if int(c.dao_fee_bps) != int(source_pool.state[5]):
        raise OfferRejected(f"the mirror's DAO fee {c.dao_fee_bps} bps is not the {rev} pool's {int(source_pool.state[5])}")
    want_h = [bytes(h) for h in (getattr(source_pool, "hiddens", None) or [ZERO_32] * len(source_pool.asset_ids))]
    if [bytes(h) for h in c.hiddens] != want_h:
        raise OfferRejected("a mirror of a V16 market keeps every asset plain" if rev == "V16"
                            else f"the mirror's hidden puzzle hashes (rCAT H) are not the {rev} pool's")


def mirror_config_from_v18(snapshot: dict[str, Any], *, protocol_fee_bps: int | None = None, registry=None,
                           existing_v19: list | tuple = (), reserves: list | None = None,
                           total_lp: int | None = None, name: str = "", symbol: str = "") -> CreationConfig:
    """mirror_config_from_v17 for a V18 (protocol 19) pool: V18 drains into its V19 twin (owner,
    2026-11-17). The same market and each asset's hidden puzzle hash carried over; opens at the V18
    pool's price and LP value."""
    import forge_v18_offer as v18
    src = v18.snapshot_to_pool(snapshot)          # proves the snapshot against its pool coin
    return _mirror_config(src, "V18", protocol_fee_bps=protocol_fee_bps, registry=registry, existing_v19=existing_v19,
                          reserves=reserves, total_lp=total_lp, name=name, symbol=symbol,
                          hiddens=list(src.hiddens))


def mirror_config_from_v17(snapshot: dict[str, Any], *, protocol_fee_bps: int | None = None, registry=None,
                           existing_v19: list | tuple = (), reserves: list | None = None,
                           total_lp: int | None = None, name: str = "", symbol: str = "") -> CreationConfig:
    """mirror_config_from_v16 for a V17 (protocol 18) pool: the same market, and each asset's
    hidden puzzle hash carried over (a V17 rCAT pool's mirror is an rCAT pool under the same H).
    Opens at the V17 pool's price and LP value, as the V16 mirror does."""
    import forge_v17_offer as v17
    src = v17.snapshot_to_pool(snapshot)          # proves the snapshot against its pool coin
    return _mirror_config(src, "V17", protocol_fee_bps=protocol_fee_bps, registry=registry, existing_v19=existing_v19,
                          reserves=reserves, total_lp=total_lp, name=name, symbol=symbol,
                          hiddens=list(src.hiddens))


def snapshot_to_pool_any(value):
    """A V19 pool from a pool object or a V19 snapshot."""
    if isinstance(value, dict):
        from forge_v19_offer import snapshot_to_pool
        return snapshot_to_pool(value)
    return value
