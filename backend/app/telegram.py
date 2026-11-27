"""Telegram-бот менеджера (@SoftlineSupportBot): уведомления об эскалациях и ответы клиенту из Telegram.

- Привязка только по одноразовому коду из кабинета: кнопка «Подключить Telegram» → t.me/<бот>?start=<код> (30 мин).
  Посторонний, написавший боту, ничего не получает.
- Уведомления: новая эскалация (причина, резюме агента, последние сообщения клиента) с кнопками «Взять диалог» /
  «Вернуть агенту»; новые сообщения клиента в диалоге, который ведёт менеджер.
- Ответ реплаем на уведомление уходит клиенту в чат на сайте (как сообщение менеджера).
- 152-ФЗ: Telegram — зарубежный сервис, поэтому в бот уходит только обезличенный текст (персональные данные
  заменены метками [ТЕЛ_1], [EMAIL_1] — как их видит модель); полный текст — только в кабинете на сервере в РФ.
- Работает long polling (getUpdates) в фоновом потоке — вебхук и открытые порты не нужны. Токен: TELEGRAM_BOT_TOKEN.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from typing import Any

import httpx

from app import db, handoff

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tg_chats (chat_id INTEGER PRIMARY KEY, username TEXT, name TEXT, manager TEXT, linked_at REAL);
CREATE TABLE IF NOT EXISTS tg_codes (code TEXT PRIMARY KEY, manager TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS tg_msgs (chat_id INTEGER, message_id INTEGER, conv_id TEXT, PRIMARY KEY (chat_id, message_id));
CREATE TABLE IF NOT EXISTS tg_state (key TEXT PRIMARY KEY, value TEXT);
"""
CODE_TTL = 30 * 60
REASONS = {'price_on_request': 'Цена по запросу', 'special_terms': 'Скидка / спецусловия', 'tender': 'Госзакупки / тендер',
           'order_status': 'Вопрос по заказу', 'complex_config': 'Сложная конфигурация', 'not_found': 'Продукт не найден',
           'client_request': 'Клиент просит менеджера', 'other': 'Другое'}


def token() -> str:
    return os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()


def enabled() -> bool:
    return bool(token())


class Bot:
    def __init__(self, http: httpx.Client | None = None) -> None:
        self.http = http or httpx.Client(timeout=40)
        self.username = os.environ.get('TELEGRAM_BOT_USERNAME', 'SoftlineSupportBot')

    def call(self, method: str, **params: Any) -> Any:
        r = self.http.post(f'https://api.telegram.org/bot{token()}/{method}', json=params)
        data = r.json()
        if not data.get('ok'):
            raise RuntimeError(f'Telegram {method}: {data.get("description")}')
        return data['result']


bot = Bot()


def init() -> None:
    with db.conn() as c:
        c.executescript(SCHEMA)


def _state(key: str, default: int = 0) -> int:
    r = db.one('SELECT value FROM tg_state WHERE key=?', (key,))
    return int(r['value']) if r else default


def _set(key: str, value: int) -> None:
    db.execute('INSERT OR REPLACE INTO tg_state (key, value) VALUES (?, ?)', (key, str(value)))


def chats() -> list[dict[str, Any]]:
    return db.rows('SELECT chat_id, username, name, manager, linked_at FROM tg_chats ORDER BY linked_at')


def link_code(manager: str) -> dict[str, Any]:
    code = secrets.token_urlsafe(9).replace('-', '').replace('_', '')[:12]
    db.execute('INSERT INTO tg_codes (code, manager, created_at) VALUES (?,?,?)', (code, manager, db.now()))
    return {'code': code, 'url': f'https://t.me/{bot.username}?start={code}', 'expires_in': CODE_TTL}


def unlink(chat_id: int) -> None:
    db.execute('DELETE FROM tg_chats WHERE chat_id=?', (chat_id,))


