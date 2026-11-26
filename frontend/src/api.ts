export type Product = { group_id: number; product: string; vendor: string; url: string; price_from_rub: number | null; domestic: boolean; registry: string }
export type Quote = { quote_id: string; number: string; total_rub: number; status: string; pdf?: string; kind?: string; discount?: number }
export type Meta = { products?: Product[]; quote?: Quote; escalation?: { id: number; reason: string }; kb?: { title: string; url: string; source: string }[]; [k: string]: unknown }
export type Message = { id: number; role: 'user' | 'assistant' | 'manager' | 'tool' | 'system'; content: string; meta: Meta; created_at?: number }

const TOKEN_KEY = 'sl_manager_token'

export function token(): string {
  try { return localStorage.getItem(TOKEN_KEY) || '' } catch { return '' }
}
export function setToken(t: string) {
  try { if (t) localStorage.setItem(TOKEN_KEY, t); else localStorage.removeItem(TOKEN_KEY) } catch { /* приватный режим */ }
}

export async function api<T = any>(path: string, opts: { method?: string; body?: unknown; auth?: boolean } = {}): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (opts.auth) headers.Authorization = `Bearer ${token()}`
  const r = await fetch(path, { method: opts.method || (opts.body ? 'POST' : 'GET'), headers, body: opts.body ? JSON.stringify(opts.body) : undefined })
  if (r.status === 401 && opts.auth) { setToken(''); window.dispatchEvent(new Event('sl-logout')) }
  if (!r.ok) {
    let msg = `Ошибка ${r.status}`
    try { const d = await r.json(); msg = typeof d.detail === 'string' ? d.detail : msg } catch { /* не JSON */ }
    throw new Error(msg)
  }
  return r.json()
}

export const rub = (x: number | null | undefined) =>
  x == null ? 'цена по запросу' : x.toLocaleString('ru-RU', { minimumFractionDigits: 0, maximumFractionDigits: 2 }) + ' ₽'

export const dt = (t?: number) => (t ? new Date(t * 1000).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : '')
