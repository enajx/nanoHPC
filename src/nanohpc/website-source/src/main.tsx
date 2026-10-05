import { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Activity, BookOpen, ChartNoAxesColumnIncreasing, ListOrdered, ScrollText, Server, UsersRound } from 'lucide-react'
import { HowToUse, PartitionSummary, type Partition } from './guide'
import { GpuAllocationChart } from './gpu-allocation-chart'
import { MachineCards, type Machine } from './machines'
import { OverviewJobs, type OverviewJob } from './overview-jobs'
import { UserCards, type User } from './users'
import { policyExplanation, siteValues } from './documentation'
import { loadSiteSettings, type SiteSettings } from './site'
import { PanelLink } from './panel-link'
import { AccentButton, storedAccent } from './accent-button'
import { FontButton, storedFont } from './font-button'
import { ClusterMap, MapToggle, storedMapShown } from './cluster-map'
import { DemoDashboard } from './demo-dashboard'
import { AboutButton } from './about-button'
import './style.css'

type Page = 'overview' | 'queue' | 'machines' | 'users' | 'usage' | 'docs' | 'policy'
type Snapshot = {
  generated_at: string
  refresh_seconds: number
  running_jobs: number
  pending_jobs: number
  jobs: OverviewJob[]
  allocated_gpus: number
  total_gpus: number
  average_wait_seconds_30d: number | null
  nodes: Machine[]
  policies: { name: string; value: string }[]
  partitions?: Partition[]
  ranking: { user: string; gpu_hours: number; decayed_gpu_hours: number; fairshare: number }[]
  users: User[]
  priorities: { job_id: string; user: string; priority: number; fairshare: number; age: number }[]
}
const pages = [
  { id: 'overview', title: 'Overview', icon: Activity },
  { id: 'docs', title: 'How to', icon: BookOpen },
  { id: 'queue', title: 'Jobs', icon: ListOrdered },
  { id: 'machines', title: 'Machines', icon: Server },
  { id: 'users', title: 'Users', icon: UsersRound },
  { id: 'usage', title: 'Cluster usage', icon: ChartNoAxesColumnIncreasing },
  { id: 'policy', title: 'Cluster policy', icon: ScrollText },
] as const