def _send(chat_id: int, text: str, conv_id: str = '', buttons: list[list[dict[str, str]]] | None = None) -> None:
    params: dict[str, Any] = {'chat_id': chat_id, 'text': text[:4000], 'disable_web_page_preview': True}
    if buttons:
        params['reply_markup'] = {'inline_keyboard': buttons}
    msg = bot.call('sendMessage', **params)
    if conv_id:
        db.execute('INSERT OR REPLACE INTO tg_msgs (chat_id, message_id, conv_id) VALUES (?,?,?)', (chat_id, msg['message_id'], conv_id))


def _broadcast(text: str, conv_id: str, buttons: list[list[dict[str, str]]] | None = None) -> None:
    for c in chats():
        try:
            _send(c['chat_id'], text, conv_id, buttons)
        except Exception as e:  # noqa: BLE001 — один недоступный чат не мешает остальным
            logger.warning('Telegram %s: %s', c['chat_id'], e)


def _buttons(cid: str) -> list[list[dict[str, str]]]:
    url = os.environ.get('PUBLIC_URL', 'https://methodagent.ru').rstrip('/') + '/manager'
    return [[{'text': '🙋 Взять диалог', 'callback_data': f'take:{cid}'}, {'text': '🤖 Вернуть агенту', 'callback_data': f'back:{cid}'}],
            [{'text': 'Открыть кабинет', 'url': url}]]


def notify_new() -> int:
    """Новые эскалации и сообщения клиентов в диалогах, которые ведёт менеджер, — в Telegram. Возвращает число отправок."""
    sent = 0
    last_e, last_m = _state('esc', -1), _state('msg', -1)
    if last_e < 0 or last_m < 0:  # первый запуск: не присылаем историю
        _set('esc', (db.one('SELECT max(id) AS v FROM escalations') or {}).get('v') or 0)
        _set('msg', (db.one('SELECT max(id) AS v FROM messages') or {}).get('v') or 0)
        return 0
    for e in db.rows('SELECT * FROM escalations WHERE id>? ORDER BY id', (last_e,)):
        _set('esc', e['id'])
        last = db.rows("SELECT content FROM messages WHERE conv_id=? AND role='user' ORDER BY id DESC LIMIT 3", (e['conv_id'],))
        lines = '\n'.join('• ' + m['content'][:300] for m in reversed(last))
        text = (f'🔔 Эскалация #{e["id"]} — {e["category"] or REASONS.get(e["reason"], "Другое")}\n\n'
                f'Резюме агента: {e["summary"][:800]}\n\nКлиент писал:\n{lines or "—"}\n\n'
                'Ответьте реплаем на это сообщение — ответ сразу придёт клиенту в чат на сайте.')
        _broadcast(text, e['conv_id'], _buttons(e['conv_id']))
        sent += 1
    rows = db.rows("SELECT m.id, m.conv_id, m.content FROM messages m JOIN conversations c ON c.id=m.conv_id "
                   "WHERE m.id>? AND m.role='user' AND c.mode='manager' ORDER BY m.id", (last_m,))
    for m in rows:
        _broadcast(f'💬 Клиент: {m["content"][:1500]}\n\n(ответьте реплаем)', m['conv_id'])
        sent += 1
    mx = (db.one('SELECT max(id) AS v FROM messages') or {}).get('v') or last_m
    _set('msg', max(mx, last_m))
    return sent


