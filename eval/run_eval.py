"""Оценка агента на обезличенных переписках службы поддержки (материалы задачи).

Переписки используются как тестовый набор, а не для дообучения: 987 обращений с категориями — это
реальные формулировки клиентов. Для каждой категории задано ожидаемое поведение агента:
  - скидки, госзакупки, follow-up по заказу → эскалация менеджеру;
  - цена товара, доступность вендора, конфигурация → поиск по каталогу (или эскалация, если товара нет);
  - счёт/КП, физлицо/срок → база знаний, КП или эскалация.
Метрики: доля верной маршрутизации, полнота эскалаций, срабатывания проверки цен, задержка, токены и
стоимость одного запроса (эти замеры идут в расчёт стоимости docs/cost.xlsx).

Запуск: python eval/run_eval.py --n 60 [--chats data/raw/task/chats]
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from app import agent, db, kb  # noqa: E402

EXPECT = {
    'Скидки / льготы / спецусловия': {'escalate_to_manager'},
    'Госзакупки / процедуры': {'escalate_to_manager'},
    'Follow-up / не дозвонился': {'escalate_to_manager'},
    'Просто цена конкретного товара': {'search_catalog', 'get_product_variants', 'escalate_to_manager'},
    'Доступность вендора (можно ли купить)': {'search_catalog', 'escalate_to_manager'},
    'Конфигурация / спецификация': {'search_catalog', 'get_product_variants', 'escalate_to_manager'},
    'Счет / КП как документ (с реквизитами)': {'search_knowledge', 'create_quote', 'escalate_to_manager', 'search_catalog'},
    'Физлицо / самозанятый / срок': {'search_knowledge', 'search_catalog', 'escalate_to_manager'},
}
LINE = re.compile(r'^#(\d+)\s+\[([^\]]+)\]\s+\[([^\]]*)\]\s+(.+)$')


def load_cases(chats_dir: Path) -> list[dict]:
    cases = []
    for f in sorted(chats_dir.glob('*.txt')):
        for ln in f.read_text(encoding='utf-8').splitlines():
            m = LINE.match(ln.strip())
            if m:
                cases.append({'id': m.group(1), 'category': m.group(2), 'page': m.group(3), 'text': m.group(4)[:600]})
    return cases


def sample(cases: list[dict], n: int, seed: int = 26) -> list[dict]:
    by = defaultdict(list)
    for c in cases:
        by[c['category']].append(c)
    rnd = random.Random(seed)
    out = []
    per = max(1, n // len(by))
    for cat, items in sorted(by.items()):
        out += rnd.sample(items, min(per, len(items)))
    return out[:n]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--chats', default=str(ROOT / 'data' / 'raw' / 'task' / 'chats'))
    ap.add_argument('--n', type=int, default=60)
    args = ap.parse_args()
    db.init_db()
    kb.load_base()
    cases = sample(load_cases(Path(args.chats)), args.n)
    (ROOT / 'eval' / 'cases.jsonl').write_text('\n'.join(json.dumps(c, ensure_ascii=False) for c in cases), encoding='utf-8')
    results = []
    for i, c in enumerate(cases, 1):
        cid = agent.new_conversation(page_url=c['page'])
        t0 = time.time()
        try:
            r = agent.reply(cid, c['text'])
            err = ''
        except Exception as e:  # noqa: BLE001
            r, err = {'content': '', 'meta': {}}, f'{type(e).__name__}: {e}'
        ms = int((time.time() - t0) * 1000)
        meta = db.loads((db.one("SELECT meta FROM messages WHERE conv_id=? AND role='assistant' ORDER BY id DESC LIMIT 1", (cid,)) or {}).get('meta'), {})
        tools = [t['tool'] for t in meta.get('tools', [])]
        calls = db.rows('SELECT provider, tokens_in, tokens_out, cost_rub, ok FROM llm_calls WHERE conv_id=?', (cid,))
        exp = EXPECT.get(c['category'])
        ok = (not exp) or bool(set(tools) & exp)
        results.append({**c, 'tools': tools, 'ok': ok, 'error': err, 'ms': ms, 'answer': r['content'][:500],
                        'escalated': 'escalate_to_manager' in tools, 'price_guard': bool(meta.get('price_guard')),
                        'llm_calls': len(calls), 'tokens_in': sum(x['tokens_in'] for x in calls), 'tokens_out': sum(x['tokens_out'] for x in calls),
                        'cost_rub': round(sum(x['cost_rub'] for x in calls), 4), 'provider': meta.get('provider', '')})
        print(f'[{i}/{len(cases)}] {"OK " if ok else "BAD"} {c["category"][:28]:28} {"→".join(tools) or "-":50} {ms} мс', flush=True)
    must_esc = [r for r in results if EXPECT.get(r['category']) == {'escalate_to_manager'}]
    n = len(results)
    # Токены по шагам агента — исходные данные для расчёта стоимости одного запроса (docs/cost.xlsx)
    steps = db.rows("SELECT step, provider, count(*) AS calls, avg(tokens_in) AS tin, avg(tokens_out) AS tout FROM llm_calls "
                    "WHERE ok=1 AND step<>'' GROUP BY step, provider")
    for st in steps:
        st['calls_per_request'] = round(st['calls'] / n, 3)
        st['tin'], st['tout'] = round(st['tin']), round(st['tout'])
    rep = {
        'steps': steps,
        'cases': n,
        'routing_accuracy': round(sum(r['ok'] for r in results) / n, 3),
        'escalation_recall': round(sum(r['escalated'] for r in must_esc) / len(must_esc), 3) if must_esc else None,
        'escalation_rate': round(sum(r['escalated'] for r in results) / n, 3),
        'price_guard_triggered': sum(r['price_guard'] for r in results),
        'errors': sum(bool(r['error']) for r in results),
        'latency_ms_median': statistics.median(r['ms'] for r in results),
        'latency_ms_p90': sorted(r['ms'] for r in results)[int(n * 0.9) - 1],
        'llm_calls_per_request': round(sum(r['llm_calls'] for r in results) / n, 2),
        'tokens_in_per_request': round(sum(r['tokens_in'] for r in results) / n),
        'tokens_out_per_request': round(sum(r['tokens_out'] for r in results) / n),
        'cost_rub_per_request': round(sum(r['cost_rub'] for r in results) / n, 4),
        'providers': Counter(r['provider'] for r in results),
        'by_category': {cat: {'n': len(v), 'ok': sum(x['ok'] for x in v)} for cat, v in
                        ((k, [r for r in results if r['category'] == k]) for k in sorted({r['category'] for r in results}))},
    }
    (ROOT / 'eval' / 'report.json').write_text(json.dumps({'summary': rep, 'results': results}, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps(rep, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
