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

from app import agent, auth, catalog, db, discounts, handoff, hub, kb, learning, llm, payments, pii, quotes, team, telegram
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
    logging.getLogger('httpx').setLevel(logging.WARNING)  # URL запросов к Telegram содержит токен бота — не пишем их в журнал
    db.init_db()
    team.init()
    n = kb.load_base()
    logger.info('База знаний: %d статей; каталог: %s; модели: %s', n, catalog.stats(), [p.name for p in llm.chain()])

    def refresh_loop() -> None:
        # Каталог: при первом старте — загрузка фида, дальше раз в сутки (по ответу заказчика этого достаточно)
        while True:
            try:
                purge_pii()
            except Exception:  # noqa: BLE001
                logger.exception('Очистка ПДн')
            st = catalog.stats()
            if not st.get('offers') or time.time() - get_settings().catalog_db.stat().st_mtime > 86400:
                try:
                    catalog.ingest()
                except Exception:  # noqa: BLE001 — ошибка загрузки не роняет сервис, работаем на прежнем каталоге
                    logger.exception('Не удалось обновить каталог из фида')
            time.sleep(3600)

    threading.Thread(target=refresh_loop, daemon=True, name='feed-refresh').start()
    payments.init()
    discounts.init()
    telegram.init()
    telegram.start()
    team.start()
    import asyncio

    task = asyncio.create_task(hub.broadcaster())
    yield
    task.cancel()


app = FastAPI(title='ИИ-агент поддержки store.softline.ru', version='1.0.0', lifespan=lifespan, docs_url='/api/docs', openapi_url='/api/openapi.json')


def _limit(request: Request, n: int = 40, window: int = 600, bucket: str = 'chat') -> None:
    """Ограничение частоты по IP — отдельный счётчик для каждого вида запросов (поиск не съедает лимит сообщений)."""
    ip = request.headers.get('x-forwarded-for', request.client.host if request.client else '-').split(',')[0].strip()
    key = f'{bucket}:{ip}'
    hits = [t for t in _rate.get(key, []) if time.time() - t < window]
    if len(hits) >= n:
        raise HTTPException(429, 'Слишком много запросов, попробуйте через несколько минут')
    _rate[key] = [*hits, time.time()]


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
    return {'status': 'ok', 'catalog': catalog.stats(), 'models': [p.name for p in llm.chain()], 'payments': payments.enabled(), 'payments_mode': payments.mode(), 'telegram': telegram.enabled()}


@app.get('/api/catalog/search')
def catalog_search(q: str = Query('', max_length=200), request: Request = None) -> dict[str, Any]:
    """Поисковая строка витрины: продуктовые линейки по запросу (тот же поиск, что у агента)."""
    if request:
        _limit(request, 120, 60, 'search')
    if len(q.strip()) < 2:
        return {'items': []}
    res = catalog.search(q, {'only_available': False}, limit=8)
    return {'items': [{'group_id': r['group_id'], 'product': r['product'], 'vendor': r['vendor'], 'category': r['category'],
                       'url': r['url'], 'price_from_rub': r['price_from_rub'], 'domestic': r['domestic'],
                       'variants': r['variants_available']} for r in res]}


@app.post('/api/chat/start')
def start(body: StartIn, request: Request) -> dict[str, Any]:
    _limit(request, 20, bucket='start')
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
    if not c.get('title'):  # заголовок диалога для кабинета — сразу по первому сообщению, не дожидаясь ответа агента
        db.execute("UPDATE conversations SET title=? WHERE id=? AND (title IS NULL OR title='')", (pii.mask(cid, body.text.strip())[:80], cid))
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


class HistoryIn(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=50)


def _safe_ids(ids: list[str]) -> list[str]:
    import re

    return [i for i in ids if re.fullmatch(r'[a-z0-9]{6,40}', i)][:50]


@app.post('/api/chat/history')
def chat_history(body: HistoryIn) -> dict[str, Any]:
    """История чатов клиента на этом устройстве: браузер хранит id своих диалогов (знание id = доступ, как к самому диалогу)."""
    ids = _safe_ids(body.ids)
    if not ids:
        return {'items': [], 'retention_days': PII_RETENTION_DAYS}
    q = ','.join('?' * len(ids))
    rows = db.rows(f"SELECT c.id, c.title, c.created_at, c.updated_at, c.status, c.mode, "
                   f"(SELECT count(*) FROM messages m WHERE m.conv_id=c.id AND m.role='user') AS n_user "
                   f"FROM conversations c WHERE c.id IN ({q}) ORDER BY c.updated_at DESC", tuple(ids))
    for r in rows:
        r['title'] = pii.unmask(r['id'], r['title'] or '') or 'Диалог без сообщений'
    return {'items': [r for r in rows if r['n_user']], 'retention_days': PII_RETENTION_DAYS}


