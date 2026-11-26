"""Онлайн-оплата через MCP-сервер ЮKassa (https://mcp.yookassa.ru, Streamable HTTP, JSON-RPC 2.0, Bearer-токен MCP).

Инструменты MCP-сервера вызывает бэкенд, а не языковая модель: сумма и описание берутся из КП/счёта, который
рассчитал код по ценам фида, — сервер ЮKassa сам суммы не проверяет (раздел «Ограничения» документации).
Клиент может оплатить только свой документ; список платежей видит только менеджер. Используются create_payment
и get_payment (одностадийный платёж, ссылка confirmation_url); возвраты MCP не создаёт — только в кабинете ЮKassa.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
from typing import Any

import httpx

from app import db, quotes

logger = logging.getLogger(__name__)
URL = os.environ.get('YOOKASSA_MCP_URL', 'https://mcp.yookassa.ru')
_ids = itertools.count(1)

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
  id TEXT PRIMARY KEY, quote_id TEXT, conv_id TEXT, amount REAL, status TEXT, url TEXT, created_by TEXT,
  created_at REAL, updated_at REAL
);
"""


class PaymentError(Exception):
    pass


def token() -> str:
    return os.environ.get('YOOKASSA_MCP_TOKEN', '')


def mode() -> str:
    """mcp — реальный MCP-сервер ЮKassa по токену; demo — демонстрационный режим без токена (страница /pay/demo/...)."""
    return 'mcp' if token() else 'demo'


def enabled() -> bool:
    return True  # в демо-режиме оплата показывается на демонстрационной странице с явной пометкой


