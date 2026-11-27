// Полный регресс в браузере (Playwright + Chrome) на стенде: клиент и кабинет менеджера в одной сессии.
// Вход в кабинет — профиль scripts/video/profile (вход выполнил человек, node scripts/video/login.mjs).
// Результат: scripts/regress/out/report.md, report.json и скриншоты каждого шага.
import { chromium } from 'playwright'
import fs from 'node:fs'

const BASE = process.env.BASE || 'https://methodagent.ru'
const OUT = new URL('./out/', import.meta.url).pathname
fs.rmSync(OUT, { recursive: true, force: true }); fs.mkdirSync(OUT, { recursive: true })
const ctx = await chromium.launchPersistentContext(new URL('../video/profile', import.meta.url).pathname, {
  channel: 'chrome', headless: false, viewport: { width: 1440, height: 900 }, ignoreDefaultArgs: ['--enable-automation'],
})
const results = []
let n = 0
async function step(name, page, fn) {
  const t0 = Date.now(); n += 1
  const shot = `${String(n).padStart(2, '0')}.jpg`
  try {
    const note = await fn()
    await page.screenshot({ path: OUT + shot, type: 'jpeg', quality: 80 })
    results.push({ n, name, ok: true, ms: Date.now() - t0, note: note || '', shot })
    console.log('OK  ', n, name, note || '')
  } catch (e) {
    await page.screenshot({ path: OUT + shot, type: 'jpeg', quality: 80 }).catch(() => {})
    results.push({ n, name, ok: false, ms: Date.now() - t0, note: String(e.message || e).slice(0, 300), shot })
    console.log('FAIL', n, name, String(e.message || e).slice(0, 200))
  }
}
const must = (cond, msg) => { if (!cond) throw new Error(msg) }

const cl = ctx.pages()[0] || await ctx.newPage()
await cl.goto(`${BASE}/`)
await cl.evaluate(() => { for (const k of Object.keys(localStorage)) if (!/token/i.test(k)) localStorage.removeItem(k) })
await cl.reload()
const input = () => cl.locator('.chat-input textarea, .chat-input input').first()
const answers = () => cl.locator('.chat-body .msg.assistant .bubble:not(.typing)').count()
async function ask(text) {
  const before = await answers()
  await input().fill(text); await input().press('Enter')
  for (let i = 0; i < 240; i++) {
    await cl.waitForTimeout(500)
    if (await answers() > before && !(await cl.locator('.bubble.typing').count())) break
  }
  must(await answers() > before, 'нет ответа ассистента за 120 с')
  const last = (await cl.locator('.chat-body .msg.assistant .bubble').last().innerText()).trim()
  must(last.length > 20, 'пустой ответ')
  must(!/не получается подобрать ответ/i.test(last), 'заглушка вместо ответа')
  return last
}

