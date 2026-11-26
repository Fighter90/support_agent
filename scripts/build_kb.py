"""Сборка базы знаний из открытых страниц store.softline.ru (раздел «Оплата и доставка»).

Страница режется на статьи по заголовкам из её оглавления и сохраняется в data/kb/store_delivery_info.md.
База знаний из материалов задачи («Базы знаний.docx») содержит только ссылки, поэтому статьи берутся
с самих страниц. Запуск: python scripts/build_kb.py
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://store.softline.ru/delivery_info/'


def lines_of(page: str) -> list[str]:
    page = re.sub(r'(?s)<(script|style|noscript|svg|header|footer|nav)[^>]*>.*?</\1>', ' ', page)
    text = html.unescape(re.sub(r'<[^>]+>', '\n', page))
    return [re.sub(r'\s+', ' ', x).strip() for x in text.split('\n') if len(x.strip()) > 1]


def main() -> None:
    page = httpx.get(URL, headers={'User-Agent': 'Mozilla/5.0'}, timeout=60, follow_redirects=True).text
    ls = lines_of(page)
    start = ls.index('Информация об оплате и доставке')
    toc_end = ls.index('Наши гарантии')
    titles = {t for t in ls[ls.index('Нужна помощь?') + 1: toc_end + 1] if len(t) > 8}
    titles |= {'Как оформить заказ на сайте?', 'Оформление и оплата заказа', 'Доставка программного обеспечения и оборудования'}
    body = ls[ls.index('Наши гарантии', toc_end + 1) + 1 if ls.count('Наши гарантии') > 1 else start + 1:]
    sections: list[tuple[str, list[str]]] = []
    for line in body:
        if line in titles:
            sections.append((line, []))
        elif sections:
            if line.startswith(('Продолжая использовать', 'Товар добавлен')):
                break
            sections[-1][1].append(line)
    out = ['# Оплата и доставка в store.softline.ru', f'Источник: {URL}', '']
    for title, body_lines in sections:
        text = ' '.join(body_lines).strip()
        if len(text) > 40:
            out += [f'## {title}', text, '']
    dst = ROOT / 'data' / 'kb' / 'store_delivery_info.md'
    dst.write_text('\n'.join(out), encoding='utf-8')
    print(f'{dst}: {len(sections)} статей', file=sys.stderr)


DOCS = 'https://docs.softline.com/ru-ru/'
DOCS_PAGES = ['id/about', 'id/registration', 'id/login', 'id/password', 'id/change-password', 'id/profile', 'id/authorization-data',
              'lk/advantages', 'lk/login', 'lk/lk-password', 'lk/lk-email', 'lk/create-lk', 'lk/create-order', 'lk/change-order',
              'lk/cancel-order', 'lk/view-statuses', 'lk/fulfillment', 'lk/invoices', 'lk/invoice-statuses', 'lk/unpaid-invoices',
              'lk/closing-documents', 'lk/edo', 'lk/agreements', 'lk/renewal', 'lk/trial-license', 'lk/products-overview',
              'lk/order-contact', 'lk/companies-and-users', 'lk/users', 'lk/calendar', 'lk/mailings-management']


def docs_kb() -> None:
    """Вторая база знаний заказчика: справка docs.softline.com (Softline ID и личный кабинет)."""
    out = ['# Справка Softline: Softline ID и личный кабинет', f'Источник: {DOCS}', '']
    n = 0
    for p in DOCS_PAGES:
        try:
            page = httpx.get(DOCS + p, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30, follow_redirects=True).text
        except httpx.HTTPError:
            continue
        m = re.search(r'(?s)<(?:article|main)[^>]*>(.*?)</(?:article|main)>', page)
        part = m.group(1) if m else page
        title_m = re.search(r'(?s)<h1[^>]*>(.*?)</h1>', part) or re.search(r'(?s)<title>(.*?)</title>', page)
        title = re.sub(r'<[^>]+>|\s+', ' ', html.unescape(title_m.group(1))).strip().split('|')[0].strip() if title_m else p
        body = ' '.join(x for x in lines_of(part) if x != title)
        body = re.sub(r'\s+', ' ', body)[:3000]
        if len(body) > 80:
            section = 'Softline ID' if p.startswith('id/') else 'Личный кабинет'
            out += [f'## {section}: {title}', f'{body} (подробнее: {DOCS}{p})', '']
            n += 1
    (ROOT / 'data' / 'kb' / 'softline_docs.md').write_text('\n'.join(out), encoding='utf-8')
    print(f'softline_docs.md: {n} статей', file=sys.stderr)


if __name__ == '__main__':
    main()
    docs_kb()
