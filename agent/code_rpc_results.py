"""Bound every terminal RPC record, including namespace aggregates and errors."""
import json
import logging

from agent.code_artifacts import INLINE_BYTES, prepare_artifact, store_json


logger = logging.getLogger(__name__)

def bounded_result(value, authority):
    # Leave room for the acceptance receipt and outer result/error framing.
    if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) <= INLINE_BYTES - 4096:
        return value
    handle = store_json(value, authority.parent_context.agent.session_id)
    prepared = prepare_artifact(handle, context=authority.parent_context, authority=authority)
    return {'_hermes_rpc_artifact': prepared}


def bounded_error(error, authority):
    handle = None
    try:
        if len(json.dumps(error, ensure_ascii=False).encode('utf-8')) <= INLINE_BYTES - 4096:
            return error
        handle = store_json(error, authority.parent_context.agent.session_id)
        prepared = prepare_artifact(handle, context=authority.parent_context, authority=authority)
    except Exception as exc:
        logger.exception("Could not deliver full RPC error evidence")
        # Never recurse through delivery of another large exception. Keep the
        # original type/session/receipt plus the host reference if storage worked.
        return {**_error_identity(error),
                'message': str(error.get('message', 'RPC failed'))[:4096],
                'partial': {'artifact': handle},
                'delivery_error': {'code': getattr(exc, 'code', 'result_delivery_unsupported'),
                                   'message': str(exc)[:4096], 'retry_safe': False, 'artifact': handle}}
    return {'version': 2, 'status': 'error', **_error_identity(error),
            'message': 'Read complete error evidence from the attached artifact.',
            'receipt': error.get('receipt'), '_hermes_rpc_error_artifact': prepared}


def _error_identity(error):
    identity = {key: error[key] for key in ('version', 'status', 'receipt') if key in error}
    for key in ('type', 'code', 'session_id'):
        value = error.get(key)
        identity[key] = value[:1024] if isinstance(value, str) else None
    return identity
