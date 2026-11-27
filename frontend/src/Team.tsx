// Команда поддержки: статус менеджера в шапке, профиль, раздел администратора «Команда», выбор коллеги.
import { createContext, useContext, useEffect, useState, type FormEvent } from 'react'
import { Copy, KeyRound, UserPlus } from 'lucide-react'
import { api, dt } from './api'

export type Me = { login: string; name: string; role: 'admin' | 'manager'; status: string; status_ru: string; online: boolean; load: number; max_load: number; has_password: boolean }
export const MeCtx = createContext<Me | null>(null)
export const useMe = () => useContext(MeCtx)
export const ROLE: Record<string, string> = { admin: 'Администратор', manager: 'Менеджер' }
const ST: [string, string][] = [['online', 'В сети'], ['away', 'Перерыв'], ['offline', 'Не в сети']]

/** Сигнал «кабинет открыт» раз в 30 с — по нему работает автоназначение обращений. */
export function usePresence(): [Me | null, (s: string) => void] {
  const [me, setMe] = useState<Me | null>(null)
  useEffect(() => {
    const ping = () => api<Me>('/api/manager/presence', { auth: true, body: {} }).then(setMe).catch(() => {})
    ping(); const t = window.setInterval(ping, 30000); return () => window.clearInterval(t)
  }, [])
  const set = (status: string) => api<Me>('/api/manager/presence', { auth: true, body: { status } }).then(setMe).catch(() => {})
  return [me, set]
}

