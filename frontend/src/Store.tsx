import { useEffect, useRef, useState } from 'react'
import { Search, ShieldCheck, Sparkles, Zap } from 'lucide-react'
import { api, rub } from './api'
import Chat from './Chat'

function SearchBox() {
  const [q, setQ] = useState('')
  const [items, setItems] = useState<any[]>([])
  const [open, setOpen] = useState(false)
  const t = useRef<number | undefined>(undefined)
  useEffect(() => {
    window.clearTimeout(t.current)
    if (q.trim().length < 2) { setItems([]); return }
    t.current = window.setTimeout(() => api<{ items: any[] }>(`/api/catalog/search?q=${encodeURIComponent(q)}`).then(d => { setItems(d.items); setOpen(true) }).catch(() => {}), 250)
  }, [q])
  const ask = () => { window.dispatchEvent(new CustomEvent('sl-ask', { detail: q })); setOpen(false) }
  return (
    <div className="search" onBlur={() => setTimeout(() => setOpen(false), 200)}>
      <Search size={16} />
      <input value={q} onChange={e => setQ(e.target.value)} onFocus={() => items.length && setOpen(true)} placeholder="Поиск по 136 000 товаров"
        onKeyDown={e => { if (e.key === 'Enter') ask() }} />
      {open && q.trim().length >= 2 && (
        <div className="suggest">
          {items.map(i => (
            <a key={i.group_id} href={i.url} target="_blank" rel="noreferrer" className="sg">
              <div><b>{i.product}</b><div className="muted small">{i.vendor} · {i.category}</div></div>
              <span className="sg-price">{i.price_from_rub ? 'от ' + rub(i.price_from_rub) : 'цена по запросу'}</span>
            </a>
          ))}
          {items.length === 0 && <div className="muted small pad">Ничего не нашлось — спросите ассистента</div>}
          <button className="sg ask" onMouseDown={ask}>Спросить ИИ-ассистента: «{q}» →</button>
        </div>
      )}
    </div>
  )
}

export default function Store() {
  const [stats, setStats] = useState<{ offers?: number; available_priced?: number; domestic?: number; loaded_at?: string }>({})
  useEffect(() => { api('/api/health').then(d => setStats(d.catalog || {})).catch(() => {}) }, [])
  return (
    <div className="store">
      <header className="topbar">
        <div className="wrap topbar-in">
          <a href="/" className="brand"><img src="/logo.png" alt="Софтлайн решения" /></a>
          <SearchBox />
          <nav><a href="https://store.softline.ru/" target="_blank" rel="noreferrer">Каталог</a><a href="/manager">Кабинет менеджера</a></nav>
        </div>
      </header>
      <main className="wrap layout">
        <section className="hero">
          <div className="eyebrow"><Sparkles size={14} /> ИИ-консультант store.softline.ru</div>
          <h1>Подберём лицензию за пару минут</h1>
          <p>Назовите продукт — агент сверит версии и редакции по каталогу. Не знаете, что нужно, — опишите задачу: он задаст уточняющие вопросы и предложит варианты с обоснованием, а после согласия подготовит коммерческое предложение.</p>
          <div className="facts">
            <div><b>{stats.offers ? stats.offers.toLocaleString('ru-RU') : '136 040'}</b><span>позиций в каталоге</span></div>
            <div><b>{stats.available_priced ? stats.available_priced.toLocaleString('ru-RU') : '55 771'}</b><span>с ценой — в наличии</span></div>
            <div><b>{stats.domestic ? stats.domestic.toLocaleString('ru-RU') : '1 208'}</b><span>из реестра отечественного ПО</span></div>
          </div>
          <ul className="bullets">
            <li><Zap size={16} /> Цены только из актуального фида магазина{stats.loaded_at ? ` (обновлён ${stats.loaded_at})` : ''}</li>
            <li><ShieldCheck size={16} /> Сложные вопросы — скидки, тендеры, цены по запросу — сразу у менеджера</li>
            <li><ShieldCheck size={16} /> GigaChat на серверах в РФ, персональные данные не уходят в модель</li>
          </ul>
        </section>
        <Chat />
      </main>
      <footer className="wrap foot">© ПАО «Софтлайн» · прототип ИИ-агента для хакатона «ИИкузница» · 8 (800) 200-08-60</footer>
    </div>
  )
}
