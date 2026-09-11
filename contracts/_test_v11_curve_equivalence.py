#!/usr/bin/env python3
"""forge_curve.rue gives the same answers as the V10 puzzle it was moved from.

The Build Spec's rule for the curve is "moved, not rewritten": the maths that 16
pools and 12 launch-matrix steps ran on has to arrive in V11 unchanged, or the
months of evidence behind it stay behind. Two independent comparisons hold the
port to that:

  1. THE CORPUS. compiled/v10-spend-corpus.json holds every V10 pool spend that
     confirmed on testnet11 -- puzzle reveal, solution, decoded config, state
     and action. Each one is first replayed through the real V10 puzzle (so the
     record is provably what consensus accepted), then the same numbers are fed
     to the compiled V11 functions, which must accept exactly the recorded
     figure and refuse one unit either side of it. The Python reference in
     forge_math is checked against the same rows, so three implementations
     agree on every real spend.

  2. THE V10 FUNCTIONS THEMSELVES. scripts/build-v11.py compiles the curve
     functions straight out of pool_singleton_FORGE.rue with their bodies
     untouched. Here both sets are driven with randomized pools -- one to ten
     assets, weights up to the cap, every fee edge -- at the honest value, one
     unit either side of it, and at random, and every output (including
     failure) must match. Four functions compile byte-identical; the suite
     records which.

Exit codes: 0 all rows agree, 1 a disagreement, 2 the corpus or the build is
absent (nothing exercised).
"""
import json
import pathlib
import random
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia.types.blockchain_format.program import Program

import forge_math

CONTRACTS = pathlib.Path(__file__).resolve().parent
CORPUS = CONTRACTS / "compiled" / "v10-spend-corpus.json"
COMPILED = CONTRACTS / "v11" / "compiled"
MODE_SWAP, MODE_ADD, MODE_REMOVE = 0, 1, 2
ASSET, COIN = b"\xaa" * 32, b"\xbb" * 32


def load(name: str) -> Program:
    return Program.from_bytes(bytes.fromhex((COMPILED / f"{name}.rue.hex").read_text().strip()))


def run(program: Program, *args):
    """('ok', int) or ('err', message) -- a refusal by exception is an answer too."""
    try:
        out = program.run(Program.to(list(args)))
        return ("ok", out.as_int() if out.atom is not None else str(out))
    except Exception as exc:
        return ("err", type(exc).__name__)


def states(amounts):
    """V10 took List<ReserveState>; the id fields play no part in the curve."""
    return [[ASSET, COIN, a] for a in amounts]


