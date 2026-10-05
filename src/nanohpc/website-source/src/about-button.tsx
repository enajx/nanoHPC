import { useEffect, useRef, useState } from 'react'

/** Open the short project help menu; Escape and outside clicks close it. */
export function AboutButton() {
  const [open, setOpen] = useState(false)
  const area = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const close = (event: MouseEvent | KeyboardEvent) => {
      if (event instanceof KeyboardEvent ? event.key === 'Escape' : !area.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', close)
    return () => { document.removeEventListener('mousedown', close); document.removeEventListener('keydown', close) }
  }, [open])
  return <div className="about" ref={area}>
    <button type="button" className="about-button" aria-label="About nanoHPC" title="About nanoHPC" aria-expanded={open} onClick={() => setOpen(!open)}>?</button>
    {open && <div className="about-box" role="dialog" aria-label="About nanoHPC">
      <p><a href="https://github.com/enajx/nanoHPC">nanoHPC on GitHub</a></p>
      <p><a href="#docs" onClick={() => setOpen(false)}>User documentation</a></p>
    </div>}
  </div>
}
