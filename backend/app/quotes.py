"""Коммерческое предложение по шаблону «Шаблон КП ПАО Софтлайн» (материалы задачи).

Агент вызывает create_quote с позициями (offer_id, количество). Цены берутся ТОЛЬКО из каталога (фида),
модель их не задаёт. Реквизиты клиента (название, ИНН, КПП) вводятся в отдельной форме и в модель не попадают.
КП и счёт выдаются клиенту сразу (ответ организаторов: проверка менеджером не обязательна); менеджер может
подтвердить документ одной кнопкой. Скидка по строке = max(акция из таблицы «Скидки», скидка агента в пределах лимита).
"""

from __future__ import annotations

import html
import os
import re
import time
from pathlib import Path
from typing import Any

from app import card, catalog, db, discounts
from app.config import ROOT_DIR, get_settings

VAT_FROM_CARD = os.environ.get('VAT_FROM_CARD', '1') == '1'  # ставка НДС с карточки товара (в тестах без сети — 0)
VAT_RATE = 22  # ставка НДС с 01.01.2026; ПО из реестра Минцифры НДС не облагается (пп. 26 п. 2 ст. 149 НК РФ)
LOGO = ROOT_DIR / 'backend' / 'app' / 'static' / 'logo.png'


class QuoteError(Exception):
    pass


def _vat(price_total: float, domestic: bool, rate: int | None = None) -> float:
    """НДС внутри конечной цены. Источник ставки — метка карточки товара («Возврат НДС 22%» / «Без НДС»),
    без неё — правило: ПО из реестра Минцифры без НДС, остальное 22%."""
    r = (0 if domestic else VAT_RATE) if rate is None else rate
    return round(price_total * r / (100 + r), 2) if r else 0.0




def _tier(o: dict[str, Any]) -> tuple[int, int]:
    """Диапазон количества, для которого действует цена за единицу (поля фида «К-во узлов» / «К-во пользователей до»)."""
    nums = [int(x) for x in re.findall(r'\d+', f"{o.get('nodes') or ''}")]
    return (min(nums), max(nums)) if len(nums) >= 2 else (1, 10 ** 6)


def create(conv_id: str, items: list[dict[str, Any]], discount_percent: float = 0, kind: str = 'quote') -> dict[str, Any]:
    discount = float(discount_percent or 0)
    cap = discounts.agent_cap()  # заказчик: агент сам даёт до 5% (лимит меняет менеджер в кабинете)
    promos = discounts.rules(active_only=True)
    if discount < 0:
        raise QuoteError('Скидка не может быть отрицательной')
    kind = 'invoice' if kind == 'invoice' else 'quote'
    ids = [int(i.get('offer_id', 0)) for i in items if i.get('offer_id')]
    offers = catalog.get_offers(ids)
    lines, problems = [], []
    for it in items:
        oid = int(it.get('offer_id', 0) or 0)
        qty = max(1, int(it.get('quantity', 1) or 1))
        o = offers.get(oid)
        if not o:
            problems.append(f'позиция {oid} не найдена в каталоге')
            continue
        if not o['price']:  # правило заказчика: позиция с ценой — в наличии; без цены — расчёт менеджера
            problems.append(f'«{o["name"]}» — цена не опубликована, нужен расчёт менеджера')
            continue
        lo, hi = _tier(o)
        if lo > 1 and not lo <= qty <= hi:  # цена за 1 узел/пользователя в диапазоне: количество = число узлов клиента
            problems.append(f'«{o["name"]}» — цена указана за 1 узел/пользователя при количестве {lo}–{hi}; '
                            f'quantity должно быть числом узлов клиента в этом диапазоне (сейчас {qty}). '
                            'Если число клиента вне диапазона — возьми вариант с подходящим диапазоном.')
            continue
        promo = discounts.best_for(o, promos)
        pp = promo['percent'] if promo else 0
        if discount > max(cap, pp):  # больше лимита агента и больше акции — только менеджер
            raise QuoteError(f'Скидку {discount:g}% агент не согласует (лимит {cap:g}%'
                             + (f', акция {pp:g}%' if pp else '') + ') — передай запрос менеджеру (reason=special_terms)')
        d = max(discount if discount <= cap else 0, pp)
        price = round(o['price'] * (1 - d / 100), 2)
        total = round(price * qty, 2)
        rate = card.vat_percent(oid) if VAT_FROM_CARD else None
        lines.append({'offer_id': oid, 'article': str(oid), 'name': o['name'], 'quantity': qty, 'list_price': o['price'], 'price': price,
                      'total': total, 'vat': _vat(total, bool(o['registry']), rate), 'domestic': (rate == 0) if rate is not None else bool(o['registry']),
                      'vat_rate': rate if rate is not None else (0 if o['registry'] else VAT_RATE), 'url': o['url'],
                      'discount': d, 'promo': promo['title'] if promo and promo['percent'] >= d else '',
                      # цена есть, но в фиде available=false: продаём (правило заказчика), менеджеру — отметка проверить поставку
                      'check_supply': not o.get('available')})
    if not lines:
        raise QuoteError('; '.join(problems) or 'нет позиций для КП')
    qid = db.new_id('q')
    n = (db.one('SELECT count(*) AS n FROM quotes') or {'n': 0})['n'] + 1
    number = f'{"СЧ" if kind == "invoice" else "КП"}-{time.strftime("%Y%m%d")}-{n:04d}'
    total = round(sum(x['total'] for x in lines), 2)
    vat = round(sum(x['vat'] for x in lines), 2)
    discount = max(x['discount'] for x in lines)
    db.execute('INSERT INTO quotes (id, number, conv_id, items, total, vat, status, created_at, updated_at, kind, discount) '
               'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
               (qid, number, conv_id, db.dumps(lines), total, vat, 'awaiting_requisites', db.now(), db.now(), kind, discount))
    return {'quote_id': qid, 'number': number, 'kind': kind, 'discount_percent': discount, 'items': lines, 'total_rub': total,
            'vat_rub': vat, 'skipped': problems, 'status': 'awaiting_requisites'}


