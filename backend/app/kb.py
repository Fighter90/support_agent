"""База знаний (RAG): статьи из data/kb/*.md + проверенные ответы менеджеров.

Статьи режутся по заголовкам «## », индексируются в SQLite FTS5 и выдаются агенту инструментом search_knowledge.
Ответ менеджера на эскалацию (с отметкой «добавить в базу знаний») становится новой статьёй — агент
пополняет базу знаний из реальных диалогов и в следующий раз отвечает на такой вопрос сам.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app import db
from app.catalog import _terms
from app.config import ROOT_DIR

KB_DIR = ROOT_DIR / 'data' / 'kb'


def _articles(path: Path) -> list[tuple[str, str, str]]:
    text = path.read_text(encoding='utf-8')
    src = re.search(r'^Источник:\s*(\S+)', text, flags=re.MULTILINE)
    url = src.group(1) if src and src.group(1).startswith('http') else ''
    out = []
    for block in re.split(r'^## ', text, flags=re.MULTILINE)[1:]:
        title, _, body = block.partition('\n')
        body = body.strip()
        if body:
            out.append((title.strip(), body, url))
    return out


def load_base() -> int:
    """Перезагружает статьи из файлов (ответы менеджеров сохраняются)."""
    with db.conn() as c:
        c.execute("DELETE FROM kb WHERE source='base'")
        n = 0
        for f in sorted(KB_DIR.glob('*.md')):
            for title, body, url in _articles(f):
                c.execute('INSERT INTO kb (title, body, source, url, created_at) VALUES (?, ?, ?, ?, ?)',
                          (title, body, 'base', url, db.now()))
                n += 1
    return n


# Разговорные формы → слова из статей (найдено оценкой RAGAS: «вернуть деньги» не находило «возврат денежных средств»)
SYNONYMS = {
    'верн': ['возврат'], 'вернуть': ['возврат'], 'деньг': ['денежн'], 'забыл': ['восстанов'], 'парол': ['пароль'],
    'мейл': ['почт', 'email'], 'e-mail': ['почт'], 'email': ['почт'], 'почт': ['email'], 'ключ': ['дистрибутив'],
    'отменит': ['отмен'], 'закрывающ': ['бухгалтерск', 'упд'], 'документ': ['бухгалтерск'], 'доставк': ['сроки'],
    'оплат': ['оплатить'], 'скидк': ['промокод'], 'гарант': ['гарантии'], 'кабинет': ['личный'],
}


def _expand(terms: list[str]) -> list[str]:
    out = list(terms)
    for t in terms:
        stem = t.strip('"*')
        for k, extra in SYNONYMS.items():
            if stem.startswith(k) or k.startswith(stem):
                out += [f'"{e}"*' for e in extra]
    return list(dict.fromkeys(out))[:14]


def search(query: str, limit: int = 3) -> list[dict[str, Any]]:
    terms = _terms(query)
    if not terms:
        return []
    found: dict[int, dict[str, Any]] = {}
    for joiner, ts in ((' AND ', terms), (' OR ', _expand(terms))):
        if len(found) >= limit:
            break
        try:
            rs = db.rows('SELECT kb.id, kb.title, kb.body, kb.source, kb.url, bm25(kb_fts, 5.0, 1.0) AS rank FROM kb_fts '
                         'JOIN kb ON kb.id = kb_fts.rowid WHERE kb_fts MATCH ? ORDER BY rank LIMIT ?',
                         (joiner.join(ts), limit))
        except Exception:  # noqa: BLE001 — неудачный синтаксис запроса FTS просто не даёт результатов
            continue
        for r in rs:
            found.setdefault(r['id'], r)
    return [{'id': r['id'], 'title': r['title'], 'text': r['body'][:1800], 'source': r['source'], 'url': r['url']}
            for r in list(found.values())[:limit]]


def add_manager_answer(question: str, answer: str, manager: str) -> int:
    title = question.strip().split('\n')[0][:160]
    return db.execute('INSERT INTO kb (title, body, source, url, created_at) VALUES (?, ?, ?, ?, ?)',
                      (title, f'{answer.strip()}\n\n(Ответ проверен менеджером: {manager})', 'manager', '', db.now()))


def articles(limit: int = 300) -> list[dict[str, Any]]:
    return db.rows('SELECT id, title, substr(body, 1, 400) AS body, source, url, created_at FROM kb ORDER BY source DESC, id LIMIT ?', (limit,))