@app.post('/api/chat/forget')
def chat_forget(body: HistoryIn) -> dict[str, Any]:
    """«Удалить историю»: персональные данные этих диалогов удаляются (таблица соответствия меток), переписка остаётся
    только в обезличенном виде — для качества и обучения агента. Клиент убирает диалоги из списка на устройстве."""
    ids = _safe_ids(body.ids)
    for cid in ids:
        db.execute('DELETE FROM pii WHERE conv_id=?', (cid,))
        db.execute("UPDATE conversations SET page_url='' WHERE id=?", (cid,))
    return {'forgotten': len(ids)}


PII_RETENTION_DAYS = 180  # срок хранения персональных данных из чата; дальше переписка хранится обезличенной


def purge_pii() -> int:
    """Ежедневно: у диалогов без активности дольше PII_RETENTION_DAYS удаляются персональные данные (152-ФЗ, ст. 5 ч. 7)."""
    old = db.rows('SELECT DISTINCT p.conv_id FROM pii p JOIN conversations c ON c.id=p.conv_id WHERE c.updated_at < ?',
                  (db.now() - PII_RETENTION_DAYS * 86400,))
    for r in old:
        db.execute('DELETE FROM pii WHERE conv_id=?', (r['conv_id'],))
    if old:
        logger.info('Удалены ПДн старых диалогов: %d', len(old))
    return len(old)


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
    db.execute("UPDATE conversations SET status='escalated', updated_at=?, title=CASE WHEN title IS NULL OR title='' "
               "THEN 'Клиент позвал оператора' ELSE title END WHERE id=?", (db.now(), cid))
    team.dispatch()
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
    _limit(request, 10, bucket='pay')
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
            'PDF можно скачать по кнопке ниже — документ готов к оплате.')
    meta = {'quote': {'quote_id': qid, 'number': q['number'], 'total_rub': q['total'], 'status': q['status'], 'kind': q.get('kind'), 'discount': q.get('discount'),
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


class InviteIn(BaseModel):
    code: str = Field(min_length=10, max_length=100)


@app.post('/api/auth/invite')
def do_invite(body: InviteIn, request: Request) -> dict[str, str]:
    """Вход нового менеджера по одноразовой ссылке-приглашению от администратора."""
    return auth.by_invite(body.code, request.client.host if request.client else '-')


# ------------------------------------------------------------------ профиль, присутствие, команда

class PresenceIn(BaseModel):
    status: str | None = Field(None, pattern='^(online|away|offline)$')


class PasswordIn(BaseModel):
    password: str = Field(min_length=8, max_length=200)


@app.get('/api/manager/me')
def me(m: dict = Depends(auth.manager)) -> dict[str, Any]:
    u = team.get(m['login'])
    return {**team.public(u), 'load': team.load(m['login'])}


@app.post('/api/manager/presence')
def presence(body: PresenceIn, m: dict = Depends(auth.manager)) -> dict[str, Any]:
    """Сигнал «кабинет открыт» (раз в 30 с) и смена статуса: В сети / Перерыв / Не в сети."""
    return {**team.heartbeat(m['login'], body.status), 'load': team.load(m['login'])}


@app.post('/api/manager/password')
def set_password(body: PasswordIn, m: dict = Depends(auth.manager)) -> dict[str, Any]:
    from app.config import get_settings

    s = get_settings()
    if m['login'] in (s.manager_login, s.jury_login):
        raise HTTPException(400, 'Пароль этой учётной записи задаётся в .env на сервере')
    team.set_password(m['login'], body.password)
    return {'ok': True}


@app.get('/api/manager/team')
def team_list(m: dict = Depends(auth.manager)) -> dict[str, Any]:
    """Коллеги со статусом и нагрузкой — для «Передать» и «Позвать коллегу», очередь обращений."""
    return {'members': [{k: u[k] for k in ('login', 'name', 'role', 'online', 'status', 'status_ru', 'load', 'max_load', 'active')}
                        for u in team.roster() if u['active']],
            'queue': db.one("SELECT count(*) AS n FROM escalations WHERE status='open' AND (assignee IS NULL OR assignee='')")['n'],
            'auto_assign': team.auto_assign(), 'reassign_seconds': team.reassign_seconds()}


@app.get('/api/manager/escalations')
def escalations(status: str = 'open', m: dict = Depends(auth.manager)) -> list[dict[str, Any]]:
    sql = 'SELECT e.*, c.title, c.scenario, u.name AS assignee_name FROM escalations e JOIN conversations c ON c.id=e.conv_id LEFT JOIN users u ON u.login=e.assignee'
    if status == 'mine':
        rs = db.rows(sql + " WHERE e.status='open' AND e.assignee=? ORDER BY e.id DESC LIMIT 200", (m['login'],))
    elif status == 'queue':
        rs = db.rows(sql + " WHERE e.status='open' AND (e.assignee IS NULL OR e.assignee='') ORDER BY e.id LIMIT 200")
    else:
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
               (e['conv_id'], 'manager', body.answer, db.dumps({'manager': m['name'], 'login': m['login']}), db.now()))
    db.execute("UPDATE escalations SET status='answered', answer=?, manager=?, assignee=?, add_to_kb=?, answered_at=? WHERE id=?",
               (body.answer, m['name'], m['login'], int(body.add_to_kb), db.now(), eid))
    team._log(e['conv_id'], eid, 'answer', m['login'])
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
                 '(SELECT round(sum(cost_rub), 4) FROM llm_calls WHERE conv_id=c.id) AS cost_rub, '
                 "(SELECT u.name FROM escalations e JOIN users u ON u.login=e.assignee WHERE e.conv_id=c.id AND e.status='open' LIMIT 1) AS assignee_name, "
                 "(SELECT group_concat(login) FROM conv_managers WHERE conv_id=c.id) AS participants, "
                 "(SELECT count(*) FROM messages WHERE conv_id=c.id AND role='user') AS n_user, "
                 "(SELECT count(*) FROM escalations WHERE conv_id=c.id AND status='open') AS open_esc "
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
            'pii_masked': db.rows('SELECT token, kind FROM pii WHERE conv_id=?', (cid,)),
            'participants': team.participants(cid),
            'team_log': db.rows('SELECT action, actor, target, created_at FROM team_log WHERE conv_id=? ORDER BY id', (cid,))}


class ManagerMessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=6000)


class TakeoverIn(BaseModel):
    on: bool = True


@app.post('/api/manager/conversations/{cid}/takeover')
def takeover(cid: str, body: TakeoverIn, m: dict = Depends(auth.manager)) -> dict[str, Any]:
    """Менеджер берёт диалог (агент замолкает) или возвращает его агенту."""
    _conv(cid)
    return handoff.take(cid, m['name'], body.on, login=m['login'])


@app.post('/api/manager/conversations/{cid}/message')
def manager_message(cid: str, body: ManagerMessageIn, m: dict = Depends(auth.manager)) -> dict[str, Any]:
    _conv(cid)
    return {'id': handoff.say(cid, m['name'], body.text, login=m['login'])}


class ColleagueIn(BaseModel):
    login: str = Field(min_length=1, max_length=64)


@app.post('/api/manager/conversations/{cid}/transfer')
def transfer(cid: str, body: ColleagueIn, m: dict = Depends(auth.manager)) -> dict[str, Any]:
    """Передать диалог коллеге (он становится ведущим)."""
    _conv(cid)
    try:
        return handoff.transfer(cid, m, body.login)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post('/api/manager/conversations/{cid}/invite')
def invite_colleague(cid: str, body: ColleagueIn, m: dict = Depends(auth.manager)) -> dict[str, Any]:
    """Позвать коллегу в диалог помощником — оба видят переписку и могут отвечать клиенту."""
    _conv(cid)
    try:
        return handoff.invite(cid, m, body.login)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post('/api/manager/conversations/{cid}/leave')
def leave_dialog(cid: str, m: dict = Depends(auth.manager)) -> dict[str, Any]:
    team.leave(cid, m['login'])
    team._log(cid, None, 'leave', m['login'])
    return {'participants': team.participants(cid)}


@app.post('/api/manager/escalations/{eid}/claim')
def claim(eid: int, m: dict = Depends(auth.manager)) -> dict[str, Any]:
    """«Взять себе» обращение из очереди или у коллеги — не дожидаясь автоназначения."""
    e = db.one('SELECT * FROM escalations WHERE id=?', (eid,))
    if not e:
        raise HTTPException(404, 'Эскалация не найдена')
    team.assign(e, m['login'], actor=m['login'])
    return {'assignee': m['login']}


