import { useEffect, useRef, useState } from 'react'
import type { Machine } from './machines'
import type { OverviewJob } from './overview-jobs'
import type { ClusterMap as Sketch, LayoutName, MapMachine } from './cluster-map/sketch'

const layouts: [LayoutName, string][] = [['tiers', 'Tiers'], ['radial', 'Radial'], ['blocks', 'Blocks']]
const layoutStorageKey = 'cluster-map-layout'
const shownStorageKey = 'cluster-map-shown'

/** Measurements from monitoring, per machine name; null until loaded or when the query fails. */
type Measured = { nfs: Map<string, number>; writes: Map<string, number>; busy: Map<string, number[]> }
type MetricFrame = { schema: { fields: { labels?: Record<string, string> }[] }; data: { values: number[][] } }
const queries = {
  nfs: 'sum by (machine) (rate(node_nfs_requests_total[2m]))',
  writes: 'sum by (machine) (rate(node_nfs_requests_total{method="Write"}[2m]))',
  busy: 'cluster_gpu_utilization_percent',
}

/** Read the latest value of each series, keyed by its labels. */
function latest(frames: MetricFrame[]): { labels: Record<string, string>; value: number }[] {
  return frames.flatMap(frame => {
    const values = frame.data.values[1]
    const value = values?.[values.length - 1]
    return Number.isFinite(value) ? [{ labels: frame.schema.fields[1]?.labels ?? {}, value }] : []
  })
}

/** Ask monitoring for shared-home requests and GPU busy % through the public read-only query route. */
async function measure(signal: AbortSignal): Promise<Measured> {
  const refIds = Object.keys(queries) as (keyof typeof queries)[]
  const response = await fetch('grafana/api/ds/query', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, cache: 'no-store', signal,
    body: JSON.stringify({
      from: 'now-5m', to: 'now',
      queries: refIds.map(refId => ({ refId, datasource: { uid: 'cluster-detail', type: 'prometheus' }, expr: queries[refId], instant: true, range: false })),
    }),
  })
  if (!response.ok) throw new Error('Monitoring unavailable')
  const reply = await response.json() as { results: Record<string, { frames?: MetricFrame[]; error?: string }> }
  for (const refId of refIds) if (reply.results[refId]?.error) throw new Error('Monitoring query failed')
  const byMachine = (refId: keyof typeof queries) => new Map(latest(reply.results[refId]?.frames ?? []).map(s => [s.labels.machine, s.value]))
  const busy = new Map<string, number[]>()
  for (const s of latest(reply.results.busy?.frames ?? [])) {
    const list = busy.get(s.labels.machine) ?? []
    list[Number(s.labels.gpu)] = s.value
    busy.set(s.labels.machine, list)
  }
  return { nfs: byMachine('nfs'), writes: byMachine('writes'), busy }
}

/** Read whether the map is shown; off when nothing is stored or storage is blocked. */
export function storedMapShown(): boolean {
  try {
    return localStorage.getItem(shownStorageKey) === 'true'
  } catch {
    return false
  }
}

/** The Map button beside MD: hides or shows the map and remembers the choice. */
export function MapToggle({ shown, onChange }: { shown: boolean; onChange: (shown: boolean) => void }) {
  const toggle = () => {
    onChange(!shown)
    try {
      localStorage.setItem(shownStorageKey, String(!shown))
    } catch {
      // Storage is blocked; the map is off next time.
    }
  }
  return <button type="button" className="markdown-link map-toggle" aria-label="Show cluster map" aria-pressed={shown} title={shown ? 'Hide the cluster map' : 'Show the cluster map'} onClick={toggle}>Cluster Map</button>
}

/** Read the last chosen layout; Tiers when none is stored or storage is blocked. */
function storedLayout(): LayoutName {
  try {
    const value = localStorage.getItem(layoutStorageKey)
    return layouts.find(([name]) => name === value)?.[0] ?? 'tiers'
  } catch {
    return 'tiers'
  }
}

