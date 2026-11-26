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


if __name__ == '__main__':
    main()
