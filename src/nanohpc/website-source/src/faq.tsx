import { useState } from 'react'
import { faqEntries, type FaqEntry } from './documentation'
import type { SiteFaqEntry } from './site'

/** The administrator's entry from cluster.yml as a FAQ entry: each blank-line-separated part is a paragraph. */
function fromSite(entry: SiteFaqEntry): FaqEntry {
  return { question: entry.question, text: entry.answer.split(/\n\s*\n/).map((part) => part.trim()).filter(Boolean) }
}

/** The FAQ page: a search box over questions and answers, and one card per question, closed until clicked.
 * The generic questions come first, then the administrator's own. */
export function Faq({ own }: { own: SiteFaqEntry[] }) {
  const [search, setSearch] = useState('')
  const words = search.toLowerCase().split(/\s+/).filter(Boolean)
  // A question shows when every typed word is in its question, answer, or command.
  const shown = [...faqEntries, ...own.map(fromSite)].filter((entry) => {
    const text = [entry.question, ...entry.text, entry.command ?? ''].join(' ').toLowerCase()
    return words.every((word) => text.includes(word))
  })
  return <div className="user-guide faq">
    <input className="faq-search panel" type="search" placeholder="Search questions" aria-label="Search questions" value={search} onChange={(event) => setSearch(event.target.value)}/>
    {shown.map((entry, index) => <details key={`${index}-${entry.question}`} className="panel"><summary>{entry.question}</summary><div className="faq-answer">{entry.text.map((text, part) => <p key={part}>{text}</p>)}{entry.command && <pre><code>{entry.command}</code></pre>}</div></details>)}
    {shown.length === 0 && <p>No question matches “{search}”.</p>}
  </div>
}
