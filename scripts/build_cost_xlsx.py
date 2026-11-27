"""Расчёт стоимости эксплуатации по шаблону организатора («ИИкузница — калькулятор экономики решения.xlsx»).

Берёт замеры токенов по шагам агента из eval/report.json (прогон на обезличенных переписках) и заполняет:
  «Расчёт»           — лист организатора: модели на один запрос (₽ без НДС);
  «Токены по шагам»  — из чего складываются токены каждого вызова модели;
  «Экономика»        — помесячно: модели, сервер, экономия времени менеджеров, окупаемость (две нагрузки);
  «Сравнение моделей»— стоимость одного запроса на GigaChat Max/Pro/Lite и YandexGPT Pro 5.1;
  «Допущения»        — источники цен и допущения.
Все итоги — формулами Excel: жёлтые ячейки можно менять, остальное пересчитается.

Запуск: python scripts/build_cost_xlsx.py [--template путь] [--report eval/report.json]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

ROOT = Path(__file__).resolve().parents[1]
VAT = 1.22
YELLOW = PatternFill('solid', fgColor='FFF2CC')
HEAD = PatternFill('solid', fgColor='A20D32')
GRAY = PatternFill('solid', fgColor='F2F2F2')
WHITE_B = Font(bold=True, color='FFFFFF')
B = Font(bold=True)
THIN = Border(*(Side(style='thin', color='D9D9D9'),) * 4)
WRAP = Alignment(wrap_text=True, vertical='top')

# Цены с НДС за 1000 токенов (официальные прайсы, синхронный режим, с 01.02.2026)
PRICES = {
    'GigaChat-2-Max': 0.65, 'GigaChat-2-Pro': 0.50, 'GigaChat-2 (Lite)': 0.065,
    'YandexGPT Pro 5.1': 0.80,
}
STEP_RU = {'tool_choice': 'Выбор инструмента (поиск, варианты, база знаний, КП, эскалация)',
           'answer': 'Ответ клиенту по результатам инструментов', 'price_fix': 'Повтор после проверки цен (редко)'}

CTX_GROWTH = 2.2  # 7,94 ₽ / 3,49 ₽ — замер на стенде 27.11 (llm_calls по многоходовым диалогам)

# Оценка до замера (перезаписывается данными eval/report.json)
DEFAULT_STEPS = [
    {'step': 'tool_choice', 'provider': 'gigachat', 'calls_per_request': 1.6, 'tin': 3400, 'tout': 70},
    {'step': 'answer', 'provider': 'gigachat', 'calls_per_request': 1.0, 'tin': 5600, 'tout': 330},
    {'step': 'price_fix', 'provider': 'gigachat', 'calls_per_request': 0.05, 'tin': 6200, 'tout': 300},
]


def style_table(ws, top: int, left: int, rows: int, cols: int, header: bool = True) -> None:
    for r in range(top, top + rows):
        for c in range(left, left + cols):
            cell = ws.cell(r, c)
            cell.border = THIN
            cell.alignment = WRAP
            if header and r == top:
                cell.fill, cell.font = HEAD, WHITE_B


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--template', default=str(ROOT / 'docs' / 'template_cost.xlsx'))  # шаблон организатора без изменений
    ap.add_argument('--report', default=str(ROOT / 'eval' / 'report.json'))
    ap.add_argument('--out', default=str(ROOT / 'docs' / 'cost.xlsx'))
    a = ap.parse_args()
    rep = json.loads(Path(a.report).read_text(encoding='utf-8'))['summary'] if Path(a.report).exists() else {}
    steps = [s for s in rep.get('steps', []) if s.get('provider') in ('gigachat', 'yandex')] or DEFAULT_STEPS
    measured = bool(rep.get('steps'))
    wb = load_workbook(a.template)
    ws = wb['Расчёт']

    # --- Лист организатора: строки 9–12 — шаги агента
    order = {'tool_choice': 0, 'answer': 1, 'price_fix': 2}
    steps = sorted(steps, key=lambda s: (order.get(s['step'], 9), s['provider']))[:4]
    for i, s in enumerate(steps):
        r = 9 + i
        model = 'GigaChat-2-Max' if s['provider'] == 'gigachat' else 'YandexGPT Pro 5.1'
        price_1m = round(PRICES[model] * 1000 / VAT, 2)  # ₽ за 1 млн токенов без НДС
        ws.cell(r, 1, STEP_RU.get(s['step'], s['step']) + ('' if s['provider'] == 'gigachat' else ' — резервная модель'))
        ws.cell(r, 2, model)
        ws.cell(r, 3, s['calls_per_request'])
        ws.cell(r, 4, s['tin'])
        ws.cell(r, 5, s['tout'])
        ws.cell(r, 6, price_1m)
        ws.cell(r, 7, price_1m)
        ws.cell(r, 8, '₽')
    src = (f"замер на {rep['cases']} обращениях из обезличенных переписок (eval/report.json)" if measured
           else 'оценка до замера; будет заменена прогоном eval/run_eval.py')
    ws['A17'] = (f'Токены — {src}. Цены: GigaChat 2 Max 0,65 ₽/1000 ток. с НДС (developers.sber.ru, тарифы юрлиц с 01.02.2026) '
                 '→ 532,79 ₽/1 млн без НДС; вход и выход по одной цене.')
    ws['A18'] = ('Свои серверы: 1 ВМ Timeweb Cloud (4 vCPU, 8 ГБ) — в листе «Экономика»; поиск по каталогу и база знаний — SQLite FTS5 '
                 'на этой же ВМ, эмбеддинги и GPU не нужны. Резерв YandexGPT Pro 5.1 — 0,8 ₽/1000 ток. с НДС.')
    ws['A19'] = ('Что меняет цифры: длина диалога (история растёт), доля вопросов с поиском по каталогу, переход простых FAQ на '
                 'GigaChat Lite (×10 дешевле), асинхронный режим (×2 дешевле), рост цен вендоров моделей.')
    for c in ('A17', 'A18', 'A19'):
        ws[c].alignment = Alignment(wrap_text=True, vertical='top')

    # --- Лист «Токены по шагам»
    t = wb.create_sheet('Токены по шагам')
    t['A1'] = 'Из чего складываются токены одного вызова модели'
    t['A1'].font = Font(bold=True, size=13)
    t['A2'] = 'Вход каждого вызова = системный промпт + описания 5 инструментов + история диалога + результаты инструментов текущего шага.'
    hdr = ['Шаг агента', 'Вызовов на запрос', 'Токенов на входе (замер)', 'Токенов на выходе (замер)',
           'Цена за 1 млн без НДС, ₽', '₽ на 1 запрос', 'Что входит в контекст']
    for j, h in enumerate(hdr, 1):
        t.cell(4, j, h)
    what = {'tool_choice': 'Промпт ~1 000 + инструменты ~900 + история + запрос клиента; на 2-м и следующих шагах — результаты поиска (до 2 500 ток.)',
            'answer': 'То же + результаты всех инструментов шага (карточки товаров, варианты, статьи базы знаний)',
            'price_fix': 'То же + неподтверждённый ответ и требование переписать с ценами из фида'}
    for i, s in enumerate(steps):
        r = 5 + i
        model = 'GigaChat-2-Max' if s['provider'] == 'gigachat' else 'YandexGPT Pro 5.1'
        t.cell(r, 1, STEP_RU.get(s['step'], s['step']) + f' [{model}]')
        t.cell(r, 2, s['calls_per_request'])
        t.cell(r, 3, s['tin'])
        t.cell(r, 4, s['tout'])
        t.cell(r, 5, round(PRICES[model] * 1000 / VAT, 2))
        t.cell(r, 6, f'=B{r}*(C{r}+D{r})*E{r}/1000000')
        t.cell(r, 7, what.get(s['step'], ''))
    last = 4 + len(steps)
    t.cell(last + 1, 1, 'Итого на 1 запрос (сообщение клиента)').font = B
    t.cell(last + 1, 2, f'=SUM(B5:B{last})')
    t.cell(last + 1, 3, f'=SUMPRODUCT(B5:B{last},C5:C{last})')
    t.cell(last + 1, 4, f'=SUMPRODUCT(B5:B{last},D5:D{last})')
    t.cell(last + 1, 6, f'=SUM(F5:F{last})')
    style_table(t, 4, 1, len(steps) + 2, 7)
    for col, w in zip('ABCDEFG', (52, 14, 16, 16, 16, 14, 70)):
        t.column_dimensions[col].width = w
    if rep:
        t.cell(last + 3, 1, 'Замеры прогона').font = B
        kv = [('Обращений в прогоне', rep.get('cases')), ('Верная маршрутизация', rep.get('routing_accuracy')),
              ('Полнота эскалаций', rep.get('escalation_recall')), ('Доля эскалаций', rep.get('escalation_rate')),
              ('Медиана задержки, мс', rep.get('latency_ms_median')), ('p90 задержки, мс', rep.get('latency_ms_p90')),
              ('Вызовов модели на запрос', rep.get('llm_calls_per_request')), ('Токенов вход / запрос', rep.get('tokens_in_per_request')),
              ('Токенов выход / запрос', rep.get('tokens_out_per_request')), ('₽ на запрос с НДС (по журналу)', rep.get('cost_rub_per_request'))]
        for i, (k, v) in enumerate(kv):
            t.cell(last + 4 + i, 1, k)
            t.cell(last + 4 + i, 2, v)
    per_req = f"'Токены по шагам'!F{last + 1}"

    # --- Лист «Экономика»
    e = wb.create_sheet('Экономика')
    e['A1'] = 'Стоимость содержания агента и экономия, ₽ без НДС в месяц'
    e['A1'].font = Font(bold=True, size=13)
    e['A2'] = 'Жёлтые ячейки — допущения, их можно менять. Две нагрузки: по ответу заказчика в чате задачи и по презентации задачи.'
    rows = [
        ('Параметр', 'Факт заказчика: 500 обращений за полгода', 'Презентация: ~500 обращений в день', 'Комментарий'),
        ('Диалогов в месяц', 500 / 6, 500 * 30, 'Встреча и чат задачи: «около 500 обращений за полгода»; слайд 2: «около 500 обращений в день»'),
        ('Сообщений клиента на диалог (запросов к агенту)', 4, 4, 'Типичный диалог: запрос → уточнение → варианты → КП'),
        ('Запросов к агенту в месяц', '=B4*B5', '=C4*C5', ''),
        ('Стоимость моделей на 1 запрос, ₽', f'={per_req}*B29', f'={per_req}*C29', 'Лист «Токены по шагам» (GigaChat 2 Max) × коэффициент роста контекста (строка 29)'),
        ('Модели в месяц, ₽', '=B6*B7', '=C6*C7', ''),
        ('Сервер Timeweb Cloud (4 vCPU, 8 ГБ, РФ), ₽/мес', 3000, 3000, 'Одна ВМ: приложение + SQLite + HTTPS; уточнить по прайсу'),
        ('Резерв на рост нагрузки / второй сервер, ₽/мес', 0, 3000, 'При 500 диалогах в день — второй экземпляр'),
        ('Сопровождение (DevOps/ML, часов в месяц)', 4, 16, 'Обновление промпта, разбор эскалаций, мониторинг'),
        ('Стоимость часа инженера, ₽', 2000, 2000, ''),
        ('ИТОГО содержание агента, ₽/мес', '=B8+B9+B10+B11*B12', '=C8+C9+C10+C11*C12', ''),
        ('Стоимость одного диалога для бизнеса, ₽', '=B13/B4', '=C13/C4', 'Всё включено'),
        ('', '', '', ''),
        ('Время менеджера на одно обращение, мин', 30, 30, 'Оценка заказчика на установочной встрече: «ответ на каждый вопрос ~30 минут»'),
        ('Стоимость часа менеджера с налогами, ₽', 950, 950, 'ФОТ ~120 тыс. ₽ + 30% взносов / 165 ч'),
        ('Доля диалогов, закрытых агентом без менеджера', round(1 - rep.get('escalation_rate', 0.4), 3), round(1 - rep.get('escalation_rate', 0.4), 3),
         'Из прогона: 1 − доля эскалаций; на эскалациях менеджер тратит ~1/3 времени (резюме готово)'),
        ('Экономия времени менеджеров, часов/мес', '=B4*B16/60*(B18+(1-B18)*2/3)', '=C4*C16/60*(C18+(1-C18)*2/3)', ''),
        ('Экономия ФОТ, ₽/мес', '=B19*B17', '=C19*C17', ''),
        ('Чистый эффект, ₽/мес', '=B20-B13', '=C20-C13', ''),
        ('Окупаемость внедрения, мес', '=IF(B21>0,B23/B21,"не окупается при этой нагрузке")', '=IF(C21>0,C23/C21,"—")', ''),
        ('Разовое внедрение (доработка, интеграция в виджет сайта, CRM), ₽', 300000, 300000, '~150 ч разработки × 2 000 ₽'),
        ('Скорость первого ответа', 'медиана 4,1 с, 24/7', 'медиана 4,1 с, 24/7', 'Сейчас менеджер отвечает в среднем через ~2 мин (данные заказчика)'),
    ]
    for i, row in enumerate(rows):
        for j, v in enumerate(row):
            e.cell(3 + i, 1 + j, v)
    style_table(e, 3, 1, len(rows), 4)
    for r in (4, 5, 9, 10, 11, 12, 16, 17, 18, 23):
        for c in (2, 3):
            e.cell(r, c).fill = YELLOW
    for r in (13, 14, 20, 21, 22):
        for c in (1, 2, 3):
            e.cell(r, c).font = B
    for r in range(4, 4 + len(rows)):
        for c in (2, 3):
            e.cell(r, c).number_format = '#,##0.00'
    for col, w in zip('ABCD', (58, 26, 26, 70)):
        e.column_dimensions[col].width = w
    # Вопрос заказчика на встрече: «сколько токенов примерно будет уходить в день и сколько они будут стоить»
    tok = f"('Токены по шагам'!C{last + 1}+'Токены по шагам'!D{last + 1})"
    extra = [('Токенов в день (вход + выход)', f'=B6/30*{tok}*B29', f'=C6/30*{tok}*C29', 'Запросов в день × токенов на запрос (замер) × рост контекста'),
             ('Модели в день, ₽ без НДС', '=B8/30', '=C8/30', ''),
             ('Высвобождено менеджеров, ставок (165 ч/мес)', '=B19/165', '=C19/165', 'Сейчас в поддержке 6–8 человек (чат + почта)'),
             ('Коэффициент роста контекста в многоходовом диалоге', CTX_GROWTH, CTX_GROWTH,
              'Замер на стенде: 4 многоходовых диалога, 12 сообщений клиента — 7,9 ₽ с НДС на сообщение против 3,5 ₽ '
              'на одиночный запрос (история и результаты инструментов копятся в контексте)')]
    for i, row in enumerate(extra):
        for j, v in enumerate(row):
            cell = e.cell(26 + i, 1 + j, v)
            cell.border, cell.alignment = THIN, WRAP
            if j in (1, 2):
                cell.number_format = '#,##0.00'
    e.cell(26, 1).font = e.cell(27, 1).font = B
    e.cell(29, 2).fill = e.cell(29, 3).fill = YELLOW
    wb.calculation.fullCalcOnLoad = True  # Excel/Numbers пересчитают формулы при открытии

    # --- Лист «Сравнение моделей»
    m = wb.create_sheet('Сравнение моделей')
    m['A1'] = 'Стоимость одного запроса при тех же токенах на разных моделях'
    m['A1'].font = Font(bold=True, size=13)
    hdr = ['Модель', 'Цена за 1000 ток. с НДС, ₽', 'Цена за 1 млн без НДС, ₽', 'Токенов на запрос (вход+выход)',
           '₽ на запрос без НДС', '₽/мес при 500 диалогах в день', 'Где работает', 'Комментарий']
    for j, h in enumerate(hdr, 1):
        m.cell(3, j, h)
    notes = {'GigaChat-2-Max': ('РФ (Сбер)', 'Выбрана: лучший function calling и качество в линейке'),
             'GigaChat-2-Pro': ('РФ (Сбер)', 'Кандидат на удешевление после A/B-теста'),
             'GigaChat-2 (Lite)': ('РФ (Сбер)', 'Для простых FAQ-вопросов (маршрутизация по типу запроса)'),
             'YandexGPT Pro 5.1': ('РФ (Яндекс)', 'Резервная модель через тот же адаптер')}
    for i, (name, price) in enumerate(PRICES.items()):
        r = 4 + i
        m.cell(r, 1, name)
        m.cell(r, 2, price)
        m.cell(r, 3, f'=B{r}*1000/{VAT}')
        m.cell(r, 4, f"='Токены по шагам'!C{last + 1}+'Токены по шагам'!D{last + 1}")
        m.cell(r, 5, f'=C{r}*D{r}/1000000')
        m.cell(r, 6, f"=E{r}*'Экономика'!C6")
        m.cell(r, 7, notes[name][0])
        m.cell(r, 8, notes[name][1])
    style_table(m, 3, 1, len(PRICES) + 1, 8)
    for col, w in zip('ABCDEFGH', (22, 16, 16, 18, 16, 20, 16, 52)):
        m.column_dimensions[col].width = w
    m.cell(10, 1, 'Зарубежные модели (GPT, Claude) не рассматривались: трансграничная передача ПДн (ст. 12 152-ФЗ), оплата и доступность из РФ.')

    # --- Лист «Допущения»
    d = wb.create_sheet('Допущения')
    lines = [
        'Источники и допущения',
        'GigaChat 2 Max — 0,65 ₽ за 1000 токенов с НДС, синхронный режим; Pro — 0,50; Lite — 0,065. developers.sber.ru/docs/ru/gigachat/tariffs/legal-tariffs (цены с 01.02.2026).',
        'YandexGPT Pro 5.1 — 0,80 ₽ за 1000 входных и выходных токенов с НДС. aistudio.yandex.ru/docs/ru/ai-studio/pricing.',
        'НДС 22% (с 01.01.2026) выделен из цен: без НДС = с НДС / 1,22 — по требованию шаблона «все суммы без НДС».',
        'Токены на запрос — замер по журналу llm_calls на прогоне агента по обезличенным перепискам (eval/run_eval.py); история диалога растёт, поэтому 3-е и 4-е сообщения дороже первого.',
        'Нагрузка: ответ заказчика в чате задачи — «500 обращений за полгода»; презентация — «около 500 обращений в день». Посчитаны обе.',
        'Время менеджера 30 мин на обращение — оценка заказчика (установочная встреча 26.11); стоимость часа 950 ₽ — допущение команды; сейчас в поддержке 6–8 человек, ответ менеджера ~2 мин. Меняется в жёлтых ячейках листа «Экономика».',
        'Сервер — одна ВМ Timeweb Cloud в РФ; GPU не нужен: поиск — SQLite FTS5, эмбеддинги не используются.',
        'Каталог (136 040 позиций) обновляется из фида раз в сутки — отдельной стоимости нет (13 с CPU на ВМ).',
    ]
    for i, ln in enumerate(lines):
        d.cell(1 + i, 1, ln).alignment = WRAP
    d['A1'].font = Font(bold=True, size=13)
    d.column_dimensions['A'].width = 140
    wb.save(a.out)
    print('Сохранено:', a.out, '| замер' if measured else '| оценка до замера')


if __name__ == '__main__':
    main()
