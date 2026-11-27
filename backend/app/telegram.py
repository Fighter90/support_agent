"""Telegram-бот менеджера (@SoftlineSupportBot): уведомления об эскалациях и ответы клиенту из Telegram.

- Привязка только по одноразовому коду из кабинета: кнопка «Подключить Telegram» → t.me/<бот>?start=<код> (30 мин).
  Посторонний, написавший боту, ничего не получает.
- Уведомления: новая эскалация (причина, резюме агента, последние сообщения клиента) с кнопками «Взять диалог» /
  «Вернуть агенту»; новые сообщения клиента в диалоге, который ведёт менеджер.
- Ответ реплаем на уведомление уходит клиенту в чат на сайте (как сообщение менеджера).
- Несколько менеджеров: каждый привязывает свой Telegram к своей учётке. Обращение получает назначенный менеджер
  (team.py), копию — администраторы; обращение в очереди (свободных нет) — все. Сообщения клиента — ведущему и помощникам.
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

from app import db, handoff, team

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tg_chats (chat_id INTEGER PRIMARY KEY, username TEXT, name TEXT, manager TEXT, linked_at REAL);
CREATE TABLE IF NOT EXISTS tg_codes (code TEXT PRIMARY KEY, manager TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS tg_msgs (chat_id INTEGER, message_id INTEGER, conv_id TEXT, PRIMARY KEY (chat_id, message_id));
CREATE TABLE IF NOT EXISTS tg_state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS tg_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, text TEXT, conv_id TEXT, buttons TEXT,
  tries INTEGER DEFAULT 0, next_at REAL, sent_at REAL, error TEXT);
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
        # Из РФ api.telegram.org доступен с перебоями: короткий таймаут соединения, повторы, очередь отправки (tg_outbox)
        # и, при необходимости, прокси (TELEGRAM_PROXY) или свой адрес Bot API (TELEGRAM_API_BASE, например обратный прокси за рубежом).
        proxy = os.environ.get('TELEGRAM_PROXY') or None
        self.http = http or httpx.Client(timeout=httpx.Timeout(40, connect=8), proxy=proxy)
        self.base = os.environ.get('TELEGRAM_API_BASE', 'https://api.telegram.org').rstrip('/')
        self.username = os.environ.get('TELEGRAM_BOT_USERNAME', 'SoftlineSupportBot')

    def call(self, method: str, **params: Any) -> Any:
        for attempt in range(3):  # сетевые сбои — повтор, чтобы уведомление не потерялось
            try:
                r = self.http.post(f'{getattr(self, "base", "https://api.telegram.org")}/bot{token()}/{method}', json=params)
                break
            except httpx.TransportError:
                if attempt == 2 or method == 'getUpdates':
                    raise
                time.sleep(1.5 * (attempt + 1))
        data = r.json()
        if not data.get('ok'):
            raise RuntimeError(f'Telegram {method}: {data.get("description")}')
        return data['result']


bot = Bot()


def init() -> None:
    from app.config import get_settings

    with db.conn() as c:
        c.executescript(SCHEMA)
        for sql in ("ALTER TABLE tg_chats ADD COLUMN login TEXT DEFAULT ''", "ALTER TABLE tg_codes ADD COLUMN login TEXT DEFAULT ''"):
            try:
                c.execute(sql)
            except Exception:  # noqa: BLE001 — колонка уже есть
                pass
    # чаты, подключённые до появления учёток, — за администратором (MANAGER_LOGIN)
    db.execute("UPDATE tg_chats SET login=? WHERE login IS NULL OR login=''", (get_settings().manager_login,))


def _state(key: str, default: int = 0) -> int:
    r = db.one('SELECT value FROM tg_state WHERE key=?', (key,))
    return int(r['value']) if r else default


def _set(key: str, value: int) -> None:
    db.execute('INSERT OR REPLACE INTO tg_state (key, value) VALUES (?, ?)', (key, str(value)))


def chats() -> list[dict[str, Any]]:
    return db.rows('SELECT chat_id, username, name, manager, login, linked_at FROM tg_chats ORDER BY linked_at')


def link_code(manager: str, login: str = '') -> dict[str, Any]:
    code = secrets.token_urlsafe(9).replace('-', '').replace('_', '')[:12]
    db.execute('INSERT INTO tg_codes (code, manager, login, created_at) VALUES (?,?,?,?)', (code, manager, login, db.now()))
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


def _recipients(logins: set[str] | None) -> list[dict[str, Any]]:
    """Кому слать: указанным менеджерам + администраторам; None — всем подключённым чатам (обращение в очереди)."""
    all_chats = chats()
    if logins is None:
        return all_chats
    admins = {u['login'] for u in db.rows("SELECT login FROM users WHERE role='admin' AND active=1")}
    want = set(logins) | admins
    return [c for c in all_chats if c.get('login') in want]


def to_user(login: str, text: str, conv_id: str = '') -> None:
    _broadcast(text, conv_id, _buttons(conv_id) if conv_id else None, {login})


def _broadcast(text: str, conv_id: str, buttons: list[list[dict[str, str]]] | None = None, logins: set[str] | None = None) -> None:
    """Ставит уведомление в очередь для каждого чата-получателя: при сбое сети оно уйдёт позже, а не потеряется."""
    for c in _recipients(logins):
        db.execute('INSERT INTO tg_outbox (chat_id, text, conv_id, buttons, next_at) VALUES (?,?,?,?,?)',
                   (c['chat_id'], text, conv_id, db.dumps(buttons) if buttons else '', db.now()))


def flush(limit: int = 20) -> int:
    """Отправка очереди: повторы с растущей паузой (до 2 суток), чтобы пережить перебои связи с Telegram."""
    sent = 0
    for m in db.rows('SELECT * FROM tg_outbox WHERE sent_at IS NULL AND next_at<=? AND tries<60 ORDER BY id LIMIT ?', (db.now(), limit)):
        try:
            _send(m['chat_id'], m['text'], m['conv_id'], db.loads(m['buttons'], None) if m['buttons'] else None)
            db.execute('UPDATE tg_outbox SET sent_at=?, tries=tries+1, error=NULL WHERE id=?', (db.now(), m['id']))
            sent += 1
        except Exception as e:  # noqa: BLE001
            # связь с api.telegram.org из РФ рвётся часто, но ненадолго: первые 10 попыток — каждые 3 с, затем реже
            delay = 3 if m['tries'] < 10 else min(3600, 10 * 2 ** min(m['tries'] - 10, 8))
            db.execute('UPDATE tg_outbox SET tries=tries+1, next_at=?, error=? WHERE id=?',
                       (db.now() + delay, str(e).replace(token(), '***')[:300], m['id']))
            break  # связи нет — остальное попробуем на следующем круге
    return sent


def _buttons(cid: str) -> list[list[dict[str, str]]]:
    url = os.environ.get('PUBLIC_URL', 'https://methodagent.ru').rstrip('/') + '/manager'
    return [[{'text': '🙋 Взять диалог', 'callback_data': f'take:{cid}'}, {'text': '🤖 Вернуть агенту', 'callback_data': f'back:{cid}'}],
            [{'text': 'Открыть кабинет', 'url': url}]]


def notify_new() -> int:
    """Новые эскалации и сообщения клиентов в диалогах, которые ведёт менеджер, — в Telegram. Возвращает число отправок."""
    sent = 0
    team.dispatch()  # сначала назначаем исполнителя — уведомление уйдёт ему
    last_e, last_m, last_l = _state('esc', -1), _state('msg', -1), _state('log', -1)
    if last_e < 0 or last_m < 0 or last_l < 0:  # первый запуск: не присылаем историю
        _set('esc', (db.one('SELECT max(id) AS v FROM escalations') or {}).get('v') or 0)
        _set('msg', (db.one('SELECT max(id) AS v FROM messages') or {}).get('v') or 0)
        _set('log', (db.one('SELECT max(id) AS v FROM team_log') or {}).get('v') or 0)
        return 0
    for e in db.rows('SELECT * FROM escalations WHERE id>? ORDER BY id', (last_e,)):
        _set('esc', e['id'])
        last = db.rows("SELECT content FROM messages WHERE conv_id=? AND role='user' ORDER BY id DESC LIMIT 3", (e['conv_id'],))
        lines = '\n'.join('• ' + m['content'][:300] for m in reversed(last))
        e = db.one('SELECT * FROM escalations WHERE id=?', (e['id'],)) or e
        who = f'Назначено: {team.name_of(e["assignee"])}' if e.get('assignee') else 'В очереди — свободных менеджеров нет, возьмите, кто может'
        text = (f'🔔 Эскалация #{e["id"]} — {e["category"] or REASONS.get(e["reason"], "Другое")}\n{who}\n\n'
                f'Резюме агента: {e["summary"][:800]}\n\nКлиент писал:\n{lines or "—"}\n\n'
                'Ответьте реплаем на это сообщение — ответ сразу придёт клиенту в чат на сайте.')
        _broadcast(text, e['conv_id'], _buttons(e['conv_id']), {e['assignee']} if e.get('assignee') else None)
        sent += 1
    # переназначения (не взяли вовремя / ушли из сети) — новому исполнителю
    for lg in db.rows("SELECT l.*, e.created_at AS esc_at, e.category FROM team_log l JOIN escalations e ON e.id=l.esc_id "
                      "WHERE l.id>? AND l.action='assign' ORDER BY l.id", (last_l,)):
        _set('log', lg['id'])
        if lg['created_at'] - lg['esc_at'] > 6 and lg['esc_id'] <= _state('esc', 0):
            _broadcast(f'📥 Вам назначено обращение #{lg["esc_id"]} ({lg["category"]}) из очереди. '
                       'Ответьте реплаем или возьмите диалог.', lg['conv_id'], _buttons(lg['conv_id']), {lg['target']})
            sent += 1
    mxl = (db.one('SELECT max(id) AS v FROM team_log') or {}).get('v') or 0
    _set('log', max(mxl, _state('log', 0)))
    rows = db.rows("SELECT m.id, m.conv_id, m.content, c.manager_login FROM messages m JOIN conversations c ON c.id=m.conv_id "
                   "WHERE m.id>? AND m.role='user' AND c.mode='manager' ORDER BY m.id", (last_m,))
    for m in rows:
        team_logins = {p['login'] for p in team.participants(m['conv_id'])} | ({m['manager_login']} if m['manager_login'] else set())
        _broadcast(f'💬 Клиент: {m["content"][:1500]}\n\n(ответьте реплаем)', m['conv_id'], None, team_logins or None)
        sent += 1
    mx = (db.one('SELECT max(id) AS v FROM messages') or {}).get('v') or last_m
    _set('msg', max(mx, last_m))
    # Регламент ответа: эскалация без ответа дольше цели (по умолчанию 2 мин) — одно напоминание
    from app import discounts
    target = discounts.sla_seconds()
    if _state('remind_from', -1) < 0:  # старые эскалации (до включения напоминаний) не трогаем
        _set('remind_from', (db.one('SELECT max(id) AS v FROM escalations') or {}).get('v') or 0)
    for e in db.rows("SELECT id, conv_id, category, assignee FROM escalations WHERE status='open' AND id>? AND created_at < ?",
                     (_state('remind_from', 0), db.now() - target)):
        if _state(f'remind:{e["id"]}', 0):
            continue
        _set(f'remind:{e["id"]}', 1)
        _broadcast(f'⏰ Эскалация #{e["id"]} ({e["category"]}) ждёт ответа дольше регламента — {target / 60:g} мин. '
                   'Ответьте реплаем или возьмите диалог.', e['conv_id'], _buttons(e['conv_id']), {e['assignee']} if e['assignee'] else None)
        sent += 1
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
        login = chat.get('login') or ''
        name = f'{team.name_of(login) if login else chat["manager"] or "Менеджер Softline"} (Telegram)'
        handoff.take(cid, name, on=action == 'take', login=login)
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
            db.execute('INSERT OR REPLACE INTO tg_chats (chat_id, username, name, manager, login, linked_at) VALUES (?,?,?,?,?,?)',
                       (chat_id, user.get('username', ''), user.get('first_name', ''), row['manager'], row.get('login') or '', db.now()))
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
    login = chat.get('login') or ''
    handoff.say(link['conv_id'], f'{team.name_of(login) if login else chat["manager"] or "Менеджер Softline"} (Telegram)', text,
                via='telegram', login=login)
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
            logger.warning('Telegram getUpdates: %s', str(e).replace(token(), '***'))
            time.sleep(5)


def _notify_loop() -> None:
    while True:
        try:
            notify_new()
            flush()
        except Exception:  # noqa: BLE001
            logger.exception('Telegram: ошибка уведомлений')
        time.sleep(2)


def outbox_stats() -> dict[str, Any]:
    r = db.one('SELECT sum(sent_at IS NOT NULL) AS sent, sum(sent_at IS NULL) AS pending, max(sent_at) AS last_sent FROM tg_outbox') or {}
    return {'sent': r.get('sent') or 0, 'pending': r.get('pending') or 0, 'last_sent': r.get('last_sent')}


def start() -> None:
    if not enabled():
        logger.info('Telegram-бот выключен: нет TELEGRAM_BOT_TOKEN')
        return

    def hello() -> None:  # getMe в фоне — недоступность Telegram не задерживает запуск сервиса
        try:
            bot.username = bot.call('getMe').get('username') or bot.username
            logger.info('Telegram-бот @%s на связи', bot.username)
        except Exception as e:  # noqa: BLE001
            logger.warning('Telegram getMe: %s', str(e).replace(token(), '***'))

    threading.Thread(target=hello, name='tg-hello', daemon=True).start()
    threading.Thread(target=_poll, name='tg-poll', daemon=True).start()
    threading.Thread(target=_notify_loop, name='tg-notify', daemon=True).start()
    logger.info('Telegram-бот запущен (long polling, очередь уведомлений)')