class MCPClient:
    """Минимальный клиент MCP (Streamable HTTP): initialize → notifications/initialized → tools/call."""

    def __init__(self) -> None:
        self.session = ''
        self.http = httpx.Client(timeout=30)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        headers = {'Authorization': f'Bearer {token()}', 'Content-Type': 'application/json',
                   'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-06-18'}
        if self.session:
            headers['Mcp-Session-Id'] = self.session
        r = self.http.post(URL, json=payload, headers=headers)
        if r.status_code >= 400:
            raise PaymentError(f'ЮKassa MCP: HTTP {r.status_code} {r.text[:200]}')
        self.session = r.headers.get('mcp-session-id', self.session)
        if 'id' not in payload or not r.content:
            return None
        if r.headers.get('content-type', '').startswith('text/event-stream'):
            for line in r.text.splitlines():
                if line.startswith('data:'):
                    msg = json.loads(line[5:].strip())
                    if msg.get('id') == payload['id']:
                        return msg
            raise PaymentError('ЮKassa MCP: пустой поток ответа')
        return r.json()

    def _init(self) -> None:
        if self.session:
            return
        self._post({'jsonrpc': '2.0', 'id': next(_ids), 'method': 'initialize', 'params': {
            'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'softline-support-agent', 'version': '1.0'}}})
        self._post({'jsonrpc': '2.0', 'method': 'notifications/initialized'})

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._init()
        msg = self._post({'jsonrpc': '2.0', 'id': next(_ids), 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}})
        if not msg or 'error' in msg:
            self.session = ''
            raise PaymentError(f'ЮKassa MCP: {(msg or {}).get("error")}')
        res = msg.get('result', {})
        if res.get('isError'):
            raise PaymentError('ЮKassa: ' + ' '.join(c.get('text', '') for c in res.get('content', []))[:300])
        if res.get('structuredContent'):
            return res['structuredContent']
        for c in res.get('content', []):
            if c.get('type') == 'text':
                try:
                    return json.loads(c['text'])
                except ValueError:
                    continue
        return {}


_client = MCPClient()


def init() -> None:
    with db.conn() as c:
        c.executescript(SCHEMA)


def create_for_quote(qid: str, created_by: str = 'client') -> dict[str, Any]:
    q = quotes.get(qid)
    if not q or q['status'] == 'awaiting_requisites':
        raise PaymentError('Сначала нужно сформировать документ с реквизитами')
    existing = db.one("SELECT * FROM payments WHERE quote_id=? AND status IN ('pending','waiting_for_capture') ORDER BY created_at DESC", (qid,))
    if existing:
        return existing
    args = {'amount': {'value': f'{q["total"]:.2f}', 'currency': 'RUB'}, 'description': f'Оплата {q["number"]} — store.softline.ru'[:128]}
    if mode() == 'mcp':
        res = _client.call('create_payment', args)
    else:  # демо: тот же контракт ответа, что у create_payment MCP-сервера
        did = 'demo-' + db.new_id()
        from app.config import get_settings
        res = {'id': did, 'status': 'pending', 'confirmation_url': f'{get_settings().public_url}/pay/demo/{did}'}
    pid = res.get('id')
    if not pid:
        raise PaymentError('ЮKassa не вернула идентификатор платежа')
    db.execute('INSERT INTO payments (id, quote_id, conv_id, amount, status, url, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
               (pid, qid, q['conv_id'], q['total'], res.get('status', 'pending'), res.get('confirmation_url', ''), created_by, db.now(), db.now()))
    return db.one('SELECT * FROM payments WHERE id=?', (pid,))


def refresh(pid: str) -> dict[str, Any] | None:
    p = db.one('SELECT * FROM payments WHERE id=?', (pid,))
    if p and mode() == 'mcp' and not pid.startswith('demo-') and p['status'] not in ('succeeded', 'canceled'):
        try:
            res = _client.call('get_payment', {'id': pid})
            if res.get('status') and res['status'] != p['status']:
                db.execute('UPDATE payments SET status=?, updated_at=? WHERE id=?', (res['status'], db.now(), pid))
                if res['status'] == 'succeeded':
                    db.execute('INSERT INTO messages (conv_id, role, content, meta, created_at) VALUES (?,?,?,?,?)',
                               (p['conv_id'], 'system', f'Оплата {p["amount"]:,.2f} ₽ получена. Спасибо! Лицензия придёт на e-mail по сроку вендора.'.replace(',', ' '),
                                '{}', db.now()))
                p = db.one('SELECT * FROM payments WHERE id=?', (pid,))
        except PaymentError as e:
            logger.warning('Статус платежа %s: %s', pid, e)
    return p


def for_quote(qid: str) -> dict[str, Any] | None:
    p = db.one('SELECT * FROM payments WHERE quote_id=? ORDER BY created_at DESC', (qid,))
    return refresh(p['id']) if p else None


def all_payments() -> list[dict[str, Any]]:
    rows = db.rows('SELECT p.*, q.number FROM payments p LEFT JOIN quotes q ON q.id = p.quote_id ORDER BY p.created_at DESC LIMIT 200')
    return [refresh(r['id']) | {'number': r['number']} if r['status'] not in ('succeeded', 'canceled') else r for r in rows]


def demo_confirm(pid: str, success: bool = True) -> dict[str, Any]:
    """Демо-страница оплаты: имитирует подтверждение платежа покупателем (только для платежей demo-*)."""
    p = db.one('SELECT * FROM payments WHERE id=?', (pid,))
    if not p or not pid.startswith('demo-'):
        raise PaymentError('Демо-платёж не найден')
    if p['status'] == 'pending':
        status = 'succeeded' if success else 'canceled'
        db.execute('UPDATE payments SET status=?, updated_at=? WHERE id=?', (status, db.now(), pid))
        text = (f'Оплата {p["amount"]:,.2f} ₽ получена (демо-режим ЮKassa). Спасибо! Лицензия придёт на e-mail по сроку вендора.'
                if success else 'Оплата отменена. Можно оплатить позже по ссылке или по счёту.').replace(',', ' ')
        db.execute('INSERT INTO messages (conv_id, role, content, meta, created_at) VALUES (?,?,?,?,?)', (p['conv_id'], 'system', text, '{}', db.now()))
    return db.one('SELECT p.*, q.number FROM payments p LEFT JOIN quotes q ON q.id=p.quote_id WHERE p.id=?', (pid,))
