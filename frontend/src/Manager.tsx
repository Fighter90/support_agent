import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { BarChart3, BookOpen, Bot, CheckCircle2, CreditCard, FileText, Headset, LogOut, MessageCircle, MessagesSquare, RefreshCw, Send, ThumbsUp } from 'lucide-react'
import { HelpCircle, Percent, Send as TgIcon, UserCog, Users } from 'lucide-react'
import { ColleaguePick, MeCtx, Profile, StatusBar, TeamAdmin, useMe, usePresence } from './Team'
import Help from './Help'
import { useRef } from 'react'
import { api, dt, rub, setToken, token } from './api'
import Markdown from './Markdown'

type Tab = 'live' | 'escalations' | 'dialogs' | 'quotes' | 'feedback' | 'kb' | 'metrics' | 'telegram' | 'help' | 'discounts' | 'team' | 'profile'
const SCEN: Record<string, string> = { known_product: 'Знает продукт', need_selection: 'Подбор под задачу', question: 'Вопрос', other: 'Другое', '': '—' }

function Login({ onOk }: { onOk: () => void }) {
  const [login, setLogin] = useState('manager')
  const [password, setPassword] = useState('')
  const [err, setErr] = useState('')
  useEffect(() => {
    const code = new URLSearchParams(location.search).get('invite')
    if (!code) return
    api<{ token: string }>('/api/auth/invite', { body: { code } })
      .then(d => { setToken(d.token); history.replaceState(null, '', '/manager'); onOk() })
      .catch(x => setErr((x as Error).message))
  }, [])
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr('')
    try { const d = await api<{ token: string }>('/api/auth/login', { body: { login, password } }); setToken(d.token); onOk() } catch (x) { setErr((x as Error).message) }
  }
  return (
    <div className="login">
      <form onSubmit={submit} className="panel">
        <img src="/logo.png" alt="Софтлайн" className="login-logo" />
        <h2>Кабинет поддержки</h2>
        <p className="muted small">Менеджеры и администратор: обращения, КП, база знаний, команда. Новый сотрудник входит по ссылке-приглашению от администратора.</p>
        <input value={login} onChange={e => setLogin(e.target.value)} placeholder="Логин" />
        <input value={password} onChange={e => setPassword(e.target.value)} placeholder="Пароль" type="password" />
        {err && <div className="err">{err}</div>}
        <button className="btn">Войти</button>
      </form>
    </div>
  )
}

function Dialog({ cid }: { cid: string }) {
  const [d, setD] = useState<any>(null)
  useEffect(() => { api(`/api/manager/conversations/${cid}`, { auth: true }).then(setD).catch(() => {}) }, [cid])
  if (!d) return <div className="muted pad">Загрузка…</div>
  return (
    <div className="dialog">
      <div className="muted small">Диалог {d.id} · {SCEN[d.scenario || '']} · статус: {d.status}{d.pii_masked?.length ? ` · замаскировано ПДн: ${d.pii_masked.length}` : ''}</div>
      {d.messages.map((m: any) => (
        <div key={m.id} className={`msg ${m.role}`}>
          <div className="who">{m.role === 'user' ? 'Клиент' : m.role === 'manager' ? 'Менеджер' : 'Агент'} · {dt(m.created_at)}
            {m.meta?.tools?.length ? <span className="tools"> · {m.meta.tools.map((t: any) => t.tool).join(' → ')}</span> : null}
            {m.meta?.cost_rub ? <span className="tools"> · {m.meta.tokens} ток. · {m.meta.cost_rub} ₽ · {m.meta.provider}</span> : null}
            {m.meta?.price_guard ? <span className="warn"> · исправлены цены: {m.meta.price_guard.join(', ')}</span> : null}
          </div>
          <div className="bubble"><Markdown text={m.content} /></div>
        </div>
      ))}
      <details><summary>Вызовы инструментов ({d.tool_calls.length})</summary>
        {d.tool_calls.map((t: any, i: number) => <pre key={i}>{t.tool}({t.args}) · {t.ms} мс{'\n'}{t.result}</pre>)}
      </details>
      <details><summary>Вызовы моделей ({d.llm_calls.length})</summary>
        <table><thead><tr><th>Модель</th><th>Вход</th><th>Выход</th><th>₽</th><th>мс</th><th>Статус</th></tr></thead><tbody>
          {d.llm_calls.map((c: any, i: number) => <tr key={i}><td>{c.provider} / {c.model}</td><td>{c.tokens_in}</td><td>{c.tokens_out}</td><td>{c.cost_rub}</td><td>{c.ms}</td><td>{c.ok ? 'ok' : c.error}</td></tr>)}
        </tbody></table>
      </details>
    </div>
  )
}


