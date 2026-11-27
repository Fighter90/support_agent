"""Действия менеджера с диалогом — общие для кабинета и Telegram-бота.

Несколько менеджеров: у диалога один ведущий (owner) и сколько угодно помощников (helper). Ведущий может передать
диалог коллеге или позвать коллегу в диалог; клиент видит, кто подключился. Все действия пишутся в team_log.
"""

from __future__ import annotations

from typing import Any

from app import db, team


def _sys(cid: str, text: str) -> None:
    db.execute('INSERT INTO messages (conv_id, role, content, meta, created_at) VALUES (?,?,?,?,?)', (cid, 'system', text, '{}', db.now()))


def take(cid: str, manager: str, on: bool = True, login: str = '') -> dict[str, Any]:
    """Менеджер берёт диалог (агент замолкает) или возвращает его ИИ-ассистенту."""
    db.execute('UPDATE conversations SET mode=?, manager=?, manager_login=?, updated_at=? WHERE id=?',
               ('manager' if on else 'agent', manager if on else '', login if on else '', db.now(), cid))
    _sys(cid, f'К диалогу подключился {manager}. Отвечаю вам лично.' if on else
         'Менеджер передал диалог ИИ-ассистенту. Я на связи — задавайте вопросы.')
    if on:
        db.execute("UPDATE escalations SET status='answered', manager=?, assignee=CASE WHEN ?!='' THEN ? ELSE assignee END, answered_at=? "
                   "WHERE conv_id=? AND status='open'", (manager, login, login, db.now(), cid))
        if login:
            team.join(cid, login, 'owner')
    else:
        db.execute('DELETE FROM conv_managers WHERE conv_id=?', (cid,))
    team._log(cid, None, 'take' if on else 'return', login or manager)
    return {'mode': 'manager' if on else 'agent'}


def say(cid: str, manager: str, text: str, via: str = 'cabinet', login: str = '') -> int:
    """Сообщение менеджера клиенту; если диалог ещё ведёт агент — менеджер забирает его, если ведёт коллега — становится помощником."""
    c = db.one('SELECT mode, manager_login FROM conversations WHERE id=?', (cid,))
    if c and c.get('mode') != 'manager':
        db.execute('UPDATE conversations SET mode=?, manager=?, manager_login=? WHERE id=?', ('manager', manager, login, cid))
        db.execute("UPDATE escalations SET status='answered', manager=?, answered_at=? WHERE conv_id=? AND status='open'", (manager, db.now(), cid))
        if login:
            team.join(cid, login, 'owner')
    elif login and c and c.get('manager_login') != login:
        team.join(cid, login, 'helper')
    mid = db.execute('INSERT INTO messages (conv_id, role, content, meta, created_at) VALUES (?,?,?,?,?)',
                     (cid, 'manager', text.strip(), db.dumps({'manager': manager, 'via': via, 'login': login}), db.now()))
    db.execute('UPDATE conversations SET updated_at=? WHERE id=?', (db.now(), cid))
    return mid


def transfer(cid: str, actor: dict[str, str], to_login: str) -> dict[str, Any]:
    """Передать диалог коллеге: он становится ведущим, клиент видит смену менеджера."""
    u = team.get(to_login)
    if not u or not u['active']:
        raise ValueError('Менеджер не найден или отключён')
    db.execute("UPDATE conversations SET mode='manager', manager=?, manager_login=?, updated_at=? WHERE id=?", (u['name'], to_login, db.now(), cid))
    db.execute("UPDATE escalations SET assignee=?, assigned_at=? WHERE conv_id=? AND status='open'", (to_login, db.now(), cid))
    team.join(cid, to_login, 'owner')
    team.leave(cid, actor['login']) if actor['login'] != to_login else None
    _sys(cid, f'Диалог передан коллеге: {u["name"]}. Он продолжит общение с вами здесь.')
    team._log(cid, None, 'transfer', actor['login'], to_login)
    _notify(cid, to_login, f'↪️ {actor["name"]} передал(а) вам диалог')
    return {'owner': to_login}


def invite(cid: str, actor: dict[str, str], login: str) -> dict[str, Any]:
    """Позвать коллегу в диалог помощником (например, технического специалиста): ведущий остаётся прежним."""
    u = team.get(login)
    if not u or not u['active']:
        raise ValueError('Менеджер не найден или отключён')
    team.join(cid, login, 'helper')
    _sys(cid, f'К диалогу подключился ещё один специалист: {u["name"]}.')
    team._log(cid, None, 'invite', actor['login'], login)
    _notify(cid, login, f'👥 {actor["name"]} зовёт вас в диалог')
    return {'participants': team.participants(cid)}


def _notify(cid: str, login: str, text: str) -> None:
    try:
        from app import telegram

        telegram.to_user(login, text + '\n\nОтвечайте реплаем — ответ придёт клиенту в чат.', cid)
    except Exception:  # noqa: BLE001 — Telegram не обязателен
        pass