class CloseIn(BaseModel):
    note: str = Field('', max_length=300)


@app.post('/api/manager/escalations/{eid}/close')
def close_escalation(eid: int, body: CloseIn, m: dict = Depends(auth.manager)) -> dict[str, Any]:
    """Закрыть без ответа (дубль, спам, клиент ушёл) — обращение уходит из очереди, данные сохраняются."""
    e = db.one('SELECT * FROM escalations WHERE id=?', (eid,))
    if not e:
        raise HTTPException(404, 'Эскалация не найдена')
    db.execute("UPDATE escalations SET status='closed', manager=?, answer=?, answered_at=? WHERE id=? AND status='open'",
               (m['name'], body.note or 'Закрыто без ответа', db.now(), eid))
    team._log(e['conv_id'], eid, 'close', m['login'])
    db.execute("UPDATE conversations SET status='closed' WHERE id=? AND mode!='manager' AND NOT EXISTS "
               "(SELECT 1 FROM escalations WHERE conv_id=? AND status='open')", (e['conv_id'], e['conv_id']))
    return {'status': 'closed'}


class StaleIn(BaseModel):
    older_than_hours: float = Field(24, ge=0.5, le=24 * 90)


# ------------------------------------------------------------------ администратор: команда и распределение


@app.post('/api/admin/escalations/close-stale')
def close_stale(body: StaleIn, m: dict = Depends(auth.admin)) -> dict[str, Any]:
    """Разобрать очередь: закрыть открытые обращения старше N часов (клиент давно ушёл). Статус меняется, данные остаются."""
    rs = db.rows("SELECT id, conv_id FROM escalations WHERE status='open' AND created_at < ?", (db.now() - body.older_than_hours * 3600,))
    for e in rs:
        db.execute("UPDATE escalations SET status='closed', manager=?, answer='Закрыто администратором: устарело', answered_at=? WHERE id=?",
                   (m['name'], db.now(), e['id']))
        team._log(e['conv_id'], e['id'], 'close', m['login'])
        db.execute("UPDATE conversations SET status='closed' WHERE id=? AND mode!='manager'", (e['conv_id'],))
    team.dispatch()
    return {'closed': len(rs)}

class UserIn(BaseModel):
    login: str = Field(min_length=3, max_length=40, pattern='^[a-zA-Z0-9._-]+$')
    name: str = Field(min_length=2, max_length=80)
    role: str = Field('manager', pattern='^(admin|manager)$')
    max_load: int = Field(5, ge=1, le=30)
    routing: bool = True


class UserPatch(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=80)
    role: str | None = Field(None, pattern='^(admin|manager)$')
    max_load: int | None = Field(None, ge=1, le=30)
    routing: bool | None = None
    active: bool | None = None
    status: str | None = Field(None, pattern='^(online|away|offline)$')


class RoutingIn(BaseModel):
    auto_assign: bool | None = None
    reassign_seconds: int | None = Field(None, ge=60, le=3600)


@app.get('/api/admin/users')
def admin_users(m: dict = Depends(auth.admin)) -> dict[str, Any]:
    return {'users': team.roster(), 'auto_assign': team.auto_assign(), 'reassign_seconds': team.reassign_seconds(),
            'queue': db.one("SELECT count(*) AS n FROM escalations WHERE status='open' AND (assignee IS NULL OR assignee='')")['n'],
            'open': db.one("SELECT count(*) AS n FROM escalations WHERE status='open'")['n']}


@app.post('/api/admin/users')
def admin_create(body: UserIn, m: dict = Depends(auth.admin)) -> dict[str, Any]:
    try:
        return team.create(body.login, body.name, body.role, body.max_load, body.routing, m['login'])
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.patch('/api/admin/users/{login}')
def admin_update(login: str, body: UserPatch, m: dict = Depends(auth.admin)) -> dict[str, Any]:
    from app.config import get_settings

    if not team.get(login):
        raise HTTPException(404, 'Нет такого пользователя')
    if login == m['login'] and (body.active is False or body.role == 'manager'):
        raise HTTPException(400, 'Нельзя отключить себя или снять с себя роль администратора')
    if login in (get_settings().manager_login, get_settings().jury_login) and (body.active is False or body.role == 'manager'):
        raise HTTPException(400, 'Учётка из .env — нельзя отключить или понизить')
    f = body.model_dump()
    for k in ('routing', 'active'):
        if f[k] is not None:
            f[k] = int(f[k])
    team._log('', None, 'user_update', m['login'], login)
    return team.update(login, **f)


