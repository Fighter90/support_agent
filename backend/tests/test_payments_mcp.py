"""Клиент MCP ЮKassa против имитации сервера по документации: Bearer, Streamable HTTP (SSE), tools/list,
create_invoice для счёта, create_payment как запасной путь, get_invoice для статуса."""

import json
import os
import tempfile

import httpx

os.environ.setdefault('DATA_DIR', tempfile.mkdtemp())

from app import db, payments  # noqa: E402

CALLS: list[tuple[str, dict]] = []


def handler(request: httpx.Request) -> httpx.Response:
    assert request.headers['Authorization'] == 'Bearer test-mcp-token'
    assert 'text/event-stream' in request.headers['Accept']
    msg = json.loads(request.content)
    method = msg.get('method')
    if 'id' not in msg:
        return httpx.Response(202)
    if method == 'initialize':
        result = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'yookassa'}}
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': msg['id'], 'result': result}, headers={'Mcp-Session-Id': 's1'})
    assert request.headers.get('Mcp-Session-Id') == 's1'
    if method == 'tools/list':
        tools = [{'name': n} for n in ('create_payment', 'get_payment', 'create_invoice', 'get_invoice')]
        result = {'tools': tools}
    else:
        name, args = msg['params']['name'], msg['params']['arguments']
        CALLS.append((name, args))
        data = {'create_invoice': {'id': 'inv-1', 'status': 'pending', 'amount': args.get('amount'), 'url': 'https://yookassa.ru/my/i/inv-1'},
                'get_invoice': {'id': 'inv-1', 'status': 'succeeded', 'payment_details': {'id': 'p-9', 'status': 'succeeded'}},
                'create_payment': {'id': 'p-1', 'status': 'pending', 'amount': args.get('amount'), 'confirmation_url': 'https://yoomoney.ru/c/p-1'}}[name]
        result = {'content': [{'type': 'text', 'text': json.dumps(data)}], 'structuredContent': data}
    body = 'event: message\ndata: ' + json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}) + '\n\n'
    return httpx.Response(200, text=body, headers={'Content-Type': 'text/event-stream'})


def _quote(kind: str) -> str:
    qid = db.new_id('q')
    items = [{'name': 'Kaspersky Endpoint Security Стандартный, 1 год', 'price': 2579.54, 'quantity': 50, 'total': 128977.0}]
    db.execute('INSERT INTO quotes (id, number, conv_id, items, total, vat, status, created_at, updated_at, kind, discount) '
               'VALUES (?,?,?,?,?,?,?,?,?,?,?)', (qid, 'СЧ-T', 'c1', db.dumps(items), 128977.0, 0, 'draft', db.now(), db.now(), kind, 5))
    return qid


def test_invoice_via_create_invoice_and_status(monkeypatch):
    monkeypatch.setenv('YOOKASSA_MCP_TOKEN', 'test-mcp-token')
    db.init_db()
    payments.init()
    payments._client = payments.MCPClient()
    payments._client.http = httpx.Client(transport=httpx.MockTransport(handler))
    payments._TOOLS = None
    p = payments.create_for_quote(_quote('invoice'))
    assert p['kind'] == 'invoice' and p['url'] == 'https://yookassa.ru/my/i/inv-1'
    name, args = CALLS[-1]
    assert name == 'create_invoice' and args['amount'] == {'value': '128977.00', 'currency': 'RUB'}
    assert args['cart'][0]['quantity'] == 50 and args['expires_at'].endswith('Z')
    assert payments.refresh(p['id'])['status'] == 'succeeded'


def test_quote_via_create_payment(monkeypatch):
    monkeypatch.setenv('YOOKASSA_MCP_TOKEN', 'test-mcp-token')
    p = payments.create_for_quote(_quote('quote'))
    assert p['kind'] == 'payment' and p['url'].startswith('https://yoomoney.ru/')
    assert CALLS[-1][0] == 'create_payment' and len(CALLS[-1][1]['description']) <= 128
