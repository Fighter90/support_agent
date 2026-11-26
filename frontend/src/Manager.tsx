import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { BarChart3, BookOpen, CheckCircle2, FileText, Headset, LogOut, MessagesSquare, RefreshCw, ThumbsDown } from 'lucide-react'
import { api, dt, rub, setToken, token } from './api'
import Markdown from './Markdown'

type Tab = 'escalations' | 'dialogs' | 'quotes' | 'feedback' | 'kb' | 'metrics'
const SCEN: Record<string, string> = { known_product: 'Знает продукт', need_selection: 'Подбор под задачу', question: 'Вопрос', other: 'Другое', '': '—' }

function Login({ onOk }: { onOk: () => void }) {
  const [login, setLogin] = useState('manager')
  const [password, setPassword] = useState('')
  const [err, setErr] = useState('')
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr('')
    try { const d = await api<{ token: string }>('/api/auth/login', { body: { login, password } }); setToken(d.token); onOk() } catch (x) { setErr((x as Error).message) }
  }
  return (
    <div className="login">
      <form onSubmit={submit} className="panel">
        <img src="/logo.png" alt="Софтлайн" className="login-logo" />
        <h2>Кабинет менеджера</h2>
        <p className="muted small">Сложные обращения, КП, база знаний и журнал агента</p>
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

function Escalations() {
  const [status, setStatus] = useState('open')
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
        <div className="seg">{['open', 'answered', 'all'].map(s => <button key={s} className={status === s ? 'on' : ''} onClick={() => setStatus(s)}>{{ open: 'Новые', answered: 'Отвеченные', all: 'Все' }[s]}</button>)}</div>
        {items.length === 0 && <div className="muted pad">Нет обращений</div>}
        {items.map(e => (
          <button key={e.id} className={`item ${sel?.id === e.id ? 'on' : ''}`} onClick={() => { setSel(e); setMsg('') }}>
            <div className="item-top"><span className="tag red">{e.category}</span><span className="muted small">{dt(e.created_at)}</span></div>
            <div className="item-title">{e.summary}</div>
          </button>
        ))}
      </div>
      <div className="detail">
        {msg && <div className="ok"><CheckCircle2 size={16} /> {msg}</div>}
        {!sel && !msg && <div className="muted pad">Выберите обращение. Агент уже собрал резюме и параметры — менеджер решает только сложное.</div>}
        {sel && (
          <>
            <div className="panel"><b>Резюме агента:</b> {sel.summary}</div>
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
  const load = () => api<any[]>('/api/manager/quotes', { auth: true }).then(setItems).catch(() => {})
  useEffect(() => { load() }, [])
  const approve = async (id: string) => { await api(`/api/manager/quotes/${id}/approve`, { auth: true, method: 'POST' }); load() }
  const pdf = async (id: string) => {
    const r = await fetch(`/api/manager/quotes/${id}/pdf`, { headers: { Authorization: `Bearer ${token()}` } })
    window.open(URL.createObjectURL(await r.blob()), '_blank')
  }
  return (
    <div className="pad">
      <table className="grid"><thead><tr><th>Номер</th><th>Заказчик</th><th>Позиции</th><th>Сумма</th><th>Статус</th><th></th></tr></thead><tbody>
        {items.map(q => (
          <tr key={q.id}>
            <td>{q.number}<div className="muted small">{dt(q.created_at)}</div></td>
            <td>{q.customer?.company || '—'}<div className="muted small">{q.customer?.inn ? `ИНН ${q.customer.inn}` : 'ждём реквизиты'}</div></td>
            <td>{q.items.map((i: any) => <div key={i.offer_id} className="small">{i.quantity} × {i.name}</div>)}</td>
            <td>{rub(q.total)}</td>
            <td><span className={`tag ${q.status === 'approved' ? 'green' : ''}`}>{{ awaiting_requisites: 'Ждём реквизиты', draft: 'Черновик', approved: 'Подтверждено' }[q.status as string]}</span></td>
            <td className="nowrap">{q.status !== 'awaiting_requisites' && <button className="btn sm ghost" onClick={() => pdf(q.id)}>PDF</button>} {q.status === 'draft' && <button className="btn sm" onClick={() => approve(q.id)}>Подтвердить</button>}</td>
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
    ['Диалогов', m.conversations], ['Решено без менеджера', `${Math.round(m.automation_rate * 100)}%`], ['Открытых эскалаций', m.escalations_open],
    ['Стоимость 1 запроса', `${m.cost_per_request_rub} ₽`], ['Всего на модели', `${m.cost_total_rub} ₽`], ['Статей от менеджеров', m.kb_from_managers],
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
        <div className="panel"><b>Каталог</b><div className="kv"><span>Позиций</span><span>{m.catalog.offers}</span></div><div className="kv"><span>С ценой</span><span>{m.catalog.available_priced}</span></div><div className="kv"><span>Обновлён</span><span>{m.catalog.loaded_at}</span></div></div>
      </div>
    </div>
  )
}

export default function Manager() {
  const [authed, setAuthed] = useState(!!token())
  const [tab, setTab] = useState<Tab>('escalations')
  useEffect(() => { const f = () => setAuthed(false); window.addEventListener('sl-logout', f); return () => window.removeEventListener('sl-logout', f) }, [])
  if (!authed) return <Login onOk={() => setAuthed(true)} />
  const tabs: [Tab, string, any][] = [['escalations', 'Эскалации', Headset], ['dialogs', 'Диалоги', MessagesSquare], ['quotes', 'КП и счета', FileText], ['feedback', 'Отзывы', ThumbsDown], ['kb', 'База знаний', BookOpen], ['metrics', 'Метрики', BarChart3]]
  return (
    <div className="mgr">
      <aside>
        <a href="/" className="brand"><img src="/logo-white.png" alt="Софтлайн" /></a>
        {tabs.map(([k, label, Icon]) => <button key={k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)}><Icon size={17} /> {label}</button>)}
        <div className="spacer" />
        <button onClick={() => location.reload()}><RefreshCw size={16} /> Обновить</button>
        <button onClick={() => { setToken(''); setAuthed(false) }}><LogOut size={16} /> Выйти</button>
      </aside>
      <section className="mgr-main">
        <h2>{tabs.find(t => t[0] === tab)![1]}</h2>
        {tab === 'escalations' && <Escalations />}
        {tab === 'dialogs' && <Dialogs />}
        {tab === 'quotes' && <Quotes />}
        {tab === 'feedback' && <Feedback />}
        {tab === 'kb' && <Kb />}
        {tab === 'metrics' && <Metrics />}
      </section>
    </div>
  )
}
