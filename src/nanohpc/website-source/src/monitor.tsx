import { useEffect, useState } from 'react'
import { Activity, ChartNoAxesColumnIncreasing, Server, UsersRound } from 'lucide-react'
import { AccentButton } from './accent-button'
import { FontButton } from './font-button'
import { ClusterMap, MapToggle, machinesMapKey, storedMapShown } from './cluster-map'
import { healthText, MachineCards, size, SpeedBoxes, type Machine } from './machines'
import { MachineLabel } from './machine-dialog'
import type { MonitorSiteSettings } from './site'

type MonitorSnapshot = {
  mode: 'monitor'
  generated_at: string
  refresh_seconds: number
  total_gpus: number | null
  nodes: Machine[]
}
type GpuReading = { machine: string; gpu: string; utilization: number | null; memoryUsed: number | null }
type MetricFrame = { schema: { fields: { labels?: Record<string, string> }[] }; data: { values: number[][] } }
type MonitorPage = 'overview' | 'machines' | 'usage' | 'users'
const pages = [
  { id: 'overview', title: 'Overview', icon: Activity },
  { id: 'machines', title: 'Machines', icon: Server },
  { id: 'usage', title: 'Usage', icon: ChartNoAxesColumnIncreasing },
  { id: 'users', title: 'Users', icon: UsersRound },
] as const

/** Reject Slurm snapshots and broken monitor snapshots before displaying them. */
function checkSnapshot(value: unknown): MonitorSnapshot {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error('Invalid monitor snapshot')
  const data = value as Record<string, unknown>
  if (data.mode !== 'monitor' || typeof data.generated_at !== 'string' || !Number.isFinite(Date.parse(data.generated_at))
      || !Number.isFinite(data.refresh_seconds) || !Array.isArray(data.nodes)
      || !data.nodes.every(node => typeof node?.name === 'string' && (node.role === 'Monitor' || node.role === 'Machine'))) {
    throw new Error('Invalid monitor snapshot')
  }
  return data as MonitorSnapshot
}

/** Read fresh public status at the collector's interval. */
function useMonitorSnapshot(): { data: MonitorSnapshot | null; failed: boolean; stale: boolean } {
  const [data, setData] = useState<MonitorSnapshot | null>(null)
  const [failed, setFailed] = useState(false)
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    const controller = new AbortController()
    const update = () => {
      setNow(Date.now())
      fetch('data/status.json', { cache: 'no-store', signal: controller.signal })
        .then(response => { if (!response.ok) throw new Error('Monitoring snapshot unavailable'); return response.json() as Promise<unknown> })
        .then(value => { setData(checkSnapshot(value)); setFailed(false) })
        .catch(() => { if (!controller.signal.aborted) setFailed(true) })
    }
    update()
    const timer = window.setInterval(update, Math.max(5, data?.refresh_seconds ?? 30) * 1000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [data?.refresh_seconds])
  return { data, failed, stale: data !== null && now - Date.parse(data.generated_at) > Math.max(90, data.refresh_seconds * 3) * 1000 }
}

/** Extract per-device values from the existing public Grafana query endpoint. */
function metricRows(frames: MetricFrame[]): { machine: string; gpu: string; value: number }[] {
  return frames.flatMap(frame => {
    const labels = frame.schema.fields[1]?.labels ?? {}
    const values = frame.data.values[1] ?? []
    const value = values[values.length - 1]
    return labels.machine && labels.gpu !== undefined && Number.isFinite(value) ? [{ machine: labels.machine, gpu: labels.gpu, value }] : []
  })
}

/** Query measured GPU utilization and memory use; a failed query leaves readings unknown. */
async function readGpuMetrics(signal: AbortSignal): Promise<GpuReading[]> {
  const response = await fetch('grafana/api/ds/query', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, cache: 'no-store', signal,
    body: JSON.stringify({ from: 'now-5m', to: 'now', queries: [
      { refId: 'util', datasource: { uid: 'cluster-detail', type: 'prometheus' }, expr: 'cluster_gpu_utilization_percent and on(machine) (time() - cluster_gpu_collection_timestamp_seconds < 90)', instant: true, range: false },
      { refId: 'memory', datasource: { uid: 'cluster-detail', type: 'prometheus' }, expr: 'cluster_gpu_memory_used_bytes and on(machine) (time() - cluster_gpu_collection_timestamp_seconds < 90)', instant: true, range: false },
    ] }),
  })
  if (!response.ok) throw new Error('GPU measurements unavailable')
  const result = await response.json() as { results?: Record<string, { frames?: MetricFrame[]; error?: string }> }
  if (!result.results?.util || !result.results?.memory || result.results.util.error || result.results.memory.error) throw new Error('GPU measurements unavailable')
  const rows = new Map<string, GpuReading>()
  for (const item of metricRows(result.results.util.frames ?? [])) rows.set(`${item.machine}\0${item.gpu}`, { machine: item.machine, gpu: item.gpu, utilization: item.value, memoryUsed: null })
  for (const item of metricRows(result.results.memory.frames ?? [])) {
    const key = `${item.machine}\0${item.gpu}`
    const row = rows.get(key) ?? { machine: item.machine, gpu: item.gpu, utilization: null, memoryUsed: null }
    row.memoryUsed = item.value
    rows.set(key, row)
  }
  return [...rows.values()]
}