/** Show a short human-readable duration in the Overview card. */
function waitingTime(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return '—'
  if (seconds < 60) return `${Math.round(seconds)} sec`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} min`
  const hours = Math.floor(minutes / 60)
  return `${hours} hr ${minutes % 60} min`
}

/** Group policy rows by partition, default partition first, then rows for all partitions. */
function policyCards(policies: { name: string; value: string }[], partitions: { name: string; default: boolean }[]): { title: string; rows: { name: string; value: string }[] }[] {
  const ordered = [...partitions].sort((left, right) => Number(right.default) - Number(left.default))
  const cards = ordered.map((partition) => ({
    title: partition.name + (partition.default ? ' (default)' : ''),
    rows: policies.filter((policy) => policy.name.startsWith(partition.name + ': ')).map((policy) => ({ name: policy.name.slice(partition.name.length + 2), value: policy.value })),
  }))
  const login = policies.filter(policy => policy.name.startsWith('Login on front: ')).map(policy => ({ name: policy.name.slice('Login on front: '.length), value: policy.value }))
  const shared = policies.filter((policy) => !partitions.some((partition) => policy.name.startsWith(partition.name + ': ')) && !policy.name.startsWith('Login on front: '))
  return [...cards, { title: 'Login on front', rows: login }, { title: 'All partitions', rows: shared }].filter((card) => card.rows.length)
}

/** Keep unknown hashes on the overview without requesting an untrusted URL. */
function currentPage(): Page {
  const hash = window.location.hash.slice(1).split('?')[0]
  if (hash === 'docs-alt') return 'docs'
  return pages.find((page) => page.id === hash)?.id ?? 'overview'
}

/** Load only the fixed, public snapshot; stale and missing data are explicit. */
function useSnapshot(demo: boolean): { data: Snapshot | null; failed: boolean; stale: boolean } {
  const [data, setData] = useState<Snapshot | null>(null)
  const [failed, setFailed] = useState(false)
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    const controller = new AbortController()
    const update = () => {
      setNow(Date.now())
      fetch('data/status.json', { cache: 'no-store', signal: controller.signal })
        .then((response) => {
          if (!response.ok) throw new Error('Monitoring snapshot unavailable')
          return response.json() as Promise<Snapshot>
        })
        .then((snapshot) => {
          if (!Number.isFinite(Date.parse(snapshot.generated_at)) || !Array.isArray(snapshot.nodes)
              || !Array.isArray(snapshot.policies) || !Array.isArray(snapshot.ranking)
              || !Array.isArray(snapshot.users) || !Array.isArray(snapshot.jobs)
              || !Array.isArray(snapshot.priorities)
              || !Number.isFinite(snapshot.refresh_seconds)) throw new Error('Invalid monitoring snapshot')
          setData(snapshot)
          setFailed(false)
        })
        .catch(() => { if (!controller.signal.aborted) setFailed(true) })
    }
    update()
    const timer = demo ? null : window.setInterval(update, Math.max(5, data?.refresh_seconds ?? 30) * 1000)
    return () => { controller.abort(); if (timer !== null) window.clearInterval(timer) }
  }, [data?.refresh_seconds, demo])
  return { data, failed, stale: !demo && data !== null && now - Date.parse(data.generated_at) > Math.max(90, data.refresh_seconds * 3) * 1000 }
}

/** Render a same-origin dashboard without embedding any service credentials. */
function Dashboard({ name, path }: { name: string; path: string }) {
  const frame = useRef<HTMLIFrameElement>(null)
  useEffect(() => {
    const resize = (): void => {
      const document = frame.current?.contentDocument
      if (!document || !frame.current) return
      // Grafana's page always fills the frame, so size the frame to the last chart, not the page.
      const panels = [...document.querySelectorAll('.react-grid-item')]
      if (!panels.length) return
      const bottom = Math.max(...panels.map((panel) => panel.getBoundingClientRect().bottom + (document.defaultView?.scrollY ?? 0)))
      // Grafana pads 32 px below its last chart.
      frame.current.style.height = `${Math.ceil(bottom) + 32}px`
    }
    const timer = window.setInterval(resize, 1000)
    return () => window.clearInterval(timer)
  }, [path])
  return <section className="dashboard panel">
    <div className="panel-heading"><h2>{name}</h2></div>
    <iframe ref={frame} title={name} src={path} referrerPolicy="same-origin" />
  </section>
}

/** Reuse the same observed machine states on Overview and Machines. */
function MachineTable({ data, stale, link }: { data: Snapshot | null; stale: boolean; link: string | null }) {
  return <section className="panel machine-list">
    {link ? <PanelLink href={link} title="Machines"/> : <div className="panel-heading"><h2>Machines</h2></div>}
    <div className="table-scroll"><table><thead><tr><th>Machine</th><th>Health</th><th>Accelerator usage · last hour</th><th>Available GPUs</th></tr></thead>
      <tbody>{data?.nodes.filter(node => node.role === 'Compute').map((node) => {
        const health = stale ? 'Unknown' : node.health ?? 'Unknown'
        const usage = stale ? 'Unknown' : node.fpga_usage_percent != null ? `FPGA ${node.fpga_usage_percent}%` : node.gpu_usage ?? 'Unknown'
        const available = stale ? 'Unknown' : node.total_gpus === 0 ? '—' : node.available_gpus != null && node.total_gpus != null ? `${node.available_gpus}/${node.total_gpus}` : 'Unknown'
        return <tr key={node.name}><th><a className="machine-link" href={`#machines?machine=${encodeURIComponent(node.name)}`} onClick={() => document.getElementById(`machine-${node.name}`)?.scrollIntoView({ block: 'start' })}>{node.name}</a></th><td><span className={`pill state-${health.toLowerCase()}`} title={stale ? 'Measurements are stale' : node.health_details?.join('; ')}>{health}</span></td><td><span className={`pill ${node.fpga_usage_percent != null && !stale ? 'state-active' : `state-${usage.toLowerCase().replaceAll(' ', '-')}`}`}>{usage}</span></td><td>{available}</td></tr>
      })}</tbody></table></div>
    {!data && <p role="status">Machine status unavailable.</p>}
  </section>
}

/** A small generic mark for clusters without a logo: a rack of three machines. */
function GenericMark() {
  return <svg className="brand-mark generic-mark" viewBox="0 0 48 48" role="img" aria-label="Cluster mark">
    <rect x="10" y="9" width="28" height="8" stroke="#172322" strokeWidth="2"/>
    <rect x="10" y="20" width="28" height="8" stroke="#172322" strokeWidth="2"/>
    <rect x="10" y="31" width="28" height="8" stroke="#172322" strokeWidth="2"/>
    <circle cx="16" cy="13" r="1.5" fill="#172322"/><circle cx="16" cy="24" r="1.5" fill="#172322"/><circle cx="16" cy="35" r="1.5" fill="#172322"/>
  </svg>
}

