from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from sage_rpc import SageRPC

EMOJI_PALETTE = ['🧪', '🔥', '🌀', '⚡', '🌿', '💎', '🚀', '🧿', '🍀', '🌊']
TXCH_EMOJI = ''      # the base asset is named, never a glyph

TOKEN_REGISTRY_PATH = Path(__file__).parent.parent / 'src' / 'lib' / 'tokenRegistry.json'


def _load_known_emoji() -> dict[str, str]:
    """assetId -> glyph from the registry; the palette below is only for assets it lacks."""
    try:
        raw = json.loads(TOKEN_REGISTRY_PATH.read_text(encoding='utf-8'))
        return {str(t['assetId']).strip().lower(): str(t['emoji']) for t in raw.get('tokens', [])
                if isinstance(t, dict) and t.get('emoji')}
    except (OSError, json.JSONDecodeError, KeyError):
        return {}


def _load_known_assets() -> dict[str, tuple[str, str]]:
    # Single source of truth shared with src/lib/tokenRegistry.ts — avoids the
    # TS/Python symbol maps drifting out of sync (T6/T11 were previously
    # swapped between the two).
    try:
        raw = json.loads(TOKEN_REGISTRY_PATH.read_text(encoding='utf-8'))
        tokens = raw.get('tokens') if isinstance(raw, dict) else None
        if not isinstance(tokens, list):
            return {}
        return {
            str(token['assetId']).strip().lower(): (str(token['symbol']), str(token['name']))
            for token in tokens
            if isinstance(token, dict) and token.get('assetId') != 'txch'
        }
    except (OSError, json.JSONDecodeError, KeyError):
        return {}


KNOWN_ASSETS: dict[str, tuple[str, str]] = _load_known_assets()


def normalize_asset_id(value: object) -> str:
    if not isinstance(value, str):
        return ''
    normalized = value.strip().lower().removeprefix('0x')
    return normalized if len(normalized) == 64 else ''


def pick_first_text(cat: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = cat.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ''


KNOWN_EMOJI: dict[str, str] = _load_known_emoji()


def emoji_for_asset(asset_id: str) -> str:
    known = KNOWN_EMOJI.get(str(asset_id).strip().lower())
    if known:
        return known
    if asset_id == 'txch':
        return TXCH_EMOJI

    checksum = 0
    for ch in asset_id:
        checksum = (checksum + ord(ch)) % len(EMOJI_PALETTE)
    return EMOJI_PALETTE[checksum]


def starts_with_known_emoji(text: str) -> bool:
    if not text:
        return False
    symbols = [TXCH_EMOJI, *EMOJI_PALETTE]
    return any(text.startswith(symbol) for symbol in symbols)


def decorate_label(asset_id: str, label: str) -> str:
    trimmed = label.strip()
    if not trimmed:
        return f"{emoji_for_asset(asset_id)} CAT {asset_id[:8]}"
    if starts_with_known_emoji(trimmed):
        return trimmed
    return f"{emoji_for_asset(asset_id)} {trimmed}"


def load_updates_from_stdin() -> dict[str, dict[str, str]]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}

    parsed = json.loads(raw)
    updates = parsed.get('updates') if isinstance(parsed, dict) else None
    if not isinstance(updates, list):
        return {}

    mapped: dict[str, dict[str, str]] = {}
    for item in updates:
        if not isinstance(item, dict):
            continue

        asset_id = normalize_asset_id(item.get('assetId') or item.get('asset_id'))
        if not asset_id:
            continue

        symbol = item.get('symbol')
        name = item.get('name')
        mapped[asset_id] = {
            'symbol': symbol.strip() if isinstance(symbol, str) else '',
            'name': name.strip() if isinstance(name, str) else '',
        }

    return mapped


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=9257)
    parser.add_argument('--updates-from-stdin', action='store_true')
    args = parser.parse_args()

    explicit_updates = load_updates_from_stdin() if args.updates_from_stdin else {}

    try:
        sage = SageRPC(host=args.host, port=args.port)
        cats = sage.get_cats()

        applied: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        errors: list[dict[str, str]] = []

        for cat in cats:
            asset_id = normalize_asset_id(cat.get('asset_id') or cat.get('assetId'))
            if not asset_id:
                continue

            known_symbol, known_name = KNOWN_ASSETS.get(asset_id, ('', ''))
            current_symbol = pick_first_text(cat, ('symbol', 'ticker', 'name', 'asset_name'))
            current_name = pick_first_text(cat, ('name', 'asset_name', 'symbol', 'ticker'))
            explicit = explicit_updates.get(asset_id, {})

            target_symbol_base = explicit.get('symbol') or known_symbol or current_symbol or f'CAT {asset_id[:8]}'
            target_name_base = explicit.get('name') or known_name or current_name or f'CAT {asset_id[:12]}'

            target_symbol = decorate_label(asset_id, target_symbol_base)
            target_name = decorate_label(asset_id, target_name_base)

            existing_symbol = current_symbol.strip()
            existing_name = current_name.strip()
            if existing_symbol == target_symbol and existing_name == target_name:
                skipped.append({
                    'assetId': asset_id,
                    'name': target_name,
                    'symbol': target_symbol,
                    'reason': 'already-updated',
                })
                continue

            try:
                updated_record = dict(cat)
                updated_record['asset_id'] = asset_id
                updated_record['name'] = target_name
                updated_record['ticker'] = target_symbol
                sage.call('update_cat', {
                    'record': updated_record,
                })
                applied.append({
                    'assetId': asset_id,
                    'name': target_name,
                    'symbol': target_symbol,
                })
            except Exception as exc:
                errors.append({
                    'assetId': asset_id,
                    'name': target_name,
                    'symbol': target_symbol,
                    'error': str(exc),
                })

        success = len(errors) == 0
        print(json.dumps({
            'success': success,
            'applied': applied,
            'skipped': skipped,
            'errors': errors,
            'message': f'Updated {len(applied)} CAT labels in Sage wallet.' if success else f'Updated {len(applied)} CAT labels with {len(errors)} error(s).',
        }))
        return 0 if success else 2
    except Exception as exc:
        print(json.dumps({'success': False, 'error': str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
