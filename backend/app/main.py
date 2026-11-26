"""FastAPI: веб-чат клиента, кабинет менеджера и раздача интерфейса (SPA)."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import agent, auth, catalog, db, kb, llm, pii, quotes
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
            if not get_settings().catalog_db.exists() or time.time() - get_settings().catalog_db.stat().st_mtime > 86400:
                try:
                    catalog.ingest()
                except Exception:  # noqa: BLE001 — ошибка загрузки не роняет сервис, работаем на прежнем каталоге
                    logger.exception('Не удалось обновить каталог из фида')
            time.sleep(3600)

    threading.Thread(target=refresh_loop, daemon=True, name='feed-refresh').start()
    yield


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


@app.get('/api/health')
def health() -> dict[str, Any]:
    return {'status': 'ok', 'catalog': catalog.stats(), 'models': [p.name for p in llm.chain()]}


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
    _conv(cid)
    try:
        return await run_in_threadpool(agent.reply, cid, body.text)
    except llm.LLMError as e:
        logger.error('Агент недоступен: %s', e)
        raise HTTPException(503, 'Модель временно недоступна, попробуйте ещё раз') from e


@app.post('/api/chat/{cid}/feedback')
def feedback(cid: str, body: FeedbackIn) -> dict[str, str]:
    _conv(cid)
    db.execute('INSERT INTO feedback (conv_id, message_id, rating, created_at) VALUES (?,?,?,?)', (cid, body.message_id, body.rating, db.now()))
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
    text = (f'Черновик коммерческого предложения {q["number"]} на сумму {q["total"]:,.2f} ₽ сформирован. '
            'Менеджер проверит его и свяжется с вами; PDF можно скачать по кнопке ниже.').replace(',', ' ')
    meta = {'quote': {'quote_id': qid, 'number': q['number'], 'total_rub': q['total'], 'status': q['status'], 'pdf': f'/api/quotes/{qid}/pdf'}}
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
        'tools': db.rows('SELECT tool, count(*) AS n, round(avg(ms)) AS avg_ms FROM tool_calls GROUP BY tool ORDER BY n DESC'),
        'catalog': catalog.stats(),
    }


@app.post('/api/manager/catalog/reload')
async def reload_catalog(m: dict = Depends(auth.manager)) -> dict[str, Any]:
    return await run_in_threadpool(catalog.ingest, None)


# ------------------------------------------------------------------ SPA

if (STATIC / 'index.html').exists():
    app.mount('/assets', StaticFiles(directory=STATIC / 'assets'), name='assets')

    @app.get('/{path:path}', include_in_schema=False)
    def spa(path: str):
        if path.startswith('api/'):
            return JSONResponse({'detail': 'Not found'}, status_code=404)
        f = STATIC / path
        if path and f.is_file() and STATIC in f.resolve().parents:
            return FileResponse(f)
        return FileResponse(STATIC / 'index.html')