/** Turn the snapshot's machines and jobs into what the map draws. */
function mapMachines(nodes: Machine[], jobs: OverviewJob[], pending: number, measured: Measured | null): MapMachine[] {
  return nodes.map(node => {
    const running = jobs.filter(job => job.state === 'RUNNING' && job.node === node.name).sort((a, b) => a.id.localeCompare(b.id))
    const total = node.total_gpus ?? 0
    const allocated = node.available_gpus != null ? total - node.available_gpus : running.reduce((sum, job) => sum + job.gpus, 0)
    // Slurm does not say which GPU index a job has, so allocated lights take users in job order.
    const users = running.flatMap(job => Array(job.gpus).fill(job.user) as string[])
    const busy = measured?.busy.get(node.name)
    return {
      name: node.name, front: node.role === 'Front node', health: node.health ?? 'Unknown',
      gpuModel: node.specs?.gpus[0]?.model ?? null, totalGpus: total,
      allocatedGpus: allocated,
      runningJobs: running.length, pendingJobs: node.role === 'Front node' ? pending : 0,
      nfsRequests: measured?.nfs.get(node.name) ?? null, nfsWrites: measured?.nfs.has(node.name) ? measured.writes.get(node.name) ?? 0 : null,
      gpus: Array.from({ length: total }, (_, i) => ({ user: i < allocated ? users[i] ?? '' : null, busy: busy?.[i] ?? null })),
    }
  })
}

/** Describe the map in words for screen readers and tests. */
function describe(machines: MapMachine[]): string {
  return `Cluster map: ${machines.map(m => m.front
    ? `${m.name}: front node, ${m.pendingJobs} pending job${m.pendingJobs === 1 ? '' : 's'}`
    : `${m.name}: ${m.allocatedGpus} of ${m.totalGpus} GPUs allocated, ${m.runningJobs} running job${m.runningJobs === 1 ? '' : 's'}, ${
      m.nfsRequests === null ? 'shared home traffic unknown' : `shared home ${Math.round(m.nfsRequests)} requests per second (${Math.round(m.nfsWrites ?? 0)} writes)`}${
      !m.gpus.length ? '' : m.gpus.some(gpu => gpu.busy === null) ? ', GPU busy unknown' : `, GPUs busy ${m.gpus.map(gpu => `${Math.round(gpu.busy!)}%`).join(', ')}`}`).join('; ')}`
}

/** Animated isometric map of the machines Slurm knows, with traffic from their running jobs. */
export function ClusterMap({ nodes, jobs, pendingJobs, stale, refreshSeconds }: { nodes: Machine[]; jobs: OverviewJob[]; pendingJobs: number; stale: boolean; refreshSeconds: number }) {
  const box = useRef<HTMLDivElement>(null)
  const sketch = useRef<Sketch | null>(null)
  const [layout, setLayout] = useState<LayoutName>(storedLayout)
  const [measured, setMeasured] = useState<Measured | null>(null)
  const machines = mapMachines(nodes, jobs, pendingJobs, measured)

  // Measurements refresh with the snapshot; a failed query means no traffic is shown, not zero.
  useEffect(() => {
    const controller = new AbortController()
    const load = () => measure(controller.signal).then(setMeasured).catch(() => { if (!controller.signal.aborted) setMeasured(null) })
    load()
    const timer = window.setInterval(load, Math.max(5, refreshSeconds) * 1000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [refreshSeconds])
  const latest = useRef({ machines, stale })
  latest.current = { machines, stale }

  // p5 loads only when this map is shown.
  useEffect(() => {
    let cancelled = false
    // p5's bundled regenerator assigns the global `regeneratorRuntime`; in strict code that fails unless the global
    // exists, and its fallback evaluates a string, which the site's Content Security Policy blocks.
    const scope = globalThis as { regeneratorRuntime?: unknown }
    if (!('regeneratorRuntime' in scope)) scope.regeneratorRuntime = undefined
    import('./cluster-map/sketch').then(({ createClusterMap }) => {
      if (cancelled || !box.current) return
      sketch.current = createClusterMap(box.current, layout)
      sketch.current.update(latest.current.machines, latest.current.stale)
    })
    return () => { cancelled = true; sketch.current?.remove(); sketch.current = null }
  }, [])
  useEffect(() => { sketch.current?.update(machines, stale) }, [JSON.stringify(machines), stale])

  const choose = (name: LayoutName) => {
    setLayout(name)
    sketch.current?.setLayout(name)
    try {
      localStorage.setItem(layoutStorageKey, name)
    } catch {
      // Storage is blocked; the map opens on Tiers next time.
    }
  }
  return <div className="cluster-map">
    <div className="gpu-chart-ranges cluster-map-layouts" role="group" aria-label="Cluster map layout">
      {layouts.map(([name, label]) => <button key={name} type="button" aria-pressed={name === layout} onClick={() => choose(name)}>{label}</button>)}
    </div>
    <div className="cluster-map-canvas" ref={box} role="img" aria-label={describe(machines)}/>
  </div>
}
