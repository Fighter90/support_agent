import { Fragment, type ReactNode } from 'react'

// Минимальная разметка ответов агента: **жирный**, ссылки, списки «•»/«-»/«1.», переносы строк. Без dangerouslySetInnerHTML.
function inline(text: string, key: string): ReactNode[] {
  const out: ReactNode[] = []
  const rx = /(\*\*[^*]+\*\*|\[[^\]]+\]\(https?:\/\/[^)\s]+\)|https?:\/\/[^\s)]+)/g
  let last = 0
  let m: RegExpExecArray | null
  let i = 0
  while ((m = rx.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index))
    const t = m[0]
    if (t.startsWith('**')) out.push(<b key={`${key}b${i}`}>{t.slice(2, -2)}</b>)
    else if (t.startsWith('[')) {
      const [, label, href] = t.match(/\[([^\]]+)\]\(([^)]+)\)/) || []
      out.push(<a key={`${key}a${i}`} href={href} target="_blank" rel="noreferrer">{label}</a>)
    } else out.push(<a key={`${key}u${i}`} href={t.replace(/[.,;]+$/, '')} target="_blank" rel="noreferrer">открыть на store.softline.ru</a>)
    last = m.index + t.length
    i++
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

export default function Markdown({ text }: { text: string }) {
  const lines = text.split('\n')
  const blocks: ReactNode[] = []
  let list: ReactNode[] = []
  const flush = () => { if (list.length) { blocks.push(<ul key={`l${blocks.length}`}>{list}</ul>); list = [] } }
  lines.forEach((ln, idx) => {
    const t = ln.trim()
    const li = t.match(/^(?:[•\-*]|\d+[.)])\s+(.*)$/)
    if (li) { list.push(<li key={idx}>{inline(li[1], `i${idx}`)}</li>); return }
    flush()
    if (!t) return
    if (t.startsWith('#')) { blocks.push(<p key={idx}><b>{t.replace(/^#+\s*/, '')}</b></p>); return }
    blocks.push(<p key={idx}>{inline(t, `p${idx}`).map((n, j) => <Fragment key={j}>{n}</Fragment>)}</p>)
  })
  flush()
  return <div className="md">{blocks}</div>
}
