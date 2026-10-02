/** A card title bar that opens the matching tab, pressing the card in on hover like the summary cards. */
export function PanelLink({ href, title }: { href: string; title: string }) {
  return <a className="panel-heading panel-link" href={href}><h2>{title}</h2><span className="panel-link-arrow" aria-hidden="true">→</span></a>
}
