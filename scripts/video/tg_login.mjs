// Окно Chrome с профилем записи видео на web.telegram.org: вход (QR-код с телефона) выполняет человек.
// Скрипт ждёт появления списка чатов и закрывает окно; сессия Telegram Web остаётся в профиле записи.
import { chromium } from 'playwright'

const ctx = await chromium.launchPersistentContext(new URL('./profile', import.meta.url).pathname, {
  channel: 'chrome', headless: false, viewport: { width: 1200, height: 860 }, ignoreDefaultArgs: ['--enable-automation'],
})
const page = ctx.pages()[0] || await ctx.newPage()
await page.goto('https://web.telegram.org/k/#@SoftlineSupportBot')
console.log('Ожидаю вход в Telegram Web…')
for (let i = 0; i < 1800; i++) {
  const ok = await page.locator('.chatlist, .chat-list, #column-left .chatlist-container').count().catch(() => 0)
  if (ok) { console.log('TG_LOGGED_IN'); break }
  await page.waitForTimeout(1000)
}
await page.waitForTimeout(3000)
await ctx.close()