def get(qid: str) -> dict[str, Any] | None:
    q = db.one('SELECT * FROM quotes WHERE id=?', (qid,))
    if q:
        q['items'] = db.loads(q['items'], [])
        q['customer'] = db.loads(q['customer'], {})
    return q


def set_requisites(qid: str, customer: dict[str, str]) -> dict[str, Any]:
    clean = {k: str(customer.get(k, '')).strip()[:300] for k in ('company', 'inn', 'kpp', 'email', 'contact')}
    if not clean['company'] or not clean['inn'].isdigit() or len(clean['inn']) not in (10, 12):
        raise QuoteError('Укажите название организации и корректный ИНН (10 или 12 цифр)')
    if clean['kpp'] and (not clean['kpp'].isdigit() or len(clean['kpp']) != 9):
        raise QuoteError('КПП — 9 цифр')
    db.execute("UPDATE quotes SET customer=?, status='issued', updated_at=? WHERE id=?", (db.dumps(clean), db.now(), qid))
    q = get(qid)
    render_pdf(q)
    return q


def approve(qid: str, manager: str) -> dict[str, Any]:
    db.execute("UPDATE quotes SET status='approved', updated_at=? WHERE id=?", (db.now(), qid))
    q = get(qid)
    render_pdf(q, approved_by=manager)
    return q


def _rub(x: float) -> str:
    return f'{x:,.2f}'.replace(',', ' ').replace('.', ',')


SELLER = {  # ПАО «Софтлайн» — со страницы store.softline.ru/delivery_info, раздел «Наши реквизиты»
    'name': 'ПАО «Софтлайн»', 'inn': '7736227885', 'kpp': '770401001',
    'address': '119021, г. Москва, ул. Льва Толстого, д. 5, стр. 1, этаж 3, помещение 1, комн. №2, 2а (А-311)',
    'bank': 'ОАО «Альфа-банк» г. Москва', 'account': '40702810502300000368', 'corr': '30101810200000000593', 'bik': '044525593',
}


