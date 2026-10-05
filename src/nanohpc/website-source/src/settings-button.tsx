import { useEffect, useState } from 'react'
import { Settings, X } from 'lucide-react'

/** Open a read-only Settings notice from the bottom of the sidebar. */
export function SettingsButton() {
  const [open, setOpen] = useState(false)
  useEffect(() => {
    if (!open) return
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false) }
    document.addEventListener('keydown', close)
    return () => document.removeEventListener('keydown', close)
  }, [open])
  return <div className="sidebar-bottom">
    <button type="button" className="settings-button" aria-expanded={open} onClick={() => setOpen(true)}><Settings size={19}/><span>Settings</span></button>
    {open && <div className="settings-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setOpen(false) }}>
      <div className="settings-box" role="dialog" aria-modal="true" aria-labelledby="settings-title">
        <button type="button" className="settings-close" aria-label="Close settings" onClick={() => setOpen(false)}><X size={18}/></button>
        <h2 id="settings-title">Settings</h2>
        <p>This is a read-only cluster monitor. Settings cannot be changed here.</p>
        <p>For help, contact the cluster administrator.</p>
      </div>
    </div>}
  </div>
}
