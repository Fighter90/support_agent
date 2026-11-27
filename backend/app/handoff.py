"""Действия менеджера с диалогом — общие для кабинета и Telegram-бота."""

from __future__ import annotations

from typing import Any

from app import db


def take(cid: str, manager: str, on: bool = True) -> dict[str, Any]:
    """Менеджер берёт диалог (агент замолкает) или возвращает его ИИ-ассистенту."""
    db.execute('UPDATE conversations SET mode=?, manager=?, updated_at=? WHERE id=?',
               ('manager' if on else 'agent', manager if on else '', db.now(), cid))
    text = (f'К диалогу подключился {manager}. Отвечаю вам лично.' if on else
            'Менеджер передал диалог ИИ-ассистенту. Я на связи — задавайте вопросы.')
    db.execute('INSERT INTO messages (conv_id, role, content, meta, created_at) VALUES (?,?,?,?,?)', (cid, 'system', text, '{}', db.now()))
    if on:
        db.execute("UPDATE escalations SET status='answered', manager=?, answered_at=? WHERE conv_id=? AND status='open'", (manager, db.now(), cid))
    return {'mode': 'manager' if on else 'agent'}


def say(cid: str, manager: str, text: str, via: str = 'cabinet') -> int:
    """Сообщение менеджера клиенту; если диалог ещё ведёт агент — менеджер забирает его."""
    c = db.one('SELECT mode FROM conversations WHERE id=?', (cid,))
    if c and c.get('mode') != 'manager':
        db.execute('UPDATE conversations SET mode=?, manager=? WHERE id=?', ('manager', manager, cid))
        db.execute("UPDATE escalations SET status='answered', manager=?, answered_at=? WHERE conv_id=? AND status='open'", (manager, db.now(), cid))
    mid = db.execute('INSERT INTO messages (conv_id, role, content, meta, created_at) VALUES (?,?,?,?,?)',
                     (cid, 'manager', text.strip(), db.dumps({'manager': manager, 'via': via}), db.now()))
    db.execute('UPDATE conversations SET updated_at=? WHERE id=?', (db.now(), cid))
    return mid
