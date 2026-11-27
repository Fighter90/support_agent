"""Онлайн-оплата через MCP-сервер ЮKassa — по документации yookassa.ru/developers/.../mcp-server
(«Подключение и настройка», «Инструменты», «Сценарии»).

- Сервер https://mcp.yookassa.ru, Streamable HTTP, JSON-RPC 2.0; заголовок Authorization: Bearer <токен MCP>.
  Токен выпускается в кабинете ЮKassa: Интеграция → Доступы к магазину → «Токены для MCP-сервера» (роль Владелец
  или Менеджер; срок до 3 лет; к API ЮKassa он не подходит). Блок появляется, только если в магазине выключена
  отправка чеков (Настройки → Онлайн-касса): через MCP данные для чека передать нельзя.
- Субаккаунт: необязательный заголовок Mcp-Param-Gateway-Id (переменная YOOKASSA_GATEWAY_ID) — так рекомендует документация.
- Инструменты: create_invoice / get_invoice — для счёта (корзина из позиций документа, срок до 30 дней; в тестовом
  магазине счета недоступны — тогда create_payment); create_payment / get_payment — для КП и как запасной путь.
  Возвраты через MCP не создаются (только get_refund) — их делает менеджер в кабинете ЮKassa.
- Сценарий «приём оплаты из публичного чата»: языковая модель НЕ получает доступ к инструментам MCP. Их вызывает
  бэкенд с суммой и позициями из документа, который рассчитал код по ценам фида; клиент может оплатить только свой
  документ, список платежей видит только менеджер.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import time
from typing import Any

import httpx

from app import db, quotes

logger = logging.getLogger(__name__)
URL = os.environ.get('YOOKASSA_MCP_URL', 'https://mcp.yookassa.ru')
_ids = itertools.count(1)

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
  id TEXT PRIMARY KEY, quote_id TEXT, conv_id TEXT, amount REAL, status TEXT, url TEXT, created_by TEXT,
  created_at REAL, updated_at REAL, kind TEXT DEFAULT 'payment'
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
        if os.environ.get('YOOKASSA_GATEWAY_ID'):
            headers['Mcp-Param-Gateway-Id'] = os.environ['YOOKASSA_GATEWAY_ID']
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

    def tools(self) -> list[str]:
        """tools/list: инструмент, которого нет в списке, через MCP-сервер недоступен (например, счета в тестовом магазине)."""
        self._init()
        msg = self._post({'jsonrpc': '2.0', 'id': next(_ids), 'method': 'tools/list', 'params': {}}) or {}
        return [t.get('name', '') for t in msg.get('result', {}).get('tools', [])]

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


_TOOLS: list[str] | None = None


def available_tools() -> list[str]:
    global _TOOLS
    if _TOOLS is None and mode() == 'mcp':
        try:
            _TOOLS = _client.tools()
        except (PaymentError, httpx.HTTPError) as e:
            logger.warning('ЮKassa MCP tools/list: %s', e)
            return []
    return _TOOLS or []


def init() -> None:
    with db.conn() as c:
        c.executescript(SCHEMA)
        try:
            c.execute("ALTER TABLE payments ADD COLUMN kind TEXT DEFAULT 'payment'")
        except Exception:  # noqa: BLE001 — колонка уже есть
            pass


def create_for_quote(qid: str, created_by: str = 'client') -> dict[str, Any]:
    q = quotes.get(qid)
    if not q or q['status'] == 'awaiting_requisites':
        raise PaymentError('Сначала нужно сформировать документ с реквизитами')
    existing = db.one("SELECT * FROM payments WHERE quote_id=? AND status IN ('pending','waiting_for_capture') ORDER BY created_at DESC", (qid,))
    if existing:
        return existing
    amount = {'value': f'{q["total"]:.2f}', 'currency': 'RUB'}
    descr = f'Оплата {q["number"]} — store.softline.ru'[:128]
    kind, res = 'payment', {}
    if mode() == 'mcp':
        if q.get('kind') == 'invoice' and 'create_invoice' in available_tools():
            # Сценарий «счёт из данных каталога»: корзина = позиции документа, срок оплаты 7 дней (максимум — 30)
            cart = [{'description': str(i['name'])[:128], 'price': {'value': f'{i["price"]:.2f}', 'currency': 'RUB'},
                     'quantity': i['quantity']} for i in q['items']]
            expires = time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(time.time() + 7 * 86400))
            try:
                res = _client.call('create_invoice', {'amount': amount, 'expires_at': expires, 'cart': cart, 'description': descr})
                kind, res['confirmation_url'] = 'invoice', res.get('url', '')
            except PaymentError as e:  # например, тестовый магазин — счёт через MCP недоступен
                logger.warning('create_invoice: %s — создаю платёж', e)
                res = {}
        if not res:
            res = _client.call('create_payment', {'amount': amount, 'description': descr})
    else:  # демо: тот же контракт ответа, что у create_payment MCP-сервера
        did = 'demo-' + db.new_id()
        from app.config import get_settings
        res = {'id': did, 'status': 'pending', 'confirmation_url': f'{get_settings().public_url}/pay/demo/{did}'}
    pid = res.get('id')
    if not pid:
        raise PaymentError('ЮKassa не вернула идентификатор платежа')
    if not res.get('confirmation_url'):
        raise PaymentError('ЮKassa не вернула ссылку на оплату')
    db.execute('INSERT INTO payments (id, quote_id, conv_id, amount, status, url, created_by, created_at, updated_at, kind) VALUES (?,?,?,?,?,?,?,?,?,?)',
               (pid, qid, q['conv_id'], q['total'], res.get('status', 'pending'), res['confirmation_url'], created_by, db.now(), db.now(), kind))
    return db.one('SELECT * FROM payments WHERE id=?', (pid,))


def refresh(pid: str) -> dict[str, Any] | None:
    p = db.one('SELECT * FROM payments WHERE id=?', (pid,))
    if p and mode() == 'mcp' and not pid.startswith('demo-') and p['status'] not in ('succeeded', 'canceled'):
        try:
            if p.get('kind') == 'invoice':  # статус оплаты счёта — в payment_details после успешной оплаты
                inv = _client.call('get_invoice', {'id': pid})
                res = {'status': 'succeeded' if (inv.get('payment_details') or {}).get('status') == 'succeeded' or inv.get('status') == 'succeeded'
                       else 'canceled' if inv.get('status') in ('canceled', 'expired') else p['status']}
            else:
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