def handle(update: dict[str, Any]) -> None:
    """Входящее обновление Telegram: привязка по коду, кнопки, ответы реплаем."""
    if 'callback_query' in update:
        q = update['callback_query']
        chat = db.one('SELECT * FROM tg_chats WHERE chat_id=?', (q['message']['chat']['id'],))
        action, _, cid = (q.get('data') or '').partition(':')
        if not chat or not db.one('SELECT id FROM conversations WHERE id=?', (cid,)):
            bot.call('answerCallbackQuery', callback_query_id=q['id'], text='Нет доступа')
            return
        name = f'{chat["manager"] or "Менеджер Softline"} (Telegram)'
        handoff.take(cid, name, on=action == 'take')
        bot.call('answerCallbackQuery', callback_query_id=q['id'],
                 text='Диалог ваш — агент молчит. Отвечайте реплаем.' if action == 'take' else 'Диалог передан ИИ-ассистенту')
        return
    msg = update.get('message') or {}
    chat_id, text = (msg.get('chat') or {}).get('id'), (msg.get('text') or '').strip()
    if not chat_id or not text:
        return
    user = msg.get('from') or {}
    if text.startswith('/start'):
        code = text.split(maxsplit=1)[1].strip() if ' ' in text else ''
        row = db.one('SELECT * FROM tg_codes WHERE code=?', (code,)) if code else None
        if row and db.now() - row['created_at'] < CODE_TTL:
            db.execute('DELETE FROM tg_codes WHERE code=?', (code,))
            db.execute('INSERT OR REPLACE INTO tg_chats (chat_id, username, name, manager, linked_at) VALUES (?,?,?,?,?)',
                       (chat_id, user.get('username', ''), user.get('first_name', ''), row['manager'], db.now()))
            bot.call('sendMessage', chat_id=chat_id, text='✅ Telegram подключён к кабинету менеджера Softline.\n\n'
                     'Сюда будут приходить эскалации и сообщения клиентов. Чтобы ответить клиенту, ответьте реплаем на уведомление. '
                     'Персональные данные клиентов здесь скрыты метками — полный текст в кабинете.')
        else:
            bot.call('sendMessage', chat_id=chat_id, text='Это бот для менеджеров Softline. Подключение — кнопкой «Подключить Telegram» в кабинете менеджера.')
        return
    chat = db.one('SELECT * FROM tg_chats WHERE chat_id=?', (chat_id,))
    if not chat:
        bot.call('sendMessage', chat_id=chat_id, text='Чат не подключён. Нажмите «Подключить Telegram» в кабинете менеджера.')
        return
    reply = msg.get('reply_to_message') or {}
    link = db.one('SELECT conv_id FROM tg_msgs WHERE chat_id=? AND message_id=?', (chat_id, reply.get('message_id'))) if reply else None
    if not link:
        bot.call('sendMessage', chat_id=chat_id, text='Чтобы ответить клиенту, ответьте реплаем на уведомление о его обращении.')
        return
    handoff.say(link['conv_id'], f'{chat["manager"] or "Менеджер Softline"} (Telegram)', text, via='telegram')
    bot.call('sendMessage', chat_id=chat_id, text='✉️ Отправлено клиенту', reply_to_message_id=msg['message_id'])


def _poll() -> None:
    offset = _state('offset', 0)
    while True:
        try:
            for u in bot.call('getUpdates', offset=offset, timeout=25, allowed_updates=['message', 'callback_query']):
                offset = u['update_id'] + 1
                _set('offset', offset)
                try:
                    handle(u)
                except Exception:  # noqa: BLE001
                    logger.exception('Telegram: ошибка обработки обновления')
        except Exception as e:  # noqa: BLE001 — сеть/таймаут: пауза и повтор
            logger.warning('Telegram getUpdates: %s', e)
            time.sleep(5)


def _notify_loop() -> None:
    while True:
        try:
            notify_new()
        except Exception:  # noqa: BLE001
            logger.exception('Telegram: ошибка уведомлений')
        time.sleep(2)


def start() -> None:
    if not enabled():
        logger.info('Telegram-бот выключен: нет TELEGRAM_BOT_TOKEN')
        return
    try:
        me = bot.call('getMe')
        bot.username = me.get('username') or bot.username
    except Exception as e:  # noqa: BLE001
        logger.warning('Telegram getMe: %s', e)
    threading.Thread(target=_poll, name='tg-poll', daemon=True).start()
    threading.Thread(target=_notify_loop, name='tg-notify', daemon=True).start()
    logger.info('Telegram-бот @%s запущен', bot.username)
