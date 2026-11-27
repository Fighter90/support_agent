// Демо-видео одним дублем (до 3 минут): слева чат клиента, справа кабинет менеджера (methodagent.ru), затем настоящий
// Telegram Web менеджера с ботом @SoftlineSupportBot. Монтажа нет: одна страница, одна запись; ускоряется только
// ожидание ответа модели (assemble.py). Субтитры, подписи и QR-коды — в кадре.
// Перед запуском: node login.mjs и node tg_login.mjs (входы выполняет человек), make_audio.py (озвучка).
import { chromium } from 'playwright'
import fs from 'node:fs'

const BASE = process.env.BASE || 'https://methodagent.ru'
const OUT = new URL('./out/', import.meta.url).pathname
fs.mkdirSync(OUT, { recursive: true })
const DUR = JSON.parse(fs.readFileSync(OUT + 'durations.json', 'utf8'))
const TEXT = Object.fromEntries(JSON.parse(fs.readFileSync(new URL('./narration.json', import.meta.url), 'utf8')).map(i => [i.id, i.sub || i.text]))
function parts(text) {
  const out = []
  for (let s of text.split(/(?<=[.!?])\s+/)) {
    while (s.length > 84) { let c = s.lastIndexOf(',', 84); if (c < 30) c = s.lastIndexOf(' ', 84); out.push(s.slice(0, c + 1).trim()); s = s.slice(c + 1).trim() }
    out.push(s)
  }
  return out
}

const W = 1920, H = 1080
const ctx = await chromium.launchPersistentContext(new URL('./profile', import.meta.url).pathname, {
  channel: 'chrome', headless: false, viewport: { width: W, height: H }, screen: { width: W, height: H }, deviceScaleFactor: 1,
  args: [`--window-size=${W},${H + 200}`], ignoreDefaultArgs: ['--enable-automation'],
  recordVideo: { dir: OUT, size: { width: W, height: H } },
})
const T0 = Date.now()
const now = () => Date.now() - T0
const timeline = { waits: [], narration: [], end: 0 }
const page = ctx.pages()[0] || await ctx.newPage()

const OVERLAY = `#cap{position:fixed;left:50%;bottom:14px;transform:translateX(-50%);max-width:1500px;padding:10px 22px;border-radius:10px;
background:rgba(20,20,22,.84);color:#fff;font-size:28px;line-height:1.3;text-align:center;display:none;z-index:2147483646;font-family:Arial,sans-serif}
#qr{position:fixed;left:18px;bottom:18px;display:flex;gap:10px;z-index:2147483645}
#qr figure{margin:0;background:#fff;border-radius:10px;padding:6px;box-shadow:0 4px 18px rgba(0,0,0,.25);text-align:center;font:600 12px Arial,sans-serif;color:#222}
#qr img{width:118px;display:block}`
const DEMO = `<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Демо</title><style>
html,body{margin:0;height:100%;background:#2b2b2e;font-family:Arial,sans-serif;overflow:hidden}
.wrap{display:flex;gap:6px;height:100%;padding:6px;box-sizing:border-box}
.pane{flex:1;display:flex;flex-direction:column;background:#fff;border-radius:8px;overflow:hidden}
.pane.l{flex:1.12}
.lbl{font-size:15px;padding:7px 12px;background:#A20D32;color:#fff;font-weight:700}
.lbl span{font-weight:400;opacity:.85;margin-left:8px}
.box{flex:1;position:relative;overflow:hidden}
${OVERLAY}
iframe{border:0;position:absolute;top:0;left:0;width:125%;height:125%;transform:scale(.8);transform-origin:0 0}
</style></head><body><div class="wrap">
<div class="pane l"><div class="lbl">Клиент<span>methodagent.ru — витрина store.softline.ru с ИИ-ассистентом</span></div><div class="box"><iframe id="cl" src="/"></iframe></div></div>
<div class="pane"><div class="lbl">Менеджер<span>methodagent.ru/manager — кабинет в реальном времени</span></div><div class="box"><iframe id="mg" src="/manager"></iframe></div></div>
</div><div id="qr"><figure><img src="/qr/qr_site.png">methodagent.ru</figure><figure><img src="/qr/qr_bot.png">@SoftlineSupportBot</figure></div><div id="cap"></div></body></html>`
await page.route(`${BASE}/__demo`, r => r.fulfill({ contentType: 'text/html; charset=utf-8', body: DEMO }))
await page.goto(`${BASE}/manager`)
await page.evaluate(() => { for (const k of Object.keys(localStorage)) if (!/token/i.test(k)) localStorage.removeItem(k) })
await page.goto(`${BASE}/__demo`)
let L = page.frameLocator('#cl'), R = page.frameLocator('#mg')
await L.locator('.chat-input textarea, .chat-input input').first().waitFor({ timeout: 30000 })
await R.getByText('Живой чат').first().waitFor({ timeout: 30000 })
await page.waitForTimeout(1200)

