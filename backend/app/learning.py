"""Обучение на обратной связи клиентов (без дообучения весов модели).

- 👍 на ответ агента → пара «вопрос клиента — ответ» сохраняется как пример (examples). На похожий вопрос
  агент получает до 2 таких примеров в системном сообщении: стиль и маршрут ответа, проверенные клиентами.
  Суммы в ₽ из примеров вырезаются — цены всегда берутся заново из актуального фида.
- 👎 → ответ попадает в кабинет менеджера («Отзывы»); менеджер пишет правильный ответ, он становится статьёй
  базы знаний (RAG), а отзыв помечается исправленным.
- Опрос в конце диалога (решён ли вопрос, оценка 1–5, комментарий) даёт CSAT и долю решённых вопросов.
"""

from __future__ import annotations

import re
from typing import Any

from app import db, kb
from app.catalog import _terms

_PRICE = re.compile(r'\d[\d\s ]*(?:[.,]\d{1,2})?\s*(?:₽|руб\.?)', re.IGNORECASE)


def _question_for(message_id: int) -> tuple[str, str, str] | None:
    m = db.one('SELECT conv_id, content FROM messages WHERE id=? AND role=?', (message_id, 'assistant'))
    if not m:
        return None
    q = db.one("SELECT content FROM messages WHERE conv_id=? AND role='user' AND id<? ORDER BY id DESC LIMIT 1", (m['conv_id'], message_id))
    return (m['conv_id'], (q or {}).get('content', ''), m['content'])


def rate(conv_id: str, message_id: int, rating: int) -> None:
    db.execute('DELETE FROM feedback WHERE conv_id=? AND message_id=?', (conv_id, message_id))
    db.execute('INSERT INTO feedback (conv_id, message_id, rating, created_at) VALUES (?,?,?,?)', (conv_id, message_id, rating, db.now()))
    if rating > 0:
        qa = _question_for(message_id)
        if qa and qa[1] and len(qa[2]) > 20:
            answer = _PRICE.sub('[цена из каталога]', qa[2])
            db.execute('INSERT OR IGNORE INTO examples (question, answer, message_id, created_at) VALUES (?,?,?,?)',
                       (qa[1][:600], answer[:1500], message_id, db.now()))


def examples_for(question: str, limit: int = 2) -> list[dict[str, Any]]:
    terms = _terms(question)
    if not terms:
        return []
    try:
        return db.rows('SELECT e.question, e.answer FROM examples_fts JOIN examples e ON e.id = examples_fts.rowid '
                       'WHERE examples_fts MATCH ? ORDER BY bm25(examples_fts) LIMIT ?', (' OR '.join(terms), limit))
    except Exception:  # noqa: BLE001
        return []


def survey(conv_id: str, resolved: bool, score: int, comment: str) -> None:
    db.execute('INSERT OR REPLACE INTO surveys (conv_id, resolved, score, comment, created_at) VALUES (?,?,?,?,?)',
               (conv_id, int(resolved), max(1, min(5, int(score))), comment.strip()[:1000], db.now()))


def negatives() -> list[dict[str, Any]]:
    rs = db.rows("SELECT f.id, f.conv_id, f.message_id, f.status, f.created_at, m.content AS answer FROM feedback f "
                 "JOIN messages m ON m.id = f.message_id WHERE f.rating < 0 ORDER BY f.id DESC LIMIT 200")
    for r in rs:
        q = db.one("SELECT content FROM messages WHERE conv_id=? AND role='user' AND id<? ORDER BY id DESC LIMIT 1", (r['conv_id'], r['message_id']))
        r['question'] = (q or {}).get('content', '')
    return rs


def fix(feedback_id: int, answer: str, manager: str) -> int:
    f = db.one('SELECT * FROM feedback WHERE id=?', (feedback_id,))
    if not f:
        raise KeyError(feedback_id)
    qa = _question_for(f['message_id'])
    kb_id = kb.add_manager_answer((qa[1] if qa else '') or 'Исправленный ответ', answer, manager)
    db.execute("UPDATE feedback SET status='fixed', fixed_kb_id=? WHERE id=?", (kb_id, feedback_id))
    return kb_id


def stats() -> dict[str, Any]:
    s = db.one('SELECT count(*) AS n, avg(score) AS csat, avg(resolved) AS resolved FROM surveys') or {}
    f = db.one('SELECT sum(rating>0) AS up, sum(rating<0) AS down FROM feedback') or {}
    return {'surveys': s.get('n') or 0, 'csat': round(s['csat'], 2) if s.get('csat') else None,
            'resolved_share': round(s['resolved'], 3) if s.get('resolved') is not None else None,
            'thumbs_up': f.get('up') or 0, 'thumbs_down': f.get('down') or 0,
            'examples': (db.one('SELECT count(*) AS n FROM examples') or {}).get('n', 0),
            'comments': db.rows("SELECT score, resolved, comment, created_at FROM surveys WHERE comment<>'' ORDER BY id DESC LIMIT 20")}
