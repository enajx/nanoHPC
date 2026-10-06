import { useEffect, useState, type ReactNode } from 'react'
import { X } from 'lucide-react'
import { healthText, type Machine } from './machines'

type Topic = 'health' | 'speed' | 'updates'

/** Explain one machine measurement in a dialog that can be closed by keyboard or pointer. */
export function MachineLabel({ node, topic, stale, demo, className, label, children }: {
  node: Machine; topic: Topic; stale: boolean; demo: boolean; className: string; label: string; children: ReactNode
}) {
  const [open, setOpen] = useState(false)
  useEffect(() => {
    if (!open) return
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false) }
    document.addEventListener('keydown', close)
    return () => document.removeEventListener('keydown', close)
  }, [open])
  const title = `${node.name}: ${label}`
  const lines: string[] = []
  if (stale) lines.push('The monitoring data is out of date, so the current state is unknown.')
  else if (topic === 'health') {
    if (node.health === 'Healthy') lines.push('No health problems reported.')
    else if (node.health_details?.length) lines.push(...node.health_details)
    else lines.push(`Machine health: ${healthText(node.health ?? 'Unknown')}.`)
  } else if (topic === 'speed') {
    const speed = node.specs?.speeds
    if (demo) lines.push('Fictional link speed shown in the public demo.')
    else if (!speed) lines.push('No recent speed test result is available.')
    else {
      const when = node.specs?.speeds_measured_at == null ? 'recently' : new Date(node.specs.speeds_measured_at * 1000).toLocaleString()
      const mb = (value: number | null | undefined) => value == null ? 'Unknown' : `${Math.round(value)} MB/s`
      lines.push(`Speed test measured ${when}.`)
      if (node.role === 'Compute') {
        lines.push(`One large file: read ${mb(speed.home_large_read)}, write ${mb(speed.home_large_write)}.`)
        lines.push(`Many small files: read ${mb(speed.home_small_read)}, write ${mb(speed.home_small_write)}.`)
      }
      lines.push(`Internet download: ${mb(speed.internet_download)}.`)
    }
  } else {
    const count = node.specs?.pending_updates
    lines.push(count == null ? 'Waiting package updates are unknown.' : count === 0 ? 'No package updates are waiting.' : `${count} package update${count === 1 ? ' is' : 's are'} waiting to be installed.`)
  }
  return <>
    <button type="button" className={`pill pill-button ${className}`} aria-label={label} aria-haspopup="dialog" onClick={() => setOpen(true)}>{children}</button>
    {open && <div className="settings-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) setOpen(false) }}>
      <div className="settings-box machine-dialog" role="dialog" aria-modal="true" aria-label={title}>
        <button type="button" className="settings-close" aria-label="Close" onClick={() => setOpen(false)}><X size={18}/></button>
        <h2>{title}</h2>
        {lines.map(line => <p key={line}>{line}</p>)}
      </div>
    </div>}
  </>
}
