import { useState } from 'react'
// Bundled latin 400 and 700 faces; the browser downloads a file only when its font is shown.
import '@fontsource/space-grotesk/latin-400.css'
import '@fontsource/space-grotesk/latin-700.css'
import '@fontsource/inter/latin-400.css'
import '@fontsource/inter/latin-700.css'
import '@fontsource/ibm-plex-sans/latin-400.css'
import '@fontsource/ibm-plex-sans/latin-700.css'
import '@fontsource/dm-sans/latin-400.css'
import '@fontsource/dm-sans/latin-700.css'

/** Main fonts in cycle order; their families live in style.css under :root[data-font]. */
const fonts = ['arial', 'space-grotesk', 'inter', 'ibm-plex-sans', 'dm-sans'] as const
type Font = typeof fonts[number]
const fontStorageKey = 'font'

/** Read the last chosen font; DM Sans when none is stored or storage is blocked. */
export function storedFont(): Font {
  try {
    const value = localStorage.getItem(fontStorageKey)
    return fonts.find(font => font === value) ?? 'dm-sans'
  } catch {
    return 'dm-sans'
  }
}

/** Switch the page to a font and remember it; a blocked storage keeps the choice for this page only. */
function applyFont(font: Font): void {
  document.documentElement.dataset.font = font
  try {
    localStorage.setItem(fontStorageKey, font)
  } catch {
    // Storage is blocked; the page opens on DM Sans next time.
  }
}

/** A small "Aa" square in the current font that moves to the next font; hidden for now, code kept. */
export function FontButton() {
  const [font, setFont] = useState<Font>(storedFont)
  const next = () => {
    const following = fonts[(fonts.indexOf(font) + 1) % fonts.length]
    applyFont(following)
    setFont(following)
  }
  return <button type="button" className="font-button" aria-label="Change font" title="Change font" hidden onClick={next}>Aa</button>
}