@app.post('/api/admin/users/{login}/invite')
def admin_reinvite(login: str, m: dict = Depends(auth.admin)) -> dict[str, Any]:
    if not team.get(login):
        raise HTTPException(404, 'Нет такого пользователя')
    return team.new_invite(login)


@app.post('/api/admin/routing')
def admin_routing(body: RoutingIn, m: dict = Depends(auth.admin)) -> dict[str, Any]:
    if body.auto_assign is not None:
        discounts.set_setting('auto_assign', int(body.auto_assign))
    if body.reassign_seconds is not None:
        discounts.set_setting('reassign_seconds', body.reassign_seconds)
    team.dispatch()
    return {'auto_assign': team.auto_assign(), 'reassign_seconds': team.reassign_seconds()}


@app.get('/api/admin/log')
def admin_log(m: dict = Depends(auth.admin)) -> list[dict[str, Any]]:
    return db.rows('SELECT l.*, u.name AS actor_name, t.name AS target_name FROM team_log l LEFT JOIN users u ON u.login=l.actor '
                   'LEFT JOIN users t ON t.login=l.target ORDER BY l.id DESC LIMIT 200')


# ------------------------------------------------------------------ Telegram-бот менеджера

@app.get('/api/manager/telegram')
def telegram_status(m: dict = Depends(auth.manager)) -> dict[str, Any]:
    chats = telegram.chats()
    for c in chats:
        c['manager'] = team.name_of(c['login']) if c.get('login') else c['manager']
    if m['role'] != 'admin':  # менеджер видит свои чаты, администратор — все
        chats = [c for c in chats if c.get('login') == m['login']]
    return {'enabled': telegram.enabled(), 'bot': telegram.bot.username, 'chats': chats, 'outbox': telegram.outbox_stats()}


@app.post('/api/manager/telegram/link')
def telegram_link(m: dict = Depends(auth.manager)) -> dict[str, Any]:
    if not telegram.enabled():
        raise HTTPException(400, 'Бот не настроен: администратор должен указать TELEGRAM_BOT_TOKEN')
    return telegram.link_code(m['name'], m['login'])