// ---------- Клиент
await step('Витрина: каталог, пометка «отвечает робот», кнопка оператора', cl, async () => {
  await input().waitFor({ timeout: 20000 })
  const body = await cl.locator('body').innerText()
  must(/135|136/.test(body), 'нет числа позиций каталога')
  must(/виртуальный помощник/i.test(body), 'нет пометки, что отвечает робот')
  must(await cl.getByText('Позвать оператора').count() > 0, 'нет кнопки «Позвать оператора»')
  return 'каталог и пометка на месте'
})
await step('Поиск по каталогу на витрине', cl, async () => {
  const s = cl.locator('.search input'); await s.fill('касперский endpoint'); await cl.waitForTimeout(1500)
  const k = await cl.locator('.suggest .sg').count()
  must(k >= 2, 'нет подсказок')
  const txt = await cl.locator('.suggest').innerText(); await s.fill('')
  must(/Kaspersky/i.test(txt), 'в подсказках нет Kaspersky')
  return `${k - 1} подсказок`
})
await step('Вопрос по базе знаний (НДС)', cl, async () => {
  const a = await ask('Цены на сайте указаны с НДС?'); must(/НДС/i.test(a), 'ответ не про НДС'); return a.slice(0, 90)
})
await step('Вопрос после покупки (ключ)', cl, async () => {
  const a = await ask('Вчера оплатили счёт, когда придёт ключ?'); must(/ключ|дистрибутив|кабинет/i.test(a), 'ответ не про ключ'); return a.slice(0, 90)
})
await step('Сценарий «знаю продукт»: MDaemon, сверка версий', cl, async () => {
  const a = await ask('Нужен MDaemon Standard на 10 пользователей, продление на год')
  must(/MDaemon/i.test(a), 'нет MDaemon в ответе'); return a.slice(0, 90)
})
await step('Сценарий «не знаю, что нужно»: уточняющие вопросы', cl, async () => {
  const a = await ask('Не знаю, что нужно, но хотим защитить почту и файлы на 50 сотрудников')
  return (a.includes('?') ? 'задан уточняющий вопрос: ' : 'сразу варианты: ') + a.slice(0, 70)
})
await step('Ответ на уточнение и российский аналог', cl, async () => {
  const a = await ask('Коммерческая компания, Windows, своя почта на Exchange, на год. А есть российский аналог?')
  must(/₽/.test(await cl.locator('.chat-body').innerText()), 'нет цен в ответе')
  return a.slice(0, 90)
})
await step('Счёт со скидкой 5% и форма реквизитов, PDF', cl, async () => {
  await ask('Берём Kaspersky Endpoint Security для бизнеса Стандартный на 50 ПК на 1 год, новая лицензия. Выставьте счёт со скидкой 5%')
  const f = cl.locator('.quote-form').last(); await f.waitFor({ timeout: 20000 })
  await f.getByPlaceholder('Название организации').fill('ООО «Ромашка»')
  await f.getByPlaceholder('ИНН').fill('7707083893'); await f.getByPlaceholder('КПП (если есть)').fill('773601001')
  await f.getByPlaceholder('E-mail для КП').fill('zakupki@romashka.ru'); await f.locator('button').click()
  const card = cl.locator('.quote:not(.muted)').last(); await card.waitFor({ timeout: 60000 })
  const txt = await card.innerText(); must(/Счёт|КП/.test(txt), 'нет карточки документа')
  const href = await card.locator('a', { hasText: 'PDF' }).getAttribute('href')
  const r = await ctx.request.get(new URL(href, BASE).toString())
  must(r.ok() && (r.headers()['content-type'] || '').includes('pdf'), 'PDF не отдаётся')
  return txt.replace(/\s+/g, ' ').slice(0, 80) + ` · PDF ${(await r.body()).length} байт`
})
await step('Оценка ответа 👍 и 👎', cl, async () => {
  const rates = cl.locator('.rate'); must(await rates.count() > 1, 'нет кнопок оценки')
  await rates.nth(0).locator('button').first().click(); await rates.nth(1).locator('button').nth(1).click()
  await cl.waitForTimeout(800); return 'оценки отправлены'
})
await step('Опрос не мешает, пока идёт диалог', cl, async () => {
  must(await cl.locator('.survey').count() === 0, 'опрос показан посреди диалога'); return 'опроса нет'
})
await step('Онлайн-оплата счёта (ЮKassa, демо)', cl, async () => {
  const [pay] = await Promise.all([ctx.waitForEvent('page'), cl.locator('.quote:not(.muted)').last().getByText('Оплатить онлайн').click()])
  await pay.waitForLoadState(); await pay.getByRole('button', { name: /Оплатить/ }).click()
  await pay.getByText(/Оплата прошла/).waitFor({ timeout: 15000 }); await pay.close()
  await cl.getByText(/Оплата .* получена/).last().waitFor({ timeout: 15000 })
  return 'оплачено, клиенту пришло подтверждение'
})

