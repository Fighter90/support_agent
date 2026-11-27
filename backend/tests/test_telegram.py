"""Telegram-бот менеджера на имитации Bot API: привязка по коду, уведомление об эскалации, ответ реплаем, кнопки."""

import json
import os
import tempfile

import httpx

os.environ.setdefault('DATA_DIR', tempfile.mkdtemp())

from app import db, telegram  # noqa: E402

SENT: list[tuple[str, dict]] = []


def handler(request: httpx.Request) -> httpx.Response:
    method = request.url.path.rsplit('/', 1)[-1]
    params = json.loads(request.content or b'{}')
    SENT.append((method, params))
    return httpx.Response(200, json={'ok': True, 'result': {'message_id': 100 + len(SENT)}})


def test_bot_flow(monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', '123:test')
    db.init_db()
    telegram.init()
    telegram.bot = telegram.Bot(httpx.Client(transport=httpx.MockTransport(handler)))
    cid = db.new_id('c')
    db.execute('INSERT INTO conversations (id, created_at, updated_at) VALUES (?,?,?)', (cid, db.now(), db.now()))
    db.execute('INSERT INTO messages (conv_id, role, content, meta, created_at) VALUES (?,?,?,?,?)',
               (cid, 'user', 'Нужна скидка 15%, тел [ТЕЛ_1]', '{}', db.now()))

    # посторонний не может подключиться без кода
    telegram.handle({'message': {'chat': {'id': 777}, 'from': {'username': 'x'}, 'text': '/start'}})
    assert telegram.chats() == []
    link = telegram.link_code('Менеджер Softline')
    assert link['url'].endswith(link['code'])
    telegram.handle({'message': {'chat': {'id': 555}, 'from': {'username': 'mgr', 'first_name': 'Сергей'}, 'text': f'/start {link["code"]}'}})
    assert [c['chat_id'] for c in telegram.chats()] == [555]

    telegram.notify_new()  # первый запуск — только запоминает позицию
    db.execute('INSERT INTO escalations (conv_id, reason, category, summary, created_at) VALUES (?,?,?,?,?)',
               (cid, 'special_terms', 'Скидка / спецусловия', 'Клиент просит скидку 15%', db.now()))
    assert telegram.notify_new() == 1
    assert telegram.flush() == 1  # уведомление уходит через очередь
    method, p = SENT[-1]
    assert method == 'sendMessage' and p['chat_id'] == 555 and 'Скидка / спецусловия' in p['text']
    assert '[ТЕЛ_1]' in p['text']  # в Telegram уходит только обезличенный текст
    assert p['reply_markup']['inline_keyboard'][0][0]['callback_data'] == f'take:{cid}'
    notif_id = 100 + len(SENT)

    telegram.handle({'callback_query': {'id': 'q1', 'data': f'take:{cid}', 'message': {'chat': {'id': 555}}}})
    assert db.one('SELECT mode FROM conversations WHERE id=?', (cid,))['mode'] == 'manager'

    telegram.handle({'message': {'chat': {'id': 555}, 'from': {}, 'message_id': 9, 'text': 'Скидку 7% согласую, пришлю КП',
                                 'reply_to_message': {'message_id': notif_id}}})
    m = db.one("SELECT * FROM messages WHERE conv_id=? AND role='manager' ORDER BY id DESC LIMIT 1", (cid,))
    assert m['content'] == 'Скидку 7% согласую, пришлю КП' and 'telegram' in m['meta']

    # сообщение клиента в диалоге менеджера пересылается в Telegram
    db.execute('INSERT INTO messages (conv_id, role, content, meta, created_at) VALUES (?,?,?,?,?)', (cid, 'user', 'Спасибо, жду', '{}', db.now()))
    assert telegram.notify_new() == 1 and telegram.flush() == 1 and 'Спасибо, жду' in SENT[-1][1]['text']


def test_outbox_survives_network_failure(monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', '123:test')
    monkeypatch.setattr(telegram.time, 'sleep', lambda *_: None)
    calls = {'n': 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls['n'] += 1
        if calls['n'] <= 3:
            raise httpx.ConnectError('Network is unreachable')
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 1}})

    telegram.bot = telegram.Bot(httpx.Client(transport=httpx.MockTransport(flaky)))
    telegram._broadcast('🔔 тест очереди', 'conv-x')
    assert telegram.flush() == 0  # сеть недоступна — сообщение остаётся в очереди
    db.execute('UPDATE tg_outbox SET next_at=0 WHERE sent_at IS NULL')
    assert telegram.flush() == 1 and telegram.outbox_stats()['pending'] == 0
