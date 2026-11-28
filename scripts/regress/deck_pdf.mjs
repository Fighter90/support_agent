// Презентация docs/presentation/deck.html → presentation.pdf (Chrome, размер страницы из @page)
import { chromium } from 'playwright'
const deck = new URL('../../docs/presentation/deck.html', import.meta.url)
const out = new URL('../../docs/presentation/presentation.pdf', import.meta.url).pathname
const b = await chromium.launch({ channel: 'chrome' })
const p = await b.newPage()
await p.goto(deck.href, { waitUntil: 'networkidle' })
await p.pdf({ path: out, preferCSSPageSize: true, printBackground: true })
await b.close()
console.log('Сохранено:', out)