/** Keep GPU measurements in step with the status refresh. */
function useGpuMetrics(refreshSeconds: number): GpuReading[] | null {
  const [readings, setReadings] = useState<GpuReading[] | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    const update = () => readGpuMetrics(controller.signal).then(setReadings).catch(() => { if (!controller.signal.aborted) setReadings(null) })
    update()
    const timer = window.setInterval(update, Math.max(5, refreshSeconds) * 1000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [refreshSeconds])
  return readings
}

/** Keep hashes inside the four monitor pages. */
function currentPage(): MonitorPage {
  const hash = window.location.hash.slice(1).split('?')[0]
  return pages.find(page => page.id === hash)?.id ?? 'overview'
}

/** Display a measured reading without suggesting that low activity means availability. */
function ReadingTable({ nodes, readings, stale }: { nodes: Machine[]; readings: GpuReading[] | null; stale: boolean }) {
  const rows = nodes.flatMap(node => node.specs?.gpus.map(gpu => ({ node, gpu })) ?? [])
  return <section className="panel table-panel"><h2>GPU readings</h2>
    {rows.length ? <div className="table-scroll"><table aria-label="GPU readings"><thead><tr><th>Machine</th><th>Device</th><th>Model</th><th>Utilization</th><th>Memory used</th></tr></thead>
      <tbody>{rows.map(({ node, gpu }) => {
        const reading = stale ? undefined : readings?.find(item => item.machine === node.name && item.gpu === gpu.index)
        return <tr key={`${node.name}-${gpu.index}`}><th>{node.name}</th><td>GPU {gpu.index}</td><td>{gpu.model}</td>
          <td>{reading?.utilization == null ? 'Unknown' : `${Math.round(reading.utilization)}%`}</td>
          <td>{reading?.memoryUsed == null ? 'Unknown' : `${size(reading.memoryUsed)} / ${size(gpu.memory_bytes)}`}</td></tr>
      })}</tbody></table></div> : <p>No GPUs detected.</p>}
  </section>
}

/** Machine health and observed GPU activity, including the monitoring host. */
function MachineTable({ nodes, stale }: { nodes: Machine[] | null; stale: boolean }) {
  return <section className="panel machine-list"><div className="panel-heading"><h2>Machines</h2></div>
    <div className="table-scroll"><table><thead><tr><th>Machine</th><th>Node type</th><th>Health</th><th>GPU activity</th><th>GPUs installed</th><th>Internet speed</th></tr></thead>
      <tbody>{nodes?.map(node => <tr key={node.name}><th><a href={`#machines?machine=${encodeURIComponent(node.name)}`}>{node.name}</a></th>
        <td>{node.role}</td><td><MachineLabel node={node} topic="health" stale={stale} demo={false} className={`state-${(stale ? 'Unknown' : node.health ?? 'Unknown').toLowerCase()}`} label={healthText(stale ? 'Unknown' : node.health ?? 'Unknown')}>{healthText(stale ? 'Unknown' : node.health ?? 'Unknown')}</MachineLabel></td>
        <td>{stale ? 'Unknown' : node.gpu_usage ?? 'Unknown'}</td><td>{node.total_gpus ?? node.specs?.gpu_count ?? 'Unknown'}</td><td><SpeedBoxes node={node} stale={stale} demo={false} named={false}/></td></tr>)}</tbody></table></div>
    {!nodes && <p role="status">Machine status unavailable.</p>}
  </section>
}

/** Same-origin Grafana dashboard with no credentials in its URL. */
function Dashboard({ name, path }: { name: string; path: string }) {
  return <section className="dashboard panel"><div className="panel-heading"><h2>{name}</h2></div><iframe title={name} src={path} referrerPolicy="same-origin"/></section>
}

