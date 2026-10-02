import { useEffect, useState } from 'react'

/** Accent sets in cycle order; their colors live in style.css under :root[data-accent]. */
const accents = ['blue', 'sand', 'ocean', 'coral', 'grape', 'sunset', 'pastel'] as const
type Accent = typeof accents[number]
const accentStorageKey = 'accent-colors'

/** Read the last chosen accent set; Palette 3 (coral) when none is stored or storage is blocked. */
export function storedAccent(): Accent {
  try {
    const value = localStorage.getItem(accentStorageKey)
    return accents.find(accent => accent === value) ?? 'coral'
  } catch {
    return 'coral'
  }
}

/** Switch the page to an accent set and remember it; a blocked storage keeps the choice for this page only. */
export function applyAccent(accent: Accent): void {
  document.documentElement.dataset.accent = accent
  try {
    localStorage.setItem(accentStorageKey, accent)
  } catch {
    // Storage is blocked; the page opens on the default palette next time.
  }
}

/** A small swatch in the current accent color that moves to the next accent set. */
export function AccentButton() {
  const [accent, setAccent] = useState<Accent>(storedAccent)
  const next = () => {
    const following = accents[(accents.indexOf(accent) + 1) % accents.length]
    applyAccent(following)
    setAccent(following)
  }
  return <button type="button" className="accent-button" aria-label="Change accent color" title="Change accent color" onClick={next}/>
}

/** Read the current theme's accent and four card colors from the CSS variables. */
function themeColors(): { accent: string; cards: string[] } {
  const style = getComputedStyle(document.documentElement)
  return { accent: style.getPropertyValue('--accent').trim(), cards: [1, 2, 3, 4].map(n => style.getPropertyValue(`--card-${n}`).trim()) }
}

/** The current theme colors, updated when the accent set changes (for drawings that cannot use CSS variables). */
export function useThemeColors(): { accent: string; cards: string[] } {
  const [colors, setColors] = useState(themeColors)
  useEffect(() => {
    const observer = new MutationObserver(() => setColors(themeColors()))
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-accent'] })
    return () => observer.disconnect()
  }, [])
  return colors
}