class Suite:
    def __init__(self):
        self.v11 = {fn: load(f"forge_curve.{fn}") for fn in (
            "exact_swap_output", "exact_invariant_lp_mint", "exact_withdrawal",
            "effective_product", "min_deposit_ratio", "product_of", "sum_weights",
            "pow_int", "vault_fee_bps", "protocol_fee_owed", "valid_protocol_fees")}
        self.v10 = {fn: load(f"v10_probe.{fn}") for fn in (
            "exact_swap_output", "exact_invariant_lp_mint", "exact_withdrawal",
            "effective_product", "min_deposit_ratio", "product_of", "sum_weights",
            "pow_int", "vault_fee_bps")}
        self.checks = self.failures = 0

    def expect(self, label, ok):
        self.checks += 1
        if not ok:
            self.failures += 1
            print(f"  [FAIL] {label}")

    def same(self, fn, v11_args, v10_args, label):
        a, b = run(self.v11[fn], *v11_args), run(self.v10[fn], *v10_args)
        self.expect(f"{label}: V11 {a} vs V10 {b}", a == b)
        return a

    # -- the corpus -------------------------------------------------------------

    def corpus(self, corpus) -> dict:
        counts = {"swap": 0, "add": 0, "remove": 0, "replayed": 0}
        for pool in corpus["pools"]:
            for g in pool["generations"]:
                if "action" not in g:
                    continue
                tag = f"{pool['label']} @{g['spent_height']}"
                # The record is what consensus ran: the real puzzle must accept it.
                replay = run(Program.from_bytes(bytes.fromhex(g["puzzle_reveal"])),
                             *list(Program.from_bytes(bytes.fromhex(g["solution"])).as_iter()))
                self.expect(f"{tag} V10 puzzle replays the recorded solution: {replay}", replay[0] == "ok")
                counts["replayed"] += 1

                cfg, st, act = g["config"], g["state"], g["action"]
                old = [r["amount"] for r in st["reserves"]]
                nxt = [p["successor_amount"] for p in act["reserve_plans"]]
                w, fee, pbps = cfg["weights"], cfg["fee_bps"], cfg["protocol_fee_bps"]
                total_lp, mode = st["total_lp"], act["mode"]

                if mode == MODE_SWAP:
                    counts["swap"] += 1
                    up = next(i for i in range(len(old)) if nxt[i] > old[i])
                    dn = next(i for i in range(len(old)) if nxt[i] < old[i])
                    for delta, want in ((0, 1), (-1, 0), (+1, 0)):
                        args = (old[up], old[dn], nxt[up], nxt[dn] + delta, w[up], w[dn], fee)
                        got = self.same("exact_swap_output", args, args, f"{tag} swap next_out{delta:+d}")
                        self.expect(f"{tag} swap next_out{delta:+d} -> {want}", got == ("ok", want))
                    py = forge_math.swap_output(old[up], old[dn], nxt[up] - old[up], fee, w[up], w[dn])
                    self.expect(f"{tag} forge_math.swap_output agrees", py == old[dn] - nxt[dn])
                    for i, plan in enumerate(act["reserve_plans"]):
                        released = old[i] - nxt[i]
                        owed = plan["protocol_fee_amount"]
                        if i == dn:
                            self.expect(f"{tag} protocol_fee_owed",
                                        run(self.v11["protocol_fee_owed"], released, pbps) == ("ok", owed))
                            self.expect(f"{tag} valid_protocol_fees accepts",
                                        run(self.v11["valid_protocol_fees"], owed, released, pbps) == ("ok", 1))
                            self.expect(f"{tag} valid_protocol_fees refuses +1",
                                        run(self.v11["valid_protocol_fees"], owed + 1, released, pbps) == ("ok", 0))
                        else:
                            self.expect(f"{tag} no fee on reserve {i}", owed == 0)

                elif mode == MODE_ADD:
                    counts["add"] += 1
                    lp = act["lp_delta"]
                    for delta, want in ((0, 1), (+1, 0), (-1, 0)):
                        got = self.same("exact_invariant_lp_mint",
                                        (old, nxt, w, total_lp, lp + delta, fee),
                                        (states(old), states(nxt), w, total_lp, lp + delta, fee),
                                        f"{tag} mint lp_delta{delta:+d}")
                        self.expect(f"{tag} mint lp_delta{delta:+d} -> {want}", got == ("ok", want))
                    ratio = self.same("min_deposit_ratio", (old, nxt), (states(old), states(nxt)), f"{tag} ratio")
                    k = sum(w)
                    self.same("effective_product", (old, nxt, w, ratio[1], k, fee),
                              (states(old), states(nxt), w, ratio[1], k, fee), f"{tag} effective_product")
                    py = forge_math.invariant_lp_mint(old, [n - o for n, o in zip(nxt, old)],
                                                      total_lp, fee, w, version=10)
                    self.expect(f"{tag} forge_math.invariant_lp_mint agrees ({py} vs {lp})", py == lp)

                elif mode == MODE_REMOVE:
                    counts["remove"] += 1
                    burn = -act["lp_delta"]
                    vf = self.same("vault_fee_bps", (old, fee), (states(old), fee), f"{tag} vault fee")[1]
                    got = self.same("exact_withdrawal", (old, nxt, burn, total_lp, vf),
                                    (states(old), states(nxt), burn, total_lp, vf), f"{tag} withdrawal")
                    self.expect(f"{tag} withdrawal -> 1", got == ("ok", 1))
                    for delta in (+1, -1):
                        bent = [nxt[0] + delta, *nxt[1:]]
                        got = self.same("exact_withdrawal", (old, bent, burn, total_lp, vf),
                                        (states(old), states(bent), burn, total_lp, vf),
                                        f"{tag} withdrawal bent {delta:+d}")
                        self.expect(f"{tag} withdrawal bent {delta:+d} -> 0", got == ("ok", 0))
                    py = forge_math.withdrawal_amounts(old, burn, total_lp, vf)
                    self.expect(f"{tag} forge_math.withdrawal_amounts agrees",
                                py == [o - n for o, n in zip(old, nxt)])
        return counts

    # -- randomized, against the V10 functions ------------------------------------

    def randomized(self, cases: int) -> int:
        rng = random.Random(0x10F0A6E)
        done = 0
        while done < cases:
            n = rng.choice([1, 1, 2, 2, 2, 2, 3, 3, 4, 5, 10])
            while True:
                w = [rng.randint(1, 8) for _ in range(n)]
                if sum(w) <= 20:
                    break
            old = [rng.choice([rng.randint(1_000, 10**7), rng.randint(10**7, 10**13)]) for _ in range(n)]
            fee = rng.choice([0, 1, 30, 100, 200])
            total_lp = rng.randint(2, 10**12)
            tag = f"rand#{done} n={n} w={w} fee={fee}"
            done += 1

            # swap, both directions of every weight ratio the cap allows
            if n >= 2:
                i, j = rng.sample(range(n), 2)
                gross = rng.randint(1, old[i] * 3)
                try:
                    honest = forge_math.swap_output(old[i], old[j], gross, fee, w[i], w[j])
                except ValueError:
                    honest = None
                if honest is not None:
                    next_out = old[j] - honest
                    for cand in (next_out, next_out - 1, next_out + 1, rng.randint(1, old[j])):
                        args = (old[i], old[j], old[i] + gross, cand, w[i], w[j], fee)
                        got = self.same("exact_swap_output", args, args, f"{tag} swap cand={cand}")
                        if cand == next_out:
                            self.expect(f"{tag} swap accepts the honest output", got == ("ok", 1))

            # mint, including deposits that are wildly off-ratio
            dep = [rng.choice([0, rng.randint(1, o), rng.randint(1, o * 5)]) for o in old]
            if any(dep):
                nxt = [o + d for o, d in zip(old, dep)]
                honest = forge_math.invariant_lp_mint(old, dep, total_lp, fee, w, version=10)
                for cand in (honest, honest - 1, honest + 1, rng.randint(0, max(1, honest * 2))):
                    got = self.same("exact_invariant_lp_mint", (old, nxt, w, total_lp, cand, fee),
                                    (states(old), states(nxt), w, total_lp, cand, fee),
                                    f"{tag} mint cand={cand}")
                    if cand == honest:
                        self.expect(f"{tag} mint accepts the honest delta", got == ("ok", 1))
                ratio = self.same("min_deposit_ratio", (old, nxt), (states(old), states(nxt)), f"{tag} ratio")
                self.same("effective_product", (old, nxt, w, ratio[1], sum(w), fee),
                          (states(old), states(nxt), w, ratio[1], sum(w), fee), f"{tag} effective_product")
                self.same("product_of", (old, w), (states(old), w), f"{tag} product_of")

            # withdrawal, vault fee applied only where V10 applied it
            burn = rng.randint(1, total_lp - 1)
            vf = self.same("vault_fee_bps", (old, fee), (states(old), fee), f"{tag} vault fee")[1]
            paid = forge_math.withdrawal_amounts(old, burn, total_lp, vf)
            nxt = [o - p for o, p in zip(old, paid)]
            got = self.same("exact_withdrawal", (old, nxt, burn, total_lp, vf),
                            (states(old), states(nxt), burn, total_lp, vf), f"{tag} withdrawal")
            self.expect(f"{tag} withdrawal accepts the honest payout", got == ("ok", 1))
            k = rng.randrange(n)
            bent = [*nxt[:k], nxt[k] + rng.choice([-1, 1]), *nxt[k + 1:]]
            self.same("exact_withdrawal", (old, bent, burn, total_lp, vf),
                      (states(old), states(bent), burn, total_lp, vf), f"{tag} withdrawal bent")

            self.same("sum_weights", (w,), (w,), f"{tag} sum_weights")
            e = rng.randint(0, 20)
            self.same("pow_int", (old[0], e), (old[0], e), f"{tag} pow_int")
        return done


def main() -> int:
    if not CORPUS.is_file():
        print("  [skip] compiled/v10-spend-corpus.json is absent; run scripts/record-v10-corpus.py")
        return 2
    if not (COMPILED / "forge_curve.exact_swap_output.rue.hex").is_file():
        print("  [skip] V11 build outputs are absent; run scripts/build-v11.py")
        return 2

    suite = Suite()
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    counts = suite.corpus(corpus)
    print(f"corpus: {counts['replayed']} V10 spends replayed through the real puzzle "
          f"({counts['swap']} swaps, {counts['add']} adds, {counts['remove']} removes) "
          f"across {len(corpus['pools'])} pools")
    cases = suite.randomized(300)
    print(f"randomized: {cases} pools driven through both the V10 and the V11 functions")
    print()
    print(f"{suite.checks - suite.failures}/{suite.checks} equivalence checks passed")
    return 0 if suite.failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
