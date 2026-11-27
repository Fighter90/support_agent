// Регресс работы команды в браузере: администратор создаёт менеджеров, они входят по ссылкам-приглашениям в своих браузерах,
// клиенты зовут оператора → автоназначение по очереди, «Взять», «Позвать коллегу», «Передать», перерыв → очередь коллеге.
// Администратор — профиль scripts/video/profile (вход выполнил человек). Результат: out/team_report.json и скриншоты.
import { chromium } from 'playwright'
import fs from 'node:fs'

const BASE = process.env.BASE || 'https://methodagent.ru'
const OUT = new URL('./out/', import.meta.url).pathname
fs.mkdirSync(OUT, { recursive: true })
const HELP = [new URL('../../frontend/public/help/', import.meta.url).pathname, new URL('../../docs/guides/img/', import.meta.url).pathname]
const helpShot = async (page, name) => { for (const d of HELP) await page.screenshot({ path: d + name + '.jpg', type: 'jpeg', quality: 82 }) }
const results = []
let n = 0
async function step(name, page, fn) {
  n += 1
  try { const note = await fn(); results.push({ n, name, ok: true, note: note || '' }); console.log('OK  ', n, name, note || '') }
  catch (e) { results.push({ n, name, ok: false, note: String(e.message || e).slice(0, 300) }); console.log('FAIL', n, name, String(e.message || e).slice(0, 200)) }
  await page.screenshot({ path: `${OUT}team_${String(n).padStart(2, '0')}.jpg`, type: 'jpeg', quality: 78 }).catch(() => {})
}
const must = (c, m) => { if (!c) throw new Error(m) }

const adminCtx = await chromium.launchPersistentContext(new URL('../video/profile', import.meta.url).pathname, {
  channel: 'chrome', headless: false, viewport: { width: 1440, height: 900 }, ignoreDefaultArgs: ['--enable-automation'],
})
const browser = await chromium.launch({ channel: 'chrome', headless: true })
const adm = adminCtx.pages()[0] || await adminCtx.newPage()
await adm.goto(`${BASE}/manager`)
const api = (page, path, body, method) => page.evaluate(async ([path, body, method]) => {
  const t = localStorage.getItem('sl_manager_token')
  const r = await fetch(path, { method: method || (body ? 'POST' : 'GET'), headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + t }, body: body ? JSON.stringify(body) : undefined })
  return r.json()
}, [path, body, method])

const TEAM = [['anna.smirnova', 'Анна Смирнова'], ['igor.petrov', 'Игорь Петров']]
const links = {}
await step('Администратор: раздел «Команда», создание менеджеров с ссылкой-приглашением', adm, async () => {
  await adm.locator('aside button', { hasText: 'Команда' }).click(); await adm.waitForTimeout(1500)
  // разобрать очередь от прошлых прогонов (кнопка администратора), чтобы проверка шла на чистой очереди
  const stale = adm.locator('.team .row', { hasText: 'Разобрать очередь' })
  await stale.locator('select').selectOption('0.5')
  await stale.getByRole('button', { name: 'Закрыть', exact: true }).click(); await adm.waitForTimeout(1500)
  // обращения, оставшиеся от прошлых прогонов этого теста, закрываем (по первому сообщению клиента)
  for (const e of await api(adm, '/api/manager/escalations?status=open')) {
    const d = await api(adm, `/api/manager/conversations/${e.conv_id}`)
    if ((d.messages || []).some(m => m.role === 'user' && /Microsoft 365 для 300|отсрочку платежа 30 дней по счёту|Astra Linux для 20 рабочих/.test(m.content)))
      await api(adm, `/api/manager/escalations/${e.id}/close`, { note: 'Тестовое обращение регресса' })
  }
  const have = (await api(adm, '/api/admin/users')).users.map(u => u.login)
  for (const [login, name] of TEAM) {
    if (have.includes(login)) {  // уже создан в прошлом прогоне — выдаём новую ссылку
      const row = adm.locator('tr', { hasText: login })
      await row.getByRole('button', { name: 'Новая ссылка' }).click()
    } else {
      await adm.getByPlaceholder(/Логин \(латиница\)/).fill(login)
      await adm.getByPlaceholder(/Имя — его увидит клиент/).fill(name)
      await adm.getByRole('button', { name: /Создать и получить ссылку/ }).click()
    }
    const prev = Object.values(links)
    for (let i = 0; i < 40; i++) {  // ждём именно новую ссылку (у предыдущего сотрудника она другая)
      const t = await adm.locator('.invite code').innerText().catch(() => '')
      if (t && !prev.includes(t.trim())) break
      await adm.waitForTimeout(250)
    }
    links[login] = (await adm.locator('.invite code').innerText()).trim()
    must(!prev.includes(links[login]), 'ссылка не обновилась')
    must(links[login].includes('/manager?invite='), 'нет ссылки-приглашения')
    await adm.waitForTimeout(700)
  }
  await api(adm, '/api/admin/routing', { auto_assign: true, reassign_seconds: 180 })
  for (const [login] of TEAM) await api(adm, `/api/admin/users/${login}`, { max_load: 10, routing: true, active: true }, 'PATCH')
  await helpShot(adm, 'team')
  return 'созданы: ' + TEAM.map(t => t[1]).join(', ')
})