// ---------- Менеджер
const mg = await ctx.newPage()
await mg.goto(`${BASE}/manager`)
await step('Кабинет: вход сохранён, все разделы', mg, async () => {
  await mg.getByText('Живой чат').first().waitFor({ timeout: 15000 })
  const menu = await mg.locator('aside').innerText()
  for (const t of ['Эскалации', 'Диалоги', 'КП и счета', 'Отзывы', 'База знаний', 'Метрики', 'Telegram', 'Справка']) must(menu.includes(t), 'нет раздела ' + t)
  return '9 разделов'
})
await step('Позвать оператора → диалог в кабинете в реальном времени', mg, async () => {
  await cl.getByText('Позвать оператора').first().click()
  const t0 = Date.now()
  await mg.locator('.list .item').filter({ hasText: /Ждёт менеджера/ }).first().waitFor({ timeout: 10000 })
  return `эскалация видна через ${Date.now() - t0} мс`
})
await step('Взять диалог и ответить клиенту', mg, async () => {
  await mg.locator('.list .item').first().click(); await mg.waitForTimeout(800)
  await mg.getByRole('button', { name: /Взять диалог/ }).click(); await mg.waitForTimeout(600)
  const ta = mg.locator('.live-input textarea'); await ta.fill('Здравствуйте! Менеджер Softline на связи, помогу с документами.')
  const t0 = Date.now(); await ta.press('Enter')
  await cl.getByText('Менеджер Softline на связи').waitFor({ timeout: 10000 })
  return `клиент получил ответ через ${Date.now() - t0} мс`
})
await step('Клиент пишет менеджеру → сообщение в кабинете', mg, async () => {
  await input().fill('Спасибо, жду закрывающие документы'); await input().press('Enter')
  await mg.getByText('жду закрывающие документы').last().waitFor({ timeout: 10000 }); return 'доставлено'
})
await step('Вернуть агенту', mg, async () => {
  await mg.getByRole('button', { name: /Вернуть агенту/ }).click()
  await cl.getByText(/передал диалог ИИ-ассистенту/).last().waitFor({ timeout: 10000 }); return 'клиент видит возврат'
})
await step('Эскалации: очередь с причиной и резюме', mg, async () => {
  await mg.getByRole('button', { name: /Эскалации/ }).click(); await mg.waitForTimeout(1500)
  return (await mg.locator('.mgr-main').innerText()).replace(/\s+/g, ' ').slice(0, 100)
})
await step('Диалоги: журнал инструментов и токенов', mg, async () => {
  await mg.getByRole('button', { name: /Диалоги/ }).click(); await mg.waitForTimeout(1200)
  await mg.locator('.list .item, tbody tr').first().click().catch(() => {}); await mg.waitForTimeout(1500)
  const txt = await mg.locator('.mgr-main').innerText(); must(/search_catalog|search_knowledge|ток/i.test(txt), 'нет журнала')
  return 'журнал на месте'
})
await step('КП и счета: статус оплаты, подтверждение', mg, async () => {
  await mg.getByRole('button', { name: /КП и счета/ }).click(); await mg.waitForTimeout(1500)
  const row = mg.locator('tbody tr').first(); const txt = await row.innerText()
  must(/Оплачен/.test(txt), 'статус оплаты не «Оплачено»')
  const b = row.getByRole('button', { name: 'Подтвердить' }); if (await b.count()) { await b.click(); await mg.waitForTimeout(800) }
  return txt.replace(/\s+/g, ' ').slice(0, 90)
})
await step('Отзывы', mg, async () => {
  await mg.getByRole('button', { name: /Отзывы/ }).click(); await mg.waitForTimeout(1500)
  return (await mg.locator('.mgr-main').innerText()).replace(/\s+/g, ' ').slice(0, 100)
})
await step('База знаний', mg, async () => {
  await mg.getByRole('button', { name: /База знаний/ }).click(); await mg.waitForTimeout(1500)
  must(/НДС|оплат/i.test(await mg.locator('.mgr-main').innerText()), 'статьи не показаны'); return 'статьи на месте'
})
await step('Метрики', mg, async () => {
  await mg.getByRole('button', { name: /Метрики/ }).click(); await mg.waitForTimeout(1500)
  const txt = await mg.locator('.mgr-main').innerText(); must(/CSAT/.test(txt) && /₽/.test(txt), 'нет CSAT/стоимости'); return 'CSAT и стоимость'
})
await step('Telegram: раздел подключения бота', mg, async () => {
  await mg.getByRole('button', { name: /Telegram/ }).click(); await mg.waitForTimeout(1500)
  const txt = await mg.locator('.mgr-main').innerText(); must(/SoftlineSupportBot|бот/i.test(txt), 'нет раздела бота')
  return /Подключить Telegram/.test(txt) ? 'бот включён' : 'бот не настроен (нет токена на сервере)'
})
await step('Telegram: бот включён, чат менеджера подключён', mg, async () => {
  const txt = await mg.locator('.mgr-main').innerText()
  must(/Подключить Telegram/.test(txt), 'бот не настроен'); must(/Отключить/.test(txt), 'нет подключённых чатов')
  return 'бот включён, подключённых чатов: ' + (await mg.getByRole('button', { name: 'Отключить' }).count())
})
await step('Справка: 13 разделов, скриншоты загружаются', mg, async () => {
  await mg.locator('aside button', { hasText: 'Справка' }).click(); await mg.waitForTimeout(2000)
  const secs = await mg.locator('.help-sec').count(); must(secs >= 13, 'разделов справки ' + secs)
  await mg.evaluate(async () => { for (const im of document.querySelectorAll('.help img')) { im.loading = 'eager'; im.scrollIntoView(); await new Promise(r => setTimeout(r, 150)) } })
  await mg.waitForTimeout(1500)
  const broken = await mg.evaluate(() => [...document.querySelectorAll('.help img')].filter(i => !i.complete || i.naturalWidth === 0).map(i => i.src))
  must(broken.length === 0, 'не загрузились: ' + broken.join(', '))
  const txt = await mg.locator('.help').innerText(); must(/Подключение личного Telegram/.test(txt) && /реплаем/.test(txt), 'нет инструкции по Telegram')
  await mg.evaluate(() => document.getElementById('help-telegram').scrollIntoView())
  return `${secs} разделов, ${await mg.locator('.help img').count()} скриншотов`
})
await step('Каталог: RAG по фиду store.softline.ru (API поиска)', cl, async () => {
  const h = await (await fetch(`${BASE}/api/health`)).json()
  must(h.catalog.offers > 130000, 'в каталоге мало позиций: ' + h.catalog.offers)
  const r = await (await fetch(`${BASE}/api/catalog/search?q=${encodeURIComponent('астра линукс')}`)).json()
  must(r.items.some(i => /Astra Linux/i.test(i.product)), 'не найден Astra Linux')
  return `${h.catalog.offers} позиций, с ценой ${h.catalog.available_priced}, загружено ${h.catalog.loaded_at}; «астра линукс» → ${r.items[0].product}`
})
await step('Опрос в конце диалога (после 45 с тишины)', cl, async () => {
  await cl.bringToFront(); await cl.waitForTimeout(52000)
  must(await cl.locator('.survey').count() > 0, 'опрос не появился')
  await cl.locator('.survey').getByRole('button', { name: 'Да' }).click(); await cl.locator('.survey').getByRole('button', { name: '5' }).click()
  await cl.locator('.survey').getByRole('button', { name: /Отправить/ }).click(); await cl.getByText(/Спасибо!/).last().waitFor({ timeout: 8000 })
  return 'опрос отправлен'
})
await step('Безопасность: /.env и API кабинета без токена', cl, async () => {
  const a = (await fetch(`${BASE}/.env`)).status, b = (await fetch(`${BASE}/api/manager/escalations`)).status
  must(a === 404 && (b === 401 || b === 403), `.env=${a}, manager api=${b}`); return `.env → ${a}, кабинет без токена → ${b}`
})

await ctx.close()
const ok = results.filter(r => r.ok).length
fs.writeFileSync(OUT + 'report.json', JSON.stringify({ base: BASE, at: new Date().toISOString(), passed: ok, total: results.length, results }, null, 1))
fs.writeFileSync(OUT + 'report.md', `# Регресс в браузере — ${new Date().toISOString().slice(0, 16)} UTC\n\nСтенд: ${BASE} · пройдено **${ok} из ${results.length}**\n\n| № | Проверка | Итог | Время | Комментарий |\n|---|---|---|---|---|\n` +
  results.map(r => `| ${r.n} | ${r.name} | ${r.ok ? '✅' : '❌'} | ${(r.ms / 1000).toFixed(1)} с | ${r.note.replace(/\|/g, '/').replace(/\n/g, ' ')} |`).join('\n') + '\n')
console.log(`ИТОГО ${ok}/${results.length}`)
