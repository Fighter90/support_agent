"""Скидки, которые агент предлагает клиентам сам.

Правила заказчика (ответы организаторов 27.11):
- агент может сам предложить скидку до 5% даже без купона клиента — лимит хранится в настройках (agent_cap) и меняется
  менеджером в кабинете («Скидки»);
- кроме общего лимита менеджер ведёт таблицу акций: скидка на продукт, вендора, категорию или конкретный артикул
  (процент согласован менеджером, поэтому может быть больше лимита агента). Агент сам говорит клиенту о действующей
  акции, а в КП/счёте скидка подставляется кодом;
- отсрочка и постоплата — только менеджер (агент сразу передаёт обращение).
"""

from __future__ import annotations

import re
from typing import Any

from app import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS discounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, scope TEXT, match TEXT, percent REAL, active INTEGER DEFAULT 1,
  valid_until REAL, note TEXT DEFAULT '', created_by TEXT DEFAULT '', created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""
SCOPES = {'all': 'Все товары', 'vendor': 'Вендор', 'product': 'Продукт (линейка)', 'category': 'Категория', 'offer': 'Артикул'}
DEFAULT_CAP = 5.0  # заказчик: агент сам даёт до 5%, больше — менеджер
MAX_RULE = 50.0


class DiscountError(Exception):
    pass


def init() -> None:
    with db.conn() as c:
        c.executescript(SCHEMA)


def agent_cap() -> float:
    r = db.one("SELECT value FROM settings WHERE key='agent_discount_cap'")
    try:
        return float(r['value']) if r else DEFAULT_CAP
    except (TypeError, ValueError):
        return DEFAULT_CAP


def set_agent_cap(value: float) -> float:
    v = float(value)
    if not 0 <= v <= 15:
        raise DiscountError('Лимит агента — от 0 до 15%')
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('agent_discount_cap', ?)", (str(v),))
    return v


def rules(active_only: bool = False) -> list[dict[str, Any]]:
    sql = 'SELECT * FROM discounts' + (' WHERE active=1 AND (valid_until IS NULL OR valid_until>?)' if active_only else '') + ' ORDER BY id DESC'
    return db.rows(sql, (db.now(),) if active_only else ())


def save(rule: dict[str, Any], manager: str) -> dict[str, Any]:
    scope = rule.get('scope') or 'product'
    if scope not in SCOPES:
        raise DiscountError('Неизвестная область действия скидки')
    match = str(rule.get('match') or '').strip()
    if scope != 'all' and len(match) < 2:
        raise DiscountError('Укажите, к чему применяется скидка (вендор, продукт, категория или артикул)')
    try:
        pct = float(rule.get('percent'))
    except (TypeError, ValueError) as e:
        raise DiscountError('Процент скидки — число') from e
    if not 0 < pct <= MAX_RULE:
        raise DiscountError(f'Скидка — от 0 до {MAX_RULE:g}%')
    title = str(rule.get('title') or '').strip()[:120] or f'{SCOPES[scope]}: {match}'.strip(': ')
    vals = (title, scope, match[:200], pct, int(bool(rule.get('active', True))), rule.get('valid_until') or None,
            str(rule.get('note') or '')[:300], manager, db.now())
    if rule.get('id'):
        db.execute('UPDATE discounts SET title=?, scope=?, match=?, percent=?, active=?, valid_until=?, note=?, created_by=?, updated_at=? WHERE id=?',
                   (*vals, int(rule['id'])))
        rid = int(rule['id'])
    else:
        rid = db.execute('INSERT INTO discounts (title, scope, match, percent, active, valid_until, note, created_by, updated_at, created_at) '
                         'VALUES (?,?,?,?,?,?,?,?,?,?)', (*vals, db.now()))
    return db.one('SELECT * FROM discounts WHERE id=?', (rid,))


def delete(rid: int) -> None:
    db.execute('DELETE FROM discounts WHERE id=?', (rid,))


def _hit(rule: dict[str, Any], o: dict[str, Any]) -> bool:
    m = (rule.get('match') or '').lower().strip()
    sc = rule['scope']
    if sc == 'all':
        return True
    if sc == 'offer':
        return str(o.get('id') or o.get('offer_id') or '') in re.findall(r'\d+', m)
    field = {'vendor': 'vendor', 'product': 'product', 'category': 'category_path'}[sc]
    val = str(o.get(field) or (o.get('category') if sc == 'category' else '') or '').lower()
    if sc == 'product' and m.isdigit():
        return str(o.get('group_id') or '') == m
    return bool(m) and m in val


def best_for(o: dict[str, Any], active: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Самая выгодная для клиента действующая акция для строки фида/результата поиска."""
    best = None
    for r in (active if active is not None else rules(active_only=True)):
        if _hit(r, o) and (best is None or r['percent'] > best['percent']):
            best = r
    return {'percent': best['percent'], 'title': best['title'], 'id': best['id']} if best else None


def setting(key: str, default: float) -> float:
    r = db.one('SELECT value FROM settings WHERE key=?', (key,))
    try:
        return float(r['value']) if r else default
    except (TypeError, ValueError):
        return default


def set_setting(key: str, value: float) -> None:
    db.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, str(value)))


SLA_DEFAULT = 120.0  # регламент: сейчас менеджер отвечает в среднем за 2 мин до первого ответа (данные заказчика)


def sla_seconds() -> float:
    return setting('sla_manager_seconds', SLA_DEFAULT)
