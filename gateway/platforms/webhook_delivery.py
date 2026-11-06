"""Validated webhook destinations live in the owner's admitted input, not a TTL cache."""
from copy import deepcopy
from contextvars import ContextVar
import hashlib
import json

reply_context: ContextVar[dict | None] = ContextVar('webhook_reply_context', default=None)

from hermes_state_runtime import RuntimeStoreError


def route_digest(adapter, chat_id):
    from gateway.platforms.webhook import _WebhookDeliveryIdentity
    adapter._reload_dynamic_routes()
    try:
        route_name = _WebhookDeliveryIdentity.route_from_chat_id(chat_id)
    except (ValueError, TypeError, IndexError):
        raise RuntimeStoreError('not_found') from None
    route = adapter._routes.get(route_name)
    if route is None:
        raise RuntimeStoreError('not_found')
    return hashlib.sha256(json.dumps(route, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_destination(delivery):
    from gateway.platforms.webhook import _is_known_platform, _REPO_RE
    if (not isinstance(delivery, dict)
            or not {'deliver', 'deliver_extra'} <= set(delivery)
            or set(delivery) - {'deliver', 'deliver_extra', 'profile', 'mirror', 'route', 'delivery_mode', 'delivery_target', 'route_name', 'delivery_id', 'worker_route'}
            or (delivery.get('profile') is not None and not isinstance(delivery['profile'], str))
            or not isinstance(delivery.get('mirror', False), bool)
            or (delivery.get('route') is not None and not isinstance(delivery['route'], str))):
        raise RuntimeStoreError('invalid_params')
    target, extra = delivery['deliver'], delivery['deliver_extra']
    if not isinstance(target, str) or not isinstance(extra, dict):
        raise RuntimeStoreError('invalid_params')
    if target == 'github_comment':
        repo, number = extra.get('repo'), extra.get('pr_number')
        if not isinstance(repo, str) or not _REPO_RE.fullmatch(repo) or not str(number).isdigit() or int(number) <= 0:
            raise RuntimeStoreError('invalid_params')
    elif target != 'log' and not _is_known_platform(target):
        raise RuntimeStoreError('invalid_params')
    # Configuration extras may contain credentials or whole rendered payloads.
    # Persist only fields the selected sender actually consumes.
    fields = {'log': (), 'github_comment': ('repo', 'pr_number')}.get(
        target, ('chat_id', 'message_thread_id', 'thread_id'))
    result = {'deliver': target, 'deliver_extra': deepcopy({key: extra[key] for key in fields if key in extra})}
    # Omit the unbound defaults to preserve pre-profile receipt fingerprints.
    if delivery.get('profile'):
        result['profile'] = delivery['profile']
    # Route opt-in (``mirror_to_session: true``): the delivered text is mirrored into the target chat's
    # session; the destination row carries it so a replayed delivery mirrors exactly like the first.
    if delivery.get('mirror') is True:
        result['mirror'] = True
        if delivery.get('route'):
            result['route'] = delivery['route']  # the mirror's label only
    from gateway.platforms.webhook_session_targets import route_delivery_target
    target = route_delivery_target(delivery)
    if target is not None:
        result['delivery_target'] = target
    if delivery.get('delivery_mode') == 'internal_turn':
        if target is None or target['kind'] != 'session':
            raise RuntimeStoreError('invalid_params')
        result['delivery_mode'] = 'internal_turn'
        for key in ('user_id', 'chat_type'):
            if extra.get(key) is not None:
                if not isinstance(extra[key], str):
                    raise RuntimeStoreError('invalid_params')
                result['deliver_extra'][key] = extra[key]
        for key in ('route_name', 'delivery_id'):
            if key in delivery:
                if not isinstance(delivery[key], str):
                    raise RuntimeStoreError('invalid_params')
                result[key] = delivery[key]
    if delivery.get('worker_route') is not None:
        from agent.session_routes import validate_route
        result['worker_route'] = validate_route(delivery['worker_route'])
    return result


def snapshot_destination(adapter, delivery):
    from gateway.config import Platform

    result = validate_destination(delivery)
    route_name = delivery.get('route')
    if route_name in adapter._routes:
        from gateway.platforms.webhook_route_runtime import configured_route, declared_route_for
        route = declared_route_for(configured_route(adapter._routes[route_name], adapter.config.extra))
        if route is not None:
            result['worker_route'] = route  # captured in the admission's owning profile scope, before commit
    if result['deliver'] not in {'log', 'github_comment'} and not result['deliver_extra'].get('chat_id'):
        profile = result.get('profile')
        with adapter._profile_scope(profile):
            home = adapter._delivery_config(profile).get_home_channel(Platform(result['deliver']))
        if home is None:
            raise RuntimeStoreError('not_found')
        result['deliver_extra']['chat_id'] = home.chat_id
    return result


def _signed_rows(adapter, chat_id):
    """``(runner, rows)``: the signed ingress admissions of *chat_id* that can own its reply leg.
    Only the signed ingress admission carries the destination; later automation rows (process/watch
    completions) on the same chat share its source but never compete. A keyed chat
    (``webhook_session_keys``) holds one signed admission per delivery and its session FIFO runs them
    one at a time: the reply leg belongs to the one executing now, else the newest."""
    runner = adapter.gateway_runner or getattr(adapter._message_handler, '__self__', None)
    from gateway.session_authorities import active_authority, all_authorities, serving_authorities
    if not all_authorities(runner):
        return runner, None
    # The reply leg runs in the routed profile's scope; that ledger holds the destination. A
    # delivery id is unique per route, so an unscoped caller may search every served ledger (a
    # parked profile's retained store is not served: reading it could fail every other profile's).
    scoped = active_authority(runner)
    candidates = [scoped] if scoped is not None else serving_authorities(runner)
    query = """SELECT payload_json, status, seq, target_session_id FROM session_admissions
        WHERE json_extract(payload_json, '$.native_text_v1.source.chat_id')=?
          AND json_extract(payload_json, '$.native_text_v1.source.platform')='webhook'
          AND json_extract(payload_json, '$.native_text_v1.automation') IS NULL"""
    rows = [(authority, row) for authority in candidates for row in authority.db._read_all(query, (chat_id,))]
    if context := reply_context.get():
        delivery_id = context.get('_webhook_delivery_id')
        obligation = context.get('_webhook_obligation_id')
        if delivery_id or obligation:
            def matches(pair):
                native = json.loads(pair[1]['payload_json'])['native_text_v1']
                message_id = native['event'].get('message_id')
                if delivery_id:
                    return message_id == delivery_id
                from gateway.delivery_ledger import compute_obligation_id
                return compute_obligation_id(native['route'], str(message_id or ''), context['content']) == obligation
            return runner, [pair for pair in rows if matches(pair)]
    from gateway.platforms.webhook_session_keys import is_keyed_chat_id
    if len(rows) > 1 and is_keyed_chat_id(chat_id):
        started = [pair for pair in rows if pair[1]['status'] == 'started']
        rows = started if len(started) == 1 else [max(rows, key=lambda pair: pair[1]['seq'])]
    return runner, rows


def retained_destination(adapter, chat_id):
    """Readback also checks today's connector and authorization; retained data is not a grant."""
    from gateway.session_envelope import restore_native, _validate_native
    runner, rows = _signed_rows(adapter, chat_id)
    if rows is None:
        return validate_destination(adapter._delivery_info.get(chat_id))
    if len(rows) != 1:
        raise RuntimeStoreError('not_found')
    payload = json.loads(rows[0][1]['payload_json'])
    envelope = payload['native_text_v1']
    event = restore_native(payload, runner)
    _validate_native(runner, event, envelope.get('provenance'))
    if runner._adapter_for_source(event.source) is not adapter:
        raise RuntimeStoreError('permission_denied')
    if envelope.get('webhook_route') != route_digest(adapter, chat_id):
        raise RuntimeStoreError('admission_conflict')
    return validate_destination(envelope.get('webhook_delivery'))


def retained_session_target(adapter, chat_id):
    """``{id, delivery_target, db, admission_seq}`` of the session *chat_id*'s reply leg runs in (its
    store and the reply leg's admission, for the dispatcher), or None (no ledger, no admission, or no
    target declared)."""
    _runner, rows = _signed_rows(adapter, chat_id)
    delivery = retained_destination(adapter, chat_id)
    declared = delivery.get('delivery_target')
    if not rows or len(rows) != 1:
        if declared is not None or delivery.get('delivery_mode') == 'internal_turn':
            raise RuntimeStoreError('not_found')
        return None
    authority, row = rows[0]
    sid = authority.logical_owner(row['target_session_id'])
    raw = authority.db._read_one('SELECT delivery_target FROM sessions WHERE id=?', (sid,))
    from gateway.delivery_targets import parse_delivery_target
    target = parse_delivery_target(raw['delivery_target']) if raw and raw['delivery_target'] is not None else None
    if declared is not None:
        if target is None:
            raise RuntimeStoreError('admission_conflict')  # stamp failed or has not landed yet: never raw egress
        target = declared  # the executing delivery owns its rendered destination, not the newest request
    if target is None:
        from gateway.platforms.webhook import _WebhookDeliveryIdentity
        config = adapter._routes.get(_WebhookDeliveryIdentity.route_from_chat_id(chat_id), {})
        if config.get('delivery_target') is not None or config.get('delivery_mode') == 'internal_turn':
            raise RuntimeStoreError('admission_conflict')
        return None
    from gateway.platforms.webhook_session_targets import route_delivery_target
    target = route_delivery_target({'delivery_target': target})
    return {'id': sid, 'delivery_target': target, 'db': authority.db, 'admission_seq': row['seq']}
