"""Карточка товара на store.softline.ru: описание, характеристики, системные требования, доставка.

Ответ организаторов: если ответа нет в базах знаний, можно брать сведения с карточки товара на сайте (правила
лицензирования, системные требования), когда уверенность высокая. Карточка отрисована на сервере, поэтому разбор —
по заголовкам блоков. Кэш 24 ч, таймаут 8 с; при недоступности сайта — данные фида.
"""

from __future__ import annotations

import html
import re
import time
from typing import Any

import httpx

from app import catalog

_CACHE: dict[int, tuple[float, dict[str, Any]]] = {}
SECTIONS = [  # (заголовок блока на странице, где он заканчивается)
    ('Описание Подробнее', ('Характеристики Подробнее', 'Сравнение Подробнее', 'Отзывы')),
    ('Характеристики Подробнее', ('Сравнение Подробнее', 'Отзывы', 'Системные требования')),
    ('Системные требования', ('Доставка и логистика', 'Артикул')),
    ('Доставка и логистика', ('Артикул', 'Информация на сайте')),
    ('лицензирован', ('Характеристики', 'Системные требования', 'Отзывы')),
]


def _text(page: str) -> str:
    t = re.sub(r'<script.*?</script>|<style.*?</style>|<noscript.*?</noscript>', ' ', page, flags=re.S)
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', t))).strip()


def _cut(text: str, start: str, ends: tuple[str, ...], limit: int) -> str:
    i = text.find(start)
    if i < 0:
        return ''
    j = min([k for k in (text.find(e, i + len(start)) for e in ends) if k > 0] or [i + limit])
    return text[i + len(start): min(j, i + limit)].strip(' :•')


def get(offer_id: int) -> dict[str, Any]:
    o = catalog.get_offers([offer_id]).get(offer_id)
    if not o:
        return {'error': 'Позиция не найдена в каталоге'}
    hit = _CACHE.get(offer_id)
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    card: dict[str, Any] = {'offer_id': offer_id, 'name': o['name'], 'vendor': o['vendor'], 'url': o['url'],
                            'feed': {k: o.get(k) for k in ('description', 'platform', 'license_type', 'term', 'delivery', 'version', 'registry') if o.get(k)}}
    try:
        r = httpx.get(o['url'], timeout=8, follow_redirects=True, headers={'User-Agent': 'Mozilla/5.0 (support-agent)'})
        if r.status_code == 200:
            text = _text(r.text)
            names = {'Описание Подробнее': 'description', 'Характеристики Подробнее': 'characteristics',
                     'Системные требования': 'system_requirements', 'Доставка и логистика': 'delivery', 'лицензирован': 'licensing'}
            for start, ends in SECTIONS:
                part = _cut(text, start, ends, 1500 if start != 'лицензирован' else 800)
                if part:
                    card[names[start]] = part
            card['source'] = 'карточка товара на store.softline.ru'
            vat = vat_from_text(text)
            if vat is not None:
                card['vat_percent'] = vat  # метка «Возврат НДС 22%» / «Без НДС» на карточке (ответ заказчика 27.11)
    except httpx.HTTPError as e:
        card['site_error'] = f'сайт недоступен: {e.__class__.__name__}; использованы данные фида'
    _CACHE[offer_id] = (time.time(), card)
    return card


def vat_from_text(text: str) -> int | None:
    """Ставка НДС по метке карточки: «Возврат НДС 22%» → 22, «Без НДС» / «НДС не облагается» → 0, нет метки → None."""
    # только метка карточки: «Возврат НДС 22%» / «22% НДС можно вернуть»; «Размер НДС (22%)» — шаблон модального окна на всех страницах
    m = re.search(r'Возврат\s+НДС\s*(\d{1,2})\s*%', text) or re.search(r'(\d{1,2})\s*%\s*НДС\s+можно\s+вернуть', text)
    if m:
        return int(m.group(1))
    if re.search(r'Без\s+НДС|НДС\s+не\s+облага', text, re.I):
        return 0
    return None


def vat_percent(offer_id: int) -> int | None:
    """Ставка из карточки (кэш 24 ч); сайт недоступен — None, тогда правило по реестру Минцифры."""
    try:
        return get(offer_id).get('vat_percent')
    except Exception:  # noqa: BLE001
        return None
