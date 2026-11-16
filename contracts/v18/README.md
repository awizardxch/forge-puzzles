# V18 (protocol 19): compiled builds

V18 is the revision that follows V16 on mainnet. Its registry launched on mainnet on 2026-11-15
at block 9,579,600, and V16 pools stay live beside it. What V18 changes is described in
[CHIP-0062](../../docs/chip/chip-awizard-weighted-n-asset-amm.md), revisions 14 and 15
(section "Revisions V17 and V18"): fee collection with canonical reserve selectors, stricter
registration (canonical configuration, a registry-pinned protocol fee rate, a named DAO's
consent, a genesis supply floor, no pool holding its own LP), and revocable CATs (CHIP-0038)
with the `rebase` and `refund` leaves.

This folder carries the **compiled puzzles** as deployed, so anyone can check the puzzles a V18
coin runs against these bytes:

- `compiled/*.hex`: each puzzle's serialized CLVM, built with rue 0.8.4.
- `compiled/*.hash`: the tree hash of the matching `.hex`.
- `compiled/manifest.json`: every output's tree hash, the pinned hash where one exists, and the
  sha256 of the source file it was built from.
- `compiled/merkle.json`: the leaf hashes and the action-layer merkle root every V18 pool carries.
- `pins.json`: the upstream puzzles (CHIP-0050 action layer) pinned byte for byte.

The `.rue` sources, the drivers and the suites will be added to this folder later. The source
hashes in `manifest.json` are fixed now, so the sources published then can be checked against the
builds published here.

## Mainnet registry

| | |
|---|---|
| Registry launcher id | `5fb01bf2dce18f5c4bb1ae99d965b58de88f2bb9a6b117d242baaf5565d15df9` |
| Launched | 2026-11-15, block 9,579,600 |
| Creation fee | 1 XCH (registry state; moved only by `set_fee`) |
| Treasury puzzle hash | `3d4e9dcf71e039f0814909d24f2a5c9c7e76359fe5b0b2ca8e49af1989750712` |
| Protocol fee recipient | `4844835b72f800e0a0c3eacb6575f90b4b637ca3bdc63ccde09d6b4f37185bae` |
| Oracle window | 32 |
