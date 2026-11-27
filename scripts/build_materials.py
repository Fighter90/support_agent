"""Папка материалов для жюри: docs/public → https://methodagent.ru/materials/ (без авторизации).

Собирает PDF описания и решения, копирует руководства, расчёт, презентацию и видео, пишет index.html.
Запуск: python scripts/build_materials.py  (после build_guides.py, сборки видео и презентации)
"""

from __future__ import annotations

import html
import shutil
import subprocess
import tempfile
from pathlib import Path

import build_guides as bg

ROOT = Path(__file__).resolve().parents[1]
DOCS, PUB = ROOT / 'docs', ROOT / 'docs' / 'public'
VIDEO = ROOT / 'scripts' / 'video' / 'out'

ITEMS = [  # (файл в public, источник, заголовок, описание)
    ('demo.mp4', VIDEO / 'demo.mp4', 'Демонстрационное видео', '2:43, один дубль на стенде methodagent.ru; ускорено только ожидание ответа модели; озвучка и субтитры'),
    ('demo.srt', VIDEO / 'demo.srt', 'Субтитры к видео (SRT)', 'Те же субтитры отдельным файлом'),
    ('cost.xlsx', DOCS / 'cost.xlsx', 'Расчёт стоимости эксплуатации', 'Шаблон организатора: стоимость одного запроса, токены по шагам, экономика, сравнение моделей'),
    ('description.pdf', None, 'Описание продукта', 'Текст для формы сдачи'),
    ('solution.pdf', None, 'Описание решения', 'Требования, архитектура, 152-ФЗ, выбор моделей, данные, качество (RAGAS), запуск'),
    ('presentation.pdf', DOCS / 'presentation' / 'presentation.pdf', 'Презентация', '9 слайдов'),
    ('01_user_guide.pdf', DOCS / 'guides' / '01_user_guide.pdf', 'Руководство пользователя (PDF)', 'Для клиентов store.softline.ru'),
    ('01_user_guide.docx', DOCS / 'guides' / '01_user_guide.docx', 'Руководство пользователя (DOCX)', ''),
    ('02_manager_guide.pdf', DOCS / 'guides' / '02_manager_guide.pdf', 'Руководство менеджера и оператора (PDF)', 'Кабинет: живой чат, эскалации, КП и счета, оплата, обучение'),
    ('02_manager_guide.docx', DOCS / 'guides' / '02_manager_guide.docx', 'Руководство менеджера и оператора (DOCX)', ''),
    ('03_admin_guide.pdf', DOCS / 'guides' / '03_admin_guide.pdf', 'Руководство администратора (PDF)', 'Развёртывание, настройки, обновление каталога, мониторинг, 152-ФЗ'),
    ('03_admin_guide.docx', DOCS / 'guides' / '03_admin_guide.docx', 'Руководство администратора (DOCX)', ''),
    ('catalog_rag_report.json', ROOT / 'eval' / 'catalog_rag_report.json', 'RAGAS: каталог (отчёт)', '40 вопросов, эталоны из фида'),
    ('rag_report.json', ROOT / 'eval' / 'rag_report.json', 'RAGAS: база знаний (отчёт)', '20 вопросов'),
    ('report.json', ROOT / 'eval' / 'report.json', 'Регресс на реальных обращениях (отчёт)', '51 обращение из обезличенных переписок'),
]


def md_pdf(md: Path, pdf: Path, tmp: Path) -> None:
    css = tmp / 'style.css'
    css.write_text(bg.CSS, encoding='utf-8')
    page = tmp / (md.stem + '.html')
    subprocess.run(['pandoc', str(md), '-s', '-o', str(page), '--embed-resources', f'--css={css}', '-M', 'lang=ru',
                    '-M', f'title={md.read_text(encoding="utf-8").splitlines()[0].lstrip("# ").strip()}'], check=True)
    # заголовок первой строки уже стал title — убираем дубль
    t = page.read_text(encoding='utf-8')
    i = t.find('<h1 id=')
    if i > 0:
        j = t.find('</h1>', i)
        t = t[:i] + t[j + 5:]
        page.write_text(t, encoding='utf-8')
    subprocess.run([bg.CHROME, '--headless=new', '--disable-gpu', '--no-pdf-header-footer', f'--print-to-pdf={pdf}', page.as_uri()],
                   check=True, capture_output=True)


def main() -> None:
    PUB.mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    md_pdf(DOCS / 'description.md', PUB / 'description.pdf', tmp)
    md_pdf(DOCS / 'solution.md', PUB / 'solution.pdf', tmp)
    rows = []
    for name, src, title, desc in ITEMS:
        if src is not None:
            if not src.exists():
                print('нет файла', src)
                continue
            shutil.copy2(src, PUB / name)
        size = (PUB / name).stat().st_size
        rows.append(f'<tr><td><a href="{name}">{html.escape(title)}</a></td><td>{html.escape(desc)}</td>'
                    f'<td class="n">{size / 1024 / 1024:.1f} МБ</td></tr>' if size > 1024 * 1024 else
                    f'<tr><td><a href="{name}">{html.escape(title)}</a></td><td>{html.escape(desc)}</td><td class="n">{size // 1024} КБ</td></tr>')
    (PUB / 'index.html').write_text(f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Материалы — ИИ-агент поддержки store.softline.ru</title>
<style>body{{font-family:Montserrat,Arial,sans-serif;max-width:1000px;margin:40px auto;padding:0 20px;color:#1E1E1E}}
h1{{color:#A20D32;font-size:26px}} table{{border-collapse:collapse;width:100%}} th{{background:#A20D32;color:#fff;text-align:left;padding:8px 10px}}
td{{border-bottom:1px solid #E5E5E7;padding:8px 10px;vertical-align:top}} a{{color:#A20D32;font-weight:600}} .n{{white-space:nowrap;color:#6B6B6E}}
video{{width:100%;border-radius:10px;margin:10px 0 24px;background:#000}} .links a{{margin-right:18px}}</style></head><body>
<h1>ИИ-агент поддержки store.softline.ru — материалы</h1>
<p>Хакатон «ИИкузница», трек «Клиентские сценарии», задача «Агент поддержки». Автор: Емельянов Сергей.</p>
<p class="links"><a href="/">Стенд</a><a href="/manager">Кабинет менеджера</a><a href="https://github.com/Fighter90/support_agent">Репозиторий</a></p>
<video controls preload="metadata" src="demo.mp4"><track kind="subtitles" srclang="ru" label="Русский (субтитры уже в кадре)" src="demo.vtt"></video>
<table><tr><th>Материал</th><th>Описание</th><th>Размер</th></tr>{''.join(rows)}</table>
</body></html>""", encoding='utf-8')
    srt = PUB / 'demo.srt'
    if srt.exists():  # VTT для встроенного плеера
        vtt = 'WEBVTT\n\n' + srt.read_text(encoding='utf-8').replace(',', '.').replace('\n\n', '\n\n')
        # запятые в тексте субтитров вернуть: меняем только во временных метках
        import re
        vtt = 'WEBVTT\n\n' + re.sub(r'(\d\d:\d\d:\d\d),(\d\d\d)', r'\1.\2', srt.read_text(encoding='utf-8'))
        (PUB / 'demo.vtt').write_text(vtt, encoding='utf-8')
    print('Готово:', sorted(p.name for p in PUB.iterdir()))


if __name__ == '__main__':
    main()
