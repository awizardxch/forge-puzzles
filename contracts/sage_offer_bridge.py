from __future__ import annotations

import json
import sys
from typing import Any

from sage_rpc import SageRPC


def emit(payload: dict[str, Any]) -> int:
    print(json.dumps(payload))
    return 0 if payload.get('success') else 1


def load_payload() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    return json.loads(raw)


def main() -> int:
    payload = load_payload()
    action = str(payload.get('action') or '').strip().lower()
    host = str(payload.get('host') or '127.0.0.1')
    port = int(payload.get('port') or 9257)

    try:
        sage = SageRPC(host=host, port=port)

        if action == 'status':
          sage.get_sync_status()
          return emit({
              'success': True,
              'available': True,
              'supportsCreateOffers': False,
              'supportsImportOffers': True,
              'supportsTakeOffers': True,
              'source': 'local-sage-rpc',
              'message': 'Local Sage offer bridge is available for router take/import operations only.',
          })

        if action == 'list':
            # Every offer the local Sage wallet holds. The responder-side
            # orphan check (api/sage-offers.js) compares the ACTIVE ones
            # against the Forge offer index: an active offer the index has
            # never seen was signed by the wallet but never reached a
            # responder -- it locks coins and can never settle on its own.
            result = sage.call('get_offers', {})
            offers = result.get('offers') if isinstance(result, dict) else result
            return emit({
                'success': True,
                'offers': offers if isinstance(offers, list) else [],
            })

        if action == 'create':
            return emit({
                'success': False,
                'error': '[aWizard] Sage create-offer action is disabled. Create offers through WalletConnect user session; use Sage bridge only for router take/import.',
            })

        if action == 'import':
            offer = payload.get('offer')
            if not isinstance(offer, str) or not offer.strip():
                return emit({
                    'success': False,
                    'error': '[aWizard] import action requires an offer string.',
                })

            result = sage.import_offer(offer)
            return emit({
                'success': True,
                'offer_id': result.get('offer_id'),
                'raw': result,
            })

        if action == 'cancel':
            offer = payload.get('offer')
            fee = payload.get('fee', 0)

            if not isinstance(offer, str) or not offer.strip():
                return emit({
                    'success': False,
                    'error': '[aWizard] cancel action requires an offer string.',
                })

            result = sage.cancel_offer(offer, fee)
            return emit({
                'success': True,
                'transaction_id': result.get('transaction_id') or result.get('tx_id'),
                'raw': result,
            })

        if action == 'take':
            offer = payload.get('offer')
            fee = payload.get('fee', 0)
            import_first = bool(payload.get('import_first', False))

            if not isinstance(offer, str) or not offer.strip():
                return emit({
                    'success': False,
                    'error': '[aWizard] take action requires an offer string.',
                })

            imported_offer_id = None
            if import_first:
                imported = sage.import_offer(offer)
                imported_offer_id = imported.get('offer_id')

            result = sage.take_offer(offer, fee)
            take_transaction_id = result.get('transaction_id') or result.get('tx_id')
            submit_result = None
            spend_bundle = result.get('spend_bundle')
            submit_error = None
            if not take_transaction_id and isinstance(spend_bundle, dict) and spend_bundle:
                try:
                    submit_result = sage.submit_transaction(spend_bundle)
                except Exception as exc:
                    submit_error = str(exc)

            transaction_id = None
            if isinstance(submit_result, dict):
                transaction_id = submit_result.get('transaction_id') or submit_result.get('tx_id')
            if not transaction_id:
                transaction_id = take_transaction_id

            return emit({
                'success': True,
                'offer_id': result.get('offer_id') or imported_offer_id,
                'transaction_id': transaction_id,
                'take_transaction_id': take_transaction_id,
                'summary': result.get('summary'),
                'spend_bundle': result.get('spend_bundle'),
                'submit_result': submit_result,
                'submit_error': submit_error,
                'raw': result,
            })

        return emit({
            'success': False,
            'error': f'[aWizard] Unsupported Sage offer bridge action: {action or "<missing>"}',
        })
    except Exception as exc:
        return emit({
            'success': False,
            'error': str(exc),
        })


if __name__ == '__main__':
    raise SystemExit(main())