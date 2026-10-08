import { PanelLink } from './panel-link'

export type OverviewJob = {
  id: string
  user: string
  state: 'RUNNING' | 'PENDING'
  gpus: number
  node: string
  priority: number
  seconds: number
  /** Slurm job limit in minutes; null means unlimited, undefined means an older snapshot. */
  time_limit_minutes?: number | null
}

/** Show a short elapsed or waiting time without dropping seconds. */
function duration(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds))
  const hours = Math.floor(whole / 3600)
  const minutes = Math.floor(whole % 3600 / 60)
  const rest = String(whole % 60).padStart(2, '0')
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${rest}` : `${minutes}:${rest}`
}

/** Display the requested or partition default time limit in days, hours, and minutes. */
function booked(minutes: number | null | undefined): string {
  if (minutes === undefined) return '—'
  if (minutes === null) return 'No limit'
  const days = Math.floor(minutes / 1440)
  const hours = Math.floor(minutes % 1440 / 60)
  const parts = [days && `${days} day${days === 1 ? '' : 's'}`, hours && `${hours} hr`, minutes % 60 && `${minutes % 60} min`].filter(Boolean)
  return parts.length ? parts.join(' ') : '0 min'
}

/** Keep running jobs visible and show the first five by Slurm priority. */
export function OverviewJobs({ jobs }: { jobs: OverviewJob[] | null }) {
  const running = jobs?.filter(job => job.state === 'RUNNING').sort((a, b) => b.seconds - a.seconds || a.id.localeCompare(b.id, undefined, { numeric: true })) ?? []
  const pending = jobs?.filter(job => job.state === 'PENDING').sort((a, b) => b.priority - a.priority || a.id.localeCompare(b.id, undefined, { numeric: true })).slice(0, 5) ?? []
  return <section className="panel overview-jobs" aria-label="Current jobs">
    <PanelLink href="#queue" title="Current jobs"/>
    <div className="overview-job-groups">
      <section><h3>Running jobs</h3><div className="table-scroll overview-job-list"><table aria-label="Running jobs"><thead><tr><th>Job ID</th><th>User</th><th>GPUs</th><th>Running time</th><th>Time booked</th><th>Machine</th></tr></thead><tbody>
        {running.map(job => <tr key={job.id}><td>{job.id}</td><td>{job.user}</td><td>{job.gpus}</td><td>{duration(job.seconds)}</td><td>{booked(job.time_limit_minutes)}</td><td>{job.node || '—'}</td></tr>)}
        {!running.length && <tr><td colSpan={6}>{jobs ? '—' : 'Unknown'}</td></tr>}
      </tbody></table></div></section>
      <section><h3>Top of queue</h3><div className="table-scroll overview-job-list"><table aria-label="Top of queue"><thead><tr><th>Job ID</th><th>User</th><th>GPUs</th><th>Waiting time</th><th>Time booked</th></tr></thead><tbody>
        {pending.map(job => <tr key={job.id}><td>{job.id}</td><td>{job.user}</td><td>{job.gpus}</td><td>{duration(job.seconds)}</td><td>{booked(job.time_limit_minutes)}</td></tr>)}
        {!pending.length && <tr><td colSpan={5}>{jobs ? '—' : 'Unknown'}</td></tr>}
      </tbody></table></div></section>
    </div>
  </section>
}