function App({ site }: { site: SiteSettings }) {
  const [page, setPage] = useState<Page>(currentPage)
  const [mapShown, setMapShown] = useState(storedMapShown)
  const [queueState, setQueueState] = useState(window.location.hash.split('?')[1] ?? '')
  const demo = site.demo === true
  const [demoUpdatedAt, setDemoUpdatedAt] = useState(Date.now())
  const { data, failed, stale } = useSnapshot(demo)
  useEffect(() => {
    if (!demo) return
    const timer = window.setInterval(() => setDemoUpdatedAt(Date.now()), 30000)
    return () => window.clearInterval(timer)
  }, [demo])
  useEffect(() => {
    const changed = () => { setPage(currentPage()); setQueueState(window.location.hash.split('?')[1] ?? '') }
    window.addEventListener('hashchange', changed)
    return () => window.removeEventListener('hashchange', changed)
  }, [])
  useEffect(() => {
    if (window.matchMedia('(max-width: 720px)').matches) {
      document.querySelector('.sidebar nav a[aria-current="page"]')?.scrollIntoView({ block: 'nearest', inline: 'center' })
    }
  }, [page])
  const selected = pages.find((item) => item.id === page) ?? pages[0]
  const refresh = data?.refresh_seconds ?? 30
  const graph = (uid: string, from: string) => `grafana/d/${uid}?orgId=1&kiosk&hideLogo=true&refresh=${refresh}s&from=${from}&to=now`
  const dashboard = (name: string, kind: 'queue' | 'queue-history' | 'usage' | 'machines' | 'history', path: string) => demo
    ? <DemoDashboard name={name} kind={kind} jobs={data?.jobs} queueFilter={queueFilter}/>
    : <Dashboard name={name} path={path}/>
  const requestedState = new URLSearchParams(queueState).get('state')
  const queueFilter = requestedState === 'RUNNING' || requestedState === 'PENDING' ? requestedState : 'RUNNING|PENDING'
  const requestedMachine = new URLSearchParams(queueState).get('machine')
  const requestedUser = new URLSearchParams(queueState).get('user')
  const hasData = data !== null
  useEffect(() => {
    if (page === 'machines' && requestedMachine) document.getElementById(`machine-${requestedMachine}`)?.scrollIntoView({ block: 'start' })
    if (page === 'users' && requestedUser) document.getElementById(`user-${requestedUser}`)?.scrollIntoView({ block: 'start' })
  }, [page, requestedMachine, requestedUser, hasData])
  const isGuide = page === 'docs'
  const markdownPage = isGuide ? 'docs' : page === 'machines' ? 'machines' : page === 'policy' ? 'policy' : null
  return <>
    <a className="skip" href="#main" onClick={(event) => { event.preventDefault(); document.getElementById('main')?.focus() }}>Skip to content</a>
    <header className="topbar"><a href="#overview" className="brand">{site.logo ? <img className="brand-mark" src={site.logo} alt={`${site.cluster_name} logo`}/> : <GenericMark/>}<span className="brand-text"><span className="brand-title">{site.cluster_name}</span><span className="brand-sub">{demo ? 'lightweight Slurm cluster and monitoring tool' : 'Slurm cluster monitor'}</span></span></a>
      <div className="header-buttons"><AboutButton/><FontButton/><AccentButton/></div>
    </header>
    <div className="layout"><aside className="sidebar">
      <nav aria-label="Cluster navigation">{pages.map(({ id, title, icon: Icon }) => <a key={id} href={`#${id}`} aria-current={page === id ? 'page' : undefined}><Icon size={19}/><span>{title}</span></a>)}</nav>
    </aside>
    <main id="main" tabIndex={-1}>
      <div className="page-heading"><h1>{selected.title}</h1><div className="page-heading-actions">{page === 'machines' && <MapToggle shown={mapShown} onChange={setMapShown}/>}{markdownPage && <a className="markdown-link" href={`${markdownPage}.md`} aria-label={`Open ${selected.title} in Markdown`}>MD</a>}{!isGuide && <div className={`freshness ${failed || stale ? 'warning' : ''}`}><span className="status-dot"/>{demo ? 'Updates every 30s' : failed ? 'Data unavailable' : stale ? 'Data is stale' : data ? `Updates every ${refresh}s` : 'Connecting…'}{(demo || data) && <small>Last update {new Date(demo ? demoUpdatedAt : data!.generated_at).toLocaleTimeString()}</small>}</div>}</div></div>
      {!isGuide && (failed || stale) && <div role="alert" className="notice">{data ? 'Showing stale data.' : 'Monitoring data unavailable.'}</div>}
      {page === 'overview' && <>
        <section aria-label="Cluster summary" className="stat-grid">
          {[['Running jobs', data?.running_jobs, '#queue?state=RUNNING'], ['Pending jobs', data?.pending_jobs, '#queue?state=PENDING'], ['GPUs allocated', data ? `${data.allocated_gpus} / ${data.total_gpus}` : undefined, '#machines'], ['30days waiting time', waitingTime(data?.average_wait_seconds_30d), '#queue']].map(([label, value, href]) => <a key={label} className="stat panel" href={String(href)}><span>{label}</span><strong>{value ?? '—'}</strong></a>)}
        </section>
        <div className="overview-machine-charts"><MachineTable data={data} stale={stale || failed} link="#machines"/><GpuAllocationChart nodes={data?.nodes ?? null} refreshSeconds={refresh} failed={failed} demo={demo}/></div>
        <OverviewJobs jobs={stale || failed ? null : data?.jobs ?? null}/>
      </>}
      {page === 'queue' && <div className="queue-dashboards">{dashboard('Running Jobs and Queue', 'queue', `${graph('nanohpc-queue', 'now-6h')}&var-state=${encodeURIComponent(queueFilter)}`)}{dashboard('Queue history', 'queue-history', graph('nanohpc-queue-history', 'now-7d'))}</div>}
      {page === 'machines' && <>{mapShown && <ClusterMap nodes={data?.nodes ?? []} jobs={data?.jobs ?? []} pendingJobs={data?.pending_jobs ?? 0} stale={stale || failed} refreshSeconds={refresh} demo={demo}/>}<MachineTable data={data} stale={stale || failed} link={null}/><MachineCards nodes={data?.nodes ?? []} stale={stale || failed}/></>}
      {page === 'users' && <>
        <section className="panel table-panel"><h2>User ranking</h2><div className="table-scroll"><table><thead><tr><th>Rank</th><th>User</th><th>Allocated GPU-hours</th><th>Fair-share factor</th></tr></thead><tbody>{data && [...data.ranking].sort((left, right) => right.gpu_hours - left.gpu_hours || left.user.localeCompare(right.user)).map((row, index) => <tr key={row.user}><td>{index + 1}</td><th><a className="user-link" href={`#users?user=${encodeURIComponent(row.user)}`}>{row.user}</a></th><td>{row.gpu_hours.toFixed(3)}</td><td>{row.fairshare.toFixed(4)}</td></tr>)}</tbody></table></div></section>
        <UserCards users={data?.users ?? []} stale={stale || failed}/>
        {dashboard('GPU usage history', 'usage', graph('nanohpc-usage', 'now-7d'))}
      </>}
      {page === 'usage' && <>{dashboard('Machine and GPU metrics', 'machines', `${graph('nanohpc-machines', 'now-6h')}&var-gpu_group=0`)}{dashboard('Long-term history (daily summaries, kept 5 years)', 'history', graph('nanohpc-history', 'now-1y'))}</>}
      {isGuide && <HowToUse values={siteValues(site)} partitions={data?.partitions ?? null}/>}
      {page === 'policy' && <>
        <h2 className="section-heading">Partition policy</h2>
        {data ? policyCards(data.policies, data.partitions ?? []).map((card) => <section className="panel policies" key={card.title}><div className="panel-heading"><h2>{card.title}</h2></div><dl>{card.rows.map((policy) => <div key={policy.name}><dt>{policy.name}</dt><dd>{policy.value}</dd></div>)}</dl></section>) : <section className="panel policies"><div className="panel-heading"><h2>Current policies</h2></div><p>Live policies are unavailable. Run <code>scontrol show partition</code> on the front node to inspect queue limits.</p></section>}
        <div className="user-guide"><section><h2>{policyExplanation.title}</h2><p>{policyExplanation.text}</p><pre><code>{policyExplanation.command}</code></pre></section>
          <section className="partition-summary"><h2>Partitions</h2><PartitionSummary partitions={data?.partitions ?? null}/></section>
        </div>
      </>}
    </main></div>
  </>
}

document.documentElement.dataset.accent = storedAccent()
document.documentElement.dataset.font = storedFont()
const root = createRoot(document.getElementById('root')!)
// The page needs the cluster's settings before it can show anything; a missing or invalid site.json is shown.
loadSiteSettings().then((site) => {
  document.title = site.cluster_name
  root.render(<App site={site}/>)
}).catch((error: unknown) => {
  console.error(error)
  root.render(<div role="alert" className="notice">This cluster website is not set up: {String(error instanceof Error ? error.message : error)}</div>)
})
