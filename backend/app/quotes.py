"""Коммерческое предложение по шаблону «Шаблон КП ПАО Софтлайн» (материалы задачи).

Агент вызывает create_quote с позициями (offer_id, количество). Цены берутся ТОЛЬКО из каталога (фида),
модель их не задаёт. Реквизиты клиента (название, ИНН, КПП) вводятся в отдельной форме и в модель не попадают.
КП создаётся черновиком и подтверждается менеджером в кабинете.
"""

from __future__ import annotations

import html
import time
from pathlib import Path
from typing import Any

from app import catalog, db
from app.config import ROOT_DIR, get_settings

VAT_RATE = 22  # ставка НДС с 01.01.2026; ПО из реестра Минцифры НДС не облагается (пп. 26 п. 2 ст. 149 НК РФ)
LOGO = ROOT_DIR / 'backend' / 'app' / 'static' / 'logo.png'


class QuoteError(Exception):
    pass


def _vat(price_total: float, domestic: bool) -> float:
    return 0.0 if domestic else round(price_total * VAT_RATE / (100 + VAT_RATE), 2)


def create(conv_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
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
        if not o['price'] or not o['available']:
            problems.append(f'«{o["name"]}» — цена не опубликована или позиция недоступна, нужен расчёт менеджера')
            continue
        total = round(o['price'] * qty, 2)
        lines.append({'offer_id': oid, 'article': str(oid), 'name': o['name'], 'quantity': qty, 'price': o['price'],
                      'total': total, 'vat': _vat(total, bool(o['registry'])), 'domestic': bool(o['registry']), 'url': o['url']})
    if not lines:
        raise QuoteError('; '.join(problems) or 'нет позиций для КП')
    qid = db.new_id('q')
    n = (db.one('SELECT count(*) AS n FROM quotes') or {'n': 0})['n'] + 1
    number = f'КП-{time.strftime("%Y%m%d")}-{n:04d}'
    total = round(sum(x['total'] for x in lines), 2)
    vat = round(sum(x['vat'] for x in lines), 2)
    db.execute('INSERT INTO quotes (id, number, conv_id, items, total, vat, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
               (qid, number, conv_id, db.dumps(lines), total, vat, 'awaiting_requisites', db.now(), db.now()))
    return {'quote_id': qid, 'number': number, 'items': lines, 'total_rub': total, 'vat_rub': vat, 'skipped': problems,
            'status': 'awaiting_requisites'}


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
    db.execute("UPDATE quotes SET customer=?, status='draft', updated_at=? WHERE id=?", (db.dumps(clean), db.now(), qid))
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


def render_html(q: dict[str, Any], approved_by: str = '') -> str:
    c = q.get('customer') or {}
    cust = html.escape(c.get('company') or '[Наименование заказчика]')
    if c.get('inn'):
        cust += f'<br>ИНН {html.escape(c["inn"])}' + (f', КПП {html.escape(c["kpp"])}' if c.get('kpp') else '')
    rows = ''.join(
        f'<tr><td>{html.escape(i["article"])}</td><td>{html.escape(i["name"])}</td><td class="n">{i["quantity"]}</td>'
        f'<td class="n">{_rub(i["price"])}</td><td class="n">{_rub(i["total"])}</td>'
        f'<td class="n">{"без НДС" if i["domestic"] else _rub(i["vat"])}</td></tr>' for i in q['items'])
    date = time.strftime('%d.%m.%Y', time.localtime(q['created_at']))
    valid = time.strftime('%d.%m.%Y', time.localtime(q['created_at'] + 14 * 86400))
    logo = f'<img class="logo" src="file://{LOGO}">' if LOGO.exists() else '<b>Софтлайн</b>'
    status = 'Черновик — ожидает проверки менеджером' if q['status'] != 'approved' else f'Проверено менеджером: {html.escape(approved_by or "менеджер")}'
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
    @page {{ size: A4; margin: 18mm 16mm; }}
    body {{ font-family: 'Montserrat', 'DejaVu Sans', Arial, sans-serif; font-size: 10pt; color: #222; }}
    .top {{ display: flex; justify-content: space-between; }} .logo {{ height: 34px; }}
    .addr {{ font-size: 8.5pt; color: #555; margin-top: 8px; line-height: 1.35; }}
    .to {{ text-align: right; font-size: 10pt; }} h1 {{ font-size: 13pt; text-align: center; margin: 26px 0 6px; }}
    .lead {{ margin: 8px 0 14px; }} table {{ width: 100%; border-collapse: collapse; font-size: 9pt; }}
    th {{ background: #A20D32; color: #fff; padding: 6px 5px; text-align: left; }} td {{ border-bottom: 1px solid #ddd; padding: 6px 5px; vertical-align: top; }}
    .n {{ text-align: right; white-space: nowrap; }} .tot td {{ font-weight: 700; border-top: 2px solid #A20D32; }}
    .cond {{ margin-top: 16px; line-height: 1.6; }} .sign {{ margin-top: 30px; }} .badge {{ color: #A20D32; font-size: 8.5pt; margin-top: 18px; }}
    </style></head><body>
    <div class="top"><div>{logo}<div class="addr">119021, город Москва, ул Льва Толстого, д. 5 стр. 1, этаж 3, помещ. 1, ком. №2, 2а (а-311)<br>
    Т +7 495 232 00 23 | info@softline.com<br>www.softline.ru | www.softline.com</div></div>
    <div class="to">Куда: {cust}<br><br>Исх. № {html.escape(q['number'])} от {date}</div></div>
    <h1>Уважаемый заказчик!</h1>
    <div class="lead">ПАО «Софтлайн» предлагает Вашему вниманию коммерческое предложение на следующих условиях:</div>
    <table><tr><th>Артикул</th><th>Наименование</th><th>Кол-во</th><th>Цена, руб.</th><th>Сумма, руб.</th><th>В т.ч. НДС, руб.</th></tr>
    {rows}<tr class="tot"><td></td><td>Итого</td><td></td><td></td><td class="n">{_rub(q['total'])}</td><td class="n">{_rub(q['vat'])}</td></tr></table>
    <div class="cond">Цена указана с учетом всех налогов, сборов и других обязательных платежей.<br>
    Условия оплаты: предоплата.<br>Срок поставки: электронная поставка, по сроку вендора (указан на карточке товара).<br>
    Срок действия коммерческого предложения — до {valid}.</div>
    <div class="sign">Бондаренко Александр<br>Директор департамента</div>
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
