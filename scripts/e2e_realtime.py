"""Сквозной регресс-тест реалтайма и кабинета менеджера на работающем стенде (без модели: агент не вызывается).

Запуск внутри контейнера: docker exec support-agent python scripts/e2e_realtime.py [http://127.0.0.1:8000]
Проверяет: вход менеджера (JWT), WebSocket клиента и менеджера, кнопку «Позвать оператора» → событие эскалации,
перехват диалога, сообщения клиент ⇄ менеджер в реальном времени (с замером задержки), возврат агенту,
КП → реквизиты → PDF, ссылку на оплату (MCP ЮKassa или демо) → подтверждение → системное сообщение, опрос и 👍/👎.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import httpx
import websockets

BASE = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:8000'
WS = BASE.replace('http', 'ws', 1)
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, info: str = '') -> None:
    results.append((name, ok, info))
    print(('✅' if ok else '❌'), name, info, flush=True)


async def wait_for(ws, pred, timeout: float = 5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            d = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout - (time.time() - t0)))
        except (asyncio.TimeoutError, TimeoutError):
            break
        if pred(d):
            return d, time.time() - t0
    return None, time.time() - t0


async def main() -> None:
    c = httpx.AsyncClient(base_url=BASE, timeout=30)
    r = await c.post('/api/auth/login', json={'login': os.environ.get('MANAGER_LOGIN', 'manager'), 'password': os.environ.get('MANAGER_PASSWORD', '')})
    check('Вход менеджера (JWT)', r.status_code == 200, f'HTTP {r.status_code}')
    tok = r.json().get('token', '')
    H = {'Authorization': f'Bearer {tok}'}
    bad = await c.post('/api/auth/login', json={'login': 'manager', 'password': 'wrong'})
    check('Неверный пароль отклоняется', bad.status_code == 401)
    check('Кабинет без токена закрыт', (await c.get('/api/manager/conversations')).status_code == 401)

    cid = (await c.post('/api/chat/start', json={})).json()['conversation_id']
    async with websockets.connect(f'{WS}/ws/chat/{cid}') as cws, websockets.connect(f'{WS}/ws/manager?token={tok}') as mws:
        await wait_for(mws, lambda d: d.get('type') == 'presence', 2)
        r = await c.post(f'/api/chat/{cid}/operator')
        check('Кнопка «Позвать оператора»', r.json().get('status') == 'escalated')
        ev, dt = await wait_for(mws, lambda d: d.get('type') == 'escalation' and d.get('conv_id') == cid)
        check('Менеджер получил эскалацию по WebSocket', ev is not None, f'{dt:.2f} с')

        await c.post(f'/api/manager/conversations/{cid}/takeover', json={'on': True}, headers=H)
        ev, dt = await wait_for(cws, lambda d: d.get('type') == 'message' and d['message']['role'] == 'system')
        check('Клиент видит «к диалогу подключился менеджер»', ev is not None and 'подключился' in ev['message']['content'], f'{dt:.2f} с')

        r = await c.post(f'/api/chat/{cid}/message', json={'text': 'Здравствуйте, нужна помощь с тендером, мой телефон +7 916 111-22-33'})
        check('В режиме менеджера агент молчит', r.json().get('queued') is True)
        ev, dt = await wait_for(mws, lambda d: d.get('type') == 'message' and d.get('conv_id') == cid and d['message']['role'] == 'user')
        check('Сообщение клиента у менеджера в реальном времени', ev is not None, f'{dt:.2f} с')
        stored = (await c.get(f'/api/manager/conversations/{cid}', headers=H)).json()
        check('ПДн видны менеджеру, в базе — метки', ev is not None and '916' in ev['message']['content'] and bool(stored['pii_masked']))

        await mws.send(json.dumps({'type': 'typing', 'conv_id': cid}))
        ev, dt = await wait_for(cws, lambda d: d.get('type') == 'typing')
        check('Клиент видит «менеджер печатает…»', ev is not None, f'{dt:.2f} с')

        await c.post(f'/api/manager/conversations/{cid}/message', json={'text': 'Добрый день! Пришлите, пожалуйста, ТЗ закупки.'}, headers=H)
        ev, dt = await wait_for(cws, lambda d: d.get('type') == 'message' and d['message']['role'] == 'manager')
        check('Ответ менеджера у клиента в реальном времени', ev is not None, f'{dt:.2f} с')

        await c.post(f'/api/manager/conversations/{cid}/takeover', json={'on': False}, headers=H)
        conv = (await c.get(f'/api/manager/conversations/{cid}', headers=H)).json()
        check('Диалог возвращён агенту', conv.get('mode') == 'agent')

    # КП → реквизиты → PDF → оплата (MCP ЮKassa или демо-режим)
    from app import quotes  # прямой вызов: позиция и цена — из каталога, как при вызове инструмента агентом
    offer = httpx.get(f'{BASE}/api/health').json()
    import sqlite3
    con = sqlite3.connect('/app/data/catalog.db')
    oid = con.execute('SELECT id FROM offers WHERE available=1 AND price>0 LIMIT 1').fetchone()[0]
    q = quotes.create(cid, [{'offer_id': oid, 'quantity': 2}], 5, 'invoice')
    r = await c.post(f'/api/quotes/{q["quote_id"]}/requisites', json={'company': 'ООО Тест', 'inn': '7701234567', 'kpp': '770101001'})
    check('Счёт с реквизитами сформирован', r.status_code == 200)
    r = await c.get(f'/api/quotes/{q["quote_id"]}/pdf')
    check('PDF счёта', r.status_code == 200 and r.headers['content-type'] == 'application/pdf', f'{len(r.content)} байт')
    r = await c.post(f'/api/quotes/{q["quote_id"]}/pay')
    pay = r.json()
    check(f'Ссылка на оплату ({offer.get("payments_mode")})', r.status_code == 200 and bool(pay.get('url')), pay.get('url', r.text)[:80])
    if pay.get('id', '').startswith('demo-'):
        r = await c.post(f'/api/payments/demo/{pay["id"]}/pay')
        check('Демо-оплата подтверждена', r.json().get('status') == 'succeeded')
    pays = (await c.get('/api/manager/payments', headers=H)).json()
    check('Платёж виден в кабинете', any(p['quote_id'] == q['quote_id'] for p in pays['items']))

    # Опрос и обратная связь
    msgs = (await c.get(f'/api/chat/{cid}')).json()['messages']
    aid = next(m['id'] for m in msgs if m['role'] == 'assistant')
    check('👎 принят', (await c.post(f'/api/chat/{cid}/feedback', json={'message_id': aid, 'rating': -1})).status_code == 200)
    fb = (await c.get('/api/manager/feedback', headers=H)).json()
    check('👎 в разделе «Отзывы»', any(n['message_id'] == aid for n in fb['negatives']))
    check('Опрос в конце диалога', (await c.post(f'/api/chat/{cid}/survey', json={'resolved': True, 'score': 5, 'comment': 'e2e'})).status_code == 200)
    m = (await c.get('/api/manager/metrics', headers=H)).json()
    check('Метрики с CSAT', m['learning']['surveys'] >= 1)
    check('/.env закрыт', (await c.get('/.env')).status_code == 404)
    ok = sum(1 for _, o, _ in results if o)
    print(f'\nИтог: {ok}/{len(results)} проверок пройдено')
    sys.exit(0 if ok == len(results) else 1)


if __name__ == '__main__':
    asyncio.run(main())
