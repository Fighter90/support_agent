import { useEffect, useRef, useState, type FormEvent } from 'react'
import { BadgeCheck, FileText, Headset, Loader2, Send, ShieldCheck, ThumbsDown, ThumbsUp, UserRound } from 'lucide-react'
import { api, rub, type Message, type Quote } from './api'
import Markdown from './Markdown'

const CONV_KEY = 'sl_conversation'
const PROMPTS = [
  'Нужен MDaemon Standard',
  'Не знаю, что нужно: хотим защитить почту и файлы на 50 сотрудников',
  'Есть российский аналог?',
  'Цены указаны с НДС?',
]

function QuoteCard({ q, onDone }: { q: Quote; onDone: () => void }) {
  const [f, setF] = useState({ company: '', inn: '', kpp: '', email: '' })
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  if (q.status !== 'awaiting_requisites') {
    return (
      <div className="quote">
        <FileText size={18} />
        <div><b>{q.number}</b> · {rub(q.total_rub)}<div className="muted">{q.status === 'approved' ? 'Подтверждено менеджером' : 'Черновик, проверяется менеджером'}</div></div>
        {q.pdf && <a className="btn sm" href={q.pdf} target="_blank" rel="noreferrer">PDF</a>}
      </div>
    )
  }
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true); setErr('')
    try { await api(`/api/quotes/${q.quote_id}/requisites`, { body: f }); onDone() } catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  return (
    <form className="quote-form" onSubmit={submit}>
      <div className="qf-head"><ShieldCheck size={16} /> Реквизиты для КП {q.number} на {rub(q.total_rub)}</div>
      <div className="muted small">Данные идут сразу в КП и не передаются языковой модели.</div>
      <input placeholder="Название организации" value={f.company} onChange={e => setF({ ...f, company: e.target.value })} required />
      <div className="row2">
        <input placeholder="ИНН" inputMode="numeric" value={f.inn} onChange={e => setF({ ...f, inn: e.target.value })} required />
        <input placeholder="КПП (если есть)" inputMode="numeric" value={f.kpp} onChange={e => setF({ ...f, kpp: e.target.value })} />
      </div>
      <input placeholder="E-mail для КП" type="email" value={f.email} onChange={e => setF({ ...f, email: e.target.value })} />
      {err && <div className="err">{err}</div>}
      <button className="btn" disabled={busy}>{busy ? <Loader2 className="spin" size={16} /> : 'Сформировать КП'}</button>
    </form>
  )
}

