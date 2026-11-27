"""RAGAS-оценка RAG по каталогу (фид Mindbox store.softline.ru) с GigaChat в роли судьи.

Источник знаний — фид https://store.softline.ru/feeds/Mindbox.yml.gz (~136 тыс. предложений), который
приложение загружает в индекс SQLite FTS5 и обновляет раз в сутки. Агент извлекает из него контекст
инструментами search_catalog (продуктовые линейки) и get_product_variants (редакции, версии, цены).

Набор вопросов строится из самого фида, как в генераторе тестсетов RAGAS (seed фиксирован, набор воспроизводим):
  price       — «Сколько стоит <конкретное предложение>?», эталон — цена из фида (сценарий «клиент знает продукт»);
  url         — клиент присылает ссылку на карточку товара; у 42% предложений фида название не уникально, точную позицию
                определяет только артикул в ссылке — эталон — цена этого артикула;
  editions    — «Какие есть варианты <продукт> и от какой цены?», эталон — минимальная цена линейки (сверка версий);
  vendor      — «Чей это продукт <продукт>?», эталон — вендор из фида;
  no_price    — товар без цены в фиде: эталон — цену не называть, передать менеджеру (правило заказчика);
  need        — сценарий «клиент не знает продукт»: потребность → допустимые вендоры/классы (ручная разметка).

Метрики (0..1), определения как в RAGAS (Es et al., 2023):
  context_recall     — эталонная линейка (group_id) есть среди извлечённых контекстов (для need — нужный класс/вендор);
  context_precision  — судья помечает относящиеся к вопросу контексты, считается rank-aware precision@k;
  faithfulness       — судья раскладывает ответ на утверждения и проверяет каждое по извлечённому контексту;
  answer_relevancy   — насколько ответ отвечает на вопрос (судья, 1–5 → 0..1);
  answer_correctness — совпадение с эталоном: цена/вендор детерминированно, для need/editions — судья;
  price_faithfulness — детерминированно: доля цен в ответе, которые есть в фиде (цены не выдумываются).
Запуск: python eval/catalog_rag_eval.py [N_на_категорию]   Отчёт: eval/catalog_rag_report.json
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from app import agent, catalog, db, kb, llm  # noqa: E402
from app.config import get_settings  # noqa: E402

NEED = [  # сценарий «не знаю, что нужно»: (вопрос, допустимые подстроки вендора/продукта/категории)
    ('Нужен российский антивирус для 20 компьютеров в офисе, что посоветуете?', ['касперск', 'kaspersky', 'доктор веб', 'dr.web', 'pro32']),
    ('Ищем отечественную операционную систему для рабочих мест вместо Windows', ['astra', 'астра', 'ред ос', 'альт', 'базальт', 'rosa', 'роса']),
    ('Нужна программа для резервного копирования серверов, желательно из реестра', ['кибер бэкап', 'киберпротект', 'rubackup', 'backup', 'бэкап']),
    ('Что есть для 3D-проектирования для инженеров-конструкторов?', ['компас', 'nanocad', 'autocad', 'аскон', 'cad', '3d']),
    ('Нужна защита почтового сервера от спама и вирусов', ['почтов', 'mail', 'anti-spam']),
    ('Нам нужен офисный пакет, российский, на 10 сотрудников', ['мойофис', 'р7', 'офис', 'office']),
    ('Нужно средство защиты информации от несанкционированного доступа для ГИС', ['secret net', 'код безопасности', 'dallas', 'страж', 'сзи']),
    ('Посоветуйте систему мониторинга сети и серверов', ['opmanager', 'мониторинг', 'monitoring', 'zabbix', 'site24x7']),
]

FOLLOW_UP = 'Коммерческая компания, 25 сотрудников, Windows и пара Linux-серверов, лучше российское решение, бюджет умеренный.'

J_PREC = ('Вопрос клиента: «{q}»\nНайденные позиции каталога (по порядку выдачи):\n{ctx}\n\n'
          'Для каждой позиции укажи, относится ли она к вопросу (помогает ответить). '
          'Ответь строго JSON: {{"relevant": [true|false, ...]}} — ровно {n} значений.')
J_FAITH = ('Контекст (позиции каталога с ценами):\n{ctx}\n\nОтвет ассистента:\n{a}\n\n'
           'Разбей ответ на фактические утверждения о товарах, ценах, вендорах, редакциях (без приветствий, вопросов клиенту и '
           'общих фраз о процессе покупки) и для каждого укажи, подтверждается ли оно контекстом. '
           'Ответь строго JSON: {{"claims": [{{"claim": "...", "supported": true|false}}]}}')
J_REL = ('Вопрос клиента: «{q}»\nОтвет ассистента: «{a}»\n'
         'Насколько ответ по существу отвечает на вопрос (уточняющий вопрос по делу тоже считается частичным ответом)? '
         '5 — полностью, 1 — не отвечает. Ответь строго JSON: {{"score": 1..5}}')
J_CORR = ('Вопрос клиента: «{q}»\nЭталон (из каталога магазина): «{ref}»\nОтвет ассистента: «{a}»\n'
          'Соответствует ли ответ эталону по существу (не противоречит и содержит ключевой факт)? 5 — полностью, 1 — нет. '
          'Ответь строго JSON: {{"score": 1..5}}')


def judge(prompt: str) -> dict:
    r = llm.provider('gigachat').chat([{'role': 'system', 'content': 'Ты строгий оценщик качества RAG-системы. Отвечай только JSON.'},
                                       {'role': 'user', 'content': prompt}], [], temperature=0.0)
    t = r.text.strip().strip('`')
    t = t[t.find('{'): t.rfind('}') + 1]
    try:
        return json.loads(t)
    except ValueError:
        return {}


def _num(s: str) -> float:
    return float(s.replace(' ', '').replace(' ', '').replace(' ', '').replace(',', '.'))


PRICE_RE = re.compile(r'(\d[\d   ]{0,12}(?:[.,]\d{1,2})?)\s*(?:₽|руб)', re.I)


def prices_in(text: str) -> list[float]:
    out = []
    for m in PRICE_RE.findall(text):
        try:
            v = _num(m)
            if v >= 10:
                out.append(v)
        except ValueError:
            pass
    return out


def build_testset(n: int, seed: int = 42) -> list[dict]:
    rnd = random.Random(seed)
    c = sqlite3.connect(f'file:{get_settings().catalog_db}?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    groups = c.execute("""SELECT group_id, product, vendor, MIN(price) mn, COUNT(*) cnt FROM offers
                          WHERE price > 0 AND group_id > 0 AND product <> '' AND vendor <> 'UBIQUITI'
                          GROUP BY group_id HAVING cnt BETWEEN 2 AND 60""").fetchall()
    rnd.shuffle(groups)
    dups = {r[0] for r in c.execute('SELECT name FROM offers GROUP BY name HAVING count(*) > 1')}
    items: list[dict] = []
    for g in groups[:n * 2]:  # точная цена конкретного предложения (до n штук)
        if sum(i['type'] == 'price' for i in items) >= n:
            break
        # только предложения с уникальным названием: в фиде встречаются одноимённые позиции с разными ценами
        # (различаются лишь параметрами) — для них корректный ответ «от …» или уточняющий вопрос, а не одна цена
        offs = [o for o in c.execute('SELECT id, name, price FROM offers WHERE group_id=? AND price>0 ORDER BY id', (g['group_id'],))
                if o['name'] not in dups]
        if not offs:
            continue
        o = offs[rnd.randrange(len(offs))]
        items.append({'type': 'price', 'q': f'Сколько стоит «{o["name"]}»?', 'group_id': g['group_id'], 'offer_id': o['id'],
                      'ref': f'{o["name"]} — {o["price"]:.2f} ₽', 'ref_price': o['price']})
    # одноимённые предложения различаются только ссылкой: клиент присылает ссылку на карточку — нужна точная цена
    amb = c.execute('SELECT id, name, price, url, group_id FROM offers WHERE price > 0 AND url LIKE ? AND name IN '
                    '(SELECT name FROM offers GROUP BY name HAVING count(*) BETWEEN 2 AND 8) ORDER BY id', ('%store.softline.ru%',)).fetchall()
    for o in rnd.sample(list(amb), max(3, n // 2)):
        items.append({'type': 'url', 'q': f'Сколько стоит вот это: {o["url"]} ?', 'group_id': o['group_id'], 'offer_id': o['id'],
                      'ref': f'{o["name"]} (артикул {o["id"]}) — {o["price"]:.2f} ₽', 'ref_price': o['price']})
    for g in groups[n: 2 * n]:  # сверка версий/редакций
        items.append({'type': 'editions', 'q': f'Какие варианты {g["product"]} у вас есть и от какой цены?', 'group_id': g['group_id'],
                      'ref': f'{g["product"]} ({g["vendor"]}): {g["cnt"]} вариантов с ценой, минимальная цена {g["mn"]:.2f} ₽', 'ref_price': g['mn']})
    for g in groups[2 * n: 3 * n]:
        items.append({'type': 'vendor', 'q': f'Кто разработчик {g["product"]} и есть ли он у вас?', 'group_id': g['group_id'],
                      'ref': f'{g["product"]} — вендор {g["vendor"]}, есть в каталоге, цена от {g["mn"]:.2f} ₽', 'ref_vendor': g['vendor']})
    nop = c.execute("""SELECT group_id, product, vendor FROM offers WHERE group_id > 0 AND product <> '' GROUP BY group_id
                       HAVING MAX(COALESCE(price,0)) = 0""").fetchall()
    rnd.shuffle(nop)
    for g in nop[: max(3, n // 2)]:
        items.append({'type': 'no_price', 'q': f'Сколько стоит {g["product"]}?', 'group_id': g['group_id'],
                      'ref': f'{g["product"]} ({g["vendor"]}) есть в каталоге, но цены в фиде нет — цену сообщает менеджер'})
    for q, keys in NEED:
        items.append({'type': 'need', 'q': q, 'keys': keys, 'ref': 'Подходящие продукты: ' + ', '.join(keys[:4])})
    return items


def contexts(conv_id: str) -> tuple[list[dict], set[int], set[int]]:
    """Извлечённые агентом контексты в порядке выдачи + множества group_id и offer_id."""
    ctx, gids, oids = [], set(), set()
    for r in db.rows("SELECT tool, result FROM tool_calls WHERE conv_id=? AND tool IN ('search_catalog','get_product_variants') ORDER BY id", (conv_id,)):
        res = db.loads(r['result'], {}) or {}
        if r['tool'] == 'search_catalog':
            for it in res.get('results', []):
                gids.add(it['group_id'])
                oids.update(o['offer_id'] for o in it.get('matched_offers', []))
                pr = f'от {it["price_from_rub"]} до {it["price_to_rub"]} ₽' if it.get('price_from_rub') else 'цена по запросу'
                ctx.append({'kind': 'line', 'group_id': it['group_id'], 'vendor': it.get('vendor') or '',
                            'text': f'{it["product"]} | {it.get("vendor")} | {it.get("category")} | {pr} | {it.get("registry") or ""}'})
                for o in it.get('matched_offers', []):  # найденные предложения линейки — тоже часть контекста
                    ctx.append({'kind': 'offer', 'group_id': it['group_id'], 'vendor': it.get('vendor') or '',
                                'text': f'{o["name"]} — {o.get("price_rub") or "цена по запросу"} ₽'})
        else:
            if res.get('group_id'):
                gids.add(res['group_id'])
            for v in res.get('variants', []):
                oids.add(v['offer_id'])
                ctx.append({'kind': 'variant', 'group_id': res.get('group_id'), 'vendor': res.get('vendor') or '',
                            'text': f'{v["name"]} — {v.get("price_rub") or "цена по запросу"} ₽'})
    return ctx, gids, oids


def score(item: dict) -> dict:
    cid = agent.new_conversation()
    ans = agent.reply(cid, item['q'])['content']
    ctx, gids, oids = contexts(cid)
    turns = 1
    if item['type'] == 'need' and not ctx and '?' in ans:
        # Сценарий «не знаю, что нужно»: агент по алгоритму задания сначала выясняет требования — отвечаем как клиент
        ans = agent.reply(cid, FOLLOW_UP)['content']
        ctx, gids, oids = contexts(cid)
        turns = 2
    esc = bool(db.one('SELECT id FROM escalations WHERE conv_id=?', (cid,)))
    t = item['type']
    blob = ' '.join(c['text'].lower() for c in ctx)
    # context recall
    if t == 'need':
        recall = float(any(k in blob for k in item['keys']))
    else:  # оборудование без продуктовой линейки (group_id=0) сверяем по артикулу
        recall = float((item['group_id'] in gids and item['group_id'] != 0) or item.get('offer_id') in oids)
    # context precision (rank-aware), судья по top-5
    top = [c for c in ctx if c['kind'] != 'offer'][:5]
    cprec = 0.0
    if top:
        rel = judge(J_PREC.format(q=item['q'], ctx='\n'.join(f'{i}. {c["text"]}' for i, c in enumerate(top, 1)), n=len(top))).get('relevant') or []
        rel = [bool(x) for x in rel][: len(top)]
        hits, ps = 0, []
        for i, ok in enumerate(rel, 1):
            if ok:
                hits += 1
                ps.append(hits / i)
        cprec = sum(ps) / hits if hits else 0.0
    # faithfulness
    ctx_text = '\n'.join(f'- {c["text"]}' for c in ctx[:40]) or '(контекст не извлечён)'
    claims = judge(J_FAITH.format(ctx=ctx_text, a=ans[:1800])).get('claims') or []
    faith = sum(1 for c in claims if c.get('supported')) / len(claims) if claims else 1.0
    # детерминированная проверка цен по фиду
    feed_prices = set()
    for c in ctx:
        feed_prices.update(prices_in(c['text'].replace(' ₽', ' ₽ ')))
        feed_prices.update(float(x) for x in re.findall(r'(\d+(?:\.\d+)?) (?:до|₽)', c['text']))
    said = prices_in(ans)
    pf = (sum(1 for p in said if any(abs(p - f) < 0.6 or (f and abs(p / f - round(p / f)) < 1e-6 and p / f <= 1000) for f in feed_prices)) / len(said)) if said else 1.0
    rel_s = judge(J_REL.format(q=item['q'], a=ans[:1800])).get('score') or 1
    # answer correctness
    if t in ('price', 'url'):
        corr = float(any(abs(p - item['ref_price']) < 0.6 for p in said))
    elif t == 'vendor':
        v = item['ref_vendor'].lower().strip('«»"')
        corr = float(v[:6] in ans.lower() or any(w in ans.lower() for w in re.findall(r'\w{5,}', v)))
    elif t == 'no_price':
        corr = float(not said and (esc or 'менеджер' in ans.lower()))
    else:
        corr = (int(judge(J_CORR.format(q=item['q'], ref=item['ref'], a=ans[:1800])).get('score') or 1) - 1) / 4
    return {'type': t, 'question': item['q'], 'reference': item['ref'], 'answer': ans, 'contexts': [c['text'] for c in ctx[:5]],
            'escalated': esc, 'turns': turns, 'context_recall': recall, 'context_precision': round(cprec, 3), 'faithfulness': round(faith, 3),
            'answer_relevancy': round((int(rel_s) - 1) / 4, 3), 'answer_correctness': round(corr, 3), 'price_faithfulness': round(pf, 3),
            'prices_in_answer': said, 'unsupported': [c.get('claim') for c in claims if not c.get('supported')][:3]}


METRICS = ('context_recall', 'context_precision', 'faithfulness', 'answer_relevancy', 'answer_correctness', 'price_faithfulness')


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    db.init_db()
    kb.load_base()
    st = catalog.stats()
    items = build_testset(n)
    rows = []
    for it in items:
        try:
            r = score(it)
        except Exception as e:  # noqa: BLE001 — сбой одного вопроса не должен ронять прогон
            print('ERR', it['q'][:60], e, flush=True)
            continue
        rows.append(r)
        print(f"{r['type']:9} R={r['context_recall']:.0f} P={r['context_precision']:.2f} F={r['faithfulness']:.2f} "
              f"A={r['answer_relevancy']:.2f} C={r['answer_correctness']:.2f} PF={r['price_faithfulness']:.2f}  {it['q'][:60]}", flush=True)
    summary = {k: round(statistics.mean(x[k] for x in rows), 3) for k in METRICS}
    by_type = {t: {k: round(statistics.mean(x[k] for x in rows if x['type'] == t), 3) for k in METRICS} | {'n': sum(x['type'] == t for x in rows)}
               for t in dict.fromkeys(x['type'] for x in rows)}
    summary |= {'questions': len(rows), 'feed_offers': st.get('offers'), 'feed_loaded_at': st.get('loaded_at')}
    (Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / 'eval' / 'catalog_rag_report.json').write_text(
        json.dumps({'summary': summary, 'by_type': by_type, 'results': rows}, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps({'summary': summary, 'by_type': by_type}, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
