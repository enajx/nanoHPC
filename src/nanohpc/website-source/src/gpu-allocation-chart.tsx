import { useEffect, useState } from 'react'
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { useThemeColors } from './accent-button'

type GpuMachine = { name: string; role: string; total_gpus?: number | null }
type MetricFrame = {
  schema: { fields: { labels?: { node?: string } }[] }
  data: { values: [number[], number[]] }
}
type MetricResult = { results: { A: { frames: MetricFrame[]; error?: string }; B: { frames: MetricFrame[]; error?: string } } }
type ChartPoint = { time: number; idle: number; [machine: string]: number }
// Used after the theme's accent and card colors run out.
const extraColors = ['#5294ff', '#ff8a67', '#a184f5', '#42b883', '#f7ce46', '#ed77b5', '#65b7c1', '#b9d064']
const hourMs = 60 * 60 * 1000
/** Each range averages its samples into blocks of about 50 to 60 points. */
const ranges = {
  '24h': { title: '24 hours', windowMs: 24 * hourMs, blockMs: hourMs / 2, block: '30-minute' },
  '7d': { title: '7 days', windowMs: 7 * 24 * hourMs, blockMs: 3 * hourMs, block: '3-hour' },
  '30d': { title: '30 days', windowMs: 30 * 24 * hourMs, blockMs: 12 * hourMs, block: '12-hour' },
} as const
type RangeKey = keyof typeof ranges
const rangeStorageKey = 'cluster-usage-range'

/** Read the last chosen range; 7d when none is stored or storage is blocked. */
function storedRange(): RangeKey {
  try {
    const value = localStorage.getItem(rangeStorageKey)
    return value && value in ranges ? value as RangeKey : '7d'
  } catch {
    return '7d'
  }
}

/** Remember the chosen range in this browser; a blocked storage keeps the choice for this page only. */
function storeRange(range: RangeKey): void {
  try {
    localStorage.setItem(rangeStorageKey, range)
  } catch {
    // Storage is blocked; the page opens on 7d next time.
  }
}

/** Average samples into blocks so the curve is smooth; averages still add up to capacity. */
function averageBlocks(points: ChartPoint[], keys: string[], blockMs: number): ChartPoint[] {
  const blocks = new Map<number, ChartPoint[]>()
  for (const point of points) {
    const start = Math.floor(point.time / blockMs) * blockMs
    if (!blocks.has(start)) blocks.set(start, [])
    blocks.get(start)!.push(point)
  }
  return [...blocks].map(([start, samples]) => {
    const block: ChartPoint = { time: start, idle: 0 }
    for (const key of keys) block[key] = samples.reduce((sum, sample) => sum + sample[key], 0) / samples.length
    return block
  })
}

/** Join allocated and total Slurm GPU counts at each Prometheus sample time. */
function pointsFromMetrics(reply: MetricResult, machines: GpuMachine[], blockMs: number): ChartPoint[] {
  if (reply.results.A.error || reply.results.B.error) throw new Error('GPU history query failed')
  const values = new Map<number, { allocated: Map<string, number>; total: Map<string, number> }>()
  for (const [key, field] of [['A', 'allocated'], ['B', 'total']] as const) {
    for (const frame of reply.results[key].frames) {
      const node = frame.schema.fields[1]?.labels?.node
      if (!node || !Array.isArray(frame.data.values[0]) || !Array.isArray(frame.data.values[1])) continue
      const [times, samples] = frame.data.values
      for (let index = 0; index < times.length; index++) {
        const time = times[index]
        const value = samples[index]
        if (!Number.isFinite(time) || !Number.isFinite(value)) continue
        if (!values.has(time)) values.set(time, { allocated: new Map(), total: new Map() })
        values.get(time)![field].set(node, value)
      }
    }
  }
  const result: ChartPoint[] = []
  for (const [time, sample] of [...values].sort(([left], [right]) => left - right)) {
    const point: ChartPoint = { time, idle: 0 }
    let valid = true
    let capacitySum = 0
    for (const machine of machines) {
      const allocated = sample.allocated.get(machine.name)
      const total = sample.total.get(machine.name)
      if (allocated === undefined && total === undefined) {
        point[machine.name] = 0
        continue
      }
      if (allocated === undefined || total === undefined || allocated < 0 || total < allocated) {
        valid = false
        break
      }
      point[machine.name] = allocated
      point.idle += total - allocated
      capacitySum += total
    }
    if (valid && capacitySum > 0) result.push(point)
  }
  return averageBlocks(result, [...machines.map(machine => machine.name), 'idle'], blockMs)
}

