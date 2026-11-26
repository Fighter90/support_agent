"""Агент поддержки: цикл вызова инструментов (function calling) поверх GigaChat / YandexGPT.

Шаг диалога:
1) текст клиента маскируется (pii.mask) и сохраняется;
2) модель получает системный промпт, историю и 5 инструментов, вызывает их до max_agent_steps раз;
3) финальный ответ проходит проверку цен: каждая сумма в ₽ должна совпасть с ценой из фида
   (или цена × количество); иначе модель переписывает ответ, а при повторной ошибке цена заменяется
   на «уточнит менеджер»;
4) всё — вызовы модели, инструменты, токены, стоимость — пишется в журнал для кабинета менеджера.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from app import catalog, db, kb, llm, pii, quotes
from app.config import get_settings

logger = logging.getLogger(__name__)
PROMPT = (Path(__file__).parent / 'prompt.md').read_text(encoding='utf-8')

ORG = ['Коммерческая', 'Образовательная', 'Государственная', 'Некоммерческая']
REASONS = ['price_on_request', 'special_terms', 'tender', 'order_status', 'complex_config', 'not_found', 'client_request', 'other']
REASON_RU = {'price_on_request': 'Цена по запросу', 'special_terms': 'Скидка / спецусловия', 'tender': 'Госзакупки / тендер',
             'order_status': 'Вопрос по заказу', 'complex_config': 'Сложная конфигурация', 'not_found': 'Продукт не найден',
             'client_request': 'Клиент просит менеджера', 'other': 'Другое'}

TOOLS: list[dict[str, Any]] = [
    {'type': 'function', 'function': {
        'name': 'search_catalog',
        'description': 'Поиск продуктов в каталоге store.softline.ru (136 тыс. позиций). Возвращает продуктовые линейки: '
                       'group_id, название, вендор, класс, цену «от», число вариантов, признак реестра отечественного ПО.',
        'parameters': {'type': 'object', 'properties': {
            'query': {'type': 'string', 'description': 'Название продукта/вендора или ключевые слова задачи, например «антивирус почтовый сервер»'},
            'scenario': {'type': 'string', 'enum': ['known_product', 'need_selection'], 'description': 'Клиент назвал продукт или подбираем под задачу'},
            'only_domestic': {'type': 'boolean', 'description': 'Только ПО из реестра отечественного ПО (российские аналоги)'},
            'org_type': {'type': 'string', 'enum': ORG, 'description': 'Тип организации клиента, если известен'},
        }, 'required': ['query']}}},
    {'type': 'function', 'function': {
        'name': 'get_product_variants',
        'description': 'Версии и редакции продуктовой линейки (group_id из search_catalog) с ценами, сроками, числом пользователей, '
                       'типом организации и лицензии. Используй для сверки версий и выбора конкретного offer_id.',
        'parameters': {'type': 'object', 'properties': {
            'group_id': {'type': 'integer'},
            'users': {'type': 'integer', 'description': 'Нужное число пользователей/устройств'},
            'term': {'type': 'string', 'description': 'Срок, как в каталоге: «12 мес.», «24 мес.», «36 мес.», «Бессрочно»'},
            'org_type': {'type': 'string', 'enum': ORG},
            'license_type': {'type': 'string', 'description': '«Полная версия», «Продление», «Подписка»'},
            'text': {'type': 'string', 'description': 'Слова, которые должны быть в названии варианта, например «Standard»'},
        }, 'required': ['group_id']}}},
    {'type': 'function', 'function': {
        'name': 'search_knowledge',
        'description': 'База знаний магазина: оформление заказа, оплата, доставка, документы, НДС, физлица, скидки, возвраты, лицензирование на организацию.',
        'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']}}},
    {'type': 'function', 'function': {
        'name': 'create_quote',
        'description': 'Сформировать коммерческое предложение (document="quote") или счёт на оплату (document="invoice"), когда клиент '
                       'согласился с выбором. Цены берутся из каталога автоматически. Скидка — только если клиент просит скидку или отсрочку, не больше 5%.',
        'parameters': {'type': 'object', 'properties': {
            'items': {'type': 'array', 'items': {'type': 'object', 'properties': {
                'offer_id': {'type': 'integer'}, 'quantity': {'type': 'integer'}}, 'required': ['offer_id', 'quantity']}},
            'document': {'type': 'string', 'enum': ['quote', 'invoice'], 'description': 'КП (по умолчанию) или счёт на оплату'},
            'discount_percent': {'type': 'number', 'description': 'Скидка 0–5% по просьбе клиента; больше — только через менеджера'},
        }, 'required': ['items']}}},
    {'type': 'function', 'function': {
        'name': 'escalate_to_manager',
        'description': 'Передать обращение менеджеру: цена по запросу, скидки и спецусловия, тендеры, вопросы по заказу, '
                       'продукт не найден, сложная конфигурация, клиент просит человека.',
        'parameters': {'type': 'object', 'properties': {
            'reason': {'type': 'string', 'enum': REASONS},
            'summary': {'type': 'string', 'description': 'Краткое резюме для менеджера: что нужно клиенту, что уже подобрано, параметры'},
        }, 'required': ['reason', 'summary']}}},
]


# ------------------------------------------------------------------ инструменты

def run_tool(conv_id: str, name: str, args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    if name == 'search_catalog':
        if args.get('scenario') and not ctx.get('scenario'):
            ctx['scenario'] = args['scenario']
        f = {'only_domestic': bool(args.get('only_domestic')), 'org_type': args.get('org_type')}
        res = catalog.search(str(args.get('query', '')), f, limit=6)
        if not res and f.get('org_type'):  # тип организации мог отсечь всё — пробуем без него
            res = catalog.search(str(args.get('query', '')), {'only_domestic': f['only_domestic']}, limit=6)
        for r in res:
            ctx['prices'].update(p for p in (r['price_from_rub'], r['price_to_rub']) if p)
            ctx['prices'].update(o['price_rub'] for o in r['matched_offers'] if o['price_rub'])
        ctx['products'] = [{'group_id': r['group_id'], 'product': r['product'], 'vendor': r['vendor'], 'url': r['url'],
                            'price_from_rub': r['price_from_rub'], 'domestic': r['domestic'], 'registry': r['registry']} for r in res[:4]]
        return {'results': res, 'hint': '' if res else 'Ничего не найдено: переформулируй запрос (синонимы, вендор, класс ПО) или передай менеджеру.'}
    if name == 'get_product_variants':
        gid = int(args.get('group_id') or 0)
        f = {k: args.get(k) for k in ('users', 'term', 'org_type', 'license_type') if args.get(k)}
        text = str(args.get('text') or '')
        res = catalog.variants(group_id=gid, filters=f, text=text, limit=15)
        # Ослабляем фильтры по одному (срок → текст → тип лицензии → тип организации), число пользователей держим до конца
        dropped = []
        for key in ('term', 'text', 'license_type', 'org_type', 'users'):
            if res.get('variants'):
                break
            if key == 'text':
                if not text:
                    continue
                text = ''
            elif key in f:
                f.pop(key)
            else:
                continue
            dropped.append(key)
            res = catalog.variants(group_id=gid, filters=f, text=text, limit=15)
        if dropped:
            ru = {'term': 'срок', 'text': 'слово в названии', 'license_type': 'тип лицензии', 'org_type': 'тип организации', 'users': 'число пользователей'}
            res['note'] = ('Точного совпадения нет; без фильтра «' + '», «'.join(ru[d] for d in dropped) +
                           '» найдены варианты ниже — предложи ближайшие и скажи клиенту, чем они отличаются от запроса.')
        ctx['prices'].update(v['price_rub'] for v in res.get('variants', []) if v.get('price_rub'))
        ctx['offers'] = [{'offer_id': v['offer_id'], 'name': v['name'], 'price_rub': v.get('price_rub'), 'url': v.get('url')}
                         for v in res.get('variants', [])[:6]]
        return res
    if name == 'search_knowledge':
        arts = kb.search(str(args.get('query', '')), limit=3)
        ctx['kb'] = [{'title': a['title'], 'url': a['url'], 'source': a['source']} for a in arts]
        return {'articles': arts}
    if name == 'create_quote':
        try:
            q = quotes.create(conv_id, list(args.get('items') or []), args.get('discount_percent') or 0, args.get('document') or 'quote')
        except (quotes.QuoteError, ValueError, TypeError) as e:
            return {'error': str(e), 'hint': 'Позиции без цены и скидки больше 5% передай менеджеру через escalate_to_manager.'}
        ctx['prices'].update([q['total_rub'], *[i['price'] for i in q['items']], *[i['total'] for i in q['items']],
                              *[i['list_price'] for i in q['items']]])
        ctx['quote'] = {'quote_id': q['quote_id'], 'number': q['number'], 'total_rub': q['total_rub'], 'status': q['status'],
                        'kind': q['kind'], 'discount': q['discount_percent']}
        doc = 'счёт' if q['kind'] == 'invoice' else 'КП'
        return {**q, 'next_step': f'Скажи клиенту, что {doc} {q["number"]} подготовлен и нужно заполнить реквизиты в форме под сообщением.'}
    if name == 'escalate_to_manager':
        reason = args.get('reason') if args.get('reason') in REASONS else 'other'
        eid = db.execute('INSERT INTO escalations (conv_id, reason, category, summary, created_at) VALUES (?,?,?,?,?)',
                         (conv_id, reason, REASON_RU[reason], str(args.get('summary', ''))[:2000], db.now()))
        db.execute("UPDATE conversations SET status='escalated', updated_at=? WHERE id=?", (db.now(), conv_id))
        ctx['escalation'] = {'id': eid, 'reason': REASON_RU[reason]}
        return {'escalation_id': eid, 'status': 'передано менеджеру', 'next_step': 'Сообщи клиенту, что менеджер ответит в этом чате.'}
    return {'error': f'Неизвестный инструмент {name}'}


# ------------------------------------------------------------------ проверка цен

_PRICE = re.compile(r'(\d[\d\s ]*(?:[.,]\d{1,2})?)\s*(?:₽|руб)', re.IGNORECASE)


def _num(s: str) -> float | None:
    try:
        return float(re.sub(r'[\s ]', '', s).replace(',', '.'))
    except ValueError:
        return None


def unverified_prices(text: str, known: set[float]) -> list[str]:
    bad = []
    for m in _PRICE.finditer(text):
        v = _num(m.group(1))
        if v is None or v < 1:
            continue
        ok = any(abs(v - p) < 0.6 for p in known) or any(
            p > 0 and abs(v / p - round(v / p)) < 1e-3 and 1 <= round(v / p) <= 5000 for p in known)
        if not ok:
            bad.append(m.group(0).strip())
    return bad


# ------------------------------------------------------------------ диалог

def new_conversation(page_url: str = '') -> str:
    cid = db.new_id('c')
    db.execute('INSERT INTO conversations (id, created_at, updated_at, page_url) VALUES (?,?,?,?)', (cid, db.now(), db.now(), page_url[:300]))
    return cid


def _history(conv_id: str) -> tuple[list[dict[str, Any]], str]:
    """История диалога и справка о товарах, найденных ранее (group_id/offer_id нужны модели для следующих шагов)."""
    s = get_settings()
    msgs = db.rows('SELECT role, content, meta FROM messages WHERE conv_id=? ORDER BY id DESC LIMIT ?', (conv_id, s.history_messages))
    out, ref = [], []
    for m in reversed(msgs):
        meta = db.loads(m['meta'], {})
        if m['role'] == 'user':
            out.append({'role': 'user', 'content': m['content']})
        elif m['role'] in ('assistant', 'manager'):
            text = m['content'] if m['role'] == 'assistant' else f'(Ответ менеджера Softline клиенту) {m["content"]}'
            out.append({'role': 'assistant', 'content': text})
            for p in meta.get('products', []):
                ref.append(f"group_id={p['group_id']} «{p['product']}» ({p['vendor']}) от {p.get('price_from_rub')} ₽")
            for o in meta.get('offers', []):
                ref.append(f"offer_id={o['offer_id']} «{o['name']}» — {o.get('price_rub')} ₽")
    note = ''
    if ref:
        note = ('Служебная справка (клиенту не показывать): товары, найденные ранее в этом диалоге —\n'
                + '\n'.join(dict.fromkeys(ref[-20:])))
    return out, note


_LEAK = re.compile(r'\[?(?:Служебная справка|Справка для агента)[^\]\n]*(?:\][^\n]*)?', re.IGNORECASE)


def _known_prices(conv_id: str) -> set[float]:
    known: set[float] = set()
    for m in db.rows("SELECT meta FROM messages WHERE conv_id=? AND role='assistant'", (conv_id,)):
        meta = db.loads(m['meta'], {})
        known.update(float(x) for x in meta.get('prices', []) if x)
    return known


def _call(conv_id: str, messages: list[dict[str, Any]], ctx: dict[str, Any]) -> llm.LLMResult:
    s = get_settings()
    last_err = ''
    for p in llm.chain():
        try:
            r = p.chat(messages, TOOLS)
        except Exception as e:  # noqa: BLE001 — любая ошибка модели → резервная модель
            last_err = f'{type(e).__name__}: {str(e)[:300]}'
            logger.warning('LLM %s недоступна: %s', p.name, last_err)
            db.execute('INSERT INTO llm_calls (conv_id, provider, model, tokens_in, tokens_out, cost_rub, ms, ok, error, created_at) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?)', (conv_id, p.name, p.model, 0, 0, 0, 0, 0, last_err, db.now()))
            ctx['degraded'] = True
            continue
        price = {'gigachat': s.gigachat_price_per_1k, 'yandex': s.yandex_price_per_1k}.get(p.name, 0.0)
        cost = round((r.tokens_in + r.tokens_out) / 1000 * price, 4)
        ctx['cost_rub'] += cost
        ctx['tokens'] += r.tokens_in + r.tokens_out
        ctx['provider'] = p.name
        step = 'price_fix' if ctx.get('guard') else ('tool_choice' if r.tool_calls else 'answer')
        db.execute('INSERT INTO llm_calls (conv_id, provider, model, tokens_in, tokens_out, cost_rub, ms, ok, created_at, step) '
                   'VALUES (?,?,?,?,?,?,?,?,?,?)', (conv_id, p.name, r.model, r.tokens_in, r.tokens_out, cost, r.ms, 1, db.now(), step))
        return r
    raise llm.LLMError(last_err or 'Нет доступных моделей')


def reply(conv_id: str, user_text: str) -> dict[str, Any]:
    s = get_settings()
    t0 = time.time()
    masked = pii.mask(conv_id, user_text.strip()[:4000])
    db.execute('INSERT INTO messages (conv_id, role, content, created_at) VALUES (?,?,?,?)', (conv_id, 'user', masked, db.now()))
    ctx: dict[str, Any] = {'prices': _known_prices(conv_id), 'cost_rub': 0.0, 'tokens': 0, 'tools': [], 'provider': ''}
    history, note = _history(conv_id)
    from app import learning

    ex = learning.examples_for(masked)
    if ex:  # примеры ответов, которые клиенты отметили полезными (цены в них вырезаны — бери из инструментов)
        note += '\n\nПримеры ответов на похожие вопросы, которые клиенты отметили полезными (стиль и маршрут, не факты):\n' + \
                '\n'.join(f'— Вопрос: {e["question"][:300]}\n  Ответ: {e["answer"][:600]}' for e in ex)
    messages = [{'role': 'system', 'content': PROMPT + ('\n\n' + note if note else '')}, *history]
    text = ''
    for _ in range(s.max_agent_steps):
        r = _call(conv_id, messages, ctx)
        if not r.tool_calls:
            text = r.text.strip()
            break
        for tc in r.tool_calls[:2]:
            t1 = time.time()
            try:
                result = run_tool(conv_id, tc.name, tc.arguments, ctx)
            except Exception as e:  # noqa: BLE001 — ошибка инструмента отдаётся модели как данные
                logger.exception('Инструмент %s упал', tc.name)
                result = {'error': f'{type(e).__name__}: {e}'}
            payload = json.dumps(result, ensure_ascii=False, default=str)
            ctx['tools'].append({'tool': tc.name, 'args': tc.arguments})
            db.execute('INSERT INTO tool_calls (conv_id, tool, args, result, ms, created_at) VALUES (?,?,?,?,?,?)',
                       (conv_id, tc.name, db.dumps(tc.arguments), payload[:4000], int((time.time() - t1) * 1000), db.now()))
            messages.append({'role': 'assistant', 'content': r.text or '', 'tool_calls': [{'id': tc.id, 'name': tc.name, 'arguments': tc.arguments}]})
            messages.append({'role': 'tool', 'tool_call_id': tc.id, 'name': tc.name, 'content': payload[:12000]})
    if not text:
        text = 'Сейчас не получается подобрать ответ автоматически — передаю вопрос менеджеру, он ответит в этом чате.'
        if not ctx.get('escalation'):
            run_tool(conv_id, 'escalate_to_manager', {'reason': 'other', 'summary': f'Агент не смог ответить на: {masked[:500]}'}, ctx)
    text = _LEAK.sub('', text).strip()
    bad = unverified_prices(text, ctx['prices'])
    if bad:
        logger.info('Неподтверждённые цены %s — переписываю ответ', bad)
        messages.append({'role': 'assistant', 'content': text})
        messages.append({'role': 'user', 'content': f'(Системная проверка) Цены {", ".join(bad)} не подтверждены инструментами. '
                                                     'Перепиши ответ, используя только цены из результатов инструментов.'})
        ctx['guard'] = True
        try:
            r = _call(conv_id, messages, ctx)
            if r.text and not r.tool_calls:
                text = r.text.strip()
        except llm.LLMError:
            pass
        for b in unverified_prices(text, ctx['prices']):
            text = text.replace(b, 'цену уточнит менеджер')
        ctx['price_guard'] = bad
    # Карточки — только тех продуктов, о которых говорится в ответе (иначе карточки противоречат тексту)
    if ctx.get('products'):
        low = text.lower()
        ctx['products'] = [p for p in ctx['products'] if _mentioned(p['product'], low)]
    meta = {k: ctx[k] for k in ('products', 'offers', 'quote', 'escalation', 'kb', 'price_guard', 'degraded') if ctx.get(k)}
    meta.update({'prices': sorted(ctx['prices'])[:200], 'tools': ctx['tools'], 'provider': ctx['provider'],
                 'cost_rub': round(ctx['cost_rub'], 4), 'tokens': ctx['tokens'], 'ms': int((time.time() - t0) * 1000)})
    mid = db.execute('INSERT INTO messages (conv_id, role, content, meta, created_at) VALUES (?,?,?,?,?)',
                     (conv_id, 'assistant', text, db.dumps(meta), db.now()))
    upd = 'UPDATE conversations SET updated_at=?, title=CASE WHEN title=\'\' THEN ? ELSE title END'
    args: list[Any] = [db.now(), masked[:80]]
    if ctx.get('scenario'):
        upd += ', scenario=?'
        args.append(ctx['scenario'])
    elif any(t['tool'] == 'search_knowledge' for t in ctx['tools']):
        upd += ", scenario=CASE WHEN scenario='' THEN 'question' ELSE scenario END"
    db.execute(upd + ' WHERE id=?', (*args, conv_id))
    return {'id': mid, 'role': 'assistant', 'content': pii.unmask(conv_id, text), 'meta': public_meta(meta)}


def _mentioned(product: str, low_text: str) -> bool:
    name = product.lower().replace('«', '').replace('»', '')
    if name in low_text.replace('«', '').replace('»', ''):
        return True
    words = [w for w in re.findall(r'[\wа-яё]+', name) if len(w) > 2][:3]
    return bool(words) and all(w in low_text for w in words)


def public_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Клиенту — только карточки, КП и статус эскалации, без служебных данных."""
    return {k: meta[k] for k in ('products', 'quote', 'escalation', 'kb') if meta.get(k)}