export default function Chat() {
  const [cid, setCid] = useState('')
  const [msgs, setMsgs] = useState<Message[]>([])
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [rated, setRated] = useState<Record<number, number>>({})
  const box = useRef<HTMLDivElement>(null)

  const load = async (id: string, after = 0) => {
    const d = await api<{ messages: Message[] }>(`/api/chat/${id}?after=${after}`)
    if (d.messages.length) setMsgs(m => (after ? [...m, ...d.messages.filter(x => !m.some(y => y.id === x.id))] : d.messages))
  }

  const start = async () => {
    const d = await api<{ conversation_id: string; messages: Message[] }>('/api/chat/start', { body: { page_url: location.href } })
    try { localStorage.setItem(CONV_KEY, d.conversation_id) } catch { /* ignore */ }
    setCid(d.conversation_id); setMsgs(d.messages)
  }

  useEffect(() => {
    let id = ''
    try { id = localStorage.getItem(CONV_KEY) || '' } catch { /* ignore */ }
    if (id) { setCid(id); load(id).catch(() => start()) } else start().catch(e => setErr(e.message))
  }, [])

  // Ответы менеджера приходят в тот же чат: опрос раз в 4 секунды
  useEffect(() => {
    if (!cid) return
    const t = setInterval(() => { if (!busy) load(cid, msgs.length ? msgs[msgs.length - 1].id : 0).catch(() => {}) }, 4000)
    return () => clearInterval(t)
  }, [cid, msgs, busy])

  useEffect(() => { box.current?.scrollTo({ top: box.current.scrollHeight, behavior: 'smooth' }) }, [msgs, busy])

  const send = async (t: string) => {
    const value = t.trim()
    if (!value || busy || !cid) return
    setText(''); setErr(''); setBusy(true)
    const tmp: Message = { id: -Date.now(), role: 'user', content: value, meta: {} }
    setMsgs(m => [...m, tmp])
    try {
      const r = await api<Message>(`/api/chat/${cid}/message`, { body: { text: value } })
      setMsgs(m => [...m, r])
    } catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
  }

  const rate = async (id: number, v: number) => {
    setRated(r => ({ ...r, [id]: v }))
    api(`/api/chat/${cid}/feedback`, { body: { message_id: id, rating: v } }).catch(() => {})
  }

  const reset = () => { try { localStorage.removeItem(CONV_KEY) } catch { /* ignore */ } setMsgs([]); start() }

  return (
    <div className="chat">
      <div className="chat-head">
        <div className="avatar"><img src="/logo-mark.svg" alt="" onError={e => ((e.target as HTMLImageElement).style.display = 'none')} />S</div>
        <div><b>ИИ-консультант Softline</b><div className="muted small">Подбор лицензий · отвечает сразу, менеджер — в рабочее время</div></div>
        <button className="link small" onClick={reset}>Новый диалог</button>
      </div>
      <div className="chat-body" ref={box}>
        {msgs.map(m => (
          <div key={m.id} className={`msg ${m.role}`}>
            {m.role === 'manager' && <div className="who"><Headset size={14} /> Менеджер Softline</div>}
            {m.role === 'user' && <div className="who right"><UserRound size={14} /> Вы</div>}
            <div className="bubble">
              <Markdown text={m.content} />
              {m.meta?.products && m.meta.products.length > 0 && (
                <div className="cards">
                  {m.meta.products.map(p => (
                    <a key={p.group_id} className="card" href={p.url} target="_blank" rel="noreferrer">
                      <div className="card-title">{p.product}</div>
                      <div className="muted small">{p.vendor}</div>
                      <div className="card-foot"><span>от {rub(p.price_from_rub)}</span>{p.domestic && <span className="tag"><BadgeCheck size={12} /> Реестр ПО</span>}</div>
                    </a>
                  ))}
                </div>
              )}
              {m.meta?.quote && <QuoteCard q={m.meta.quote} onDone={() => load(cid, m.id)} />}
              {m.meta?.escalation && <div className="esc"><Headset size={14} /> Передано менеджеру: {m.meta.escalation.reason}</div>}
            </div>
            {m.role === 'assistant' && m.id > 0 && msgs[0]?.id !== m.id && (
              <div className="rate">
                <button className={rated[m.id] === 1 ? 'on' : ''} onClick={() => rate(m.id, 1)} title="Полезно"><ThumbsUp size={13} /></button>
                <button className={rated[m.id] === -1 ? 'on' : ''} onClick={() => rate(m.id, -1)} title="Не помогло"><ThumbsDown size={13} /></button>
              </div>
            )}
          </div>
        ))}
        {busy && <div className="msg assistant"><div className="bubble typing"><span /><span /><span /> Подбираю по каталогу…</div></div>}
        {msgs.length <= 1 && !busy && (
          <div className="prompts">{PROMPTS.map(p => <button key={p} onClick={() => send(p)}>{p}</button>)}</div>
        )}
      </div>
      {err && <div className="err pad">{err}</div>}
      <form className="chat-input" onSubmit={e => { e.preventDefault(); send(text) }}>
        <textarea value={text} rows={1} placeholder="Назовите продукт или опишите задачу…" onChange={e => setText(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(text) } }} />
        <button className="send" disabled={busy || !text.trim()} aria-label="Отправить"><Send size={18} /></button>
      </form>
      <div className="note">Персональные данные маскируются и обрабатываются на серверах в РФ (152-ФЗ).</div>
    </div>
  )
}