def render_html(q: dict[str, Any], approved_by: str = '') -> str:
    c = q.get('customer') or {}
    invoice = q.get('kind') == 'invoice'
    cust = html.escape(c.get('company') or '[Наименование заказчика]')
    if c.get('inn'):
        cust += f'<br>ИНН {html.escape(c["inn"])}' + (f', КПП {html.escape(c["kpp"])}' if c.get('kpp') else '')
    disc = float(q.get('discount') or 0)
    rows = ''.join(
        f'<tr><td>{html.escape(i["article"])}</td><td>{html.escape(i["name"])}'
        f'{"<br><span class=m>скидка " + format(i.get("discount", 0), "g") + "% от " + _rub(i["list_price"]) + "</span>" if i.get("discount") else ""}</td><td class="n">{i["quantity"]}</td>'
        f'<td class="n">{_rub(i["price"])}</td><td class="n">{_rub(i["total"])}</td>'
        f'<td class="n">{"без НДС" if i["domestic"] else _rub(i["vat"])}</td></tr>' for i in q['items'])
    date = time.strftime('%d.%m.%Y', time.localtime(q['created_at']))
    valid = time.strftime('%d.%m.%Y', time.localtime(q['created_at'] + (5 if invoice else 14) * 86400))
    logo = f'<img class="logo" src="file://{LOGO}">' if LOGO.exists() else '<b>Софтлайн</b>'
    what = 'Счёт' if invoice else 'Коммерческое предложение'
    status = (f'{what} сформирован автоматически по ценам каталога store.softline.ru' if q['status'] != 'approved'
              else f'Проверено менеджером: {html.escape(approved_by or "менеджер")}')
    promo_names = sorted({i.get('promo') for i in q['items'] if i.get('promo')})
    disc_line = (f'Применена скидка до {disc:g}%' + (f' (акция: {html.escape(", ".join(promo_names))})' if promo_names else ' (в пределах полномочий ИИ-агента)') + '.<br>') if disc else ''
    s = SELLER
    if invoice:
        head = f"""<table class="bank"><tr><td colspan="2">{s['bank']}<br><span class="m">Банк получателя</span></td><td>БИК</td><td>{s['bik']}</td></tr>
        <tr><td colspan="2"></td><td>Сч. №</td><td>{s['corr']}</td></tr>
        <tr><td>ИНН {s['inn']}</td><td>КПП {s['kpp']}</td><td rowspan="2">Сч. №</td><td rowspan="2">{s['account']}</td></tr>
        <tr><td colspan="2">{s['name']}<br><span class="m">Получатель</span></td></tr></table>
        <h1>Счёт на оплату № {html.escape(q['number'])} от {date}</h1>
        <div class="lead">Поставщик: {s['name']}, ИНН {s['inn']}, КПП {s['kpp']}, {s['address']}<br>Покупатель: {cust}</div>"""
        tail = (f'<div class="cond">{disc_line}Оплата счёта означает согласие с условиями поставки (оферта store.softline.ru).<br>'
                f'Счёт действителен до {valid}. Поставка — электронная, после поступления оплаты.</div>'
                '<div class="sign">Руководитель ____________ &nbsp;&nbsp;&nbsp; Бухгалтер ____________</div>')
    else:
        head = f"""<div class="top"><div>{logo}<div class="addr">119021, город Москва, ул Льва Толстого, д. 5 стр. 1, этаж 3, помещ. 1, ком. №2, 2а (а-311)<br>
        Т +7 495 232 00 23 | info@softline.com<br>www.softline.ru | www.softline.com</div></div>
        <div class="to">Куда: {cust}<br><br>Исх. № {html.escape(q['number'])} от {date}</div></div>
        <h1>Уважаемый заказчик!</h1>
        <div class="lead">ПАО «Софтлайн» предлагает Вашему вниманию коммерческое предложение на следующих условиях:</div>"""
        tail = (f'<div class="cond">{disc_line}Цена указана с учетом всех налогов, сборов и других обязательных платежей.<br>'
                'Условия оплаты: предоплата.<br>Срок поставки: электронная поставка, по сроку вендора (указан на карточке товара).<br>'
                f'Срок действия коммерческого предложения — до {valid}.</div><div class="sign">Бондаренко Александр<br>Директор департамента</div>')
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
    @page {{ size: A4; margin: 18mm 16mm; }}
    body {{ font-family: 'Montserrat', 'DejaVu Sans', Arial, sans-serif; font-size: 10pt; color: #222; }}
    .top {{ display: flex; justify-content: space-between; }} .logo {{ height: 34px; }}
    .addr {{ font-size: 8.5pt; color: #555; margin-top: 8px; line-height: 1.35; }} .m {{ font-size: 8pt; color: #666; }}
    .to {{ text-align: right; font-size: 10pt; }} h1 {{ font-size: 13pt; text-align: center; margin: 22px 0 6px; }}
    .lead {{ margin: 8px 0 14px; line-height: 1.5; }} table {{ width: 100%; border-collapse: collapse; font-size: 9pt; }}
    th {{ background: #A20D32; color: #fff; padding: 6px 5px; text-align: left; }} td {{ border-bottom: 1px solid #ddd; padding: 6px 5px; vertical-align: top; }}
    .bank td {{ border: 1px solid #999; }} .n {{ text-align: right; white-space: nowrap; }}
    .tot td {{ font-weight: 700; border-top: 2px solid #A20D32; }}
    .cond {{ margin-top: 16px; line-height: 1.6; }} .sign {{ margin-top: 30px; }} .badge {{ color: #A20D32; font-size: 8.5pt; margin-top: 18px; }}
    </style></head><body>{head}
    <table><tr><th>Артикул</th><th>Наименование</th><th>Кол-во</th><th>Цена, руб.</th><th>Сумма, руб.</th><th>В т.ч. НДС, руб.</th></tr>
    {rows}<tr class="tot"><td></td><td>Итого</td><td></td><td></td><td class="n">{_rub(q['total'])}</td><td class="n">{_rub(q['vat'])}</td></tr></table>
    {tail}
    <div class="badge">{status}. Сформировано ИИ-агентом store.softline.ru по ценам каталога на {date}.</div>
    </body></html>"""


def pdf_path(qid: str) -> Path:
    return get_settings().quotes_dir / f'{qid}.pdf'


def render_pdf(q: dict[str, Any], approved_by: str = '') -> Path:
    from weasyprint import HTML

    p = pdf_path(q['id'])
    p.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=render_html(q, approved_by), base_url=str(ROOT_DIR)).write_pdf(p)
    return p