// Общий WebSocket кабинета: новые сообщения, эскалации, «печатает…», присутствие менеджеров
type Ev = { type: string; conv_id?: string; message?: any; [k: string]: any }
const listeners = new Set<(e: Ev) => void>()
let sock: WebSocket | null = null
function useEvents(fn: (e: Ev) => void) {
  const ref = useRef(fn); ref.current = fn
  useEffect(() => {
    const l = (e: Ev) => ref.current(e)
    listeners.add(l)
    if (!sock || sock.readyState > 1) {
      sock = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/manager?token=${encodeURIComponent(token())}`)
      sock.onmessage = ev => { const d = JSON.parse(ev.data); listeners.forEach(x => x(d)) }
      sock.onclose = () => { sock = null; setTimeout(() => listeners.forEach(x => x({ type: 'reconnect' })), 3000) }
    }
    return () => { listeners.delete(l) }
  }, [])
}
function sendTyping(conv_id: string) { if (sock?.readyState === 1) sock.send(JSON.stringify({ type: 'typing', conv_id })) }

const QUICK = ['Здравствуйте! Меня зовут ' , 'Подготовлю КП и пришлю сюда в течение 15 минут.', 'Уточните, пожалуйста, количество пользователей и срок лицензии.', 'Для госзакупки пришлите, пожалуйста, ТЗ или проект контракта.', 'Спасибо за обращение! Если появятся вопросы — пишите.']

function LiveChat() {
  const [list, setList] = useState<any[]>([])
  const [sel, setSel] = useState('')
  const [conv, setConv] = useState<any>(null)
  const [text, setText] = useState('')
  const [typing, setTyping] = useState(false)
  const [unread, setUnread] = useState<Record<string, number>>({})
  const [mine, setMine] = useState(false)
  const me = useMe()
  const body = useRef<HTMLDivElement>(null)
  const loadList = () => api<any[]>('/api/manager/conversations', { auth: true }).then(setList).catch(() => {})
  const loadConv = (cid: string) => api(`/api/manager/conversations/${cid}`, { auth: true }).then(setConv).catch(() => {})
  useEffect(() => { loadList() }, [])
  useEffect(() => { if (sel) { loadConv(sel); setUnread(u => ({ ...u, [sel]: 0 })) } }, [sel])
  useEffect(() => { body.current?.scrollTo({ top: body.current.scrollHeight }) }, [conv, typing])
  useEvents(e => {
    if (e.type === 'typing' && e.conv_id === sel) { setTyping(true); setTimeout(() => setTyping(false), 3000) }
    if (e.type === 'message' && e.conv_id) {
      if (e.conv_id === sel) { setTyping(false); setConv((c: any) => c && !c.messages.some((m: any) => m.id === e.message.id) ? { ...c, messages: [...c.messages, e.message] } : c) }
      else if (e.message.role === 'user') setUnread(u => ({ ...u, [e.conv_id!]: (u[e.conv_id!] || 0) + 1 }))
      loadList()
    }
    if (e.type === 'escalation') loadList()
  })
  const send = async (t: string) => {
    if (!sel || !t.trim()) return
    await api(`/api/manager/conversations/${sel}/message`, { auth: true, body: { text: t } }); setText('')
    setConv((c: any) => c ? { ...c, mode: 'manager' } : c)
  }
  const takeover = async (on: boolean) => { await api(`/api/manager/conversations/${sel}/takeover`, { auth: true, body: { on } }); loadConv(sel); loadList() }
  const colleague = async (kind: 'transfer' | 'invite', login: string) => { await api(`/api/manager/conversations/${sel}/${kind}`, { auth: true, body: { login } }); loadConv(sel); loadList() }
  const visible = list.filter(c => c.n_messages > 1 && (!mine || c.manager_login === me?.login || (c.participants || '').split(',').includes(me?.login || '')))
  const parts: any[] = conv?.participants || []
  return (
    <div className="split">
      <div className="list">
        <div className="seg"><button className={!mine ? 'on' : ''} onClick={() => setMine(false)}>Все диалоги</button><button className={mine ? 'on' : ''} onClick={() => setMine(true)}>Мои</button></div>
        {visible.map(c => (
          <button key={c.id} className={`item ${sel === c.id ? 'on' : ''}`} onClick={() => setSel(c.id)}>
            <div className="item-top">
              <span className={`tag ${c.status === 'escalated' ? 'red' : c.mode === 'manager' ? 'green' : ''}`}>{c.mode === 'manager' ? `Ведёт ${c.manager || 'менеджер'}` : c.status === 'escalated' ? (c.assignee_name ? `Назначен: ${c.assignee_name}` : 'В очереди') : 'Агент'}</span>
              <span>{unread[c.id] ? <span className="unread">{unread[c.id]}</span> : null} <span className="muted small">{dt(c.updated_at)}</span></span>
            </div>
            <div className="item-title">{c.title || 'Без сообщений'}</div>
          </button>
        ))}
      </div>
      <div className="detail">
        {!conv ? <div className="muted pad">Выберите диалог. Сообщения клиентов приходят по WebSocket мгновенно; можно взять диалог на себя — агент замолчит, клиент увидит «к диалогу подключился менеджер».</div> : (
          <div className="live">
            <div className="live-head">
              <b>{conv.title?.slice(0, 60) || conv.id}</b>
              <span className={`tag ${conv.mode === 'manager' ? 'green' : ''}`}>{conv.mode === 'manager' ? `Ведёт ${conv.manager || 'менеджер'}` : 'Отвечает агент'}</span>
              {parts.filter(x => x.role === 'helper').map(x => <span key={x.login} className="tag">помогает {x.name || x.login}</span>)}
              <span className="spacer" />
              {conv.mode === 'manager' && conv.manager_login !== me?.login && <button className="btn sm" onClick={() => takeover(true)}><Headset size={14} /> Забрать себе</button>}
              {conv.mode === 'manager' && <ColleaguePick label="Передать" exclude={[conv.manager_login]} onPick={l => colleague('transfer', l)} />}
              {conv.mode === 'manager' && <ColleaguePick label="Позвать коллегу" exclude={parts.map(x => x.login)} onPick={l => colleague('invite', l)} />}
              {conv.mode === 'manager'
                ? <button className="btn sm ghost" onClick={() => takeover(false)}><Bot size={14} /> Вернуть агенту</button>
                : <button className="btn sm" onClick={() => takeover(true)}><Headset size={14} /> Взять диалог</button>}
            </div>
            <div className="live-body" ref={body}>
              {conv.messages.map((m: any) => m.role === 'system' ? <div key={m.id} className="sysmsg">{m.content}</div> : (
                <div key={m.id} className={`msg ${m.role}`}>
                  <div className="who">{m.role === 'user' ? 'Клиент' : m.role === 'manager' ? `Менеджер${m.meta?.manager ? ' · ' + m.meta.manager : ''}` : 'ИИ-ассистент'} · {dt(m.created_at)}
                    {m.meta?.tools?.length ? <span className="tools"> · {m.meta.tools.map((x: any) => x.tool).join(' → ')}</span> : null}</div>
                  <div className="bubble"><Markdown text={m.content} /></div>
                </div>))}
              {typing && <div className="msg user"><div className="bubble typing"><span /><span /><span /> Клиент печатает…</div></div>}
            </div>
            <div className="live-input">
              <div className="quick">{QUICK.map(q => <button key={q} onClick={() => setText(text + q)}>{q.length > 40 ? q.slice(0, 40) + '…' : q}</button>)}</div>
              <div className="row">
                <textarea rows={2} value={text} placeholder="Ответ клиенту — придёт в его чат мгновенно" onChange={e => { setText(e.target.value); sendTyping(sel) }}
                  onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(text) } }} />
                <button className="btn" onClick={() => send(text)}><Send size={16} /></button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

function Escalations() {
  const [status, setStatus] = useState('mine')
  const me = useMe()
  const [items, setItems] = useState<any[]>([])
  const [sel, setSel] = useState<any>(null)
  const [answer, setAnswer] = useState('')
  const [toKb, setToKb] = useState(true)
  const [msg, setMsg] = useState('')
  const load = useCallback(() => api<any[]>(`/api/manager/escalations?status=${status}`, { auth: true }).then(setItems).catch(() => {}), [status])
  useEffect(() => { load(); const t = setInterval(load, 8000); return () => clearInterval(t) }, [load])
  const send = async () => {
    if (!sel || !answer.trim()) return
    await api(`/api/manager/escalations/${sel.id}/answer`, { auth: true, body: { answer, add_to_kb: toKb } })
    setMsg(toKb ? 'Ответ отправлен клиенту и добавлен в базу знаний агента' : 'Ответ отправлен клиенту'); setAnswer(''); setSel(null); load()
  }
  return (
    <div className="split">
      <div className="list">
        <div className="seg">{['mine', 'queue', 'open', 'answered', 'all'].map(s => <button key={s} className={status === s ? 'on' : ''} onClick={() => setStatus(s)}>{({ mine: 'Мои', queue: 'Очередь', open: 'Все новые', answered: 'Отвеченные', all: 'Все' } as any)[s]}</button>)}</div>
        {items.length === 0 && <div className="muted pad">Нет обращений</div>}
        {items.map(e => (
          <button key={e.id} className={`item ${sel?.id === e.id ? 'on' : ''}`} onClick={() => { setSel(e); setMsg('') }}>
            <div className="item-top"><span className="tag red">{e.category}</span><span className="muted small">{dt(e.created_at)}{e.status === 'open' && <Wait since={e.created_at} />}</span></div>
            <div className="item-title">{e.summary}</div>
            {e.status === 'open' && <div className="muted small">{e.assignee_name ? `Назначен: ${e.assignee_name}` : 'В очереди — свободных нет'}{e.reassigns ? ` · переназначений: ${e.reassigns}` : ''}</div>}
          </button>
        ))}
      </div>
      <div className="detail">
        {msg && <div className="ok"><CheckCircle2 size={16} /> {msg}</div>}
        {!sel && !msg && <div className="muted pad">Выберите обращение. Агент уже собрал резюме и параметры — менеджер решает только сложное.</div>}
        {sel && (
          <>
            <div className="panel"><b>Резюме агента:</b> {sel.summary}
              {sel.status === 'open' && <div className="row" style={{ marginTop: 8, gap: 8, alignItems: 'center' }}>
                <span className="muted small">{sel.assignee_name ? `Исполнитель: ${sel.assignee_name}` : 'В очереди'}</span>
                {sel.assignee !== me?.login && <button className="btn sm" onClick={async () => { await api(`/api/manager/escalations/${sel.id}/claim`, { auth: true, method: 'POST' }); setSel({ ...sel, assignee: me?.login, assignee_name: me?.name }); load() }}>Взять себе</button>}
                <button className="btn sm ghost" onClick={async () => { await api(`/api/manager/escalations/${sel.id}/close`, { auth: true, body: { note: '' } }); setMsg('Обращение закрыто без ответа'); setSel(null); load() }}>Закрыть без ответа</button>
              </div>}
            </div>
            {sel.status === 'open' && (
              <div className="panel answer">
                <textarea rows={4} value={answer} onChange={e => setAnswer(e.target.value)} placeholder="Ответ клиенту — придёт в его чат" />
                <label className="check"><input type="checkbox" checked={toKb} onChange={e => setToKb(e.target.checked)} /> Добавить ответ в базу знаний — агент будет отвечать на такие вопросы сам</label>
                <button className="btn" onClick={send}>Отправить клиенту</button>
              </div>
            )}
            {sel.answer && <div className="panel"><b>Ответ менеджера ({sel.manager}):</b> {sel.answer}</div>}
            <Dialog cid={sel.conv_id} />
          </>
        )}
      </div>
    </div>
  )
}

function Dialogs() {
  const [items, setItems] = useState<any[]>([])
  const [sel, setSel] = useState('')
  useEffect(() => { api<any[]>('/api/manager/conversations', { auth: true }).then(setItems).catch(() => {}) }, [])
  return (
    <div className="split">
      <div className="list">
        {items.filter(c => c.n_messages > 1).map(c => (
          <button key={c.id} className={`item ${sel === c.id ? 'on' : ''}`} onClick={() => setSel(c.id)}>
            <div className="item-top"><span className={`tag ${c.status === 'escalated' ? 'red' : ''}`}>{SCEN[c.scenario || '']}</span><span className="muted small">{dt(c.updated_at)}</span></div>
            <div className="item-title">{c.title || 'Без сообщений'}</div>
            <div className="muted small">{c.n_messages} сообщ. · {c.cost_rub ?? 0} ₽ · {c.status}</div>
          </button>
        ))}
      </div>
      <div className="detail">{sel ? <Dialog cid={sel} /> : <div className="muted pad">Журнал всех диалогов агента: сообщения, вызовы инструментов, токены и стоимость.</div>}</div>
    </div>
  )
}

function Quotes() {
  const [items, setItems] = useState<any[]>([])
  const [pays, setPays] = useState<{ enabled: boolean; mode?: string; items: any[] }>({ enabled: false, items: [] })
  const [msg, setMsg] = useState('')
  const load = () => {
    api<any[]>('/api/manager/quotes', { auth: true }).then(setItems).catch(() => {})
    api('/api/manager/payments', { auth: true }).then(setPays).catch(() => {})
  }
  const link = async (id: string) => {
    try { await api(`/api/manager/quotes/${id}/payment-link`, { auth: true, method: 'POST' }); setMsg('Ссылка на оплату ЮKassa отправлена клиенту в чат'); load() } catch (e) { setMsg((e as Error).message) }
  }
  const payOf = (id: string) => pays.items.find(p => p.quote_id === id)
  useEffect(() => { load() }, [])
  const approve = async (id: string) => { await api(`/api/manager/quotes/${id}/approve`, { auth: true, method: 'POST' }); load() }
  const pdf = async (id: string) => {
    const r = await fetch(`/api/manager/quotes/${id}/pdf`, { headers: { Authorization: `Bearer ${token()}` } })
    window.open(URL.createObjectURL(await r.blob()), '_blank')
  }
  return (
    <div className="pad">
      <p className="muted small">{pays.mode === 'mcp' ? 'Онлайн-оплата: MCP-сервер ЮKassa (create_payment / get_payment). Ссылка создаётся на сумму документа, статус обновляется автоматически.' : 'Онлайн-оплата: демо-режим (без списания денег). Для боевого режима добавьте YOOKASSA_MCP_TOKEN — ссылки будут создаваться через MCP-сервер ЮKassa.'}</p>
      {msg && <div className="ok">{msg}</div>}
      <table className="grid"><thead><tr><th>Номер</th><th>Заказчик</th><th>Позиции</th><th>Сумма</th><th>Статус</th><th>Оплата (ЮKassa)</th><th></th></tr></thead><tbody>
        {items.map(q => (
          <tr key={q.id}>
            <td>{q.number}<div className="muted small">{dt(q.created_at)}</div></td>
            <td>{q.customer?.company || '—'}<div className="muted small">{q.customer?.inn ? `ИНН ${q.customer.inn}` : 'ждём реквизиты'}</div></td>
            <td>{q.items.map((i: any) => <div key={i.offer_id} className="small">{i.quantity} × {i.name}</div>)}</td>
            <td>{rub(q.total)}</td>
            <td>{q.items?.some((x: any) => x.check_supply) && <span className="tag" title="У позиции есть цена, но в фиде она отмечена «нет в наличии»: документ выдан клиенту, менеджеру — подтвердить фактическую поставку">Проверить поставку</span>} <span className={`tag ${q.status === 'approved' ? 'green' : ''}`}>{{ awaiting_requisites: 'Ждём реквизиты', draft: 'Выдан клиенту', issued: 'Выдан клиенту', approved: 'Подтверждено' }[q.status as string]}</span></td>
            <td>{payOf(q.id) ? <span className={`tag ${payOf(q.id).status === 'succeeded' ? 'green' : ''}`}>{({ pending: 'Ожидает оплаты', succeeded: 'Оплачен', canceled: 'Отменён', waiting_for_capture: 'Ожидает подтверждения' } as any)[payOf(q.id).status] || payOf(q.id).status}</span> : '—'}</td>
            <td className="nowrap">{q.status !== 'awaiting_requisites' && <button className="btn sm ghost" onClick={() => pdf(q.id)}>PDF</button>} {(q.status === 'draft' || q.status === 'issued') && <button className="btn sm" onClick={() => approve(q.id)}>Подтвердить</button>} {pays.enabled && q.status !== 'awaiting_requisites' && !payOf(q.id) && <button className="btn sm ghost" onClick={() => link(q.id)}><CreditCard size={13} /> Ссылка на оплату</button>}</td>
          </tr>
        ))}
      </tbody></table>
      {items.length === 0 && <div className="muted pad">КП появятся, когда клиент согласует выбор в чате.</div>}
    </div>
  )
}

function Feedback() {
  const [d, setD] = useState<any>(null)
  const [fix, setFix] = useState<Record<number, string>>({})
  const load = () => api('/api/manager/feedback', { auth: true }).then(setD).catch(() => {})
  useEffect(() => { load() }, [])
  if (!d) return <div className="muted pad">Загрузка…</div>
  const s = d.stats
  const save = async (id: number) => { await api(`/api/manager/feedback/${id}/fix`, { auth: true, body: { answer: fix[id] } }); load() }
  return (
    <div className="pad">
      <div className="tiles">
        <div className="tile"><div className="muted small">CSAT (1–5)</div><b>{s.csat ?? '—'}</b></div>
        <div className="tile"><div className="muted small">Вопрос решён</div><b>{s.resolved_share != null ? Math.round(s.resolved_share * 100) + '%' : '—'}</b></div>
        <div className="tile"><div className="muted small">Опросов</div><b>{s.surveys}</b></div>
        <div className="tile"><div className="muted small">👍 / 👎</div><b>{s.thumbs_up} / {s.thumbs_down}</b></div>
        <div className="tile"><div className="muted small">Примеров для агента</div><b>{s.examples}</b></div>
      </div>
      <p className="muted small">👍 — ответ становится примером для агента на похожие вопросы. 👎 — напишите правильный ответ: он попадёт в базу знаний, и агент будет отвечать так.</p>
      {d.negatives.length === 0 && <div className="muted pad">Отрицательных отзывов нет.</div>}
      {d.negatives.map((n: any) => (
        <div key={n.id} className="panel fb">
          <div className="muted small">{dt(n.created_at)} · {n.status === 'fixed' ? <span className="tag green">исправлено, в базе знаний</span> : <span className="tag red">требует исправления</span>}</div>
          <div><b>Вопрос:</b> {n.question}</div>
          <div className="bubble"><Markdown text={n.answer} /></div>
          {n.status !== 'fixed' && <>
            <textarea rows={3} placeholder="Правильный ответ — станет статьёй базы знаний" value={fix[n.id] || ''} onChange={e => setFix({ ...fix, [n.id]: e.target.value })} />
            <button className="btn sm" disabled={!fix[n.id]} onClick={() => save(n.id)}>Исправить и добавить в базу знаний</button>
          </>}
        </div>
      ))}
      {s.comments.length > 0 && <div className="panel"><b>Комментарии из опросов</b>{s.comments.map((c: any, i: number) => <div key={i} className="kv"><span>{c.comment}</span><span>{c.score}/5 · {c.resolved ? 'решён' : 'не решён'}</span></div>)}</div>}
    </div>
  )
}

function Kb() {
  const [items, setItems] = useState<any[]>([])
  const [f, setF] = useState({ title: '', body: '' })
  const load = () => api<any[]>('/api/manager/kb', { auth: true }).then(setItems).catch(() => {})
  useEffect(() => { load() }, [])
  const add = async (e: FormEvent) => { e.preventDefault(); await api('/api/manager/kb', { auth: true, body: f }); setF({ title: '', body: '' }); load() }
  return (
    <div className="pad">
      <form className="panel kb-form" onSubmit={add}>
        <b>Добавить статью</b>
        <input placeholder="Вопрос / заголовок" value={f.title} onChange={e => setF({ ...f, title: e.target.value })} />
        <textarea rows={3} placeholder="Ответ" value={f.body} onChange={e => setF({ ...f, body: e.target.value })} />
        <button className="btn sm">Добавить</button>
      </form>
      {items.map(a => (
        <div key={a.id} className="kb-item"><span className={`tag ${a.source === 'manager' ? 'green' : ''}`}>{a.source === 'manager' ? 'от менеджера' : 'база'}</span> <b>{a.title}</b><div className="muted small">{a.body}</div></div>
      ))}
    </div>
  )
}

function Metrics() {
  const [m, setM] = useState<any>(null)
  useEffect(() => { api('/api/manager/metrics', { auth: true }).then(setM).catch(() => {}) }, [])
  if (!m) return <div className="muted pad">Загрузка…</div>
  const tiles = [
    ['Диалогов', m.conversations], ['Закрыто агентом', `${Math.round(m.automation_rate * 100)}%`], ['Открытых эскалаций', m.escalations_open],
    ['Первый ответ клиенту (медиана)', m.sla?.agent_first_response_median_sec != null ? `${m.sla.agent_first_response_median_sec} с` : '—'],
    ['Ответ менеджера на эскалацию', m.sla?.manager_first_response_median_sec != null ? sec(m.sla.manager_first_response_median_sec) : '—'],
    ['В регламенте ' + sec(m.sla?.target_sec ?? 120), m.sla?.manager_within_sla_share != null ? `${Math.round(m.sla.manager_within_sla_share * 100)}%` : '—'],
    ['Стоимость 1 запроса', rub(m.cost_per_request_rub)], ['Всего на модели', rub(m.cost_total_rub)], ['Статей от менеджеров', m.kb_from_managers],
    ['CSAT', m.learning?.csat ?? '—'], ['Вопрос решён (опрос)', m.learning?.resolved_share != null ? `${Math.round(m.learning.resolved_share * 100)}%` : '—'],
  ]
  return (
    <div className="pad">
      <div className="tiles">{tiles.map(([k, v]) => <div key={k} className="tile"><div className="muted small">{k}</div><b>{v}</b></div>)}</div>
      <div className="cols">
        <div className="panel"><b>Сценарии</b>{m.scenarios.map((s: any) => <div key={s.scenario} className="kv"><span>{SCEN[s.scenario] || s.scenario}</span><span>{s.n}</span></div>)}</div>
        <div className="panel"><b>Эскалации по причинам</b>{m.escalations_by_reason.map((s: any) => <div key={s.category} className="kv"><span>{s.category}</span><span>{s.n}</span></div>)}</div>
        <div className="panel"><b>Инструменты агента</b>{m.tools.map((s: any) => <div key={s.tool} className="kv"><span>{s.tool}</span><span>{s.n} · {s.avg_ms} мс</span></div>)}</div>
        <div className="panel"><b>Модели</b>{m.llm.map((s: any) => <div key={s.provider} className="kv"><span>{s.provider}</span><span>{s.calls} выз. · {(s.tokens_in || 0) + (s.tokens_out || 0)} ток. · {s.cost_rub} ₽ · {s.avg_ms} мс</span></div>)}</div>
        <div className="panel"><b>КП</b>{m.quotes.map((s: any) => <div key={s.status} className="kv"><span>{s.status}</span><span>{s.n} · {rub(s.total_rub)}</span></div>)}</div>
        <div className="panel"><b>Регламент ответа</b>
          <div className="kv"><span>Было: первый ответ человека</span><span>~2 мин (данные заказчика)</span></div>
          <div className="kv"><span>Сейчас: первый ответ агента, медиана / p90</span><span>{m.sla?.agent_first_response_median_sec ?? '—'} с / {m.sla?.agent_first_response_p90_sec ?? '—'} с</span></div>
          <div className="kv"><span>Цель для менеджера по эскалации</span><span>{sec(m.sla?.target_sec ?? 120)}</span></div>
          <div className="kv"><span>Просрочено сейчас</span><span>{m.sla?.escalations_overdue ?? 0}</span></div>
          <div className="kv"><span>«Закрыто агентом» (с подтверждением КП одной кнопкой)</span><span>{m.resolved_without_human} (из них КП {m.closed_with_quote_approval})</span></div></div>
        <div className="panel"><b>Команда</b>
          <div className="kv"><span>В сети / всего сотрудников</span><span>{m.team?.online ?? 0} / {m.team?.members ?? 0}</span></div>
          <div className="kv"><span>Обращений в очереди</span><span>{m.team?.queue ?? 0}</span></div>
          {(m.team?.by_manager || []).map((r: any) => <div key={r.name} className="kv"><span>{r.name}</span><span>{r.answered} отв. · в среднем {r.avg_sec != null ? sec(r.avg_sec) : '—'}</span></div>)}</div>
        <div className="panel"><b>Каталог</b><div className="kv"><span>Позиций</span><span>{m.catalog.offers}</span></div><div className="kv"><span>С ценой</span><span>{m.catalog.available_priced}</span></div><div className="kv"><span>Обновлён</span><span>{m.catalog.loaded_at}</span></div></div>
      </div>
    </div>
  )
}

const sec = (x: number) => x >= 90 ? `${Math.round(x / 6) / 10} мин` : `${Math.round(x)} с`

function Wait({ since }: { since: number }) {
  const [, tick] = useState(0)
  useEffect(() => { const t = window.setInterval(() => tick(x => x + 1), 5000); return () => window.clearInterval(t) }, [])
  const w = Date.now() / 1000 - since
  return <span className={`tag ${w > 120 ? 'red' : 'green'}`} style={{ marginLeft: 6 }} title="Регламент первого ответа менеджера — 2 мин">ждёт {sec(w)}</span>
}

function Discounts() {
  const [d, setD] = useState<any>(null)
  const [form, setForm] = useState<any>({ scope: 'product', match: '', percent: 5, title: '', active: true, valid: '' })
  const [cap, setCap] = useState('')
  const [sla, setSla] = useState('')
  const [msg, setMsg] = useState('')
  const [q, setQ] = useState('')
  const [prev, setPrev] = useState<any[]>([])
  const load = () => api<any>('/api/manager/discounts', { auth: true }).then(x => { setD(x); setCap(String(x.agent_cap)); setSla(String(x.sla_manager_seconds / 60)) })
  useEffect(() => { load() }, [])
  useEffect(() => { const t = window.setTimeout(() => { if (q.trim().length >= 2) api<any>(`/api/manager/discounts/preview?q=${encodeURIComponent(q)}`, { auth: true }).then(x => setPrev(x.items)) ; else setPrev([]) }, 300); return () => window.clearTimeout(t) }, [q, d])
  const save = async () => {
    try {
      const body = { ...form, percent: Number(form.percent), valid_until: form.valid ? new Date(form.valid + 'T23:59:59').getTime() / 1000 : null }
      await api('/api/manager/discounts', { auth: true, body }); setForm({ scope: 'product', match: '', percent: 5, title: '', active: true, valid: '' }); setMsg('Скидка сохранена — агент начнёт предлагать её сразу'); load()
    } catch (e) { setMsg((e as Error).message) }
  }
  const saveSettings = async () => {
    try { await api('/api/manager/settings', { auth: true, body: { agent_discount_cap: Number(cap), sla_manager_seconds: Number(sla) * 60 } }); setMsg('Настройки сохранены'); load() } catch (e) { setMsg((e as Error).message) }
  }
  const toggle = async (r: any) => { await api('/api/manager/discounts', { auth: true, body: { ...r, active: !r.active } }); load() }
  const del = async (id: number) => { await api(`/api/manager/discounts/${id}`, { auth: true, method: 'DELETE' }); load() }
  const edit = (r: any) => setForm({ ...r, active: !!r.active, valid: r.valid_until ? new Date(r.valid_until * 1000).toISOString().slice(0, 10) : '' })
  if (!d) return <div className="muted pad">Загрузка…</div>
  return (
    <div className="pad">
      <div className="panel">
        <b>Полномочия агента и регламент</b>
        <p className="muted small">Агент сам предлагает клиенту скидку до этого лимита — даже без купона. Больше — только менеджер. Отсрочку и постоплату агент всегда передаёт менеджеру.</p>
        <div className="row-form">
          <label>Скидка агента без согласования, %<input type="number" min={0} max={15} step={0.5} value={cap} onChange={e => setCap(e.target.value)} /></label>
          <label>Регламент первого ответа менеджера, мин<input type="number" min={0.5} max={60} step={0.5} value={sla} onChange={e => setSla(e.target.value)} /></label>
          <button className="btn" onClick={saveSettings}>Сохранить</button>
        </div>
      </div>
      <div className="panel">
        <b>{form.id ? 'Изменить скидку' : 'Новая скидка (акция)'}</b>
        <p className="muted small">Акция из таблицы согласована менеджером, поэтому может быть больше лимита агента. Агент сам говорит о ней клиенту, а в КП и счёте она подставляется автоматически.</p>
        <div className="row-form">
          <label>Применяется к<select value={form.scope} onChange={e => setForm({ ...form, scope: e.target.value })}>{Object.entries(d.scopes).map(([k, v]) => <option key={k} value={k}>{v as string}</option>)}</select></label>
          {form.scope !== 'all' && <label className="grow">{{ vendor: 'Вендор (часть названия)', product: 'Продукт — часть названия линейки или group_id', category: 'Категория (часть названия)', offer: 'Артикулы через запятую' }[form.scope as string]}<input value={form.match} onChange={e => setForm({ ...form, match: e.target.value })} placeholder={{ vendor: 'Касперск', product: 'Kaspersky Endpoint Security', category: 'Антивирусы', offer: '4986, 4985' }[form.scope as string]} /></label>}
          <label>Скидка, %<input type="number" min={0.5} max={50} step={0.5} value={form.percent} onChange={e => setForm({ ...form, percent: e.target.value })} /></label>
          <label>Действует до<input type="date" value={form.valid} onChange={e => setForm({ ...form, valid: e.target.value })} /></label>
          <label className="grow">Название для клиента<input value={form.title} onChange={e => setForm({ ...form, title: e.target.value })} placeholder="Например: Неделя Kaspersky" /></label>
          <button className="btn" onClick={save}>{form.id ? 'Сохранить' : 'Добавить'}</button>
          {form.id && <button className="btn ghost" onClick={() => setForm({ scope: 'product', match: '', percent: 5, title: '', active: true, valid: '' })}>Отмена</button>}
        </div>
        {msg && <div className="ok small">{msg}</div>}
      </div>
      <div className="panel">
        <b>Таблица скидок</b>
        {d.rules.length === 0 ? <div className="muted small">Акций пока нет — агент даёт только скидку в пределах лимита.</div> : (
          <table><thead><tr><th>Название</th><th>Применяется к</th><th>Условие</th><th>Скидка</th><th>Действует до</th><th>Статус</th><th>Кто</th><th></th></tr></thead>
            <tbody>{d.rules.map((r: any) => (
              <tr key={r.id}><td>{r.title}</td><td>{d.scopes[r.scope]}</td><td>{r.match || '—'}</td><td><b>{r.percent}%</b></td>
                <td>{r.valid_until ? dt(r.valid_until) : 'бессрочно'}</td>
                <td><span className={`tag ${r.active ? 'green' : ''}`}>{r.active ? 'Действует' : 'Выключена'}</span></td><td className="muted small">{r.created_by}</td>
                <td className="nowrap"><button className="btn sm ghost" onClick={() => edit(r)}>Изменить</button> <button className="btn sm ghost" onClick={() => toggle(r)}>{r.active ? 'Выключить' : 'Включить'}</button> <button className="btn sm ghost" onClick={() => del(r.id)}>Удалить</button></td></tr>))}
            </tbody></table>)}
      </div>
      <div className="panel">
        <b>Проверка: что увидит клиент</b>
        <input className="full" value={q} onChange={e => setQ(e.target.value)} placeholder="Найдите товар в каталоге, например «касперский»" />
        {prev.map((i, k) => <div key={k} className="kv"><span>{i.product} · {i.vendor}</span><span>{i.price_from_rub ? 'от ' + rub(i.price_from_rub) : 'цена по запросу'}{i.promo ? ` → −${i.promo.percent}% «${i.promo.title}»` : ' · акции нет'}</span></div>)}
      </div>
    </div>
  )
}

function Telegram() {
  const [st, setSt] = useState<{ enabled: boolean; bot: string; chats: any[]; outbox?: { sent: number; pending: number; last_sent?: number } } | null>(null)
  const [msg, setMsg] = useState('')
  const load = () => api<any>('/api/manager/telegram', { auth: true }).then(setSt).catch(e => setMsg((e as Error).message))
  useEffect(() => { load(); const t = window.setInterval(load, 4000); return () => window.clearInterval(t) }, [])
  const link = async () => {
    try { const r = await api<{ url: string }>('/api/manager/telegram/link', { auth: true, method: 'POST' }); window.open(r.url, '_blank'); setMsg('Откройте бота и нажмите «Запустить» — чат подключится автоматически (код действует 30 минут).') }
    catch (e) { setMsg((e as Error).message) }
  }
  const unlink = async (id: number) => { await api(`/api/manager/telegram/chats/${id}`, { auth: true, method: 'DELETE' }); load() }
  if (!st) return <div className="muted pad">Загрузка…</div>
  return (
    <div className="panel">
      <p>Бот <b>@{st.bot}</b> присылает менеджеру новые эскалации (причина, резюме агента, сообщения клиента) с кнопками «Взять диалог» и «Вернуть агенту».
        Ответ реплаем на уведомление сразу уходит клиенту в чат на сайте. Сообщения клиента в диалоге, который ведёт менеджер, тоже приходят в Telegram.</p>
      <p className="muted small">152-ФЗ: Telegram — зарубежный сервис, поэтому в бот уходит только обезличенный текст (телефоны, e-mail, ИНН заменены метками). Полный текст — в кабинете.</p>
      {!st.enabled ? <div className="err">Бот не настроен: администратор должен указать TELEGRAM_BOT_TOKEN в .env на сервере.</div> : (
        <>
          <button className="btn" onClick={link}><TgIcon size={16} /> Подключить Telegram</button>
          {msg && <div className="ok small" style={{ marginTop: 8 }}>{msg}</div>}
          {st.outbox && <div className="muted small" style={{ marginTop: 10 }}>Уведомлений отправлено: {st.outbox.sent}{st.outbox.pending ? ` · в очереди: ${st.outbox.pending} (связь с Telegram восстановится — отправятся автоматически)` : ''}{st.outbox.last_sent ? ` · последнее: ${dt(st.outbox.last_sent)}` : ''}</div>}
          <h3 style={{ marginTop: 18 }}>Подключённые чаты</h3>
          <p className="muted small">Каждый сотрудник подключает свой Telegram. Приходят: назначенные вам обращения и сообщения клиентов в ваших диалогах;
            обращения из очереди (когда свободных нет) — всем; администраторам — копии всех обращений. Администратор видит все чаты команды.</p>
          {st.chats.length === 0 ? <div className="muted small">Пока нет. Нажмите «Подключить Telegram».</div> : st.chats.map(c => (
            <div key={c.chat_id} className="kv"><span>{c.name || 'Менеджер'} {c.username ? `@${c.username}` : ''} · {c.manager} · с {dt(c.linked_at)}</span>
              <button className="btn sm ghost" onClick={() => unlink(c.chat_id)}>Отключить</button></div>
          ))}
        </>
      )}
    </div>
  )
}

export default function Manager() {
  const [authed, setAuthed] = useState(!!token())
  if (!authed) return <Login onOk={() => setAuthed(true)} />
  return <Cabinet onLogout={() => setAuthed(false)} />
}

function Cabinet({ onLogout }: { onLogout: () => void }) {
  const [authed, setAuthed] = useState(true)
  const [me, setStatus] = usePresence()
  const [tab, setTab] = useState<Tab>('live')
  useEffect(() => { const f = () => setAuthed(false); window.addEventListener('sl-logout', f); return () => window.removeEventListener('sl-logout', f) }, [])
  useEffect(() => { if (!authed) onLogout() }, [authed])
  const tabs: [Tab, string, any][] = [['live', 'Живой чат', MessageCircle], ['escalations', 'Эскалации', Headset], ['dialogs', 'Диалоги', MessagesSquare], ['quotes', 'КП и счета', FileText], ['discounts', 'Скидки', Percent], ['feedback', 'Отзывы', ThumbsUp], ['kb', 'База знаний', BookOpen], ['metrics', 'Метрики', BarChart3], ['telegram', 'Telegram', TgIcon],
    ...(me?.role === 'admin' ? [['team', 'Команда', Users] as [Tab, string, any]] : []), ['profile', 'Профиль', UserCog], ['help', 'Справка', HelpCircle]]
  return (
    <MeCtx.Provider value={me}>
    <div className="mgr">
      <aside>
        <a href="/" className="brand"><img src="/logo-white.png" alt="Софтлайн" /></a>
        {tabs.map(([k, label, Icon]) => <button key={k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)}><Icon size={17} /> {label}</button>)}
        <div className="spacer" />
        <button onClick={() => location.reload()}><RefreshCw size={16} /> Обновить</button>
        <button onClick={async () => { await api('/api/manager/presence', { auth: true, body: { status: 'offline' } }).catch(() => {}); setToken(''); setAuthed(false) }}><LogOut size={16} /> Выйти</button>
      </aside>
      <section className="mgr-main">
        <StatusBar me={me} setStatus={setStatus} />
        <h2>{tabs.find(t => t[0] === tab)?.[1]}{me && tab === 'team' ? <span className="muted small"> · раздел администратора</span> : null}</h2>
        {tab === 'live' && <LiveChat />}
        {tab === 'escalations' && <Escalations />}
        {tab === 'dialogs' && <Dialogs />}
        {tab === 'quotes' && <Quotes />}
        {tab === 'feedback' && <Feedback />}
        {tab === 'kb' && <Kb />}
        {tab === 'metrics' && <Metrics />}
        {tab === 'discounts' && <Discounts />}
        {tab === 'telegram' && <Telegram />}
        {tab === 'help' && <Help />}
        {tab === 'team' && me?.role === 'admin' && <TeamAdmin />}
        {tab === 'profile' && <Profile />}
      </section>
    </div>
    </MeCtx.Provider>
  )
}
