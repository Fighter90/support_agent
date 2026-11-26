"""Реалтайм по WebSocket: клиентский чат и кабинет менеджера.

Один фоновый «вещатель» раз в 0,7 с берёт из БД новые сообщения и эскалации и рассылает их:
клиенту — сообщения его диалога (без служебных данных), менеджерам — все события. Так реалтайм не зависит от того,
в каком потоке создано сообщение (агент работает в пуле потоков). Индикатор «печатает…» передаётся напрямую.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from typing import Any

from fastapi import WebSocket

from app import agent, db, pii

logger = logging.getLogger(__name__)

clients: dict[str, set[WebSocket]] = defaultdict(set)
managers: set[WebSocket] = set()
_state = {'msg': 0, 'esc': 0}


async def _send(ws: WebSocket, data: dict[str, Any]) -> bool:
    try:
        await ws.send_json(data)
        return True
    except Exception:  # noqa: BLE001 — сокет закрыт
        return False


async def to_managers(data: dict[str, Any]) -> None:
    dead = [ws for ws in list(managers) if not await _send(ws, data)]
    for ws in dead:
        managers.discard(ws)


async def to_client(cid: str, data: dict[str, Any]) -> None:
    dead = [ws for ws in list(clients.get(cid, ())) if not await _send(ws, data)]
    for ws in dead:
        clients[cid].discard(ws)


def _message(m: dict[str, Any], internal: bool) -> dict[str, Any]:
    meta = db.loads(m['meta'], {})
    return {'id': m['id'], 'role': m['role'], 'content': pii.unmask(m['conv_id'], m['content']), 'created_at': m['created_at'],
            'meta': meta if internal else agent.public_meta(meta)}


async def broadcaster() -> None:
    _state['msg'] = (db.one('SELECT max(id) AS v FROM messages') or {}).get('v') or 0
    _state['esc'] = (db.one('SELECT max(id) AS v FROM escalations') or {}).get('v') or 0
    while True:
        await asyncio.sleep(0.7)
        try:
            for m in db.rows('SELECT * FROM messages WHERE id>? ORDER BY id LIMIT 200', (_state['msg'],)):
                _state['msg'] = m['id']
                await to_client(m['conv_id'], {'type': 'message', 'message': _message(m, False)})
                await to_managers({'type': 'message', 'conv_id': m['conv_id'], 'message': _message(m, True)})
            for e in db.rows('SELECT id, conv_id, category, summary FROM escalations WHERE id>? ORDER BY id', (_state['esc'],)):
                _state['esc'] = e['id']
                await to_managers({'type': 'escalation', 'conv_id': e['conv_id'], 'id': e['id'], 'category': e['category'],
                                   'summary': pii.unmask(e['conv_id'], e['summary'])})
        except Exception:  # noqa: BLE001 — вещатель не должен падать
            logger.exception('Ошибка рассылки событий')


async def client_socket(ws: WebSocket, cid: str) -> None:
    await ws.accept()
    clients[cid].add(ws)
    try:
        while True:
            data = await ws.receive_json()
            if data.get('type') == 'typing':
                await to_managers({'type': 'typing', 'conv_id': cid, 'who': 'client'})
    except Exception:  # noqa: BLE001 — отключение клиента
        pass
    finally:
        clients[cid].discard(ws)


async def manager_socket(ws: WebSocket) -> None:
    await ws.accept()
    managers.add(ws)
    await to_managers({'type': 'presence', 'managers_online': len(managers)})
    try:
        while True:
            data = await ws.receive_json()
            if data.get('type') == 'typing' and data.get('conv_id'):
                await to_client(data['conv_id'], {'type': 'typing', 'who': 'manager'})
    except Exception:  # noqa: BLE001
        pass
    finally:
        managers.discard(ws)
        with contextlib.suppress(Exception):
            await to_managers({'type': 'presence', 'managers_online': len(managers)})
