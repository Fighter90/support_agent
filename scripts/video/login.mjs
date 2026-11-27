// Открывает окно Chrome с отдельным профилем записи видео на странице входа в кабинет.
// Пароль вводит человек; скрипт ждёт появления токена в localStorage и закрывает окно.
import { chromium } from 'playwright'

const BASE = process.env.BASE || 'https://methodagent.ru'
const ctx = await chromium.launchPersistentContext(new URL('./profile', import.meta.url).pathname, {
  channel: 'chrome', headless: false, viewport: { width: 1100, height: 800 },
})
const page = ctx.pages()[0] || await ctx.newPage()
await page.goto(`${BASE}/manager`)
console.log('Ожидаю вход в кабинет…')
for (let i = 0; i < 1800; i++) {
  const ok = await page.evaluate(() => Object.keys(localStorage).some(k => /token/i.test(k) && localStorage.getItem(k))).catch(() => false)
  if (ok) { console.log('LOGGED_IN'); break }
  await page.waitForTimeout(1000)
}
await ctx.close()
