import { useEffect, useRef, useState, type FormEvent } from 'react'
import { BadgeCheck, Bot, CreditCard, FileText, Headset, History, Loader2, MessageCircle, Minus, Send, ShieldCheck, ThumbsDown, ThumbsUp, UserRound } from 'lucide-react'
import { api, rub, type Message, type Quote } from './api'
import Markdown from './Markdown'

const CONV_KEY = 'sl_conversation'
const HIST_KEY = 'sl_history'          // id диалогов этого браузера — для «Истории чатов»
const HIST_DAYS = 30                   // сколько дней история видна на устройстве
type Hist = { id: string; at: number }
function histGet(): Hist[] {
  try { return (JSON.parse(localStorage.getItem(HIST_KEY) || '[]') as Hist[]).filter(h => Date.now() - h.at < HIST_DAYS * 86400000) } catch { return [] }
}
function histSet(h: Hist[]) { try { localStorage.setItem(HIST_KEY, JSON.stringify(h.slice(0, 30))) } catch { /* приватный режим */ } }
function histAdd(id: string) { histSet([{ id, at: Date.now() }, ...histGet().filter(h => h.id !== id)]) }
const PROMPTS = [
  'Нужен MDaemon Standard',
  'Не знаю, что нужно: хотим защитить почту и файлы на 50 сотрудников',
  'Есть российский аналог?',
  'Цены указаны с НДС?',
]

let payEnabled = false
fetch('/api/health').then(r => r.json()).then(d => { payEnabled = !!d.payments }).catch(() => {})