@app.delete('/api/manager/telegram/chats/{chat_id}')
def telegram_unlink(chat_id: int, m: dict = Depends(auth.manager)) -> dict[str, Any]:
    c = db.one('SELECT login FROM tg_chats WHERE chat_id=?', (chat_id,))
    if c and m['role'] != 'admin' and c['login'] != m['login']:
        raise HTTPException(403, 'Можно отключить только свой Telegram')
    telegram.unlink(chat_id)
    return {'ok': True}


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
    # «Закрыто агентом» (ответ организаторов): ответ без менеджера ИЛИ документ, который менеджер лишь подтвердил одной кнопкой
    closed = one("SELECT count(*) v FROM conversations WHERE id IN (SELECT conv_id FROM messages WHERE role='user') "
                 "AND id NOT IN (SELECT conv_id FROM escalations) AND id NOT IN (SELECT conv_id FROM messages WHERE role='manager' "
                 "AND meta NOT LIKE '%\"status\": \"approved\"%')")
    closed_approved = one("SELECT count(DISTINCT conv_id) v FROM messages WHERE role='manager' AND meta LIKE '%\"status\": \"approved\"%' "
                          "AND conv_id NOT IN (SELECT conv_id FROM escalations)")
    return {
        'conversations': convs, 'user_messages': user_msgs,
        'resolved_without_human': closed, 'closed_with_quote_approval': closed_approved,
        'automation_rate': round(closed / convs, 3) if convs else 0,
        'sla': sla_stats(),
        'team': {'online': sum(1 for u in team.roster() if u['online']), 'members': db.one('SELECT count(*) AS n FROM users WHERE active=1')['n'],
                 'queue': one("SELECT count(*) v FROM escalations WHERE status='open' AND (assignee IS NULL OR assignee='')"),
                 'by_manager': db.rows("SELECT coalesce(u.name, e.manager) AS name, count(*) AS answered, "
                                       "round(avg(e.answered_at - e.created_at)) AS avg_sec FROM escalations e LEFT JOIN users u ON u.login=e.assignee "
                                       "WHERE e.status='answered' AND e.answered_at IS NOT NULL GROUP BY 1 ORDER BY answered DESC")},
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


def _median(xs: list[float]) -> float | None:
    xs = sorted(xs)
    return round(xs[len(xs) // 2], 1) if xs else None


def sla_stats() -> dict[str, Any]:
    """Регламент ответа: первый ответ клиенту (агент) и первый ответ менеджера на эскалацию против цели (сейчас ~2 мин)."""
    target = discounts.sla_seconds()
    first = []
    for c in db.rows("SELECT conv_id, min(created_at) AS t FROM messages WHERE role='user' GROUP BY conv_id"):
        a = db.one("SELECT min(created_at) AS t FROM messages WHERE conv_id=? AND role IN ('assistant','manager') AND created_at>=?", (c['conv_id'], c['t']))
        if a and a['t']:
            first.append(a['t'] - c['t'])
    mgr, overdue = [], 0
    for e in db.rows('SELECT id, conv_id, created_at, status, answered_at FROM escalations'):
        a = db.one("SELECT min(created_at) AS t FROM messages WHERE conv_id=? AND role='manager' AND created_at>=?", (e['conv_id'], e['created_at']))
        t = min([x for x in ((a or {}).get('t'), e.get('answered_at')) if x] or [0])
        if t:
            mgr.append(t - e['created_at'])
        elif e['status'] == 'open' and db.now() - e['created_at'] > target:
            overdue += 1
    return {'target_sec': target, 'agent_first_response_median_sec': _median(first),
            'agent_first_response_p90_sec': round(sorted(first)[int(len(first) * 0.9) - 1], 1) if len(first) >= 2 else _median(first),
            'manager_first_response_median_sec': _median(mgr),
            'manager_within_sla_share': round(sum(x <= target for x in mgr) / len(mgr), 3) if mgr else None,
            'escalations_overdue': overdue, 'baseline_sec': 120}


# ------------------------------------------------------------------ скидки (таблица менеджера) и регламент

class DiscountIn(BaseModel):
    id: int | None = None
    title: str = ''
    scope: str = 'product'
    match: str = ''
    percent: float
    active: bool = True
    valid_until: float | None = None
    note: str = ''


class SettingsIn(BaseModel):
    agent_discount_cap: float | None = None
    sla_manager_seconds: float | None = None


@app.get('/api/manager/discounts')
def discounts_list(m: dict = Depends(auth.manager)) -> dict[str, Any]:
    return {'rules': discounts.rules(), 'agent_cap': discounts.agent_cap(), 'sla_manager_seconds': discounts.sla_seconds(),
            'scopes': discounts.SCOPES}


@app.post('/api/manager/discounts')
def discounts_save(body: DiscountIn, m: dict = Depends(auth.manager)) -> dict[str, Any]:
    try:
        return discounts.save(body.model_dump(), m['name'])
    except discounts.DiscountError as e:
        raise HTTPException(400, str(e)) from e


@app.delete('/api/manager/discounts/{rid}')
def discounts_delete(rid: int, m: dict = Depends(auth.manager)) -> dict[str, Any]:
    discounts.delete(rid)
    return {'ok': True}


@app.post('/api/manager/settings')
def settings_save(body: SettingsIn, m: dict = Depends(auth.admin)) -> dict[str, Any]:
    try:
        if body.agent_discount_cap is not None:
            discounts.set_agent_cap(body.agent_discount_cap)
        if body.sla_manager_seconds is not None:
            if not 30 <= body.sla_manager_seconds <= 3600:
                raise discounts.DiscountError('Регламент ответа — от 30 с до 60 мин')
            discounts.set_setting('sla_manager_seconds', body.sla_manager_seconds)
    except discounts.DiscountError as e:
        raise HTTPException(400, str(e)) from e
    return {'agent_cap': discounts.agent_cap(), 'sla_manager_seconds': discounts.sla_seconds()}


@app.get('/api/manager/discounts/preview')
def discounts_preview(q: str = Query('', max_length=200), m: dict = Depends(auth.manager)) -> dict[str, Any]:
    """Проверка правила: какие товары каталога под него попадут (по поиску) и какая скидка будет применена."""
    res = catalog.search(q, {}, limit=8) if len(q.strip()) >= 2 else []
    active = discounts.rules(active_only=True)
    return {'items': [{'product': r['product'], 'vendor': r['vendor'], 'price_from_rub': r['price_from_rub'],
                       'promo': discounts.best_for({'vendor': r['vendor'], 'product': r['product'], 'category_path': r.get('category', ''),
                                                    'group_id': r['group_id']}, active)} for r in res]}


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
