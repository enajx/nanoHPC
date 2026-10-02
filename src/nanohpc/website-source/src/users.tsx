import { Medal } from 'lucide-react'
import { size } from './machines'

export type User = {
  user: string
  gpu_hours_7d: number
  gpu_hours_30d: number
  gpu_hours_365d: number
  home: { used_bytes: number; soft_bytes: number; hard_bytes: number } | null
}

/** Show recent allocation, home use, and any quota issue for each cluster user. */
export function UserCards({ users, stale }: { users: User[]; stale: boolean }) {
  const ordered = [...users].sort((left, right) =>
    (Number.isFinite(right.gpu_hours_30d) ? right.gpu_hours_30d : -Infinity)
    - (Number.isFinite(left.gpu_hours_30d) ? left.gpu_hours_30d : -Infinity)
    || left.user.localeCompare(right.user))
  const ranks = new Map((stale ? [] : ordered
    .filter(row => Number.isFinite(row.gpu_hours_30d) && row.gpu_hours_30d > 0)
    .slice(0, 3)).map((row, index) => [row.user, index + 1]))
  return <section className="user-specs" aria-label="Users"><div className="spec-card-grid">{ordered.map(row => {
    const home = stale ? null : row.home
    const storageIssue = home == null ? null : home.used_bytes >= home.hard_bytes ? 'At hard quota' : home.used_bytes >= home.soft_bytes ? 'At soft quota' : null
    const rank = ranks.get(row.user)
    return <section key={row.user} id={`user-${row.user}`} className="panel user-spec" aria-label={`${row.user} stats`}>
      <div className="panel-heading"><h2>{row.user}</h2><div className="user-card-badges">
        {storageIssue && <span className={`pill ${storageIssue === 'At hard quota' ? 'state-offline' : 'state-warning'}`}>{storageIssue}</span>}
        {rank && <span className={`rank-badge rank-${rank}`} title={`#${rank} by GPU-hours in the last 30 days`}><Medal aria-hidden="true" size={16}/>{`#${rank}`}</span>}
      </div></div>
      <dl className="compact-details">
        <div><dt>GPU hours · 7 days</dt><dd>{stale ? 'Unknown' : row.gpu_hours_7d.toFixed(2)}</dd></div>
        <div><dt>GPU hours · 30 days</dt><dd>{stale ? 'Unknown' : row.gpu_hours_30d.toFixed(2)}</dd></div>
        <div><dt>GPU hours · 365 days</dt><dd>{stale ? 'Unknown' : row.gpu_hours_365d.toFixed(2)}</dd></div>
        <div><dt>Home storage</dt><dd>{size(home?.used_bytes)} / {size(home?.hard_bytes)}</dd></div>
      </dl>
    </section>
  })}</div></section>
}