/** Render the monitor-specific navigation and content without scheduler concepts. */
export function MonitorApp({ site }: { site: MonitorSiteSettings }) {
  const [page, setPage] = useState<MonitorPage>(currentPage)
  const [mapShown, setMapShown] = useState(() => storedMapShown(machinesMapKey))
  const { data, failed, stale } = useMonitorSnapshot()
  const refresh = data?.refresh_seconds ?? 30
  const readings = useGpuMetrics(refresh)
  useEffect(() => {
    const changed = () => setPage(currentPage())
    window.addEventListener('hashchange', changed)
    return () => window.removeEventListener('hashchange', changed)
  }, [])
  const selected = pages.find(item => item.id === page) ?? pages[0]
  const old = failed || stale
  const nodes = data?.nodes ?? []
  const gpuCount = data ? data.total_gpus ?? 'Unknown' : '—'
  const graph = (uid: string, from: string) => `grafana/d/${uid}?orgId=1&kiosk&hideLogo=true&refresh=${refresh}s&from=${from}&to=now`
  const freshness = <div className={`freshness ${old ? 'warning' : ''}`}><span className="status-dot"/>{failed ? 'Data unavailable' : stale ? 'Data is stale' : data ? `Updates every ${refresh}s` : 'Connecting…'}{data && <small>Last update {new Date(data.generated_at).toLocaleTimeString()}</small>}</div>
  return <>
    <a className="skip" href="#main" onClick={event => { event.preventDefault(); document.getElementById('main')?.focus() }}>Skip to content</a>
    <header className="topbar"><a href="#overview" className="brand">{site.logo ? <img className="brand-mark" src={site.logo} alt={`${site.cluster_name} logo`}/> : <svg className="brand-mark generic-mark" viewBox="0 0 48 48" role="img" aria-label="Cluster mark"><rect x="10" y="9" width="28" height="8"/><rect x="10" y="20" width="28" height="8"/><rect x="10" y="31" width="28" height="8"/></svg>}<span className="brand-text"><span className="brand-title">{site.cluster_name}</span><span className="brand-sub">Machine monitor</span></span></a><div className="header-buttons"><FontButton/><AccentButton/></div></header>
    <div className="layout"><aside className="sidebar"><nav aria-label="Cluster navigation">{pages.map(({ id, title, icon: Icon }) => <a key={id} href={`#${id}`} aria-current={page === id ? 'page' : undefined}><Icon size={19}/><span>{title}</span></a>)}</nav></aside>
      <main id="main" tabIndex={-1} className={page === 'overview' ? 'compact' : undefined}><div className="page-heading"><h1>{selected.title}</h1><div className="page-heading-actions">{page === 'machines' && <MapToggle shown={mapShown} onChange={setMapShown}/>}</div></div>
        {old && <div role="alert" className="notice">{data ? 'Showing stale data.' : 'Monitoring data unavailable.'}</div>}
        {page === 'overview' && <><section aria-label="Cluster summary" className="stat-grid monitor-stats">
          <a className="stat panel" href="#machines"><span>Machines</span><strong>{data ? nodes.length : '—'}</strong></a>
          <a className="stat panel" href="#machines"><span>Healthy machines</span><strong>{data && !old ? nodes.filter(node => node.health === 'Healthy').length : '—'}</strong></a>
          <a className="stat panel" href="#machines"><span>GPUs installed</span><strong>{gpuCount}</strong></a>
        </section><MachineTable nodes={data?.nodes ?? null} stale={old}/></>}
        {page === 'machines' && <>{mapShown && <ClusterMap nodes={nodes} jobs={[]} pendingJobs={0} stale={old} refreshSeconds={refresh} mode="monitor"/>}<MachineTable nodes={data?.nodes ?? null} stale={old}/><ReadingTable nodes={nodes} readings={readings} stale={old}/><MachineCards nodes={nodes} stale={old} roles={['Monitor', 'Machine']}/></>}
        {page === 'usage' && <><Dashboard name="Machine and GPU metrics" path={`${graph('nanohpc-machines', 'now-6h')}&var-gpu_group=0`}/><Dashboard name="Long-term history" path={graph('nanohpc-history', 'now-1y')}/></>}
        {page === 'users' && <section className="panel table-panel"><h2>Login names</h2>{site.users.length ? <ul aria-label="Login names" className="monitor-users">{site.users.map(user => <li key={user}>{user}</li>)}</ul> : <p>No login names configured.</p>}</section>}
        <footer>{freshness}<p>{site.cluster_name} runs on <a href="https://github.com/enajx/nanoHPC">nanoHPC</a></p></footer>
      </main></div>
  </>
}
