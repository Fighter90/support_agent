import { useEffect, useRef, useState, type FormEvent } from 'react'
import { BadgeCheck, Bot, FileText, Headset, Loader2, Send, ShieldCheck, ThumbsDown, ThumbsUp, UserRound } from 'lucide-react'
import { api, rub, type Message, type Quote } from './api'
import Markdown from './Markdown'

const CONV_KEY = 'sl_conversation'
const PROMPTS = [
  'Нужен MDaemon Standard',
  'Не знаю, что нужно: хотим защитить почту и файлы на 50 сотрудников',
  'Есть российский аналог?',
  'Цены указаны с НДС?',
]

function QuoteCard({ q, onDone, filled }: { q: Quote; onDone: () => void; filled: boolean }) {
  const [f, setF] = useState({ company: '', inn: '', kpp: '', email: '' })
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  if (q.status === 'awaiting_requisites' && filled) {
    return <div className="quote muted"><ShieldCheck size={16} /> Реквизиты получены — документ {q.number} сформирован ниже</div>
  }
  if (q.status !== 'awaiting_requisites') {
    return (
      <div className="quote">
        <FileText size={18} />
        <div><b>{q.kind === 'invoice' ? 'Счёт' : 'КП'} {q.number}</b> · {rub(q.total_rub)}<div className="muted">{q.status === 'approved' ? 'Подтверждено менеджером' : 'Черновик, проверяется менеджером'}</div></div>
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
      <div className="qf-head"><ShieldCheck size={16} /> Реквизиты для {q.kind === 'invoice' ? 'счёта' : 'КП'} {q.number} на {rub(q.total_rub)}{q.discount ? ` (скидка ${q.discount}%)` : ''}</div>
      <div className="muted small">Данные идут сразу в КП и не передаются языковой модели.</div>
      <input placeholder="Название организации" value={f.company} onChange={e => setF({ ...f, company: e.target.value })} required />
      <div className="row2">
        <input placeholder="ИНН" inputMode="numeric" value={f.inn} onChange={e => setF({ ...f, inn: e.target.value })} required />
        <input placeholder="КПП (если есть)" inputMode="numeric" value={f.kpp} onChange={e => setF({ ...f, kpp: e.target.value })} />
      </div>
      <input placeholder="E-mail для КП" type="email" value={f.email} onChange={e => setF({ ...f, email: e.target.value })} />
      {err && <div className="err">{err}</div>}
      <button className="btn" disabled={busy}>{busy ? <Loader2 className="spin" size={16} /> : q.kind === 'invoice' ? 'Сформировать счёт' : 'Сформировать КП'}</button>
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
  const [survey, setSurvey] = useState<{ resolved?: boolean; score?: number; comment: string; sent: boolean }>({ comment: '', sent: false })
  const filledQuotes = new Set(msgs.filter(x => x.meta?.quote && x.meta.quote.status !== 'awaiting_requisites').map(x => x.meta.quote!.quote_id))
  const answered = msgs.filter(x => x.role !== 'user').length >= 3

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

  const operator = async () => {
    if (!cid) return
    try { const r = await api<{ message?: Message }>(`/api/chat/${cid}/operator`, { method: 'POST' }); if (r.message) setMsgs(m => [...m, r.message!]) } catch (e) { setErr((e as Error).message) }
  }

  const sendSurvey = async () => {
    if (survey.resolved === undefined || !survey.score) return
    try { await api(`/api/chat/${cid}/survey`, { body: { resolved: survey.resolved, score: survey.score, comment: survey.comment } }); setSurvey(v => ({ ...v, sent: true })) } catch (e) { setErr((e as Error).message) }
  }

  const reset = () => { try { localStorage.removeItem(CONV_KEY) } catch { /* ignore */ } setMsgs([]); setSurvey({ comment: '', sent: false }); start() }

  return (
    <div className="chat">
      <div className="chat-head">
        <div className="avatar"><Bot size={22} /></div>
        <div><b>ИИ-ассистент Softline</b><div className="muted small">Отвечает виртуальный помощник · менеджер — по кнопке</div></div>
        <div className="head-actions">
          <button className="btn sm ghost" onClick={operator} title="Переключиться на живого менеджера"><Headset size={14} /> Позвать оператора</button>
          <button className="link small" onClick={reset}>Новый диалог</button>
        </div>
      </div>
      <div className="chat-body" ref={box}>
        {msgs.map(m => (
          <div key={m.id} className={`msg ${m.role}`}>
            {m.role === 'manager' && <div className="who"><Headset size={14} /> Менеджер Softline</div>}
            {m.role === 'assistant' && <div className="who"><Bot size={14} /> ИИ-ассистент</div>}
            {m.role === 'user' && <div className="who right"><UserRound size={14} /> Вы</div>}
            <div className="bubble">
              <Markdown text={m.content} />
              {m.meta?.products && m.meta.products.length > 0 && (
                <div className="cards">
                  {m.meta.products.map(p => (
                    <a key={p.group_id} className="card" href={p.url} target="_blank" rel="noreferrer">
                      <div className="card-title">{p.product}</div>
                      <div className="muted small">{p.vendor}</div>
                      <div className="card-foot"><span>от {rub(p.price_from_rub)}</span>{p.domestic && <span className="tag"><BadgeCheck size={12} /> {/№\s*\d/.test(p.registry || '') ? 'Реестр ПО' : 'Российский вендор'}</span>}</div>
                    </a>
                  ))}
                </div>
              )}
              {m.meta?.quote && <QuoteCard q={m.meta.quote} filled={filledQuotes.has(m.meta.quote.quote_id)} onDone={() => load(cid, m.id)} />}
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
        {answered && !busy && (
          <div className="survey">
            {survey.sent ? <div className="ok small">Спасибо! Ваш ответ поможет обучить ассистента.</div> : (<>
              <b>Ответьте, пожалуйста, — это помогает обучать ассистента</b>
              <div className="row-q">Вопрос решён?
                <button className={survey.resolved === true ? 'on' : ''} onClick={() => setSurvey(v => ({ ...v, resolved: true }))}>Да</button>
                <button className={survey.resolved === false ? 'on' : ''} onClick={() => setSurvey(v => ({ ...v, resolved: false }))}>Нет</button>
              </div>
              <div className="row-q">Оценка: {[1, 2, 3, 4, 5].map(n => <button key={n} className={survey.score === n ? 'on' : ''} onClick={() => setSurvey(v => ({ ...v, score: n }))}>{n}</button>)}</div>
              <input placeholder="Что улучшить? (необязательно)" value={survey.comment} onChange={e => setSurvey(v => ({ ...v, comment: e.target.value }))} />
              <button className="btn sm" disabled={survey.resolved === undefined || !survey.score} onClick={sendSurvey}>Отправить оценку</button>
            </>)}
          </div>
        )}
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
      <div className="note">Ответы готовит ИИ-ассистент по каталогу и базе знаний store.softline.ru · ПДн маскируются и обрабатываются в РФ (152-ФЗ)</div>
    </div>
  )
}
