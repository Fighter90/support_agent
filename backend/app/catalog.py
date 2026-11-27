"""Каталог store.softline.ru: загрузка товарного фида (YML Mindbox) и поиск.

- Фид разбирается потоково (lxml.iterparse): 136 тыс. предложений, ~300 МБ XML, память не растёт.
- Результат пишется во временный файл и атомарно подменяет catalog.db — сайт не останавливается.
- group_id в фиде = id листовой категории = продуктовая линейка («MDaemon», «Kaspersky Endpoint Security…»).
  Все предложения группы — это её версии/редакции/сроки: на этом построена сверка версий.
- Поиск: SQLite FTS5 по названию, вендору, пути категорий и описанию + ранжирование bm25,
  наличие и цена поднимают предложение выше. Смысловую переформулировку запроса делает агент (LLM).
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

import httpx
from lxml import etree

from app.config import get_settings

logger = logging.getLogger(__name__)

NONE_VALUES = {'другое или отсутствует', '', 'нет'}
PARAM_FIELDS = {
    'Тип организации': 'org_type',
    'Пользователь': 'user_type',
    'Срок действия': 'term',
    'К-во пользователей до': 'users_max',
    'К-во узлов': 'nodes',
    'Тип лицензии': 'license_type',
    'Совместимая архитектура/платформа': 'platform',
    'Номер записи в реестре отечественного ПО': 'registry',
    'Версия': 'version',
    'Тип поставки': 'delivery',
    'Лицензионная программа': 'program',
}

SCHEMA = """
CREATE TABLE offers (
  id INTEGER PRIMARY KEY, group_id INTEGER, name TEXT, vendor TEXT, url TEXT, price REAL, available INTEGER,
  category_id INTEGER, product TEXT, category_path TEXT, description TEXT, params TEXT,
  org_type TEXT, user_type TEXT, term TEXT, users_max TEXT, nodes TEXT, license_type TEXT, platform TEXT,
  registry TEXT, version TEXT, delivery TEXT, program TEXT
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""
POST_SCHEMA = """
CREATE INDEX ix_offers_group ON offers(group_id);
CREATE VIRTUAL TABLE offers_fts USING fts5(name, vendor, product, category_path, description,
  content='offers', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
INSERT INTO offers_fts(rowid, name, vendor, product, category_path, description)
  SELECT id, name, vendor, product, category_path, description FROM offers;
"""


def _open_source(src: str) -> io.BufferedReader | gzip.GzipFile:
    """Путь к файлу (xml или xml.gz) или URL фида."""
    if src.startswith('http'):
        tmp = Path(get_settings().data_dir) / 'feed.download'
        tmp.parent.mkdir(parents=True, exist_ok=True)
        with httpx.stream('GET', src, timeout=600, follow_redirects=True) as r:
            r.raise_for_status()
            with open(tmp, 'wb') as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        src = str(tmp)
    f = open(src, 'rb')
    head = f.read(2)
    f.seek(0)
    return gzip.GzipFile(fileobj=f) if head == b'\x1f\x8b' else f


def _categories(src: str) -> dict[int, tuple[str, int | None]]:
    cats: dict[int, tuple[str, int | None]] = {}
    with _open_source(src) as f:
        for _, el in etree.iterparse(f, events=('end',), tag=('category', 'offers'), recover=True, huge_tree=True):
            if el.tag == 'offers':
                break
            parent = el.get('parentId')
            cats[int(el.get('id'))] = ((el.text or '').strip().replace('​', ''), int(parent) if parent else None)
            el.clear()
    return cats


def _path(cats: dict[int, tuple[str, int | None]], cid: int | None) -> list[str]:
    out: list[str] = []
    seen = set()
    while cid is not None and cid in cats and cid not in seen:
        seen.add(cid)
        name, cid = cats[cid]
        out.append(name)
    return list(reversed(out))


def _offers(src: str, cats: dict[int, tuple[str, int | None]]) -> Iterable[tuple]:
    with _open_source(src) as f:
        for _, el in etree.iterparse(f, events=('end',), tag='offer', recover=True, huge_tree=True):
            try:
                g = el.findtext('group_id') or el.get('group_id') or '0'
                gid = int(g) if g.isdigit() else 0
                cid_t = el.findtext('categoryId') or ''
                cid = int(cid_t) if cid_t.isdigit() else None
                params: dict[str, list[str]] = {}
                for p in el.findall('param'):
                    v = (p.text or '').strip()
                    if v.lower() not in NONE_VALUES:
                        params.setdefault(p.get('name', ''), []).append(v)
                fields = {col: ', '.join(params.get(name, [])) for name, col in PARAM_FIELDS.items()}
                product = cats.get(gid, ('', None))[0] or (cats.get(cid, ('', None))[0] if cid else '')
                path = ' / '.join(_path(cats, gid if gid in cats else cid))
                price = float(el.findtext('price') or 0)
                yield (
                    int(el.get('id')), gid, (el.findtext('name') or '').strip(), (el.findtext('vendor') or '').strip(),
                    (el.findtext('url') or '').strip(), price, 1 if el.get('available') == 'true' else 0, cid, product,
                    path, (el.findtext('description') or '').strip(), json.dumps(params, ensure_ascii=False),
                    *[fields[c] for c in PARAM_FIELDS.values()],
                )
            finally:
                el.clear()
                while el.getprevious() is not None:
                    del el.getparent()[0]


def ingest(src: str | None = None) -> dict[str, Any]:
    """Полная пересборка каталога из фида. Возвращает статистику."""
    s = get_settings()
    src = src or s.feed_url
    t0 = time.time()
    if src.startswith('http'):  # скачиваем один раз, разбираем дважды (категории и предложения)
        f = _open_source(src)
        f.close()
        src = str(Path(s.data_dir) / 'feed.download')
    cats = _categories(src)
    tmp = s.catalog_db.with_suffix('.tmp')
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)
    cols = 12 + len(PARAM_FIELDS)
    batch: list[tuple] = []
    n = 0
    for row in _offers(src, cats):
        batch.append(row)
        if len(batch) >= 5000:
            con.executemany(f'INSERT OR REPLACE INTO offers VALUES ({",".join("?" * cols)})', batch)
            n += len(batch)
            batch.clear()
    if batch:
        con.executemany(f'INSERT OR REPLACE INTO offers VALUES ({",".join("?" * cols)})', batch)
        n += len(batch)
    con.executescript(POST_SCHEMA)
    stats = {
        'offers': n,
        'categories': len(cats),
        'available_priced': con.execute('SELECT count(*) FROM offers WHERE price>0').fetchone()[0],  # правило заказчика: есть цена — в наличии
        'domestic': con.execute("SELECT count(*) FROM offers WHERE registry<>''").fetchone()[0],
        'loaded_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'seconds': round(time.time() - t0, 1),
        'priced_rule': True,
    }
    con.execute('INSERT INTO meta VALUES (?, ?)', ('stats', json.dumps(stats, ensure_ascii=False)))
    con.commit()
    con.close()
    os.replace(tmp, s.catalog_db)
    dl = Path(s.data_dir) / 'feed.download'
    if dl.exists() and str(dl) == src:
        dl.unlink()  # скачанный фид (~290 МБ) после разбора не нужен
    logger.info('Каталог загружен: %s', stats)
    return stats


