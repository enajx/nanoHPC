import { useState } from 'react'
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { OverviewJob } from './overview-jobs'

type DashboardName = 'queue' | 'queue-history' | 'usage' | 'machines' | 'history'
type Range = '24h' | '7d' | '30d'
const panels: Record<DashboardName, string[]> = {
  queue: [],
  'queue-history': ['Allocated GPUs', 'Running jobs', 'Queue size'],
  usage: ['Allocated GPU-hours', 'Fair-share factor'],
  machines: ['CPU in use', 'Available memory', 'Available filesystem space', 'GPU utilization', 'GPU memory used', 'GPU temperature', 'GPU power'],
  history: ['Allocated GPU-hours at daily sample', '24-hour mean CPU usage', '24-hour mean GPU utilization', 'Observed fraction of the 24-hour window'],
}

/** Stable fictional points for the static demo, with a different span for each control. */
function samples(name: string, range: Range): { label: string; value: number }[] {
  const count = range === '24h' ? 24 : range === '7d' ? 28 : 30
  const seed = [...name].reduce((sum, char) => sum + char.charCodeAt(0), 0)
  return Array.from({ length: count }, (_, index) => ({
    label: range === '24h' ? `${index}:00` : range === '7d' ? `Day ${Math.floor(index / 4) + 1}` : `Day ${index + 1}`,
    value: Math.round(Math.max(0, (seed % 55) + 14 * Math.sin(index / 3 + seed) + 6 * Math.cos(index / 5 + seed / 4))),
  }))
}

/** Interactive sample panels for Pages, where no Grafana service is running. */
export function DemoDashboard({ name, kind, jobs, queueFilter }: { name: string; kind: DashboardName; jobs?: OverviewJob[]; queueFilter?: string }) {
  const [range, setRange] = useState<Range>('7d')
  return <section className="dashboard demo-dashboard panel">
    <div className="panel-heading"><h2>{name}</h2>{kind !== 'queue' && <div className="gpu-chart-ranges" role="group" aria-label="Dashboard time range">
      {(['24h', '7d', '30d'] as Range[]).map(value => <button key={value} type="button" aria-pressed={value === range} onClick={() => setRange(value)}>{value}</button>)}
    </div>}</div>
    {kind === 'queue' && <div className="table-scroll demo-queue"><table aria-label="Sample jobs"><thead><tr><th>Job ID</th><th>User</th><th>State</th><th>GPUs</th><th>Machine</th><th>Priority</th></tr></thead><tbody>
      {(jobs ?? []).filter(job => !queueFilter || queueFilter === 'RUNNING|PENDING' || job.state === queueFilter).map(job => <tr key={job.id}><td>{job.id}</td><td>{job.user}</td><td>{job.state}</td><td>{job.gpus}</td><td>{job.node || '—'}</td><td>{job.priority}</td></tr>)}
    </tbody></table></div>}
    <div className="demo-panels">{panels[kind].map(title => <div className="demo-chart" key={title}>
      <h3>{title}</h3><div className="demo-chart-plot" role="img" aria-label={`${title}, fictional sample data for ${range}`}>
        <ResponsiveContainer width="100%" height="100%"><LineChart data={samples(title, range)} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
          <CartesianGrid vertical={false} stroke="#deded8"/><XAxis dataKey="label" tick={{ fontSize: 11 }} minTickGap={25}/><YAxis width={35} tick={{ fontSize: 11 }}/>
          <Tooltip contentStyle={{ background: '#fff', border: '2px solid #172322', color: '#172322' }}/>
          <Line dataKey="value" name={title} type="monotone" stroke="var(--accent)" strokeWidth={3} dot={false} isAnimationActive={false}/>
        </LineChart></ResponsiveContainer>
      </div>
    </div>)}</div>
  </section>
}