/** Name the block that a tooltip value averages. */
function blockLabel(start: number, range: RangeKey): string {
  const time = (value: number) => new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  return `${new Date(start).toLocaleDateString()} ${time(start)}\u2013${time(start + ranges[range].blockMs)} (${ranges[range].block} average)`
}

/** Show 24 hours, 7 days, or 30 days of Slurm allocations as stacked machine and idle GPU capacity. */
export function GpuAllocationChart({ nodes, refreshSeconds, failed, demo }: { nodes: GpuMachine[] | null; refreshSeconds: number; failed: boolean; demo?: boolean }) {
  const machines = (nodes ?? []).filter(node => node.role === 'Compute' && Number.isFinite(node.total_gpus) && (node.total_gpus ?? 0) > 0)
    .sort((left, right) => left.name.localeCompare(right.name))
  const totalCapacity = machines.reduce((sum, machine) => sum + (machine.total_gpus ?? 0), 0)
  const names = machines.map(machine => machine.name).join('\u0000')
  // The first machine uses the theme accent, then the theme's card colors, skipping any equal to the accent.
  const theme = useThemeColors()
  const colors = [theme.accent, ...theme.cards.filter(card => card.toLowerCase() !== theme.accent.toLowerCase()), ...extraColors]
  const [range, setRange] = useState<RangeKey>(storedRange)
  const [chart, setChart] = useState<{ range: RangeKey; points: ChartPoint[]; from: number; to: number } | null>(null)
  const [state, setState] = useState<'loading' | 'ready' | 'failed'>('loading')
  const choose = (next: RangeKey) => { storeRange(next); setRange(next) }
  useEffect(() => {
    if (!machines.length) return
    setState('loading')
    if (demo) {
      const to = Date.now()
      const from = to - ranges[range].windowMs
      const capacity = totalCapacity
      const stages: [number, number][] = [
        [0, 0.2], [0.1, 0.4], [0.18, 0.7],
        [0.25, 1], [0.29, 1], [0.34, 1], [0.38, 1],
        [0.46, 0.5], [0.5, 0.5], [0.55, 0.5],
        [0.63, 1], [0.67, 1], [0.72, 1], [0.76, 1], [0.84, 0.6],
        [0.9, 0.2], [1, 0.2],
      ]
      const points = stages.map(([position, fraction]) => {
        const point: ChartPoint = { time: from + position * ranges[range].windowMs, idle: 0 }
        const allocation = machines.map(() => 0)
        const used = Math.round(capacity * fraction)
        let machineIndex = 0
        for (let gpu = 0; gpu < used; gpu++) {
          while (allocation[machineIndex] >= (machines[machineIndex].total_gpus ?? 0)) machineIndex = (machineIndex + 1) % machines.length
          allocation[machineIndex]++
          machineIndex = (machineIndex + 1) % machines.length
        }
        machines.forEach((machine, index) => { point[machine.name] = allocation[index] })
        point.idle = capacity - used
        return point
      })
      setChart({ range, points, from, to })
      setState('ready')
      return
    }
    const controller = new AbortController()
    const load = () => {
      const end = new Date()
      const from = end.getTime() - ranges[range].windowMs
      const body = {
        from: new Date(from).toISOString(), to: end.toISOString(),
        queries: ['cluster_node_allocated_gpus', 'cluster_node_total_gpus'].map((expr, index) => ({
          refId: index ? 'B' : 'A', datasource: { uid: 'cluster-detail', type: 'prometheus' },
          expr, instant: false, range: true, intervalMs: 300000, maxDataPoints: 9000,
        })),
      }
      fetch('grafana/api/ds/query', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body), cache: 'no-store', signal: controller.signal,
      }).then(response => {
        if (!response.ok) throw new Error('GPU history unavailable')
        return response.json() as Promise<MetricResult>
      }).then(reply => {
        setChart({ range, points: pointsFromMetrics(reply, machines, ranges[range].blockMs), from, to: end.getTime() })
        setState('ready')
      }).catch(() => { if (!controller.signal.aborted) setState('failed') })
    }
    load()
    const timer = window.setInterval(load, Math.max(30, refreshSeconds) * 1000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [names, totalCapacity, refreshSeconds, range, demo])
  const title = `Cluster Usage (${ranges[range].title})`
  // Until the chosen range has loaded, show loading instead of the previous range's data.
  const current = chart?.range === range ? chart : null
  const points = current?.points ?? []
  return <section className="panel gpu-allocation-chart" aria-label={title}>
    <div className="panel-heading gpu-chart-heading">
      <a className="panel-link gpu-chart-title" href="#usage"><h2>{title}</h2></a>
      <div className="gpu-chart-ranges" role="group" aria-label="Time range">
        {(Object.keys(ranges) as RangeKey[]).map(key => <button key={key} type="button" aria-pressed={key === range} onClick={() => choose(key)}>{key}</button>)}
      </div>
      <a className="panel-link panel-link-arrow" href="#usage" aria-hidden="true" tabIndex={-1}>→</a>
    </div>
    <div className="gpu-chart-body">
      {failed || state === 'failed' ? <p role="alert">GPU history unavailable</p> : !nodes ? <p role="status">Loading GPU history</p> : !machines.length ? <p>No GPU machines</p> : state === 'loading' || !current ? <p role="status">Loading GPU history</p> : !points.length ? <p>No GPU history yet</p> :
        <div className="gpu-chart-plot" role="img" aria-label={`Slurm GPUs allocated by machine over the last ${ranges[range].title}, with idle capacity`}>
          <div className="gpu-chart-svg"><ResponsiveContainer width="100%" height="100%">
            <AreaChart data={points} margin={{ top: 8, right: 8, bottom: 8, left: 8 }}>
              <XAxis dataKey="time" type="number" domain={[current.from, current.to]} hide />
              {demo && <YAxis domain={[0, totalCapacity]} hide />}
              <CartesianGrid vertical={false} stroke="#deded8" />
              <Tooltip labelFormatter={(_, payload) => demo ? new Date(Number(payload[0]?.payload.time)).toLocaleString() : blockLabel(Number(payload[0]?.payload.time), range)} formatter={(value, name) => { const gpus = demo ? Math.round(Number(value)) : Number(Number(value).toFixed(1)); return [`${gpus} GPU${gpus === 1 ? '' : 's'}`, name] }} contentStyle={{ background: '#fff', border: '3px solid #172322', borderRadius: 12, boxShadow: '4px 4px 0 #172322', color: '#172322' }} itemStyle={{ color: '#172322' }} />
              {machines.map((machine, index) => <Area key={machine.name} dataKey={machine.name} name={machine.name} type={demo ? 'basis' : 'monotone'} stackId="gpus" stroke="#172322" strokeWidth={2} strokeLinejoin="round" fill={colors[index % colors.length]} isAnimationActive={false} />)}
              <Area dataKey="idle" name="Idle" type={demo ? 'basis' : 'monotone'} stackId="gpus" stroke="none" fill="#fff" isAnimationActive={false} />
            </AreaChart>
          </ResponsiveContainer></div>
        </div>}
    </div>
  </section>
}