# ---------------------------------------------------------------- поиск

def _db() -> sqlite3.Connection:
    con = sqlite3.connect(get_settings().catalog_db, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


_STATS_CACHE: dict[float, dict[str, Any]] = {}


def stats() -> dict[str, Any]:
    p = get_settings().catalog_db
    if not p.exists() or p.stat().st_size == 0:
        return {}
    key = p.stat().st_mtime
    if key in _STATS_CACHE:
        return dict(_STATS_CACHE[key])
    try:
        con = sqlite3.connect(f'file:{p}?mode=ro', uri=True)  # только чтение: не создаёт пустой файл
        try:
            r = con.execute("SELECT value FROM meta WHERE key='stats'").fetchone()
            st = json.loads(r[0]) if r else {}
            if st and not st.get('priced_rule'):  # индекс собран до правила «есть цена — в наличии»: пересчитываем
                st['available_priced'] = con.execute('SELECT count(*) FROM offers WHERE price>0').fetchone()[0]
                st['priced_rule'] = True
            _STATS_CACHE.clear()
            _STATS_CACHE[key] = st
            return dict(st)
        finally:
            con.close()
    except sqlite3.Error:
        return {}


_WORD = re.compile(r'[\w\.]+', re.UNICODE)
_STOP = {'для', 'на', 'и', 'в', 'с', 'по', 'нужен', 'нужна', 'нужно', 'купить', 'лицензия', 'лицензии', 'хочу', 'есть',
         'ли', 'а', 'или', 'как', 'мне', 'нам', 'под', 'от', 'до', 'шт', 'the', 'for', 'of', 'сколько', 'стоит',
         'можно', 'какой', 'какая', 'какие', 'что', 'это', 'ваш', 'вас', 'у', 'не', 'подскажите', 'пожалуйста',
         'здравствуйте', 'добрый', 'день', 'нас', 'мы', 'я', 'так', 'же', 'бы', 'ещё', 'еще', 'надо'}


# Как клиенты пишут названия по-русски → как они записаны в фиде
TRANSLIT = {'линукс': 'linux', 'линкус': 'linux', 'астра': 'astra', 'касперский': 'kaspersky', 'касперского': 'kaspersky',
            'майкрософт': 'microsoft', 'офис': 'office', 'ворд': 'word', 'эксель': 'excel', 'виндовс': 'windows',
            'виндоус': 'windows', 'адоб': 'adobe', 'фотошоп': 'photoshop', 'автокад': 'autocad', 'мдемон': 'mdaemon',
            'эмдемон': 'mdaemon', 'доктор': 'dr.web', 'драйвер': 'driver', 'зум': 'zoom', 'виндовз': 'windows',
            'ред': 'ред', 'альт': 'альт', 'вмваре': 'vmware', 'оракл': 'oracle', 'аутлук': 'outlook', 'тимс': 'teams'}


def _terms(query: str) -> list[str]:
    out = []
    low = query.lower()
    for ru, en in TRANSLIT.items():
        if ru != en:
            low = re.sub(rf'(?<![\w]){ru}\w*', en, low)
    for w in _WORD.findall(low):
        w = w.strip('.-')
        if len(w) < 2 or w in _STOP:
            continue
        if re.fullmatch(r'[а-яё]+', w) and len(w) > 4:
            w = w[: max(4, len(w) - 3)]  # грубый стемминг: «антивирусы» → «антивир*», «офисный» → «офис*»
        out.append('"' + w.replace('"', '') + '"*')
    return out[:8]


RU_VENDORS = ['Касперск', 'Доктор Веб', 'РЕД СОФТ', 'Астра', 'BaseALT', 'Базальт', 'МойОфис', 'Новые облачные технологии', 'Р7',
              'Positive Technologies', 'Код безопасности', 'КриптоПро', 'Киберпротект', 'InfoWatch', 'Солар', 'SearchInform',
              'Лаборатория МБК', 'РОСА', 'Конфидент', 'Аладдин', 'Гарда', 'UserGate', 'Индид', 'Content AI', '1С', 'Мобильные ТелеСистемы',
              'Яндекс', 'VK', 'Postgres Professional', 'Мой Офис', 'Атом', 'Крипто-Про', 'СёрчИнформ', 'Газинформсервис', 'Ideco',
              'Киберпротект', 'Эшелон', 'ИнфоТеКС', 'Security Code', 'Kaspersky', 'Dr.Web', 'Avanpost', 'Ростелеком']


def is_domestic(vendor: str, registry: str, path: str = '') -> bool:
    return bool(registry) or 'Импортозамещ' in path or any(v.lower() in (vendor or '').lower() for v in RU_VENDORS)


def _where(filters: dict[str, Any] | None) -> tuple[str, list[Any]]:
    f = filters or {}
    sql, args = [], []
    if f.get('only_available', True):
        # Правило заказчика (встреча): «товар с ценой — в наличии, без цены — запрос цены через менеджера».
        # Флаг available в фиде у ~11 тыс. позиций с ценой равен false, поэтому наличие определяем по цене.
        sql.append('o.price>0')
    if f.get('only_priced'):
        sql.append('o.price>0')
    if f.get('only_domestic'):
        # В фиде номер реестра Минцифры заполнен не у всех российских продуктов: добавляем крупных российских вендоров
        sql.append("(o.registry<>'' OR o.category_path LIKE '%Импортозамещ%' OR " + ' OR '.join(["o.vendor LIKE ?"] * len(RU_VENDORS)) + ')')
        args.extend(f'%{v}%' for v in RU_VENDORS)
    for key in ('org_type', 'user_type', 'term', 'license_type'):
        if f.get(key):
            sql.append(f'o.{key} LIKE ?')
            args.append(f'%{f[key]}%')
    if f.get('vendor'):
        sql.append('o.vendor LIKE ?')
        args.append(f'%{f["vendor"]}%')
    if f.get('users'):
        # «К-во пользователей до»: берём редакции, которые покрывают нужное число пользователей
        sql.append("(o.users_max='' OR CAST(o.users_max AS INTEGER) >= ?)")
        args.append(int(f['users']))
    return (' AND ' + ' AND '.join(sql)) if sql else '', args


def _add(results: dict[int, dict[str, Any]], r: sqlite3.Row, limit: int) -> None:
    """Добавить строку фида в выдачу, сгруппировав по продуктовой линейке."""
    g = r['group_id'] or r['id']
    if g not in results:
        if len(results) >= limit:
            return
        results[g] = {
            'group_id': g, 'product': r['product'] or r['name'], 'vendor': r['vendor'],
            'category': ' / '.join(r['category_path'].split(' / ')[-3:-1]),
            'url': r['url'], 'domestic': is_domestic(r['vendor'], r['registry'], r['category_path']),
            'registry': (f"запись в реестре отечественного ПО № {r['registry']}" if r['registry'] else
                         ('российский вендор; номер записи в реестре в каталоге не указан' if is_domestic(r['vendor'], '', r['category_path']) else '')),
            'matched_offers': [], 'prices': [],
        }
    item = results[g]
    if len(item['matched_offers']) < 3:
        item['matched_offers'].append({'offer_id': r['id'], 'name': r['name'], 'price_rub': r['price'] or None})
    if r['price']:
        item['prices'].append(r['price'])


_URL_ID = re.compile(r'store\.softline\.ru/[^\s]*?-(\d{3,9})/?')


# Сценарий «клиент не знает продукт»: класс ПО из запроса → ведущие линейки этого класса в каталоге Softline.
# Нужен, потому что названия продуктов часто не содержат слов клиента («офисный пакет» ≠ «МойОфис»).
INTENTS = [
    (r'офисн\w* (пакет|програм)|офис(?!\w)|редактор\w* документ', 'МойОфис Стандартный|Р7-Офис'),
    (r'(операционн\w* систем|(?<!\w)ос(?!\w))', 'Astra Linux Special Edition|Операционная система РЕД ОС|Альт Рабочая станция'),
    (r'антивирус|защит\w* (компьютер|рабоч|пк|конечн)', 'Kaspersky Endpoint Security для бизнеса|Dr.Web Desktop Security Suite'),
    (r'резервн\w* копир|бэкап|backup', 'Кибер Бэкап|RuBackup'),
    (r'несанкционирован|(?<!\w)нсд(?!\w)|сзи', 'Secret Net Studio|Dallas Lock'),
    (r'мониторинг\w* (сет|сервер|инфраструкт)', 'Astra Monitoring|10-Страйк Мониторинг Сети|OpManager'),
    (r'видеоконференц|(?<!\w)вкс(?!\w)', 'TrueConf|Контур Толк'),
    (r'3d|трехмерн|трёхмерн|конструктор|чертеж|чертёж', 'КОМПАС-3D|nanoCAD'),
]


def search(query: str, filters: dict[str, Any] | None = None, limit: int = 8) -> list[dict[str, Any]]:
    """Поиск с учётом класса ПО (INTENTS): сначала ведущие линейки класса, затем обычная выдача по словам запроса."""
    low = query.lower()
    extra: list[dict[str, Any]] = []
    for pat, words in INTENTS:
        if re.search(pat, low):
            for phrase in words.split('|'):
                extra += [r for r in _search(phrase, filters, 1) if r not in extra]
    base = _search(query, filters, limit)
    seen, out = set(), []
    exact = base[:1] if base and re.search(r'(?<!\w)' + re.escape(base[0]['product'].lower()) + r'(?!\w)', low) else []
    for r in [*exact, *extra[:4], *base]:  # продукт, названный клиентом, остаётся первым
        if r['group_id'] not in seen:
            seen.add(r['group_id'])
            out.append(r)
    return out[:limit]


def _search(query: str, filters: dict[str, Any] | None = None, limit: int = 8, joiner_only: str = '') -> list[dict[str, Any]]:
    """Продуктовые линейки (группы) по запросу: название, вендор, класс, диапазон цен, пример предложений.
    Ссылка на карточку store.softline.ru распознаётся по артикулу в конце URL (…-413411/)."""
    m = _URL_ID.search(query)
    if m:
        o = get_offers([int(m.group(1))]).get(int(m.group(1)))
        if o:  # ищем ровно продуктовую линейку этого артикула
            query = f'"{o["vendor"]}" ' + (o['product'] or o['name'])
            filters = {**(filters or {}), 'only_available': False}
    terms = _terms(_URL_ID.sub(' ', query))
    if not terms:
        return []
    where, args = _where(filters)
    results: dict[int, dict[str, Any]] = {}
    with _db() as c:
        # 1) Клиент знает продукт: точное название предложения (часто копируют с сайта, в кавычках) — первым результатом
        exact: list[sqlite3.Row] = []
        if m:  # ссылка на карточку: ровно этот артикул — первым (одноимённых предложений в фиде много)
            exact = c.execute('SELECT o.* FROM offers o WHERE o.id=?', (int(m.group(1)),)).fetchall()
        for cand in ([] if exact else [*re.findall(r'[«"“]([^»"”]{6,300})[»"”]', query), query.strip(' ?!.')]):
            exact = c.execute('SELECT o.* FROM offers o WHERE lower(o.name) = lower(?) LIMIT 3', (cand.strip(),)).fetchall()
            if not exact:  # название продуктовой линейки целиком («Сколько стоит Faronics Campus Affairs?», «… BIMIT?»)
                low = cand.lower()
                exact = [r for r in c.execute('SELECT o.* FROM offers o WHERE instr(?, lower(o.product)) > 0 AND length(o.product) >= 4 '
                                              'ORDER BY length(o.product) DESC, (o.price=0) LIMIT 20', (low,)).fetchall()
                         if re.search(r'(?<!\w)' + re.escape(r['product'].lower()) + r'(?!\w)', low)][:1]
            if exact:
                break
        for r in exact:
            _add(results, r, limit + 1)
        if m and exact:  # подсказка агенту: это ровно та позиция, на которую дал ссылку клиент
            results[exact[0]['group_id'] or exact[0]['id']]['matched_offers'][0]['from_client_link'] = True
        for joiner in ((joiner_only,) if joiner_only else (' AND ', ' OR ')):
            if len(results) >= limit:
                break
            q = joiner.join(terms)
            sql = f"""SELECT o.*, bm25(offers_fts, 6.0, 4.0, 5.0, 2.0, 1.0) AS rank FROM offers_fts
                      JOIN offers o ON o.id = offers_fts.rowid
                      WHERE offers_fts MATCH ? {where}
                      ORDER BY rank - (o.price>0)*0.5 + (o.group_id=0)*3 LIMIT 400"""
            try:
                found = c.execute(sql, [q, *args]).fetchall()
            except sqlite3.OperationalError:
                continue
            for r in found:
                _add(results, r, limit)
        for item in results.values():
            agg = c.execute('SELECT count(*), sum(price>0), min(CASE WHEN price>0 THEN price END), '
                            'max(CASE WHEN price>0 THEN price END) FROM offers WHERE group_id=? OR (group_id=0 AND id=?)',
                            (item['group_id'], item['group_id'])).fetchone()
            item['variants_total'], item['variants_available'] = agg[0], agg[1] or 0
            item['price_from_rub'], item['price_to_rub'] = agg[2], agg[3]
            item.pop('prices')
    return list(results.values())


VARIANT_COLS = ('id', 'name', 'price', 'available', 'url', 'org_type', 'user_type', 'term', 'users_max', 'nodes',
                'license_type', 'platform', 'version', 'delivery', 'registry')


def variants(group_id: int | None = None, offer_id: int | None = None, filters: dict[str, Any] | None = None,
             text: str = '', limit: int = 25) -> dict[str, Any]:
    """Все версии/редакции продуктовой линейки с ценами и параметрами (сверка версий)."""
    with _db() as c:
        if offer_id and not group_id:
            r = c.execute('SELECT group_id FROM offers WHERE id=?', (offer_id,)).fetchone()
            group_id = r[0] if r else None
        if not group_id:
            return {'error': 'Продукт не найден'}
        f = dict(filters or {})
        f.setdefault('only_available', False)
        where, args = _where(f)
        if text:
            for t in _terms(text)[:4]:
                where += ' AND o.name LIKE ?'
                args.append('%' + t.strip('"*') + '%')
        rs = c.execute(f'SELECT o.* FROM offers o WHERE o.group_id=? {where} '
                       "ORDER BY (o.price=0), CASE WHEN o.users_max<>'' THEN CAST(o.users_max AS INTEGER) END, o.price LIMIT ?", [group_id, *args, limit]).fetchall()
        total = c.execute('SELECT count(*), max(product), max(vendor) FROM offers WHERE group_id=?', (group_id,)).fetchone()
        facets: dict[str, list[str]] = {}
        for col in ('org_type', 'user_type', 'term', 'license_type', 'version'):
            vals = [v[0] for v in c.execute(f"SELECT DISTINCT {col} FROM offers WHERE group_id=? AND {col}<>'' LIMIT 12", (group_id,))]
            if len(vals) > 1:
                facets[col] = vals
    out = []
    for r in rs:
        d = {k: r[k] for k in VARIANT_COLS if r[k] not in ('', None)}
        d['offer_id'] = d.pop('id')
        d['price_rub'] = d.pop('price') or None
        d['available'] = bool(r['price'])
        if not d['price_rub']:
            d['price_note'] = 'цена по запросу у менеджера'
        out.append(d)
    return {'group_id': group_id, 'product': total[1], 'vendor': total[2], 'variants_total': total[0],
            'shown': len(out), 'differences': facets, 'variants': out}


def get_offers(ids: list[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    with _db() as c:
        q = ','.join('?' * len(ids))
        return {r['id']: dict(r) for r in c.execute(f'SELECT * FROM offers WHERE id IN ({q})', ids)}


def known_prices(ids: list[int]) -> set[float]:
    return {float(o['price']) for o in get_offers(ids).values() if o['price']}


def ingest_isolated(src: str | None = None, timeout: int = 1800) -> dict[str, Any]:
    """Пересборка каталога в отдельном процессе: разбор фида (~300 МБ XML) занимает сотни мегабайт памяти,
    а Python не возвращает её системе. В отдельном процессе память освобождается сразу после загрузки,
    веб-сервер остаётся лёгким (иначе он уходит в swap и отвечает 504)."""
    import json as _json
    import subprocess
    import sys

    code = 'import json,sys; from app import catalog; print(json.dumps(catalog.ingest(sys.argv[1] or None), ensure_ascii=False, default=str))'
    r = subprocess.run([sys.executable, '-c', code, src or ''], capture_output=True, text=True, timeout=timeout,
                       cwd=str(Path(__file__).resolve().parents[1]))
    if r.returncode != 0:
        raise RuntimeError(f'Загрузка каталога завершилась с ошибкой: {r.stderr[-500:]}')
    return _json.loads(r.stdout.strip().splitlines()[-1])
