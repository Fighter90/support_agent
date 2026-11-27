"""FastAPI: веб-чат клиента, кабинет менеджера и раздача интерфейса (SPA)."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import agent, auth, catalog, db, hub, kb, learning, llm, payments, pii, quotes
from app.config import ROOT_DIR, get_settings

logger = logging.getLogger('support_agent')
STATIC = ROOT_DIR / 'frontend' / 'dist'
_rate: dict[str, list[float]] = {}

GREETING = ('Здравствуйте! Я ИИ-консультант Softline. Помогу подобрать лицензию из каталога store.softline.ru, '
            'сверить версии и подготовить коммерческое предложение. Назовите продукт или опишите задачу — '
            'например, «нужен антивирус на 50 компьютеров».')


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.basicConfig(level='INFO', format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    db.init_db()
    n = kb.load_base()
    logger.info('База знаний: %d статей; каталог: %s; модели: %s', n, catalog.stats(), [p.name for p in llm.chain()])

    def refresh_loop() -> None:
        # Каталог: при первом старте — загрузка фида, дальше раз в сутки (по ответу заказчика этого достаточно)
        while True:
            st = catalog.stats()
            if not st.get('offers') or time.time() - get_settings().catalog_db.stat().st_mtime > 86400:
                try:
                    catalog.ingest()
                except Exception:  # noqa: BLE001 — ошибка загрузки не роняет сервис, работаем на прежнем каталоге
                    logger.exception('Не удалось обновить каталог из фида')
            time.sleep(3600)

    threading.Thread(target=refresh_loop, daemon=True, name='feed-refresh').start()
    payments.init()
    import asyncio

    task = asyncio.create_task(hub.broadcaster())
    yield
    task.cancel()


app = FastAPI(title='ИИ-агент поддержки store.softline.ru', version='1.0.0', lifespan=lifespan, docs_url='/api/docs', openapi_url='/api/openapi.json')


def _limit(request: Request, n: int = 40, window: int = 600) -> None:
    ip = request.headers.get('x-forwarded-for', request.client.host if request.client else '-').split(',')[0].strip()
    hits = [t for t in _rate.get(ip, []) if time.time() - t < window]
    if len(hits) >= n:
        raise HTTPException(429, 'Слишком много сообщений, попробуйте через несколько минут')
    _rate[ip] = [*hits, time.time()]


def _conv(cid: str) -> dict[str, Any]:
    c = db.one('SELECT * FROM conversations WHERE id=?', (cid,))
    if not c:
        raise HTTPException(404, 'Диалог не найден')
    return c


def _messages(cid: str, after: int = 0, internal: bool = False) -> list[dict[str, Any]]:
    out = []
    for m in db.rows('SELECT * FROM messages WHERE conv_id=? AND id>? ORDER BY id', (cid, after)):
        meta = db.loads(m['meta'], {})
        out.append({'id': m['id'], 'role': m['role'], 'content': pii.unmask(cid, m['content']), 'created_at': m['created_at'],
                    'meta': meta if internal else agent.public_meta(meta)})
    return out


# ------------------------------------------------------------------ клиент

class StartIn(BaseModel):
    page_url: str = ''


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class RequisitesIn(BaseModel):
    company: str
    inn: str
    kpp: str = ''
    email: str = ''
    contact: str = ''


class FeedbackIn(BaseModel):
    message_id: int
    rating: int = Field(ge=-1, le=1)


class SurveyIn(BaseModel):
    resolved: bool
    score: int = Field(ge=1, le=5)
    comment: str = Field(default='', max_length=1000)


class FixIn(BaseModel):
    answer: str = Field(min_length=3, max_length=6000)


@app.get('/api/health')
def health() -> dict[str, Any]:
    return {'status': 'ok', 'catalog': catalog.stats(), 'models': [p.name for p in llm.chain()], 'payments': payments.enabled(), 'payments_mode': payments.mode()}


@app.get('/api/catalog/search')
def catalog_search(q: str = Query('', max_length=200), request: Request = None) -> dict[str, Any]:
    """Поисковая строка витрины: продуктовые линейки по запросу (тот же поиск, что у агента)."""
    if request:
        _limit(request, 120, 60)
    if len(q.strip()) < 2:
        return {'items': []}
    res = catalog.search(q, {'only_available': False}, limit=8)
    return {'items': [{'group_id': r['group_id'], 'product': r['product'], 'vendor': r['vendor'], 'category': r['category'],
                       'url': r['url'], 'price_from_rub': r['price_from_rub'], 'domestic': r['domestic'],
                       'variants': r['variants_available']} for r in res]}


@app.post('/api/chat/start')
def start(body: StartIn, request: Request) -> dict[str, Any]:
    _limit(request, 20)
    cid = agent.new_conversation(body.page_url)
    mid = db.execute('INSERT INTO messages (conv_id, role, content, created_at) VALUES (?,?,?,?)', (cid, 'assistant', GREETING, db.now()))
    return {'conversation_id': cid, 'messages': [{'id': mid, 'role': 'assistant', 'content': GREETING, 'meta': {}}]}


@app.get('/api/chat/{cid}')
def history(cid: str, after: int = 0) -> dict[str, Any]:
    c = _conv(cid)
    return {'conversation_id': cid, 'status': c['status'], 'messages': _messages(cid, after)}


@app.post('/api/chat/{cid}/message')
async def message(cid: str, body: MessageIn, request: Request) -> dict[str, Any]:
    _limit(request)
    c = _conv(cid)
    if c.get('mode') == 'manager':
        # Диалог ведёт менеджер: агент молчит, сообщение мгновенно уходит менеджеру по WebSocket
        mid = db.execute('INSERT INTO messages (conv_id, role, content, created_at) VALUES (?,?,?,?)',
                         (cid, 'user', pii.mask(cid, body.text.strip()), db.now()))
        db.execute('UPDATE conversations SET updated_at=? WHERE id=?', (db.now(), cid))
        return {'queued': True, 'id': mid, 'manager': c.get('manager', '')}
    try:
        return await run_in_threadpool(agent.reply, cid, body.text)
    except llm.LLMError as e:
        logger.error('Агент недоступен: %s', e)
        raise HTTPException(503, 'Модель временно недоступна, попробуйте ещё раз') from e


@app.post('/api/chat/{cid}/operator')
def call_operator(cid: str) -> dict[str, Any]:
    """Кнопка «Позвать оператора»: клиент сам переключается на менеджера (требование заказчика)."""
    c = _conv(cid)
    if c['status'] == 'escalated':
        return {'status': 'already'}
    last = [m['content'] for m in db.rows("SELECT content FROM messages WHERE conv_id=? AND role='user' ORDER BY id DESC LIMIT 3", (cid,))]
    summary = 'Клиент нажал «Позвать оператора». Последние вопросы: ' + (' | '.join(reversed(last)) or 'диалог только начат')
    eid = db.execute('INSERT INTO escalations (conv_id, reason, category, summary, created_at) VALUES (?,?,?,?,?)',
                     (cid, 'client_request', agent.REASON_RU['client_request'], summary[:2000], db.now()))
    db.execute("UPDATE conversations SET status='escalated', updated_at=? WHERE id=?", (db.now(), cid))
    text = 'Подключаю менеджера Softline — он ответит здесь же, в чате. Обычно это занимает пару минут в рабочее время.'
    mid = db.execute('INSERT INTO messages (conv_id, role, content, meta, created_at) VALUES (?,?,?,?,?)',
                     (cid, 'assistant', text, db.dumps({'escalation': {'id': eid, 'reason': 'Клиент просит менеджера'}}), db.now()))
    return {'status': 'escalated', 'message': {'id': mid, 'role': 'assistant', 'content': text,
                                               'meta': {'escalation': {'id': eid, 'reason': 'Клиент просит менеджера'}}}}


@app.websocket('/ws/chat/{cid}')
async def ws_chat(ws: WebSocket, cid: str) -> None:
    await hub.client_socket(ws, cid)


@app.websocket('/ws/manager')
async def ws_manager(ws: WebSocket, token: str = Query('')) -> None:
    from fastapi.security import HTTPAuthorizationCredentials

    try:
        auth.manager(HTTPAuthorizationCredentials(scheme='Bearer', credentials=token))
    except HTTPException:
        await ws.close(code=4401)
        return
    await hub.manager_socket(ws)


@app.post('/api/quotes/{qid}/pay')
async def pay(qid: str, request: Request) -> dict[str, Any]:
    """Ссылка на оплату через MCP ЮKassa — только для своего документа, сумма из КП/счёта."""
    _limit(request, 10)
    try:
        p = await run_in_threadpool(payments.create_for_quote, qid, 'client')
    except payments.PaymentError as e:
        raise HTTPException(400, str(e)) from e
    return {'url': p['url'], 'status': p['status'], 'id': p['id']}


@app.get('/api/payments/demo/{pid}')
def demo_payment(pid: str) -> dict[str, Any]:
    p = db.one('SELECT p.id, p.amount, p.status, q.number FROM payments p LEFT JOIN quotes q ON q.id=p.quote_id WHERE p.id=?', (pid,))
    if not p or not pid.startswith('demo-'):
        raise HTTPException(404, 'Платёж не найден')
    return p


@app.post('/api/payments/demo/{pid}/{action}')
def demo_payment_action(pid: str, action: str) -> dict[str, Any]:
    try:
        return payments.demo_confirm(pid, action == 'pay')
    except payments.PaymentError as e:
        raise HTTPException(404, str(e)) from e


@app.get('/api/quotes/{qid}/payment')
async def payment_status(qid: str) -> dict[str, Any]:
    p = await run_in_threadpool(payments.for_quote, qid)
    return {'payment': p}


@app.post('/api/chat/{cid}/feedback')
def feedback(cid: str, body: FeedbackIn) -> dict[str, str]:
    _conv(cid)
    learning.rate(cid, body.message_id, body.rating)
    return {'status': 'ok'}


@app.post('/api/chat/{cid}/survey')
def chat_survey(cid: str, body: SurveyIn) -> dict[str, str]:
    """Опрос в конце диалога: «Ответьте, пожалуйста, — это помогает обучать ассистента»."""
    _conv(cid)
    learning.survey(cid, body.resolved, body.score, pii.mask(cid, body.comment))
    return {'status': 'ok'}


@app.post('/api/quotes/{qid}/requisites')
async def requisites(qid: str, body: RequisitesIn) -> dict[str, Any]:
    q = quotes.get(qid)
    if not q:
        raise HTTPException(404, 'КП не найдено')
    try:
        q = await run_in_threadpool(quotes.set_requisites, qid, body.model_dump())
    except quotes.QuoteError as e:
        raise HTTPException(400, str(e)) from e
    doc = 'Счёт на оплату' if q.get('kind') == 'invoice' else 'Коммерческое предложение'
    total = f'{q["total"]:,.2f}'.replace(',', ' ').replace('.', ',')
    text = (f'{doc} {q["number"]} на сумму {total} ₽ сформирован. ' +
            'PDF можно скачать по кнопке ниже; менеджер проверит документ и при необходимости свяжется с вами.')
    meta = {'quote': {'quote_id': qid, 'number': q['number'], 'total_rub': q['total'], 'status': q['status'], 'kind': q.get('kind'),
                      'pdf': f'/api/quotes/{qid}/pdf'}}
    db.execute('INSERT INTO messages (conv_id, role, content, meta, created_at) VALUES (?,?,?,?,?)', (q['conv_id'], 'assistant', text, db.dumps(meta), db.now()))
    return {'quote_id': qid, 'number': q['number'], 'status': q['status'], 'pdf': f'/api/quotes/{qid}/pdf'}


@app.get('/api/quotes/{qid}/pdf')
def quote_pdf(qid: str) -> FileResponse:
    q = quotes.get(qid)
    if not q or q['status'] == 'awaiting_requisites':
        raise HTTPException(404, 'КП ещё не сформировано')
    p = quotes.pdf_path(qid)
    if not p.exists():
        quotes.render_pdf(q)
    return FileResponse(p, media_type='application/pdf', filename=f'{q["number"]}.pdf')


# ------------------------------------------------------------------ менеджер

class LoginIn(BaseModel):
    login: str
    password: str


class AnswerIn(BaseModel):
    answer: str = Field(min_length=1, max_length=6000)
    add_to_kb: bool = True
    close: bool = True


class KbIn(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=3, max_length=8000)


@app.post('/api/auth/login')
def do_login(body: LoginIn, request: Request) -> dict[str, str]:
    return auth.login(body.login, body.password, request.client.host if request.client else '-')


@app.get('/api/manager/escalations')
def escalations(status: str = 'open', m: dict = Depends(auth.manager)) -> list[dict[str, Any]]:
    sql = 'SELECT e.*, c.title, c.scenario FROM escalations e JOIN conversations c ON c.id=e.conv_id'
    rs = db.rows(sql + (' WHERE e.status=?' if status != 'all' else '') + ' ORDER BY e.id DESC LIMIT 200', (status,) if status != 'all' else ())
    for r in rs:
        r['summary'] = pii.unmask(r['conv_id'], r['summary'])
        r['title'] = pii.unmask(r['conv_id'], r['title'] or '')
    return rs


@app.post('/api/manager/escalations/{eid}/answer')
def answer(eid: int, body: AnswerIn, m: dict = Depends(auth.manager)) -> dict[str, Any]:
    e = db.one('SELECT * FROM escalations WHERE id=?', (eid,))
    if not e:
        raise HTTPException(404, 'Эскалация не найдена')
    db.execute('INSERT INTO messages (conv_id, role, content, meta, created_at) VALUES (?,?,?,?,?)',
               (e['conv_id'], 'manager', body.answer, db.dumps({'manager': m['name']}), db.now()))
    db.execute("UPDATE escalations SET status='answered', answer=?, manager=?, add_to_kb=?, answered_at=? WHERE id=?",
               (body.answer, m['name'], int(body.add_to_kb), db.now(), eid))
    db.execute('UPDATE conversations SET status=?, updated_at=? WHERE id=?', ('answered' if body.close else 'escalated', db.now(), e['conv_id']))
    kb_id = None
    if body.add_to_kb:
        q = db.one("SELECT content FROM messages WHERE conv_id=? AND role='user' ORDER BY id DESC LIMIT 1", (e['conv_id'],))
        question = (q or {}).get('content') or e['summary']
        kb_id = kb.add_manager_answer(question, pii.mask(e['conv_id'], body.answer), m['name'])
    return {'status': 'answered', 'kb_id': kb_id}


@app.get('/api/manager/conversations')
def conversations(m: dict = Depends(auth.manager)) -> list[dict[str, Any]]:
    rs = db.rows('SELECT c.*, (SELECT count(*) FROM messages WHERE conv_id=c.id) AS n_messages, '
                 '(SELECT round(sum(cost_rub), 4) FROM llm_calls WHERE conv_id=c.id) AS cost_rub '
                 'FROM conversations c ORDER BY c.updated_at DESC LIMIT 300')
    for r in rs:
        r['title'] = pii.unmask(r['id'], r['title'] or '')
    return rs


@app.get('/api/manager/conversations/{cid}')
def conversation(cid: str, m: dict = Depends(auth.manager)) -> dict[str, Any]:
    c = _conv(cid)
    return {**c, 'messages': _messages(cid, internal=True),
            'tool_calls': db.rows('SELECT tool, args, substr(result,1,1500) AS result, ms, created_at FROM tool_calls WHERE conv_id=? ORDER BY id', (cid,)),
            'llm_calls': db.rows('SELECT provider, model, tokens_in, tokens_out, cost_rub, ms, ok, error, created_at FROM llm_calls WHERE conv_id=? ORDER BY id', (cid,)),
            'quotes': db.rows('SELECT id, number, total, status, created_at FROM quotes WHERE conv_id=?', (cid,)),
            'pii_masked': db.rows('SELECT token, kind FROM pii WHERE conv_id=?', (cid,))}


class ManagerMessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=6000)


class TakeoverIn(BaseModel):
    on: bool = True


@app.post('/api/manager/conversations/{cid}/takeover')
def takeover(cid: str, body: TakeoverIn, m: dict = Depends(auth.manager)) -> dict[str, Any]:
    """Менеджер берёт диалог (агент замолкает) или возвращает его агенту."""
    _conv(cid)
    db.execute('UPDATE conversations SET mode=?, manager=?, updated_at=? WHERE id=?',
               ('manager' if body.on else 'agent', m['name'] if body.on else '', db.now(), cid))
    text = (f'К диалогу подключился {m["name"]}. Отвечаю вам лично.' if body.on else
            'Менеджер передал диалог ИИ-ассистенту. Я на связи — задавайте вопросы.')
    db.execute('INSERT INTO messages (conv_id, role, content, meta, created_at) VALUES (?,?,?,?,?)', (cid, 'system', text, '{}', db.now()))
    if body.on:
        db.execute("UPDATE escalations SET status='answered', manager=?, answered_at=? WHERE conv_id=? AND status='open'", (m['name'], db.now(), cid))
    return {'mode': 'manager' if body.on else 'agent'}


@app.post('/api/manager/conversations/{cid}/message')
def manager_message(cid: str, body: ManagerMessageIn, m: dict = Depends(auth.manager)) -> dict[str, Any]:
    c = _conv(cid)
    if c.get('mode') != 'manager':
        db.execute('UPDATE conversations SET mode=?, manager=? WHERE id=?', ('manager', m['name'], cid))
    mid = db.execute('INSERT INTO messages (conv_id, role, content, meta, created_at) VALUES (?,?,?,?,?)',
                     (cid, 'manager', body.text.strip(), db.dumps({'manager': m['name']}), db.now()))
    db.execute('UPDATE conversations SET updated_at=? WHERE id=?', (db.now(), cid))
    return {'id': mid}


@app.post('/api/manager/quotes/{qid}/payment-link')
async def manager_payment_link(qid: str, m: dict = Depends(auth.manager)) -> dict[str, Any]:
    try:
        p = await run_in_threadpool(payments.create_for_quote, qid, m['name'])
    except payments.PaymentError as e:
        raise HTTPException(400, str(e)) from e
    q = quotes.get(qid)
    db.execute('INSERT INTO messages (conv_id, role, content, meta, created_at) VALUES (?,?,?,?,?)',
               (q['conv_id'], 'manager', f'Ссылка на онлайн-оплату {q["number"]} через ЮKassa: {p["url"]}',
                db.dumps({'manager': m['name'], 'payment': {'url': p['url'], 'status': p['status']}}), db.now()))
    return p


@app.get('/api/manager/payments')
async def manager_payments(m: dict = Depends(auth.manager)) -> dict[str, Any]:
    return {'enabled': payments.enabled(), 'mode': payments.mode(), 'items': await run_in_threadpool(payments.all_payments)}


@app.get('/api/manager/quotes')
def list_quotes(m: dict = Depends(auth.manager)) -> list[dict[str, Any]]:
    rs = db.rows('SELECT * FROM quotes ORDER BY created_at DESC LIMIT 200')
    for r in rs:
        r['items'] = db.loads(r['items'], [])
        r['customer'] = db.loads(r['customer'], {})
    return rs


@app.post('/api/manager/quotes/{qid}/approve')
async def approve(qid: str, m: dict = Depends(auth.manager)) -> dict[str, Any]:
    q = quotes.get(qid)
    if not q or q['status'] == 'awaiting_requisites':
        raise HTTPException(400, 'КП ещё без реквизитов клиента')
    q = await run_in_threadpool(quotes.approve, qid, m['name'])
    db.execute('INSERT INTO messages (conv_id, role, content, meta, created_at) VALUES (?,?,?,?,?)',
               (q['conv_id'], 'manager', f'Коммерческое предложение {q["number"]} проверено и подтверждено. Итоговый PDF — по кнопке ниже.',
                db.dumps({'quote': {'quote_id': qid, 'number': q['number'], 'total_rub': q['total'], 'status': 'approved', 'pdf': f'/api/quotes/{qid}/pdf'}}), db.now()))
    return {'status': 'approved'}


@app.get('/api/manager/quotes/{qid}/pdf')
def manager_pdf(qid: str, m: dict = Depends(auth.manager)) -> FileResponse:
    return quote_pdf(qid)


@app.get('/api/manager/feedback')
def feedback_list(m: dict = Depends(auth.manager)) -> dict[str, Any]:
    items = learning.negatives()
    for r in items:
        r['question'] = pii.unmask(r['conv_id'], r['question'])
    return {'negatives': items, 'stats': learning.stats()}


@app.post('/api/manager/feedback/{fid}/fix')
def feedback_fix(fid: int, body: FixIn, m: dict = Depends(auth.manager)) -> dict[str, Any]:
    try:
        return {'kb_id': learning.fix(fid, body.answer, m['name'])}
    except KeyError:
        raise HTTPException(404, 'Отзыв не найден') from None


@app.get('/api/manager/kb')
def kb_list(m: dict = Depends(auth.manager)) -> list[dict[str, Any]]:
    return kb.articles()


@app.post('/api/manager/kb')
def kb_add(body: KbIn, m: dict = Depends(auth.manager)) -> dict[str, Any]:
    return {'id': kb.add_manager_answer(body.title, body.body, m['name'])}


@app.get('/api/manager/metrics')
def metrics(m: dict = Depends(auth.manager)) -> dict[str, Any]:
    one = lambda sql, p=(): (db.one(sql, p) or {}).get('v') or 0  # noqa: E731
    convs = one("SELECT count(*) v FROM conversations WHERE id IN (SELECT conv_id FROM messages WHERE role='user')")
    escal = one('SELECT count(DISTINCT conv_id) v FROM escalations')
    user_msgs = one("SELECT count(*) v FROM messages WHERE role='user'")
    cost = one('SELECT sum(cost_rub) v FROM llm_calls')
    return {
        'conversations': convs, 'user_messages': user_msgs,
        'resolved_without_human': convs - escal, 'automation_rate': round((convs - escal) / convs, 3) if convs else 0,
        'escalations_open': one("SELECT count(*) v FROM escalations WHERE status='open'"),
        'escalations_by_reason': db.rows('SELECT category, count(*) AS n FROM escalations GROUP BY category ORDER BY n DESC'),
        'scenarios': db.rows("SELECT CASE scenario WHEN '' THEN 'other' ELSE scenario END AS scenario, count(*) AS n FROM conversations "
                             "WHERE id IN (SELECT conv_id FROM messages WHERE role='user') GROUP BY 1"),
        'quotes': db.rows('SELECT status, count(*) AS n, round(sum(total), 2) AS total_rub FROM quotes GROUP BY status'),
        'kb_from_managers': one("SELECT count(*) v FROM kb WHERE source='manager'"),
        'llm': db.rows('SELECT provider, count(*) AS calls, sum(ok) AS ok, sum(tokens_in) AS tokens_in, sum(tokens_out) AS tokens_out, '
                       'round(sum(cost_rub), 4) AS cost_rub, round(avg(ms)) AS avg_ms FROM llm_calls GROUP BY provider'),
        'cost_total_rub': round(cost, 4),
        'cost_per_request_rub': round(cost / user_msgs, 4) if user_msgs else 0,
        'feedback': db.rows('SELECT rating, count(*) AS n FROM feedback GROUP BY rating'),
        'learning': learning.stats(),
        'tools': db.rows('SELECT tool, count(*) AS n, round(avg(ms)) AS avg_ms FROM tool_calls GROUP BY tool ORDER BY n DESC'),
        'catalog': catalog.stats(),
    }


@app.post('/api/manager/catalog/reload')
async def reload_catalog(m: dict = Depends(auth.manager)) -> dict[str, Any]:
    return await run_in_threadpool(catalog.ingest, None)


# ------------------------------------------------------------------ материалы для жюри (публичная папка без авторизации)

MATERIALS = ROOT_DIR / 'docs' / 'public'
if MATERIALS.is_dir():
    app.mount('/materials', StaticFiles(directory=MATERIALS, html=True), name='materials')


# ------------------------------------------------------------------ SPA

if (STATIC / 'index.html').exists():
    app.mount('/assets', StaticFiles(directory=STATIC / 'assets'), name='assets')

    @app.get('/{path:path}', include_in_schema=False)
    def spa(path: str):
        if path.startswith('api/') or any(part.startswith('.') for part in path.split('/')):
            return JSONResponse({'detail': 'Not found'}, status_code=404)
        f = STATIC / path
        if path and f.is_file() and STATIC in f.resolve().parents:
            return FileResponse(f)
        return FileResponse(STATIC / 'index.html')
