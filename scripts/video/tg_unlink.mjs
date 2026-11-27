// Отключает Telegram-чат от кабинета: node tg_unlink.mjs <chat_id>
import { chromium } from 'playwright'
const BASE = process.env.BASE || 'https://methodagent.ru'
const id = process.argv[2]
const ctx = await chromium.launchPersistentContext(new URL('./profile', import.meta.url).pathname, {
  channel: 'chrome', headless: false, viewport: { width: 1200, height: 800 }, ignoreDefaultArgs: ['--enable-automation'],
})
const p = ctx.pages()[0] || await ctx.newPage()
await p.goto(`${BASE}/manager`)
const out = await p.evaluate(async id => {
  const t = Object.entries(localStorage).find(([k]) => /token/i.test(k))?.[1] || ''
  const h = { Authorization: 'Bearer ' + t }
  const d = await fetch('/api/manager/telegram/chats/' + id, { method: 'DELETE', headers: h })
  return d.status + ' ' + await (await fetch('/api/manager/telegram', { headers: h })).text()
}, id)
console.log(out)
await ctx.close()