function PayButton({ qid }: { qid: string }) {
  const [st, setSt] = useState<{ busy?: boolean; err?: string; status?: string }>({})
  const pay = async () => {
    setSt({ busy: true })
    try {
      const r = await api<{ url: string; status: string }>(`/api/quotes/${qid}/pay`, { method: 'POST' })
      setSt({ status: r.status }); if (r.url) window.open(r.url, '_blank')
    } catch (e) { setSt({ err: (e as Error).message }) }
  }
  if (st.status === 'succeeded') return <span className="tag green">Оплачено</span>
  return <button className="btn sm ghost" onClick={pay} disabled={st.busy} title={st.err || 'Оплата картой или СБП через ЮKassa'}><CreditCard size={14} /> {st.err ? 'Ошибка оплаты' : 'Оплатить онлайн'}</button>
}

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
        <div><b>{q.kind === 'invoice' ? 'Счёт' : 'КП'} {q.number}</b> · {rub(q.total_rub)}<div className="muted">{q.status === 'approved' ? 'Подтверждено менеджером' : 'Готов — можно скачать и оплатить'}{q.discount ? ` · скидка ${q.discount}%` : ''}</div></div>
        {q.pdf && <a className="btn sm" href={q.pdf} target="_blank" rel="noreferrer">PDF</a>}
        {payEnabled && <PayButton qid={q.quote_id} />}
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
  const [mgrTyping, setMgrTyping] = useState(false)
  const [mgrMode, setMgrMode] = useState(false)
  const [model, setModel] = useState('')         // модель диалога, если не основная (открытые веса)
  const [min, setMin] = useState(false)          // свёрнут в иконку внизу справа
  const [unread, setUnread] = useState(0)
  const [hist, setHist] = useState<any[] | null>(null)
  const ws = useRef<WebSocket | null>(null)
  const box = useRef<HTMLDivElement>(null)
  const [survey, setSurvey] = useState<{ resolved?: boolean; score?: number; comment: string; sent: boolean }>({ comment: '', sent: false })
  const filledQuotes = new Set(msgs.filter(x => x.meta?.quote && x.meta.quote.status !== 'awaiting_requisites').map(x => x.meta.quote!.quote_id))
  // Опрос показываем, только когда разговор похоже завершён: была оплата, или клиент молчит 45 с после ответа
  // (1,5 мин, если ассистент задал уточняющий вопрос), и нет незаполненной формы реквизитов. Пока диалог идёт — не мешаем.
  const lastChange = useRef(Date.now())
  const [clock, setClock] = useState(Date.now())
  useEffect(() => { lastChange.current = Date.now() }, [msgs.length, text])
  useEffect(() => { const t = window.setInterval(() => setClock(Date.now()), 5000); return () => window.clearInterval(t) }, [])
  const lastMsg = msgs[msgs.length - 1]
  const paid = msgs.some(x => x.role === 'system' && /Оплата .*получена/.test(x.content))
  const pendingForm = msgs.some(x => x.meta?.quote?.status === 'awaiting_requisites' && !filledQuotes.has(x.meta.quote.quote_id))
  // Вежливое «Нужна помощь с оформлением?» в конце ответа — не уточняющий вопрос: опрос можно показывать через 45 с
  const closing = /(нужна|нужно|требуется)[^?]{0,40}помощь|остал[ии]сь[^?]{0,20}вопрос|ч[её]м ещ[её]|есть ли (ещё|еще|другие) вопрос|могу ли (ещё|еще) помочь/i
  const asksQuestion = !!lastMsg && lastMsg.role === 'assistant' && /\?\s*$/.test(lastMsg.content.trim()) && !closing.test(lastMsg.content.slice(-120))
  const answered = msgs.filter(x => x.role !== 'user').length >= 2 && !!lastMsg && lastMsg.role !== 'user' && !pendingForm && !mgrMode
    && (paid || clock - lastChange.current > (asksQuestion ? 90000 : 45000))  // на встречный вопрос ассистента ждём дольше

  const load = async (id: string, after = 0) => {
    const d = await api<{ messages: Message[]; model?: string }>(`/api/chat/${id}?after=${after}`)
    if (!after) setModel(d.model || '')
    if (d.messages.length) setMsgs(m => (after ? [...m, ...d.messages.filter(x => !m.some(y => y.id === x.id))] : d.messages))
  }

  const start = async () => {
    const want = new URLSearchParams(location.search).get('model') === 'open' ? 'openweights' : ''  // /?model=open — диалог на модели с открытыми весами
    const d = await api<{ conversation_id: string; model?: string; messages: Message[] }>('/api/chat/start', { body: { page_url: location.href, model: want } })
    setModel(d.model || '')
    try { localStorage.setItem(CONV_KEY, d.conversation_id) } catch { /* ignore */ }
    histAdd(d.conversation_id)
    setCid(d.conversation_id); setMsgs(d.messages)
  }

  useEffect(() => {
    let id = ''
    try { id = localStorage.getItem(CONV_KEY) || '' } catch { /* ignore */ }
    if (id) { setCid(id); histAdd(id); load(id).catch(() => start()) } else start().catch(e => setErr(e.message))
  }, [])

  // Реалтайм: сообщения менеджера и системные события приходят по WebSocket; опрос раз в 15 с — запасной канал
  useEffect(() => {
    if (!cid) return
    let closed = false
    const connect = () => {
      const sock = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/chat/${cid}`)
      ws.current = sock
      sock.onmessage = ev => {
        const d = JSON.parse(ev.data)
        if (d.type === 'typing' && d.who === 'manager') { setMgrTyping(true); setTimeout(() => setMgrTyping(false), 3000) }
        if (d.type === 'message') {
          const m: Message = d.message
          if (m.role === 'manager') setMgrTyping(false)
          if (m.role !== 'user') setUnread(u => u + 1)
          if (m.role === 'system') setMgrMode(/подключился/.test(m.content))
          setMsgs(cur => cur.some(x => x.id === m.id) ? cur : [...cur.filter(x => !(x.id < 0 && x.role === m.role && x.content === m.content)), m])
        }
      }
      sock.onclose = () => { if (!closed) setTimeout(connect, 3000) }
    }
    connect()
    return () => { closed = true; ws.current?.close() }
  }, [cid])

  useEffect(() => {
    if (!cid) return
    const t = setInterval(() => { if (!busy) load(cid, msgs.length ? Math.max(...msgs.map(x => x.id)) : 0).catch(() => {}) }, 15000)
    return () => clearInterval(t)
  }, [cid, msgs, busy])

  // Поисковая строка витрины → вопрос ассистенту
  useEffect(() => {
    const f = (e: Event) => { const q = (e as CustomEvent).detail as string; if (q) send(`Подберите, пожалуйста: ${q}`) }
    window.addEventListener('sl-ask', f)
    return () => window.removeEventListener('sl-ask', f)
  })

  useEffect(() => { box.current?.scrollTo({ top: box.current.scrollHeight, behavior: 'smooth' }) }, [msgs, busy])

  const send = async (t: string) => {
    const value = t.trim()
    if (!value || busy || !cid) return
    setText(''); setErr(''); setBusy(true)
    const tmp: Message = { id: -Date.now(), role: 'user', content: value, meta: {} }
    setMsgs(m => [...m, tmp])
    try {
      const r = await api<Message & { queued?: boolean }>(`/api/chat/${cid}/message`, { body: { text: value } })
      if (r.queued) { setMgrMode(true); setMsgs(m => m.map(x => x.id === tmp.id ? { ...x, id: r.id } : x)) }
      else setMsgs(m => m.some(x => x.id === r.id) ? m : [...m, r])
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

  const openHistory = async () => {
    const ids = histGet().map(h => h.id)
    try { const d = await api<{ items: any[] }>('/api/chat/history', { body: { ids } }); setHist(d.items) } catch (e) { setErr((e as Error).message) }
  }
  const openChat = (id: string) => {
    try { localStorage.setItem(CONV_KEY, id) } catch { /* ignore */ }
    histAdd(id); setHist(null); setMgrMode(false); setSurvey({ comment: '', sent: false }); setCid(id); load(id).catch(() => {})
  }
  const forget = async () => {
    const ids = histGet().map(h => h.id)
    await api('/api/chat/forget', { body: { ids } }).catch(() => {})
    histSet([]); setHist(null); reset()
  }
  const reset = () => { try { localStorage.removeItem(CONV_KEY) } catch { /* ignore */ } setMsgs([]); setSurvey({ comment: '', sent: false }); start() }

  if (min) return (
    <button className="chat-fab" onClick={() => { setMin(false); setUnread(0) }} aria-label="Открыть чат с ассистентом">
      <MessageCircle size={26} />{unread ? <span className="fab-badge">{unread}</span> : null}
    </button>
  )
  return (
    <div className="chat">
      <div className="chat-head">
        <div className="avatar"><Bot size={22} /></div>
        <div><b>ИИ-ассистент Softline</b><div className="muted small">{mgrMode ? 'Диалог ведёт менеджер Softline' : 'Отвечает виртуальный помощник · менеджер — по кнопке'}{model ? <span className="tag model-tag"> {model}</span> : null}</div></div>
        <div className="head-actions">
          <button className="btn sm ghost" onClick={operator} title="Переключиться на живого менеджера"><Headset size={14} /> Позвать оператора</button>
          <button className="link small" onClick={reset}>Новый диалог</button>
          <button className="link small" onClick={openHistory} title="Ваши прошлые чаты на этом устройстве"><History size={13} /> История</button>
          <button className="icon-btn" onClick={() => { setMin(true); setUnread(0) }} title="Свернуть в иконку" aria-label="Свернуть чат"><Minus size={16} /></button>
        </div>
      </div>
      {hist && (
        <div className="hist">
          <div className="hist-head"><b>История чатов</b><button className="link small" onClick={() => setHist(null)}>Закрыть</button></div>
          <p className="muted small">Ваши диалоги на этом устройстве за {HIST_DAYS} дней. Нажмите, чтобы открыть и продолжить. Персональные данные из чатов хранятся 180 дней, дальше — только обезличенная переписка.</p>
          {hist.length === 0 && <div className="muted small">Пока нет прошлых чатов.</div>}
          {hist.map(h => (
            <button key={h.id} className={`hist-item ${h.id === cid ? 'on' : ''}`} onClick={() => openChat(h.id)}>
              <span>{h.title}</span>
              <span className="muted small">{new Date(h.updated_at * 1000).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })}{h.mode === 'manager' ? ' · с менеджером' : ''}{h.id === cid ? ' · открыт' : ''}</span>
            </button>))}
          {hist.length > 0 && <button className="link small danger" onClick={forget}>Удалить историю и мои данные из чатов</button>}
        </div>
      )}
      <div className="chat-body" ref={box}>
        {msgs.map(m => (
          m.role === 'system' ? <div key={m.id} className="sysmsg">{m.content}</div> :
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
        {busy && !mgrMode && <div className="msg assistant"><div className="bubble typing"><span /><span /><span /> Подбираю по каталогу…</div></div>}
        {mgrTyping && <div className="msg manager"><div className="bubble typing"><span /><span /><span /> Менеджер печатает…</div></div>}
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
        <textarea value={text} rows={1} placeholder="Назовите продукт или опишите задачу…" onChange={e => { setText(e.target.value); if (mgrMode && ws.current?.readyState === 1) ws.current.send(JSON.stringify({ type: 'typing' })) }}
          onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(text) } }} />
        <button className="send" disabled={busy || !text.trim()} aria-label="Отправить"><Send size={18} /></button>
      </form>
      <div className="note">Ответы готовит ИИ-ассистент по каталогу и базе знаний store.softline.ru · ПДн маскируются и обрабатываются в РФ (152-ФЗ)</div>
    </div>
  )
}