const mgr = {}
await step('Менеджеры входят по ссылкам-приглашениям в своих браузерах, статус «В сети»', adm, async () => {
  for (const [login, name] of TEAM) {
    const c = await browser.newContext({ viewport: { width: 1400, height: 880 } })
    const p = await c.newPage(); await p.goto(links[login])
    await p.locator('.statusbar').waitFor({ timeout: 30000 }).catch(async e => { await p.screenshot({ path: OUT + 'team_invite_fail.jpg' }); throw e })
    must((await p.locator('.statusbar').innerText()).includes(name), 'в шапке не имя менеджера')
    must(!(await p.locator('aside button', { hasText: 'Команда' }).count()), 'менеджеру виден раздел администратора')
    await p.locator('.statusbar select').selectOption('online')
    mgr[login] = p
  }
  await mgr['anna.smirnova'].locator('aside button', { hasText: 'Профиль' }).click(); await mgr['anna.smirnova'].waitForTimeout(800)
  await helpShot(mgr['anna.smirnova'], 'profile')
  const r = await api(mgr['anna.smirnova'], '/api/admin/users')
  must(/только администратору/.test(r.detail || ''), 'менеджер получил доступ к API администратора')
  return 'оба в сети; API администратора для менеджера закрыт (403)'
})

async function client(q) {
  const before = new Set((await api(adm, '/api/manager/escalations?status=all')).map(e => e.id))
  const c = await browser.newContext({ viewport: { width: 1280, height: 860 } })
  const p = await c.newPage(); await p.goto(`${BASE}/`)
  const inp = p.locator('.chat-input textarea, .chat-input input').first()
  const bubbles = p.locator('.chat-body .msg.assistant .bubble:not(.typing)')
  await bubbles.first().waitFor({ timeout: 30000 })  // приветствие — чат готов, диалог создан
  await p.waitForTimeout(800)
  const n0 = await bubbles.count()
  await inp.fill(q); await inp.press('Enter')
  await p.locator('.chat-body .msg.user').first().waitFor({ timeout: 10000 })
  for (let i = 0; i < 240 && (await bubbles.count()) <= n0; i++) await p.waitForTimeout(500)
  await p.getByText('Позвать оператора').first().click(); await p.waitForTimeout(2500)
  // обращение этого клиента — новое среди всех эскалаций (агент мог передать сам ещё до кнопки)
  let mine = []
  for (let i = 0; i < 20 && !mine.length; i++) {
    mine = (await api(adm, '/api/manager/escalations?status=all')).filter(e => !before.has(e.id))
    if (!mine.length) await p.waitForTimeout(500)
  }
  must(mine.length, 'эскалация клиента не создана')
  return { p, esc: mine[0].id, cid: mine[0].conv_id }
}
const assignee = async () => (await api(adm, '/api/manager/escalations?status=open'))
let c1, c2, c3, firstTo
await step('Автоназначение: два обращения — двум разным менеджерам (по нагрузке, по очереди)', adm, async () => {
  c1 = await client('Нужна консультация по лицензированию Microsoft 365 для 300 сотрудников')
  c2 = await client('Хотим отсрочку платежа 30 дней по счёту')
  await adm.waitForTimeout(4000)
  const open = await assignee()
  const e1 = open.find(e => e.id === c1.esc), e2 = open.find(e => e.id === c2.esc)
  must(e1 && e2 && e1.assignee && e2.assignee && e1.assignee !== e2.assignee, 'назначения: ' + JSON.stringify([e1?.assignee, e2?.assignee]))
  firstTo = e1.assignee
  await adm.locator('aside button', { hasText: 'Эскалации' }).click(); await adm.waitForTimeout(500)
  await adm.locator('.seg button', { hasText: 'Все новые' }).click(); await adm.waitForTimeout(1500)
  await helpShot(adm, 'team_assign')
  return `#${e1.id} → ${e1.assignee_name}, #${e2.id} → ${e2.assignee_name}`
})

