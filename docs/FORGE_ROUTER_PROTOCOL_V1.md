# Forge Router Protocol v1 (Draft)

Goal: permissionless offer settlement for Forge pools where any operator can run a router and compete to fill user offers.

## Design principles

- Non-custodial: users create and sign offers in their wallet.
- Deterministic: router validates pool and quote constraints before broadcast.
- Open participation: no allowlist required to run a router.
- Safe fallback: clients can relay to multiple routers and keep offer portability.

## Current supported scope

- CAT-CAT and CAT-XCH style single-pool swaps using one launcher id.
- One-hop settlement intent (inIndex, outIndex, amountIn, minAmountOut).

## Client relay behavior

Client submits to local relay endpoint:

- POST /api/forge-router

Relay can try router strategies in sequence:

1) Forge Router v1 API
- POST {routerBase}/v1/offers/swap

2) Tibet-compatible API
- GET {routerBase}/pair/{launcherId}
- POST {routerBase}/offer/{launcherId} with action SWAP

## Forge Router v1 request

- offer: string (bech32 offer)
- launcherId: string (pool launcher id)
- inIndex: number
- outIndex: number
- tokenInAssetId: string (optional)
- tokenOutAssetId: string (optional)
- amountIn: string (optional integer mojos)
- minAmountOut: string (optional integer mojos)

## Forge Router v1 response

Success:

- success: true
- txId: string (optional)
- offerId: string (optional)
- message: string (optional)

Failure:

- success: false
- error: string
- detail: string (optional)

## Operator requirements

- Keep chain index for pool coin lineage and latest reserves.
- Verify launcherId exists and is synced before accepting settlement.
- Reject stale/invalid offers with explicit reasons.
- Broadcast signed spend bundle and expose tx id when available.
- Log deterministic failure categories for observability.

## Config in this repo

- Server-side router list: FORGE_ROUTER_ENDPOINTS (comma-separated URLs)
- Client-side router list: VITE_FORGE_ROUTER_ENDPOINTS
- Optional local override: browser localStorage key awizard:forge:router-endpoints (JSON array)

## N-asset extension path

For multi-asset pools, keep v1 compatible and add:

- routeLegs: array of pool legs with launcherId/inIndex/outIndex per leg
- settlementBoundary: external-offer | pool-singleton-v2
- constraints: per-leg minOut and global minOut

Routers can adopt this as v2 without breaking v1 CAT-CAT clients.
