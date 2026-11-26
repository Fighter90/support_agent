"""Оценка RAG по методике RAGAS (Es et al., 2023) с GigaChat в роли судьи.

Для каждого вопроса прогоняется весь агент (как в чате), из журнала берутся статьи, которые вернул search_knowledge.
Метрики (0..1):
  context_recall    — найдена ли эталонная статья среди выданных (по заголовку эталона);
  context_precision — доля выданных статей, которые судья счёл относящимися к вопросу (с учётом ранга, как в RAGAS);
  faithfulness      — судья разбивает ответ на утверждения и проверяет каждое по контексту: доля подтверждённых;
  answer_relevancy  — насколько ответ отвечает на вопрос (судья, шкала 1–5 → 0..1).
Вопросы — реальные формулировки из обезличенных переписок и типовые вопросы со встречи с заказчиком.
Запуск: python eval/rag_eval.py
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from app import agent, db, kb, llm  # noqa: E402

DATASET = [
    ('Цены на сайте указаны с НДС?', 'НДС'),
    ('Как получить закрывающие документы по заказу?', 'закрывающих документов'),
    ('Где посмотреть статус моего заказа?', 'статуса заказа'),
    ('Вчера оплатили счёт, когда придёт ключ?', 'Ключи и дистрибутивы'),
    ('Как оплатить заказ? Какие способы оплаты есть?', 'Как оплатить заказ'),
    ('Можно ли купить лицензию как физлицо и использовать в компании?', 'на имя организации'),
    ('Какие сроки доставки электронной лицензии?', 'Виды и сроки доставки'),
    ('Как получить скидку?', 'скидку'),
    ('Забыл пароль от личного кабинета, что делать?', 'восстановить пароль'),
    ('Как подключить ЭДО для получения документов?', 'ЭДО'),
    ('Можно ли вернуть деньги за электронную лицензию?', 'возврата денежных средств'),
    ('Какие данные нужны для оформления заказа на юрлицо?', 'данные нужны'),
    ('Как продлить лицензию через личный кабинет?', 'Продление лицензий'),
    ('Вы работаете по 44-ФЗ?', 'госзакупками'),
    ('Как зарегистрироваться в Softline ID?', 'зарегистрироваться'),
    ('Как отменить заказ?', 'Отмена заказа'),
    ('Можно ли оформить предзаказ?', 'предзаказ'),
    ('Есть ли техподдержка по купленным программам?', 'Техподдержка'),
    ('Почему у товара нет цены?', 'нет цены'),
    ('Как изменить e-mail в личном кабинете?', 'Изменение почты'),
]

JUDGE_REL = ('Вопрос клиента: «{q}»\nСтатья базы знаний: «{t}: {b}»\n'
             'Относится ли статья к вопросу и помогает ли на него ответить? Ответь строго JSON: {{"relevant": true|false}}')
JUDGE_FAITH = ('Контекст (статьи базы знаний):\n{ctx}\n\nОтвет ассистента:\n{a}\n\n'
               'Разбей ответ на отдельные фактические утверждения (без приветствий и вопросов клиенту) и для каждого укажи, '
               'подтверждается ли оно контекстом. Ответь строго JSON: {{"claims": [{{"claim": "...", "supported": true|false}}]}}')
JUDGE_ANS = ('Вопрос клиента: «{q}»\nОтвет ассистента: «{a}»\n'
             'Насколько ответ по существу отвечает на вопрос? 5 — полностью, 1 — не отвечает. Ответь строго JSON: {{"score": 1..5}}')


def judge(prompt: str) -> dict:
    g = llm.provider('gigachat')
    r = g.chat([{'role': 'system', 'content': 'Ты строгий оценщик качества RAG-системы. Отвечай только JSON.'},
                {'role': 'user', 'content': prompt}], [], temperature=0.0)
    text = r.text.strip().strip('`')
    text = text[text.find('{'): text.rfind('}') + 1]
    try:
        return json.loads(text)
    except ValueError:
        return {}


def main() -> None:
    db.init_db()
    kb.load_base()
    rows = []
    for q, ref in DATASET:
        cid = agent.new_conversation()
        r = agent.reply(cid, q)
        calls = db.rows("SELECT result FROM tool_calls WHERE conv_id=? AND tool='search_knowledge'", (cid,))
        arts = []
        for c in calls:
            arts += (db.loads(c['result'], {}) or {}).get('articles', [])
        if not arts:  # агент не обратился к базе знаний — оцениваем по прямому поиску
            arts = kb.search(q, 3)
        recall = float(any(ref.lower() in (a['title'] + ' ' + a['text'][:300]).lower() for a in arts))
        rel = [bool(judge(JUDGE_REL.format(q=q, t=a['title'], b=a['text'][:700])).get('relevant')) for a in arts[:3]]
        # context precision@k по RAGAS: среднее precision@i по позициям релевантных
        hits, prec = 0, []
        for i, ok in enumerate(rel, 1):
            if ok:
                hits += 1
                prec.append(hits / i)
        cprec = sum(prec) / max(1, hits) if rel else 0.0
        ctx = '\n'.join(f'- {a["title"]}: {a["text"][:900]}' for a in arts[:3])
        claims = judge(JUDGE_FAITH.format(ctx=ctx, a=r['content'][:1500])).get('claims') or []
        faith = sum(1 for c in claims if c.get('supported')) / len(claims) if claims else 1.0
        ans = judge(JUDGE_ANS.format(q=q, a=r['content'][:1500])).get('score') or 1
        rows.append({'question': q, 'reference': ref, 'answer': r['content'], 'contexts': [a['title'] for a in arts[:3]],
                     'used_kb_tool': bool(calls), 'context_recall': recall, 'context_precision': round(cprec, 3),
                     'faithfulness': round(faith, 3), 'answer_relevancy': round((int(ans) - 1) / 4, 3),
                     'unsupported': [c['claim'] for c in claims if not c.get('supported')][:3]})
        print(f"{q[:55]:55} R={recall:.0f} P={cprec:.2f} F={faith:.2f} A={rows[-1]['answer_relevancy']:.2f}", flush=True)
    summary = {k: round(statistics.mean(x[k] for x in rows), 3) for k in ('context_recall', 'context_precision', 'faithfulness', 'answer_relevancy')}
    summary['kb_tool_rate'] = round(sum(x['used_kb_tool'] for x in rows) / len(rows), 3)
    summary['questions'] = len(rows)
    (ROOT / 'eval' / 'rag_report.json').write_text(json.dumps({'summary': summary, 'results': rows}, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