export function StatusBar({ me, setStatus }: { me: Me | null; setStatus: (s: string) => void }) {
  const [q, setQ] = useState<{ queue: number; members: any[] } | null>(null)
  useEffect(() => { const f = () => api('/api/manager/team', { auth: true }).then(setQ).catch(() => {}); f(); const t = window.setInterval(f, 8000); return () => window.clearInterval(t) }, [])
  if (!me) return null
  const online = q?.members.filter(m => m.online).length ?? 0
  return (
    <div className="statusbar">
      <span className={`dot ${me.online ? '' : 'off'}`} />
      <b>{me.name}</b><span className="tag">{ROLE[me.role]}</span>
      <select aria-label="Мой статус" value={me.status} onChange={e => setStatus(e.target.value)}>{ST.map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
      <span className="muted small">Нагрузка: {me.load}/{me.max_load}</span>
      <span className="spacer" />
      <span className="muted small">В сети: {online} из {q?.members.length ?? '—'}</span>
      <span className={`tag ${q?.queue ? 'red' : 'green'}`}>В очереди: {q?.queue ?? 0}</span>
    </div>
  )
}

/** Выпадающий выбор коллеги: «Передать» / «Позвать коллегу». */
export function ColleaguePick({ label, onPick, exclude = [] }: { label: string; onPick: (login: string) => void; exclude?: string[] }) {
  const [list, setList] = useState<any[]>([])
  useEffect(() => { api('/api/manager/team', { auth: true }).then(d => setList(d.members)).catch(() => {}) }, [])
  return (
    <select className="btn sm ghost pick" value="" aria-label={label} onChange={e => e.target.value && onPick(e.target.value)}>
      <option value="">{label}…</option>
      {list.filter(m => !exclude.includes(m.login)).map(m => <option key={m.login} value={m.login}>{m.name} · {m.online ? 'в сети' : m.status_ru.toLowerCase()} · {m.load}/{m.max_load}</option>)}
    </select>
  )
}

export function Profile() {
  const me = useMe()
  const [pw, setPw] = useState(''); const [pw2, setPw2] = useState(''); const [msg, setMsg] = useState('')
  const save = async (e: FormEvent) => {
    e.preventDefault(); setMsg('')
    if (pw !== pw2) return setMsg('Пароли не совпадают')
    try { await api('/api/manager/password', { auth: true, body: { password: pw } }); setPw(''); setPw2(''); setMsg('Пароль сохранён — входите с логином и этим паролем') } catch (x) { setMsg((x as Error).message) }
  }
  if (!me) return <div className="muted pad">Загрузка…</div>
  return (
    <div className="cols">
      <div className="panel">
        <b>Учётная запись</b>
        <div className="kv"><span>Имя (видит клиент)</span><span>{me.name}</span></div>
        <div className="kv"><span>Логин</span><span>{me.login}</span></div>
        <div className="kv"><span>Роль</span><span>{ROLE[me.role]}</span></div>
        <div className="kv"><span>Статус</span><span>{me.status_ru}</span></div>
        <div className="kv"><span>Нагрузка</span><span>{me.load} из {me.max_load} диалогов</span></div>
        <p className="muted small">Статус «В сети» — вам назначаются новые обращения по очереди. «Перерыв» и «Не в сети» — новые не приходят,
          а ваши невзятые обращения возвращаются в очередь коллегам. Закрыли кабинет без Telegram — через 15 минут статус считается «Не в сети».</p>
      </div>
      <form className="panel row-form" onSubmit={save}>
        <b><KeyRound size={15} /> Пароль для входа</b>
        <p className="muted small">{me.has_password ? 'Пароль задан. Можно сменить.' : 'Вы вошли по ссылке-приглашению. Задайте пароль, чтобы входить по логину.'} Не меньше 8 символов; администратор пароль не видит.</p>
        <input type="password" placeholder="Новый пароль" value={pw} onChange={e => setPw(e.target.value)} autoComplete="new-password" />
        <input type="password" placeholder="Повторите пароль" value={pw2} onChange={e => setPw2(e.target.value)} autoComplete="new-password" />
        <button className="btn" disabled={pw.length < 8}>Сохранить пароль</button>
        {msg && <div className="ok small">{msg}</div>}
      </form>
    </div>
  )
}

const ACT: Record<string, string> = { assign: 'назначено', release: 'вернулось в очередь', timeout: 'не взято вовремя', take: 'взял(а) диалог', return: 'вернул(а) агенту',
  transfer: 'передал(а) диалог', invite: 'позвал(а) коллегу', leave: 'вышел(ла) из диалога', answer: 'ответил(а) на обращение', close: 'закрыл(а) без ответа', user_update: 'изменил(а) учётку' }

export function TeamAdmin() {
  const [d, setD] = useState<any>(null)
  const [log, setLog] = useState<any[]>([])
  const [f, setF] = useState({ login: '', name: '', role: 'manager', max_load: 5, routing: true })
  const [inv, setInv] = useState<{ who: string; url: string } | null>(null)
  const [msg, setMsg] = useState('')
  const [stale, setStale] = useState(24)
  const closeStale = async () => { const r = await api<any>('/api/admin/escalations/close-stale', { auth: true, body: { older_than_hours: stale } }); setMsg(`Закрыто устаревших обращений: ${r.closed}`); load() }
  const load = () => { api('/api/admin/users', { auth: true }).then(setD).catch(e => setMsg((e as Error).message)); api<any[]>('/api/admin/log', { auth: true }).then(setLog).catch(() => {}) }
  useEffect(() => { load(); const t = window.setInterval(load, 6000); return () => window.clearInterval(t) }, [])
  const link = (code: string) => `${location.origin}/manager?invite=${code}`
  const create = async (e: FormEvent) => {
    e.preventDefault(); setMsg('')
    try { const r = await api<any>('/api/admin/users', { auth: true, body: f }); setInv({ who: r.name, url: link(r.invite_code) }); setF({ login: '', name: '', role: 'manager', max_load: 5, routing: true }); load() }
    catch (x) { setMsg((x as Error).message) }
  }
  const patch = async (login: string, body: any) => { try { await api(`/api/admin/users/${login}`, { auth: true, method: 'PATCH', body }); load() } catch (x) { setMsg((x as Error).message) } }
  const reinvite = async (u: any) => { const r = await api<any>(`/api/admin/users/${u.login}/invite`, { auth: true, method: 'POST' }); setInv({ who: u.name, url: link(r.invite_code) }) }
  const routing = async (body: any) => { await api('/api/admin/routing', { auth: true, body }); load() }
  if (!d) return <div className="muted pad">{msg || 'Загрузка…'}</div>
  return (
    <div className="pad team">
      <div className="tiles">
        <div className="tile"><div className="muted small">Сотрудников</div><b>{d.users.filter((u: any) => u.active).length}</b></div>
        <div className="tile"><div className="muted small">В сети сейчас</div><b>{d.users.filter((u: any) => u.online).length}</b></div>
        <div className="tile"><div className="muted small">Открытых обращений</div><b>{d.open}</b></div>
        <div className="tile"><div className="muted small">Ждут в очереди</div><b>{d.queue}</b></div>
      </div>
      <div className="cols">
        <form className="panel row-form" onSubmit={create}>
          <b><UserPlus size={15} /> Новый сотрудник</b>
          <input placeholder="Логин (латиница), например anna.smirnova" value={f.login} onChange={e => setF({ ...f, login: e.target.value })} />
          <input placeholder="Имя — его увидит клиент, например Анна Смирнова" value={f.name} onChange={e => setF({ ...f, name: e.target.value })} />
          <label className="small">Роль <select value={f.role} onChange={e => setF({ ...f, role: e.target.value })}><option value="manager">Менеджер</option><option value="admin">Администратор</option></select></label>
          <label className="small">Одновременно диалогов, не больше <input type="number" min={1} max={30} value={f.max_load} onChange={e => setF({ ...f, max_load: Number(e.target.value) })} style={{ width: 70 }} /></label>
          <label className="check small"><input type="checkbox" checked={f.routing} onChange={e => setF({ ...f, routing: e.target.checked })} /> Получает обращения автоматически</label>
          <button className="btn" disabled={f.login.length < 3 || f.name.length < 2}>Создать и получить ссылку-приглашение</button>
          {msg && <div className={`${/Закрыто/.test(msg) ? 'ok' : 'err'} small`}>{msg}</div>}
          {inv && <div className="ok small invite">Ссылка для «{inv.who}» (одноразовая, 72 ч) — отправьте сотруднику; пароль он задаст сам в «Профиле»:
            <code>{inv.url}</code><button type="button" className="btn sm ghost" onClick={() => navigator.clipboard?.writeText(inv.url)}><Copy size={13} /> Копировать</button></div>}
        </form>
        <div className="panel">
          <b>Распределение обращений</b>
          <label className="check"><input type="checkbox" checked={d.auto_assign} onChange={e => routing({ auto_assign: e.target.checked })} /> Автоназначение по очереди</label>
          <label className="small">Не взял за <select value={d.reassign_seconds} onChange={e => routing({ reassign_seconds: Number(e.target.value) })}>
            {[60, 120, 180, 300, 600].map(s => <option key={s} value={s}>{s / 60} мин</option>)}</select> — передать следующему</label>
          <div className="row" style={{ gap: 8, alignItems: 'center', margin: '6px 0' }}>
            <span className="small">Разобрать очередь: закрыть обращения старше</span>
            <select value={stale} onChange={e => setStale(Number(e.target.value))}>{[0.5, 1, 6, 24, 72].map(h => <option key={h} value={h}>{h < 1 ? '30 мин' : h + ' ч'}</option>)}</select>
            <button type="button" className="btn sm ghost" onClick={closeStale}>Закрыть</button>
          </div>
          <p className="muted small">Очередь — по времени обращения. Исполнитель — менеджер «в сети» с автоназначением и с наименьшей нагрузкой ниже лимита;
            при равенстве — кто дольше без нового обращения. Свободных нет — обращение ждёт в очереди и видно всем, уведомление в Telegram получают все.
            Перерыв или выход — невзятые обращения возвращаются в очередь. Взять обращение вручную может любой менеджер.</p>
        </div>
      </div>
      <table className="tbl">
        <thead><tr><th>Сотрудник</th><th>Роль</th><th>Статус</th><th>Нагрузка</th><th>Ответов за 24 ч</th><th>Telegram</th><th>Автоназначение</th><th>Доступ</th><th></th></tr></thead>
        <tbody>{d.users.map((u: any) => (
          <tr key={u.login} className={u.active ? '' : 'muted'}>
            <td><span className={`dot ${u.online ? '' : 'off'}`} /> <b>{u.name}</b><div className="muted small">{u.login}{u.created_by === 'env' ? ' · из .env' : ''}{!u.has_password ? ' · ещё не входил(а)' : ''}</div></td>
            <td><select value={u.role} onChange={e => patch(u.login, { role: e.target.value })}><option value="manager">Менеджер</option><option value="admin">Администратор</option></select></td>
            <td><select value={u.status} onChange={e => patch(u.login, { status: e.target.value })}>{ST.map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
              <div className="muted small">{u.last_seen ? 'был(а) ' + dt(u.last_seen) : 'не входил(а)'}</div></td>
            <td><input type="number" min={1} max={30} value={u.max_load} style={{ width: 54 }} onChange={e => patch(u.login, { max_load: Number(e.target.value) })} />
              <div className="muted small">сейчас {u.load}: диалогов {u.active_dialogs}, назначено {u.assigned_open}</div></td>
            <td>{u.answered_24h}</td>
            <td>{u.telegram ? '✓' : '—'}</td>
            <td><input type="checkbox" aria-label="Автоназначение" checked={!!u.routing} onChange={e => patch(u.login, { routing: e.target.checked })} /></td>
            <td><button className="btn sm ghost" onClick={() => patch(u.login, { active: !u.active })}>{u.active ? 'Отключить' : 'Включить'}</button></td>
            <td>{u.created_by !== 'env' && <button className="btn sm ghost" onClick={() => reinvite(u)}>Новая ссылка</button>}</td>
          </tr>))}
        </tbody>
      </table>
      <div className="panel"><b>Журнал команды</b>
        {log.slice(0, 25).map(l => <div key={l.id} className="kv small"><span>{dt(l.created_at)} · {l.actor_name || l.actor} {ACT[l.action] || l.action}{l.target ? ' → ' + (l.target_name || l.target) : ''}{l.esc_id ? ` · обращение #${l.esc_id}` : ''}</span></div>)}
        {!log.length && <div className="muted small">Пока пусто</div>}
      </div>
    </div>
  )
}
