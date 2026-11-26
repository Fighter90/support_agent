"""Маскирование персональных данных до передачи текста в языковую модель (152-ФЗ, принцип минимизации).

Модели для подбора ПО персональные данные не нужны. Email, телефоны, ИНН/КПП/ОГРН, номера карт и ФИО
заменяются метками [EMAIL_1], [ТЕЛ_1], [ИНН_1], [ФИО_1]; соответствие хранится только в нашей БД на сервере в РФ
(таблица pii) и подставляется обратно лишь в интерфейсе менеджера. Реквизиты для КП клиент вводит в отдельную
форму — они идут сразу в генератор КП, минуя модель.
"""

from __future__ import annotations

import re

from app import db

PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ('email', 'EMAIL', re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+', re.UNICODE)),
    ('card', 'КАРТА', re.compile(r'\b(?:\d[ -]?){15,18}\d\b')),
    ('phone', 'ТЕЛ', re.compile(r'(?:\+7|\b8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}\b')),
    ('ogrn', 'ОГРН', re.compile(r'\b\d{13}(?:\d{2})?\b')),
    ('inn', 'ИНН', re.compile(r'\b\d{12}\b|\b\d{10}\b')),
    ('kpp', 'КПП', re.compile(r'(?<=КПП)[\s:№]*\d{9}\b', re.IGNORECASE)),
    ('passport', 'ПАСПОРТ', re.compile(r'\b\d{2}\s?\d{2}\s?\d{6}\b')),
    # ФИО: «Иванов Иван Иванович» / «Иван Иванович» (отчество — признак ФИО) и «меня зовут Иван»
    ('fio', 'ФИО', re.compile(r'\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:вич|вна|чна|ична|оглы|кызы)\b')),
    ('fio', 'ФИО', re.compile(r'\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:вич|вна|ична)\b')),
    ('name', 'ИМЯ', re.compile(r'(?<=меня зовут )[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?', re.IGNORECASE)),
]


def mask(conv_id: str, text: str) -> str:
    """Возвращает текст с метками вместо ПДн и сохраняет соответствие для диалога."""
    existing = {r['value']: r['token'] for r in db.rows('SELECT token, value FROM pii WHERE conv_id=?', (conv_id,))}
    counters: dict[str, int] = {}
    for r in existing.values():
        k = r.strip('[]').rsplit('_', 1)[0]
        counters[k] = counters.get(k, 0) + 1

    def repl_factory(kind: str, label: str):
        def repl(m: re.Match[str]) -> str:
            value = m.group(0).strip()
            if not value:
                return m.group(0)
            if value in existing:
                return existing[value]
            counters[label] = counters.get(label, 0) + 1
            token = f'[{label}_{counters[label]}]'
            existing[value] = token
            db.execute('INSERT OR IGNORE INTO pii (conv_id, token, value, kind) VALUES (?, ?, ?, ?)', (conv_id, token, value, kind))
            return token
        return repl

    for kind, label, rx in PATTERNS:
        text = rx.sub(repl_factory(kind, label), text)
    return text


def unmask(conv_id: str, text: str) -> str:
    for r in db.rows('SELECT token, value FROM pii WHERE conv_id=?', (conv_id,)):
        text = text.replace(r['token'], r['value'])
    return text
