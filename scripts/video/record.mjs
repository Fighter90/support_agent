// Запись демонстрационного видео одним дублем: слева чат клиента, справа кабинет менеджера (methodagent.ru).
// Пишет out/raw.webm и out/timeline.json (реальные отрезки, ожидание модели и моменты озвучки).
// Перед запуском: node login.mjs (вход в кабинет делает человек), python make_audio.py (озвучка и длительности).
import { chromium } from 'playwright'
import fs from 'node:fs'

const BASE = process.env.BASE || 'https://methodagent.ru'
const OUT = new URL('./out/', import.meta.url).pathname
fs.mkdirSync(OUT, { recursive: true })
const DUR = JSON.parse(fs.readFileSync(OUT + 'durations.json', 'utf8'))  // id → секунды озвучки
const TEXT = Object.fromEntries(JSON.parse(fs.readFileSync(new URL('./narration.json', import.meta.url), 'utf8')).map(i => [i.id, i.text]))
function parts(text) {  // субтитры: предложения, длинные — по запятой
  const out = []
  for (let s of text.split(/(?<=[.!?])\s+/)) {
    while (s.length > 84) { let c = s.lastIndexOf(',', 84); if (c < 30) c = s.lastIndexOf(' ', 84); out.push(s.slice(0, c + 1).trim()); s = s.slice(c + 1).trim() }
    out.push(s)
  }
  return out
}

const W = 1920, H = 1080
const ctx = await chromium.launchPersistentContext(new URL('./profile', import.meta.url).pathname, {
  channel: 'chrome', headless: true, viewport: { width: W, height: H }, screen: { width: W, height: H }, deviceScaleFactor: 1,
  args: [`--window-size=${W},${H}`],
  recordVideo: { dir: OUT, size: { width: W, height: H } },
})
const T0 = Date.now()
const now = () => Date.now() - T0
const timeline = { waits: [], narration: [], end: 0 }

const page = ctx.pages()[0] || await ctx.newPage()
const DEMO = `<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Демо</title><style>
html,body{margin:0;height:100%;background:#2b2b2e;font-family:Arial,sans-serif;overflow:hidden}
.wrap{display:flex;gap:6px;height:100%;padding:6px;box-sizing:border-box}
.pane{flex:1;display:flex;flex-direction:column;background:#fff;border-radius:8px;overflow:hidden}
.pane.l{flex:1.12}
.lbl{font-size:15px;padding:7px 12px;background:#A20D32;color:#fff;font-weight:700}
.lbl span{font-weight:400;opacity:.85;margin-left:8px}
.box{flex:1;position:relative;overflow:hidden}
#cap{position:fixed;left:50%;bottom:34px;transform:translateX(-50%);max-width:1500px;padding:10px 22px;border-radius:10px;
background:rgba(20,20,22,.82);color:#fff;font-size:30px;line-height:1.3;text-align:center;display:none;z-index:9}
iframe{border:0;position:absolute;top:0;left:0;width:125%;height:125%;transform:scale(.8);transform-origin:0 0}
</style></head><body><div class="wrap">
<div class="pane l"><div class="lbl">Клиент<span>methodagent.ru — витрина store.softline.ru с ИИ-ассистентом</span></div><div class="box"><iframe id="cl" src="/"></iframe></div></div>
<div class="pane"><div class="lbl">Менеджер<span>methodagent.ru/manager — кабинет в реальном времени</span></div><div class="box"><iframe id="mg" src="/manager"></iframe></div></div>
</div><div id="cap"></div></body></html>`
await page.route(`${BASE}/__demo`, r => r.fulfill({ contentType: 'text/html; charset=utf-8', body: DEMO }))
// чистый диалог клиента
await page.goto(`${BASE}/manager`)
await page.evaluate(() => { for (const k of Object.keys(localStorage)) if (!/token/i.test(k)) localStorage.removeItem(k) })
await page.goto(`${BASE}/__demo`)
const L = page.frameLocator('#cl'), R = page.frameLocator('#mg')
await L.locator('.chat-input textarea, .chat-input input').first().waitFor({ timeout: 30000 })
await R.getByText('Живой чат').first().waitFor({ timeout: 30000 })
await page.waitForTimeout(1500)

const cap = t => page.evaluate(x => { const c = document.getElementById('cap'); c.textContent = x; c.style.display = x ? 'block' : 'none' }, t)
const say = async (id, extra = 600) => {
  timeline.narration.push({ id, t: now() })
  const ps = parts(TEXT[id]), total = ps.reduce((a, p) => a + p.length, 0)
  for (const p of ps) { await cap(p); await page.waitForTimeout(DUR[id] * 1000 * p.length / total) }
  await cap(''); await page.waitForTimeout(extra)
}
const input = () => L.locator('.chat-input textarea, .chat-input input').first()
const answers = () => L.locator('.chat-body .msg.assistant .bubble:not(.typing)').count()
async function ask(text) {
  const n = await answers()
  await input().click()
  await input().pressSequentially(text, { delay: 22 })
  await page.waitForTimeout(300)
  await input().press('Enter')
  const w = { start: now() }
  await cap('ожидание ответа модели — в ролике ускорено')
  for (let i = 0; i < 240; i++) {  // до 120 с
    await page.waitForTimeout(500)
    if (await answers() > n && !(await L.locator('.bubble.typing').count())) break
  }
  w.end = now(); timeline.waits.push(w)
  await cap('')
  await page.waitForTimeout(500)
  await L.locator('.chat-body').evaluate(e => e.scrollTo({ top: e.scrollHeight, behavior: 'smooth' }))
}