// подписи поверх любой страницы (в том числе Telegram Web)
const ensureOverlay = () => page.evaluate(([css]) => {
  if (!document.getElementById('cap')) {
    const st = document.createElement('style'); st.textContent = css; document.head.appendChild(st)
    const c = document.createElement('div'); c.id = 'cap'; document.body.appendChild(c)
  }
}, [OVERLAY])
const cap = async t => { await ensureOverlay(); await page.evaluate(x => { const c = document.getElementById('cap'); c.textContent = x; c.style.display = x ? 'block' : 'none' }, t) }
const say = async (id, extra = 500) => {
  timeline.narration.push({ id, t: now() })
  const ps = parts(TEXT[id]), total = ps.reduce((a, p) => a + p.length, 0)
  for (const p of ps) { await cap(p); await page.waitForTimeout(DUR[id] * 1000 * p.length / total) }
  await cap(''); await page.waitForTimeout(extra)
}
const input = () => L.locator('.chat-input textarea, .chat-input input').first()
const answers = () => L.locator('.chat-body .msg.assistant .bubble:not(.typing)').count()
async function ask(text, delay = 18) {
  const n = await answers()
  await input().click(); await input().pressSequentially(text, { delay }); await page.waitForTimeout(200); await input().press('Enter')
  const w = { start: now() }
  await cap('ожидание ответа модели — в ролике ускорено')
  for (let i = 0; i < 240; i++) {
    await page.waitForTimeout(500)
    if (await answers() > n && !(await L.locator('.bubble.typing').count())) break
  }
  w.end = now(); timeline.waits.push(w)
  await cap(''); await page.waitForTimeout(300)
  await L.locator('.chat-body').evaluate(e => e.scrollTo({ top: e.scrollHeight, behavior: 'smooth' }))
}

// 1. Вступление
await say('intro', 200)

// 2. Сценарий «не знаю, что нужно»
await ask('Не знаю, что нужно, но хотим защитить почту и файлы на 50 сотрудников')
await say('need1', 200)
await ask('Коммерческая компания, Windows, своя почта на Exchange, на год. А есть российский аналог?')
await say('need2', 600)

// 3. Акция из таблицы менеджера и счёт
await R.locator('aside button', { hasText: 'Скидки' }).click()
await ask('Сколько стоит Kaspersky Endpoint Security для бизнеса Стандартный на 50 ПК на 1 год, новая лицензия?')
await say('promo', 300)
await ask('Берём Kaspersky Endpoint Security для бизнеса Стандартный на 50 ПК на 1 год, новая лицензия. Выставьте счёт')
const form = L.locator('.quote-form').last()
if (await form.count()) {
  await form.getByPlaceholder('Название организации').pressSequentially('ООО «Ромашка»', { delay: 20 })
  await form.getByPlaceholder('ИНН').pressSequentially('7707083893', { delay: 20 })
  await form.getByPlaceholder('E-mail для КП').pressSequentially('zakupki@romashka.ru', { delay: 12 })
  await form.locator('button').click()
  await L.locator('.quote:not(.muted)').last().waitFor({ timeout: 60000 }).catch(() => {})
  await L.locator('.chat-body').evaluate(e => e.scrollTo({ top: e.scrollHeight, behavior: 'smooth' }))
}
await say('requisites', 300)

// 4. Сценарий «знаю продукт» и вопрос после покупки
await ask('Нужен MDaemon Standard на 10 пользователей, продление на год')
await say('known', 300)
await ask('Мы вчера оплатили счёт. Когда придёт ключ?')
await say('kb', 300)

// 5. Позвать оператора → кабинет и Telegram
await R.locator('aside button', { hasText: 'Живой чат' }).click()
await L.getByText('Позвать оператора').first().click()
await page.waitForTimeout(1500)
await say('operator', 200)

// 6. Настоящий Telegram менеджера: эскалация от бота, ответ реплаем
const sent = Date.now()
await page.goto('https://web.telegram.org/k/#@SoftlineSupportBot')
const esc = page.locator('.bubble', { hasText: 'Мы вчера оплатили счёт' }).filter({ hasText: 'Эскалация' }).last()
await esc.waitFor({ timeout: 45000 }).catch(() => {})
await page.waitForTimeout(800)
const tgSay = say('tg1', 200)
await page.waitForTimeout(3500)
await esc.click({ button: 'right' }).catch(() => {})
await page.waitForTimeout(500)
await page.locator('.btn-menu-item', { hasText: /Ответить|Reply/ }).first().click().catch(() => {})
await page.waitForTimeout(400)
const box = page.locator('.input-message-input[contenteditable="true"]').last()
await box.click().catch(() => {})
await page.keyboard.type('Здравствуйте! Я менеджер Softline. Скидку согласую, пришлю закрывающие документы сегодня.', { delay: 22 })
await tgSay
await page.keyboard.press('Enter')
await page.waitForTimeout(3000)
timeline.telegram_sec = Math.round((Date.now() - sent) / 1000)

// 7. Обратно: ответ из Telegram у клиента
await page.goto(`${BASE}/__demo`)
L = page.frameLocator('#cl'); R = page.frameLocator('#mg')
await L.getByText('пришлю закрывающие документы').last().waitFor({ timeout: 20000 }).catch(() => {})
await L.locator('.chat-body').evaluate(e => e.scrollTo({ top: e.scrollHeight })).catch(() => {})
await page.waitForTimeout(600)
await say('tg2', 300)

// 8. Финал: метрики регламента
await R.locator('aside button', { hasText: 'Метрики' }).click().catch(() => {})
await page.waitForTimeout(800)
await say('outro', 1500)

timeline.end = now()
fs.writeFileSync(OUT + 'timeline.json', JSON.stringify(timeline, null, 1))
const v = page.video()
await ctx.close()
fs.renameSync(await v.path(), OUT + 'raw.webm')
console.log('DONE', timeline.end, 'telegram', timeline.telegram_sec)
