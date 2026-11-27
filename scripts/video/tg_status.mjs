// Показывает, какие Telegram-чаты привязаны к кабинету (вкладка Telegram), и с каким аккаунтом открыт Telegram Web.
import { chromium } from 'playwright'
const BASE = process.env.BASE || 'https://methodagent.ru'
const ctx = await chromium.launchPersistentContext(new URL('./profile', import.meta.url).pathname, {
  channel: 'chrome', headless: false, viewport: { width: 1400, height: 900 }, ignoreDefaultArgs: ['--enable-automation'],
})
const p = ctx.pages()[0] || await ctx.newPage()
await p.goto(`${BASE}/manager`)
await p.locator('aside button', { hasText: 'Telegram' }).click()
await p.waitForTimeout(2500)
console.log('=== КАБИНЕТ ===\n' + (await p.locator('body').innerText()).slice(0, 1500))
const token = await p.evaluate(() => Object.entries(localStorage).find(([k]) => /token/i.test(k))?.[1] || '')
const r = await p.evaluate(async t => (await fetch('/api/manager/telegram', { headers: { Authorization: 'Bearer ' + t } })).text(), token)
console.log('=== API ===\n' + r.slice(0, 1500))
await p.goto('https://web.telegram.org/k/#@SoftlineSupportBot')
await p.waitForTimeout(6000)
console.log('=== TG WEB: последние сообщения бота ===\n' + (await p.locator('.bubbles').innerText().catch(() => '')).slice(-800))
await p.screenshot({ path: '/tmp/tg_status.png' })
await ctx.close()