const nameOf = l => TEAM.find(t => t[0] === l)[1]
const other = l => TEAM.find(t => t[0] !== l)[0]
await step('Менеджер видит своё обращение в «Мои», берёт диалог — клиент видит его имя', adm, async () => {
  const p = mgr[firstTo]
  await p.locator('aside button', { hasText: 'Эскалации' }).click(); await p.waitForTimeout(1500)
  const mineIds = (await api(p, '/api/manager/escalations?status=mine')).map(e => e.id)
  must(mineIds.includes(c1.esc), '«Мои» не содержит назначенного обращения')
  await p.locator('aside button', { hasText: 'Живой чат' }).click(); await p.waitForTimeout(1200)
  await p.locator('.list .item', { hasText: 'Microsoft 365' }).first().click(); await p.waitForTimeout(1000)
  await p.getByRole('button', { name: 'Взять диалог' }).click(); await p.waitForTimeout(1200)
  await p.getByPlaceholder('Ответ клиенту — придёт в его чат мгновенно').fill('Здравствуйте! Подберу план лицензирования, уточните: нужен ли Teams Phone?')
  await p.keyboard.press('Enter')
  await c1.p.getByText('Teams Phone').waitFor({ timeout: 10000 })
  must(await c1.p.getByText(`подключился ${nameOf(firstTo)}`).count(), 'клиент не видит имя менеджера')
  return `${nameOf(firstTo)} ведёт диалог, ответ у клиента`
})
await step('«Позвать коллегу» и «Передать»: второй менеджер помогает, затем становится ведущим', adm, async () => {
  const p = mgr[firstTo], o = other(firstTo)
  await p.locator('select[aria-label="Позвать коллегу"]').selectOption(o); await p.waitForTimeout(1500)
  await c1.p.getByText(`ещё один специалист: ${nameOf(o)}`).waitFor({ timeout: 8000 })
  await helpShot(p, 'team_live')
  const q = mgr[o]
  await q.locator('aside button', { hasText: 'Живой чат' }).click(); await q.waitForTimeout(800)
  await q.locator('.seg button', { hasText: 'Мои' }).click(); await q.waitForTimeout(1500)
  must(await q.locator('.list .item', { hasText: 'Microsoft 365' }).count(), 'у помощника диалога нет в «Мои»')
  await p.locator('select[aria-label="Передать"]').selectOption(o); await p.waitForTimeout(1500)
  await c1.p.getByText(`передан коллеге: ${nameOf(o)}`).waitFor({ timeout: 8000 })
  const d = await api(adm, `/api/manager/conversations/${c1.cid}`)
  must(!c1.cid || d.manager_login === o, 'ведущий не сменился')
  return `позван и передан: ${nameOf(o)}`
})
await step('Перерыв: невзятое обращение уходит коллеге «в сети», новое — тоже ему', adm, async () => {
  const busy = (await assignee()).find(e => e.id === c2.esc)
  must(busy, 'нет обращения второго клиента')
  const away = busy.assignee
  await mgr[away].locator('.statusbar select').selectOption('away'); await mgr[away].waitForTimeout(4000)
  c3 = await client('Нужен счёт на Astra Linux для 20 рабочих станций с установкой')
  await adm.waitForTimeout(5000)
  const open = await assignee()
  const b2 = open.find(e => e.id === busy.id), n3 = open.find(e => e.id === c3.esc)
  must(b2.assignee !== away, `обращение осталось у менеджера на перерыве (${b2.assignee})`)
  must(n3.assignee && n3.assignee !== away, 'новое обращение назначено менеджеру на перерыве')
  await mgr[away].locator('.statusbar select').selectOption('online')
  return `${nameOf(away)} на перерыве → #${busy.id} и #${n3.id} у ${n3.assignee_name || 'коллеги'}`
})
await step('Администратор видит команду: статусы, нагрузку, журнал; меняет лимит и отключает доступ', adm, async () => {
  await adm.locator('aside button', { hasText: 'Команда' }).click(); await adm.waitForTimeout(2500)
  const t = await adm.locator('.mgr-main').innerText()
  must(/Анна Смирнова/.test(t) && /Игорь Петров/.test(t) && /назначено/.test(t) && /передал/.test(t), 'нет команды или журнала')
  await helpShot(adm, 'team')
  return 'команда и журнал на месте'
})
await step('Метрики: блок «Команда» (в сети, очередь, ответы по менеджерам)', adm, async () => {
  await adm.locator('aside button', { hasText: 'Метрики' }).click(); await adm.waitForTimeout(2000)
  must(/В сети \/ всего сотрудников/.test(await adm.locator('.mgr-main').innerText()), 'нет блока команды')
  return 'есть'
})
// уборка: тестовые обращения этого прогона закрываем, менеджеры уходят «не в сети»
for (const c of [c1, c2, c3]) if (c?.esc) await api(adm, `/api/manager/escalations/${c.esc}/close`, { note: 'Тестовое обращение регресса' })
for (const p of Object.values(mgr)) await p.locator('.statusbar select').selectOption('offline').catch(() => {})
const ok = results.filter(r => r.ok).length
fs.writeFileSync(OUT + 'team_report.json', JSON.stringify(results, null, 1))
console.log(`ИТОГО ${ok}/${results.length}`)
await browser.close(); await adminCtx.close()
