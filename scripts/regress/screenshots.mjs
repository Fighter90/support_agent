// Скриншоты разделов кабинета и чата для «Справки» и руководств: frontend/public/help/*.jpg и docs/guides/img/*.jpg.
import { chromium } from 'playwright'
import fs from 'node:fs'

const BASE = process.env.BASE || 'https://methodagent.ru'
const ROOT = new URL('../../', import.meta.url).pathname
const OUTS = [ROOT + 'frontend/public/help/', ROOT + 'docs/guides/img/']
OUTS.forEach(d => fs.mkdirSync(d, { recursive: true }))
const ctx = await chromium.launchPersistentContext(new URL('../video/profile', import.meta.url).pathname, {
  channel: 'chrome', headless: false, viewport: { width: 1440, height: 860 }, ignoreDefaultArgs: ['--enable-automation'],
})
const shot = async (page, name) => { for (const d of OUTS) await page.screenshot({ path: d + name + '.jpg', type: 'jpeg', quality: 82 }) }

const mg = ctx.pages()[0] || await ctx.newPage()
await mg.goto(`${BASE}/manager`)
await mg.getByText('Живой чат').first().waitFor({ timeout: 15000 })
await mg.waitForTimeout(1500)
const tabs = [['live', 'Живой чат'], ['escalations', 'Эскалации'], ['dialogs', 'Диалоги'], ['quotes', 'КП и счета'], ['discounts', 'Скидки'], ['feedback', 'Отзывы'],
  ['kb', 'База знаний'], ['metrics', 'Метрики'], ['telegram', 'Telegram']]
for (const [key, label] of tabs) {
  await mg.locator('aside button', { hasText: label }).first().click()
  await mg.waitForTimeout(1500)
  if (key === 'live' || key === 'dialogs') { await mg.locator('.list .item').first().click().catch(() => {}); await mg.waitForTimeout(1500) }
  if (key === 'escalations') { await mg.locator('.list .item, .esc-item, tbody tr').first().click().catch(() => {}); await mg.waitForTimeout(1000) }
  await shot(mg, key)
}
// вход в кабинет — в отдельном окне без сохранённого входа
const anon = await chromium.launch({ channel: 'chrome', headless: false })
const ap = await anon.newPage({ viewport: { width: 1440, height: 860 } })
await ap.goto(`${BASE}/manager`); await ap.waitForTimeout(1500); await shot(ap, 'login'); await anon.close()
// чат клиента: кнопка «Позвать оператора»
const cl = await ctx.newPage()
await cl.goto(`${BASE}/`); await cl.waitForTimeout(2500); await shot(cl, 'client')
await ctx.close()
console.log('OK', fs.readdirSync(OUTS[0]))