// 1. Витрина, поиск по каталогу
const search = L.locator('.search input')
await search.click(); await search.pressSequentially('астра линукс', { delay: 70 })
await page.waitForTimeout(800)
await say('intro', 300)
await search.fill(''); await L.locator('h1, body').first().click({ position: { x: 5, y: 5 } }).catch(() => {})

// 2. Сценарий «не знаю, что нужно»
await ask('Не знаю, что нужно, но хотим защитить почту и файлы на 50 сотрудников')
await say('need1')
await ask('Коммерческая компания, Windows, своя почта на Exchange, лицензия на год. А есть российский аналог?')
await say('need2', 1200)

// 3. Счёт со скидкой и реквизиты
await ask('Берём Kaspersky Endpoint Security для бизнеса Стандартный на 50 ПК на 1 год, новая лицензия. Выставьте счёт со скидкой 5%')
await say('invoice')
const form = L.locator('.quote-form').last()
if (await form.count()) {
  await form.getByPlaceholder('Название организации').pressSequentially('ООО «Ромашка»', { delay: 30 })
  await form.getByPlaceholder('ИНН').pressSequentially('7707083893', { delay: 30 })
  await form.getByPlaceholder('КПП (если есть)').pressSequentially('773601001', { delay: 30 })
  await form.getByPlaceholder('E-mail для КП').pressSequentially('zakupki@romashka.ru', { delay: 20 })
  const w = { start: now() }
  await form.locator('button').click()
  await L.locator('.quote:not(.muted)').last().waitFor({ timeout: 60000 }).catch(() => {})
  w.end = now(); timeline.waits.push(w)
  await L.locator('.chat-body').evaluate(e => e.scrollTo({ top: e.scrollHeight, behavior: 'smooth' }))
}
await say('requisites')

// 4. Сценарий «знаю продукт»
await ask('Сколько стоит MDaemon Standard на 10 пользователей, продление на год?')
await say('known', 1200)

// 5. Позвать оператора → кабинет в реальном времени
await L.getByText('Позвать оператора').first().click()
await page.waitForTimeout(1200)
await say('operator', 200)
const item = R.locator('.list .item').filter({ hasText: /нужно, но хотим защитить/ }).first()
await item.click().catch(async () => { await R.locator('.list .item').first().click() })
await page.waitForTimeout(1200)
await R.getByRole('button', { name: /Взять диалог/ }).click().catch(() => {})
await page.waitForTimeout(800)
const ta = R.locator('.live-input textarea')
await ta.click()
await ta.pressSequentially('Здравствуйте! Я Сергей, менеджер Softline. Счёт вижу, сейчас пришлю ссылку на оплату.', { delay: 25 })
await ta.press('Enter')
await page.waitForTimeout(1500)
await say('takeover')

// 6. Ссылка на оплату ЮKassa (MCP) и оплата клиентом
await R.getByRole('button', { name: /КП и счета/ }).click()
await page.waitForTimeout(1500)
await R.getByRole('button', { name: /Ссылка на оплату/ }).first().click().catch(() => {})
await page.waitForTimeout(2500)
await say('paylink')
const href = await L.locator('.chat-body a[href*="/pay/"]').last().getAttribute('href').catch(() => null)
if (href) {
  await page.evaluate(u => { document.getElementById('cl').src = u }, href)
  await L.getByRole('button', { name: /Оплатить/ }).waitFor({ timeout: 20000 }).catch(() => {})
  await page.waitForTimeout(1500)
  await L.getByRole('button', { name: /Оплатить/ }).click().catch(() => {})
  await page.waitForTimeout(2000)
  await R.getByRole('button', { name: /Метрики/ }).click(); await page.waitForTimeout(400)
  await R.getByRole('button', { name: /КП и счета/ }).click(); await page.waitForTimeout(1500)
}
await say('paid')

// 7. Метрики и финал
await R.getByRole('button', { name: /Метрики/ }).click()
await page.waitForTimeout(1500)
await say('metrics')
await say('outro', 1200)

timeline.end = now()
fs.writeFileSync(OUT + 'timeline.json', JSON.stringify(timeline, null, 1))
const v = page.video()
await ctx.close()
fs.renameSync(await v.path(), OUT + 'raw.webm')
console.log('DONE', timeline.end)
