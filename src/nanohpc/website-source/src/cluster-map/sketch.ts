// Isometric cluster map, ported from ~/code/animation-cluster (p5.js global mode) to p5 instance mode.
// Machines come from the monitoring snapshot instead of machines.yaml, and every motion shows a measurement:
// dots on a link are shared-home (NFS) requests between that machine and the front node (1 dot = 10 requests;
// writes orange toward the front node, reads and file-handling blue toward the machine), and each GPU light shows
// Slurm allocation, the job's user, and measured busy %. Pan, zoom, rotation, and the YAML picker are left out.
import p5 from 'p5'
import { mapAreas } from './areas'

/** One machine as the snapshot reports it. */
export type MapMachine = {
  mode?: 'slurm' | 'monitor'
  name: string
  front: boolean
  building: string | null
  partitions: string[]
  health: string
  gpuModel: string | null
  fpgaUsagePercent: number | null
  totalGpus: number
  allocatedGpus: number
  runningJobs: number
  pendingJobs: number
  /** Measured shared-home requests per second, and the writes among them; null when not measured. */
  nfsRequests: number | null
  nfsWrites: number | null
  /** One entry per GPU: the user of the job it is allocated to ('' when unknown), or null when free, and busy %. */
  gpus: { user: string | null; busy: number | null }[]
}

export type LayoutName = 'default' | 'partitions' | 'geographic'

export type ClusterMap = {
  update: (machines: MapMachine[], stale: boolean) => void
  setLayout: (name: LayoutName) => void
  remove: () => void
}

type Node = MapMachine & {
  order: number
  group: 'front' | 'compute'
  x: number | null
  y: number | null
  tx: number
  ty: number
  scale: number
  load: number
  phase: number
  tier: number
  neighbors: Set<Node>
  out: number
  in: number
}
type Point = { x: number; y: number }
type Link = { from: Node; to: Node; pts: Point[]; segs: number[]; total: number; dueIn: number; dueOut: number }
type Packet = { link: Link; d: number; dir: 1 | -1; kind: 'forward' | 'back'; size: number; done?: boolean }
type Piece = { group: Node['group']; label: string; members: Node[] }

// Website style: dark ink borders and hard offset shadows, flat fills, the theme accent (read each frame).
const INK = '#172322'
const MUTED = '#65716c'
const COMPUTE_AREA = '#f0f2ee'
// No spaces, so p5 does not quote the list and the fallbacks still apply.
const MONO = 'ui-monospace,SFMono-Regular,Consolas,monospace'

// Tile size in world units; true isometric (30°).
const TW = 100
const TH = TW * Math.tan(Math.PI / 6)
// A drawn server: half its footprint in tiles, the height of one layer in world units, and its face colors.
// Compute machines have one layer per GPU; the front node has FRONT_LAYERS.
const SERVER_HALF = 0.42
const LAYER_HEIGHT = 20
const FRONT_LAYERS = 3
const SIDE_LEFT = '#9aaecb'
const SIDE_RIGHT = '#6885a9'
const NODE_GAP = 2
const GROUP_GAP = 4
const TIER_GAP = 6
// Largest zoom, margins around the machines in pixels (the top leaves room for the layout buttons),
// and half the width of a machine label in world units.
const MAX_ZOOM = 0.8
const MARGIN = 16
const TOP_MARGIN = 44
const LABEL_HALF_WIDTH = 60
const LABEL_SHADOW = 3
const LABEL_GAP = 6
const SPREAD = 1.1
const BLOCK_GAP = 3
// A quarter of the original's speeds, in world units per second.
const FORWARD_SPEED = 85
const BACK_SPEED = 65
// Shared-home requests one dot stands for.
const REQUESTS_PER_DOT = 10
const DOT_GAP = 30
// Job colors on the GPU lights, one per user.
// The Overview chart's machine palette.
const USER_COLORS = ['#5294ff', '#ff8a67', '#a184f5', '#42b883', '#f7ce46', '#ed77b5', '#65b7c1', '#b9d064']

/** Start the map in `container`; the canvas follows the container's size. */
export function createClusterMap(container: HTMLElement, layout: LayoutName, maxHeight: number | null): ClusterMap {
  let nodes: Node[] = []
  let links: Link[] = []
  let pieces: Piece[] = []
  let packets: Packet[] = []
  let pulses: { node: Node; kind: Packet['kind']; t: number }[] = []
  let time = 0
  let stale = false
  let layoutName = layout
  let cam = { x: 0, y: 0, z: 1 }
  let camTarget: typeof cam | null = null
  let heightTarget: number | null = null
  let hovered: Node | null = null
  let accent = '#5294ff'
  let writeColor = '#ffd166'
  let healthColors = { green: '#06d6a0', yellow: '#ffd166' }
  let font = 'Arial'
  let sketch: p5

  const iso = (tx: number, ty: number): Point => ({ x: (tx - ty) * TW / 2, y: (tx + ty) * TH / 2 })
  const depth = (n: Node) => (n.x ?? 0) + (n.y ?? 0)
  const isSource = (n: Node) => n.out > 0 && n.in === 0

  // ---------------------------------------------------------------- scene

  function buildScene(machines: MapMachine[]) {
    const previous = new Map(nodes.map(n => [n.name, n]))
    const next: Node[] = machines.map((m, order) => {
      const old = previous.get(m.name)
      return {
        ...m, order, group: m.front ? 'front' : 'compute',
        x: old ? old.x : null, y: old ? old.y : null, tx: 0, ty: 0,
        scale: 1, load: old ? old.load : 0, phase: old ? old.phase : Math.random() * Math.PI * 2,
        tier: 0, neighbors: new Set(), out: 0, in: 0,
      }
    })
    const oldLinks = new Map(links.map(l => [`${l.from.name}\u0000${l.to.name}`, l]))
    const nextLinks: Link[] = []
    for (const from of next.filter(n => n.front)) {
      for (const to of next.filter(n => !n.front)) {
        from.out++
        to.in++
        from.neighbors.add(to)
        to.neighbors.add(from)
        const old = oldLinks.get(`${from.name}\u0000${to.name}`)
        nextLinks.push({ from, to, pts: [], segs: [], total: 0, dueIn: old ? old.dueIn : 0, dueOut: old ? old.dueOut : 0 })
      }
    }
    // Machines that only send work (front nodes) are drawn larger.
    for (const n of next) {
      n.scale = isSource(n) ? 1.45 : 1
      n.tier = n.front || !next.some(m => m.front) ? 0 : 1
    }
    const kept = new Set(nextLinks.map(l => `${l.from.name}\u0000${l.to.name}`))
    const keep = (l: Link) => kept.has(`${l.from.name}\u0000${l.to.name}`)
    const relink = (l: Link) => nextLinks.find(n => n.from.name === l.from.name && n.to.name === l.to.name)!
    packets = packets.filter(p => keep(p.link)).map(p => ({ ...p, link: relink(p.link) }))
    const names = new Set(next.map(n => n.name))
    pulses = pulses.filter(p => names.has(p.node.name)).map(p => ({ ...p, node: next.find(n => n.name === p.node.name)! }))
    const changed = next.length !== nodes.length || next.some(n => !previous.has(n.name) || n.building !== previous.get(n.name)?.building || n.partitions.join(',') !== previous.get(n.name)?.partitions.join(','))
    nodes = next
    links = nextLinks
    hovered = null
    if (changed || !pieces.length) applyLayout(!nodes.some(n => n.x !== null))
    else placeMachines()
  }

  // ---------------------------------------------------------------- layouts

  const areaPieces = (name: LayoutName): Piece[] => mapAreas(nodes, name).map(area => ({ group: area.front ? 'front' : 'compute', label: area.label, members: area.members }))

  // Offsets along a line for a list of runs, centered on 0.
  function stackLine(runs: Piece[]): [Node, number][] {
    const out: [Node, number][] = []
    let cursor = 0
    let prev: Node | null = null
    runs.forEach((run, ri) => run.members.forEach((n, i) => {
      if (prev) {
        const step = Math.max(NODE_GAP, Math.ceil(prev.scale + n.scale))
        cursor += i === 0 && ri > 0 ? step + (GROUP_GAP - NODE_GAP) : step
      }
      out.push([n, cursor])
      prev = n
    }))
    const mid = out.length ? (out[0][1] + out[out.length - 1][1]) / 2 : 0
    return out.map(([n, o]) => [n, o - mid])
  }

  // Columns left to right by tier; inside a column, one line grouped by group.
  function layoutDefault(): Piece[] {
    const out: Piece[] = []
    const areas = areaPieces('default')
    for (let t = 0; t <= Math.max(...nodes.map(n => n.tier)); t++) {
      const runs = areas.filter(area => area.members[0].tier === t)
      for (const [n, off] of stackLine(runs)) { n.tx = t * TIER_GAP; n.ty = off }
      out.push(...runs)
    }
    return out
  }

  // Front nodes in the middle; each partition or building takes one side.
  function layoutSides(name: 'partitions' | 'geographic'): Piece[] {
    const areas = areaPieces(name)
    const center = areas.filter(area => area.group === 'front')
    const centerLine = stackLine(center)
    for (const [n, off] of centerLine) { n.tx = 0; n.ty = off }
    const out: Piece[] = [...center]
    const lines = areas.filter(area => area.group !== 'front').map(area => ({ runs: [area], offs: stackLine([area]) }))
    if (!lines.length) return out
    const half = (offs: [Node, number][]) => (offs.length ? offs[offs.length - 1][1] : 0)
    const centerHalf = Math.max(0, ...centerLine.map(([, o]) => Math.abs(o)))
    const reach = Math.max(5, ...lines.map(l => half(l.offs) + 3), centerHalf + 4)
    const dirs = [[1, 0], [0, 1], [-1, 0], [0, -1]]
    lines.forEach((l, s) => {
      const [dx, dy] = dirs[s % 4]
      const distance = reach * (1 + Math.floor(s / 4))
      for (const [n, off] of l.offs) {
        n.tx = dx * distance + (dx === 0 ? off : 0)
        n.ty = dy * distance + (dy === 0 ? off : 0)
      }
      out.push(...l.runs)
    })
    return out
  }

  /** Put each partition in a compact block and keep the front node in its own tier. */
  function layoutPartitions(): Piece[] {
    const out: Piece[] = []
    const areas = areaPieces('partitions')
    let x = 0
    for (let t = 0; t <= Math.max(...nodes.map(n => n.tier)); t++) {
      const runs = areas.filter(a => a.members[0].tier === t)
      if (!runs.length) continue
      const blocks = runs.map(r => {
        const cols = Math.ceil(Math.sqrt(r.members.length))
        const rows = Math.ceil(r.members.length / cols)
        const step = Math.max(BLOCK_GAP, ...r.members.map(n => Math.ceil(n.scale * 2)))
        return { r, cols, step, w: (rows - 1) * step, h: (cols - 1) * step }
      })
      const colWidth = Math.max(...blocks.map(b => b.w))
      let y = -(blocks.reduce((sum, b) => sum + b.h, 0) + GROUP_GAP * (blocks.length - 1)) / 2
      for (const b of blocks) {
        b.r.members.forEach((n, i) => {
          n.tx = x + (colWidth - b.w) / 2 + Math.floor(i / b.cols) * b.step
          n.ty = y + (i % b.cols) * b.step
        })
        y += b.h + GROUP_GAP
      }
      out.push(...runs)
      x += colWidth + TIER_GAP
    }
    return out
  }

  const layouts: Record<LayoutName, () => Piece[]> = { default: layoutDefault, partitions: layoutPartitions, geographic: () => layoutSides('geographic') }

  /** Spread a layout until every name box clears the other names and servers. */
  function placeMachines() {
    readPageFont()
    pieces = layouts[layoutName]()
    for (let step = 0; step < 40 && labelsOverlap(); step++) for (const n of nodes) { n.tx *= SPREAD; n.ty *= SPREAD }
    container.dataset.labelBoxes = JSON.stringify(nodes.map(n => ({ name: n.name, ...withShadow(labelBox(n, n.tx, n.ty)), server: serverOutline(n) })))
    container.dataset.pipes = JSON.stringify(links.map(link => ({ from: link.from.name, to: link.to.name, tiles: routeTiles(link.from, link.to, destination) })))
  }

  function labelsOverlap(): boolean {
    type Box = { x: number; y: number; w: number; h: number }
    const near = (a: Box, b: Box) => a.x < b.x + b.w + LABEL_GAP && b.x < a.x + a.w + LABEL_GAP && a.y < b.y + b.h + LABEL_GAP && b.y < a.y + a.h + LABEL_GAP
    const boxes = nodes.map(n => withShadow(labelBox(n, n.tx, n.ty)))
    const servers = nodes.map(serverOutline)
    return boxes.some((a, i) => boxes.some((b, j) => i !== j && (near(a, b) || near(a, servers[j]))))
  }

  function serverOutline(n: Node) {
    const b = serverAt(n, n.tx, n.ty)
    return { x: b.x, y: b.y, w: b.w, h: b.h }
  }

  const withShadow = (box: { x: number; y: number; w: number; h: number }) => ({ ...box, w: box.w + LABEL_SHADOW, h: box.h + LABEL_SHADOW })

  function readPageFont() {
    font = getComputedStyle(container).fontFamily.split(',')[0].trim().replace(/["']/g, '')
  }

  function applyLayout(instant: boolean) {
    if (!nodes.length) { pieces = []; return }
    placeMachines()
    for (const n of nodes) if (instant || n.x === null) { n.x = n.tx; n.y = n.ty }
    fitView(!instant)
  }

  // Extent of what is drawn, in world units, where machines are heading:
  // group areas, servers, and the labels above them.
  function sceneBounds() {
    const xs: number[] = []
    const ys: number[] = []
    for (const pc of pieces) {
      const pad = Math.max(1, ...pc.members.map(n => n.scale))
      const x0 = Math.min(...pc.members.map(n => n.tx)) - pad, x1 = Math.max(...pc.members.map(n => n.tx)) + pad
      const y0 = Math.min(...pc.members.map(n => n.ty)) - pad, y1 = Math.max(...pc.members.map(n => n.ty)) + pad
      for (const c of [iso(x0, y0), iso(x1, y0), iso(x1, y1), iso(x0, y1)]) { xs.push(c.x); ys.push(c.y) }
    }
    for (const n of nodes) {
      const b = serverAt(n, n.tx, n.ty)
      xs.push(Math.min(b.x, b.cx - LABEL_HALF_WIDTH), Math.max(b.x + b.w, b.cx + LABEL_HALF_WIDTH))
      ys.push(b.y - (n.front ? 30 : 42) - 14, b.y + b.h)
    }
    return { x0: Math.min(...xs), x1: Math.max(...xs), y0: Math.min(...ys), y1: Math.max(...ys) }
  }

  // Draw machines at a readable size, smaller only when the cluster is wider than the page,
  // and make the map as tall as the machines plus a margin.
  function fitView(animate: boolean) {
    if (!nodes.length || !sketch) return
    const b = sceneBounds()
    const top = maxHeight === null ? TOP_MARGIN : 34
    const margin = maxHeight === null ? MARGIN : 8
    const z = Math.min((sketch.width - margin * 2) / (b.x1 - b.x0), MAX_ZOOM, maxHeight === null ? Infinity : (maxHeight - top - margin) / (b.y1 - b.y0))
    const height = Math.ceil((b.y1 - b.y0) * z + top + margin)
    const target = { z, x: sketch.width / 2 - ((b.x0 + b.x1) / 2) * z, y: top - b.y0 * z }
    if (animate) { camTarget = target; heightTarget = height }
    else { cam = target; camTarget = null; heightTarget = null; setHeight(height) }
  }

  function setHeight(height: number) {
    if (Math.abs(height - sketch.height) < 0.5) return
    container.style.height = `${height}px`
    sketch.resizeCanvas(sketch.width, height)
  }

  // Colored areas follow their machines' current positions.
  function currentRegions() {
    return pieces.map(pc => {
      const pad = Math.max(1, ...pc.members.map(n => n.scale))
      return {
        label: pc.members[0]?.mode === 'monitor' ? (pc.group === 'front' ? 'MONITOR' : 'MACHINES') : pc.label, group: pc.group,
        from: [Math.min(...pc.members.map(n => n.x!)) - pad, Math.min(...pc.members.map(n => n.y!)) - pad],
        to: [Math.max(...pc.members.map(n => n.x!)) + pad, Math.max(...pc.members.map(n => n.y!)) + pad],
      }
    })
  }

  // ---------------------------------------------------------------- links

  type Tile = [number, number]
  const current = (n: Node): Tile => [n.x!, n.y!]
  const destination = (n: Node): Tile => [n.tx, n.ty]

  /** Keep Default and Geographic pipes, and use a shared trunk in Partitions. */
  function routeTiles(a: Node, b: Node, at: (node: Node) => Tile): Tile[] {
    if (layoutName === 'partitions') return trunkTiles(a, b, at)
    const [ax, ay] = at(a), [bx, by] = at(b)
    if (Math.abs(ax - bx) < 1e-6 || Math.abs(ay - by) < 1e-6) return [[ax, ay], [bx, by]]
    const mx = (ax + bx) / 2
    return [[ax, ay], [mx, ay], [mx, by], [bx, by]]
  }

  /** Branch each machine from one lane shared by its partition area. */
  function trunkTiles(a: Node, b: Node, at: (node: Node) => Tile): Tile[] {
    const [ax, ay] = at(a), [bx, by] = at(b)
    const spine = (ax + Math.min(...nodes.filter(node => !node.front).map(node => at(node)[0]))) / 2
    const area = pieces.find(piece => piece.members.includes(b))?.members ?? [b]
    const columns = [...new Set(area.map(node => Math.round(at(node)[1] * 1000) / 1000))].sort((p, q) => p - q)
    const middle = Math.max(0, Math.floor((columns.length - 1) / 2))
    const lane = columns.length > 1 ? (columns[middle] + columns[middle + 1]) / 2 : by
    const tiles: Tile[] = [[ax, ay], [spine, ay], [spine, lane], [bx, lane], [bx, by]]
    return tiles.filter((tile, index) => index === 0 || Math.abs(tile[0] - tiles[index - 1][0]) > 1e-6 || Math.abs(tile[1] - tiles[index - 1][1]) > 1e-6)
  }

  function updateLinkGeometry() {
    for (const l of links) {
      l.pts = routeTiles(l.from, l.to, current).map(([x, y]) => iso(x, y))
      l.segs = []
      l.total = 0
      for (let i = 0; i < l.pts.length - 1; i++) {
        const len = Math.hypot(l.pts[i + 1].x - l.pts[i].x, l.pts[i + 1].y - l.pts[i].y)
        l.segs.push(len)
        l.total += len
      }
    }
  }

  function pointAlong(link: Link, d: number): Point {
    for (let i = 0; i < link.segs.length; i++) {
      if (d <= link.segs[i] || i === link.segs.length - 1) {
        const t = link.segs[i] ? Math.min(1, Math.max(0, d / link.segs[i])) : 0
        return { x: link.pts[i].x + (link.pts[i + 1].x - link.pts[i].x) * t, y: link.pts[i].y + (link.pts[i + 1].y - link.pts[i].y) * t }
      }
      d -= link.segs[i]
    }
    return link.pts[0]
  }

  // ---------------------------------------------------------------- simulation

  // Dots follow measured shared-home requests: reads and file-handling toward the machine, writes back.
  function simulate(dt: number) {
    time += dt
    for (const p of pulses) p.t += dt
    pulses = pulses.filter(p => p.t < 1)
    for (const l of links) {
      const writes = stale ? 0 : l.to.nfsWrites ?? 0
      const others = stale ? 0 : Math.max(0, (l.to.nfsRequests ?? 0) - writes)
      l.dueIn = others ? l.dueIn + dt * others / REQUESTS_PER_DOT : 0
      l.dueOut = writes ? l.dueOut + dt * writes / REQUESTS_PER_DOT : 0
      const lastStart = (dir: Packet['dir']) => Math.min(Infinity, ...packets.filter(p => p.link === l && p.dir === dir).map(p => p.d))
      if (l.dueIn >= 1 && lastStart(1) >= DOT_GAP) { packets.push({ link: l, d: 0, dir: 1, kind: 'forward', size: 6 }); l.dueIn-- }
      if (l.dueOut >= 1 && lastStart(-1) >= DOT_GAP) { packets.push({ link: l, d: 0, dir: -1, kind: 'back', size: 6 }); l.dueOut-- }
      l.dueIn = Math.min(l.dueIn, 1)
      l.dueOut = Math.min(l.dueOut, 1)
    }
    for (const p of packets) {
      p.d += dt * (p.kind === 'forward' ? FORWARD_SPEED : BACK_SPEED)
      if (p.d >= p.link.total) p.done = true
    }
    // The front node shows one ring at a time, so steady traffic does not stack rings into a glow.
    for (const p of packets.filter(p => p.done)) {
      const node = p.dir === 1 ? p.link.to : p.link.from
      if (node.front && pulses.some(pulse => pulse.node === node)) continue
      pulses.push({ node, kind: p.kind, t: 0 })
    }
    packets = packets.filter(p => !p.done)
    // The bar shows allocation in Slurm mode and measured activity in monitor mode.
    for (const n of nodes) {
      const target = n.fpgaUsagePercent !== null ? n.fpgaUsagePercent / 100 : n.front || !n.totalGpus ? 0 : n.mode === 'monitor'
        ? n.gpus.reduce((sum, gpu) => sum + (gpu.busy ?? 0), 0) / n.totalGpus / 100
        : n.allocatedGpus / n.totalGpus
      n.load += (target - n.load) * Math.min(1, dt * 2)
    }
  }

  function animateView(dt: number) {
    const k = Math.min(1, dt * 6)
    for (const n of nodes) {
      n.x = Math.abs(n.tx - n.x!) < 0.001 ? n.tx : n.x! + (n.tx - n.x!) * k
      n.y = Math.abs(n.ty - n.y!) < 0.001 ? n.ty : n.y! + (n.ty - n.y!) * k
    }
    if (camTarget) {
      cam = { x: cam.x + (camTarget.x - cam.x) * k, y: cam.y + (camTarget.y - cam.y) * k, z: cam.z + (camTarget.z - cam.z) * k }
      if (Math.abs(cam.z - camTarget.z) < 0.001 && Math.abs(cam.x - camTarget.x) < 0.5) camTarget = null
    }
    if (heightTarget !== null) {
      const next = Math.abs(heightTarget - sketch.height) < 1 ? heightTarget : sketch.height + (heightTarget - sketch.height) * k
      setHeight(Math.round(next))
      if (next === heightTarget) heightTarget = null
    }
  }

  // ---------------------------------------------------------------- drawing

  // A server standing on its tile: ground corners (north, east, south, west), its layers, and its screen box.
  function serverAt(n: Node, x: number, y: number) {
    const a = SERVER_HALF * n.scale
    const layers = n.front ? FRONT_LAYERS : Math.max(1, n.gpus.length)
    const lh = LAYER_HEIGHT * n.scale
    const height = layers * lh
    const N = iso(x - a, y - a), E = iso(x + a, y - a), S = iso(x + a, y + a), W = iso(x - a, y + a)
    const c = iso(x, y)
    return { N, E, S, W, layers, lh, x: W.x, y: N.y - height, w: E.x - W.x, h: S.y - N.y + height, cx: c.x, topCy: c.y - height }
  }

  const nodeBox = (n: Node) => serverAt(n, n.x!, n.y!)

  function draw(p: p5) {
    const dt = Math.min(p.deltaTime / 1000, 0.05)
    simulate(dt)
    animateView(dt)
    updateLinkGeometry()
    updateHover(p)
    // Transparent, so the page background shows through.
    p.clear()
    const theme = getComputedStyle(document.documentElement)
    accent = theme.getPropertyValue('--accent').trim() || accent
    const [second, third] = ['--card-2', '--card-3'].map(name => theme.getPropertyValue(name).trim())
    writeColor = (second.toLowerCase() === accent.toLowerCase() ? third : second) || writeColor
    healthColors = { green: theme.getPropertyValue('--status-green').trim() || healthColors.green, yellow: theme.getPropertyValue('--status-yellow').trim() || healthColors.yellow }
    readPageFont()
    if (!nodes.length) return
    const regions = currentRegions()
    p.push()
    p.translate(cam.x, cam.y)
    p.scale(cam.z)
    for (const r of regions) drawRegion(p, r)
    for (const pulse of pulses) drawPulse(p, pulse)
    for (const l of links) drawLink(p, l)
    for (const packet of packets) drawPacket(p, packet)
    for (const r of regions) drawRegionLabel(p, r)
    for (const n of [...nodes].sort((a, b) => depth(a) - depth(b))) drawNode(p, n)
    for (const n of nodes) drawLabel(p, n)
    p.pop()
    drawStale(p)
    drawTooltip(p)
  }

  type Region = ReturnType<typeof currentRegions>[number]
  const regionCorners = (r: Region) => [iso(r.from[0], r.from[1]), iso(r.to[0], r.from[1]), iso(r.to[0], r.to[1]), iso(r.from[0], r.to[1])]

  const dotColor = (kind: Packet['kind']) => (kind === 'forward' ? accent : writeColor)

  // A box like the website's cards: white, dark border, hard offset shadow.
  function drawBox(p: p5, x: number, y: number, w: number, h: number, border: number, shadow: number, radius: number, alpha: number) {
    p.noStroke()
    p.fill(23, 35, 34, alpha)
    p.rect(x + shadow, y + shadow, w, h, radius)
    p.fill(255, alpha)
    p.stroke(23, 35, 34, alpha)
    p.strokeWeight(border)
    p.rect(x, y, w, h, radius)
  }

  function drawRegion(p: p5, r: Region) {
    const fill = p.color(r.group === 'front' ? accent : COMPUTE_AREA)
    if (r.group === 'front') fill.setAlpha(70)
    p.fill(fill)
    p.stroke(INK)
    p.strokeWeight(2)
    const [a, b, c, d] = regionCorners(r)
    p.quad(a.x, a.y, b.x, b.y, c.x, c.y, d.x, d.y)
  }

  // Label along the area's lower-left edge, in monospace like the website's small labels.
  function drawRegionLabel(p: p5, r: Region) {
    const corners = regionCorners(r)
    const bi = corners.reduce((best, c, i) => (c.y > corners[best].y ? i : best), 0)
    const n1 = corners[(bi + 1) % 4]
    const n2 = corners[(bi + 3) % 4]
    const start = n1.x < n2.x ? n1 : n2
    const end = corners[bi]
    p.push()
    p.translate(start.x, start.y)
    p.rotate(Math.atan2(end.y - start.y, end.x - start.x))
    p.noStroke()
    p.fill(INK)
    p.textFont(MONO)
    p.textStyle(p.NORMAL)
    p.textSize(11)
    p.textAlign(p.LEFT, p.TOP)
    p.text(r.label, 10, 6)
    p.pop()
  }

  function drawPulse(p: p5, pulse: { node: Node; kind: Packet['kind']; t: number }) {
    const c = iso(pulse.node.x!, pulse.node.y!)
    const col = p.color(accent)
    col.setAlpha(200 * (1 - pulse.t))
    p.noFill()
    p.stroke(col)
    p.strokeWeight(2)
    const r = (0.5 + pulse.t * 0.9) * pulse.node.scale
    p.ellipse(c.x, c.y, TW * r * 1.4, TH * r * 1.4)
  }

  // A white rail with a dark outline; dark dashes move only while shared-home requests flow.
  function drawLink(p: p5, l: Link) {
    const ctx = p.drawingContext as CanvasRenderingContext2D
    const hot = hovered !== null && (hovered === l.to || hovered === l.from)
    const dim = hovered !== null && !hot
    const shape = (color: p5.Color | string, weight: number) => {
      p.stroke(typeof color === 'string' ? p.color(color) : color)
      p.strokeWeight(weight)
      p.beginShape()
      for (const pt of l.pts) p.vertex(pt.x, pt.y)
      p.endShape()
    }
    p.noFill()
    p.strokeCap(p.SQUARE)
    p.strokeJoin(p.MITER)
    shape(dim ? p.color(23, 35, 34, 90) : INK, 11)
    shape('#ffffff', 7)
    const requests = stale ? 0 : l.to.nfsRequests ?? 0
    const speed = requests ? 10 + Math.min(30, requests / REQUESTS_PER_DOT * 3) : 0
    ctx.setLineDash([10, 10])
    ctx.lineDashOffset = -(time * speed) % 20
    shape(hot ? accent : dim ? p.color(23, 35, 34, 90) : INK, 2)
    ctx.setLineDash([])
    ctx.lineDashOffset = 0
  }

  function drawPacket(p: p5, packet: Packet) {
    const d = packet.dir === 1 ? packet.d : packet.link.total - packet.d
    const pos = pointAlong(packet.link, d)
    p.fill(dotColor(packet.kind))
    p.stroke(INK)
    p.strokeWeight(1.5)
    p.circle(pos.x, pos.y, packet.size * 1.6)
  }

  // An isometric stack: blue-grey sides, the top face in the theme accent, dark outlines.
  // Each compute layer is one GPU with its light: dark when free; lit in the job's user color when allocated,
  // glowing brighter and pulsing faster with the GPU's measured busy %. GPU 0 is the top layer.
  function drawNode(p: p5, n: Node) {
    const b = nodeBox(n)
    const alpha = hovered !== null && hovered !== n && !hovered.neighbors.has(n) ? 110 : 255
    const tone = (color: string) => { const c = p.color(color); c.setAlpha(alpha); return c }
    const up = (pt: Point, dy: number) => ({ x: pt.x, y: pt.y - dy })
    const face = (points: Point[], color: string) => {
      p.fill(tone(color))
      p.quad(points[0].x, points[0].y, points[1].x, points[1].y, points[2].x, points[2].y, points[3].x, points[3].y)
    }
    p.stroke(tone(INK))
    p.strokeWeight(1.5)
    for (let k = 0; k < b.layers; k++) {
      const bottom = k * b.lh, top = (k + 1) * b.lh
      face([up(b.W, top), up(b.S, top), up(b.S, bottom), up(b.W, bottom)], SIDE_LEFT)
      face([up(b.S, top), up(b.E, top), up(b.E, bottom), up(b.S, bottom)], SIDE_RIGHT)
      // A point on this layer's left face: u along the front edge, v down from the layer's top.
      const at = (u: number, v: number) => ({ x: b.W.x + (b.S.x - b.W.x) * u, y: b.W.y + (b.S.y - b.W.y) * u - top + v * b.lh })
      const slotStart = at(0.36, 0.5), slotEnd = at(0.86, 0.5)
      p.stroke(tone(INK))
      p.line(slotStart.x, slotStart.y, slotEnd.x, slotEnd.y)
      const gpu = n.gpus[b.layers - 1 - k]
      if (gpu) {
        const light = at(0.18, 0.5)
        if (n.mode === 'monitor') {
          const busy = (gpu.busy ?? 0) / 100
          const color = p.color(gpu.busy === null ? '#475569' : accent)
          color.setAlpha(alpha * (0.3 + 0.7 * busy))
          p.fill(color)
        } else if (gpu.user === null) {
          p.fill(tone('#475569'))
        } else {
          const color = p.color(gpu.user ? userColor(gpu.user) : '#94a3b8')
          const busy = (gpu.busy ?? 0) / 100
          color.setAlpha(alpha * (0.55 + 0.45 * busy) * (0.85 + 0.15 * Math.sin(time * (0.5 + busy * 3) + k + n.phase)))
          p.fill(color)
        }
        p.strokeWeight(1)
        p.circle(light.x, light.y, 8 * n.scale)
        p.strokeWeight(1.5)
      }
    }
    const height = b.layers * b.lh
    face([up(b.N, height), up(b.E, height), up(b.S, height), up(b.W, height)], topColor(n))
  }

  /** Use the same health colors as the machine labels. */
  function topColor(n: Node): string {
    return n.health === 'Healthy' ? healthColors.green : n.health === 'Warning' ? healthColors.yellow : COMPUTE_AREA
  }

  function userColor(user: string): string {
    let hash = 0
    for (const char of user) hash = (hash * 31 + char.charCodeAt(0)) >>> 0
    return USER_COLORS[hash % USER_COLORS.length]
  }

  const subtitle = (n: Node) => n.mode === 'monitor'
    ? n.front ? 'Monitor' : `${n.totalGpus} GPUs`
    : n.front
    ? `${n.pendingJobs} pending`
    : n.fpgaUsagePercent !== null ? `FPGA ${Math.round(n.fpgaUsagePercent)}%` : n.totalGpus ? `${n.allocatedGpus}/${n.totalGpus} GPUs` : 'CPU'

  /** Return a machine's name box in map coordinates, before its hard shadow. */
  function labelBox(n: Node, tx: number, ty: number) {
    const b = serverAt(n, tx, ty)
    sketch.textFont(MONO)
    sketch.textStyle(sketch.NORMAL)
    sketch.textSize(10)
    const subW = sketch.textWidth(subtitle(n))
    sketch.textFont(font)
    sketch.textStyle(sketch.BOLD)
    sketch.textSize(12)
    const w = sketch.textWidth(n.name) + subW + 34
    const h = n.totalGpus > 0 || n.fpgaUsagePercent !== null ? 42 : 30
    return { x: b.cx - w / 2, y: b.y - h - 14, w, h }
  }

  // Label box like the website's cards: the name in the page font, numbers in monospace.
  function drawLabel(p: p5, n: Node) {
    const b = nodeBox(n)
    const alpha = hovered !== null && hovered !== n ? 120 : 255
    const sub = subtitle(n)
    const { x, y, w, h } = labelBox(n, n.x!, n.y!)
    const hasBar = n.totalGpus > 0 || n.fpgaUsagePercent !== null
    p.stroke(23, 35, 34, alpha)
    p.strokeWeight(1.5)
    p.line(b.cx, y + h, b.cx, b.topCy)
    drawBox(p, x, y, w, h, 2, 3, 0, alpha)
    p.noStroke()
    p.fill(23, 35, 34, alpha)
    p.textAlign(p.LEFT, p.TOP)
    p.text(n.name, x + 11, y + 8)
    if (hasBar) {
      // GPU activity in monitor mode; GPU allocation or FPGA usage in Slurm mode.
      const bw = w - 22
      p.fill(255, alpha)
      p.stroke(23, 35, 34, alpha)
      p.strokeWeight(1.5)
      p.rect(x + 11, y + 27, bw, 7)
      const bar = p.color(accent)
      bar.setAlpha(alpha)
      p.fill(bar)
      p.rect(x + 11, y + 27, bw * n.load, 7)
    }
    p.noStroke()
    p.textFont(MONO)
    p.textStyle(p.NORMAL)
    p.textSize(10)
    p.fill(n.front ? INK : MUTED)
    p.textAlign(p.RIGHT, p.TOP)
    p.text(sub, x + w - 11, y + 10)
  }

  function drawStale(p: p5) {
    if (!stale) return
    p.textFont(font)
    p.textStyle(p.BOLD)
    p.textSize(12)
    const text = nodes.some(node => node.mode === 'monitor') ? 'Data is stale' : 'Data is stale: traffic paused'
    drawBox(p, 12, 12, p.textWidth(text) + 20, 28, 2, 3, 0, 255)
    p.noStroke()
    p.fill(INK)
    p.textAlign(p.LEFT, p.TOP)
    p.text(text, 22, 20)
  }

  // Tooltip like the Overview chart's: white, 3 px dark border, rounded, hard 4 px shadow.
  function drawTooltip(p: p5) {
    if (!hovered) return
    const n = hovered
    const rows = n.mode === 'monitor'
      ? [n.front ? 'Monitor host' : 'Machine', `Health: ${n.health}`, n.gpuModel ? `${n.totalGpus} × ${n.gpuModel}` : `${n.totalGpus} GPUs`,
        ...n.gpus.map((gpu, i) => `GPU ${i}: utilization ${gpu.busy === null ? 'unknown' : `${Math.round(gpu.busy)}%`}`)]
      : n.front
      ? ['Front node', `Health: ${n.health}`, `Serves the shared home to ${n.out} machine${n.out === 1 ? '' : 's'}`, `Pending jobs: ${n.pendingJobs}`]
      : ['Compute', `Health: ${n.health}`, n.fpgaUsagePercent !== null ? `FPGA usage: ${Math.round(n.fpgaUsagePercent)}%` : n.gpuModel ? `${n.totalGpus} × ${n.gpuModel}` : n.totalGpus ? `${n.totalGpus} GPUs` : 'CPU only',
        ...(n.totalGpus ? [`GPUs allocated: ${n.allocatedGpus} of ${n.totalGpus}`] : []), `Running jobs: ${n.runningJobs}`,
        n.nfsRequests === null ? 'Shared home: not measured' : `Shared home: ${Math.round(n.nfsRequests)} requests/s (${Math.round(n.nfsWrites ?? 0)} writes)`,
        ...n.gpus.map((gpu, i) => `GPU ${i}: ${gpu.user === null ? 'free' : gpu.user || 'allocated'}, busy ${gpu.busy === null ? 'unknown' : `${Math.round(gpu.busy)}%`}`)]
    p.textFont(font)
    p.textStyle(p.NORMAL)
    p.textSize(12)
    const w = Math.max(...rows.map(r => p.textWidth(r)), p.textWidth(n.name)) + 32
    const h = 32 + rows.length * 17 + 8
    let x = p.mouseX + 16
    let y = p.mouseY + 16
    if (x + w > p.width - 8) x = p.mouseX - w - 16
    if (y + h > p.height - 8) y = p.height - h - 8
    drawBox(p, x, y, w, h, 3, 4, 12, 255)
    p.noStroke()
    p.fill(INK)
    p.textStyle(p.BOLD)
    p.textAlign(p.LEFT, p.TOP)
    p.text(n.name, x + 16, y + 13)
    p.textStyle(p.NORMAL)
    rows.forEach((r, i) => p.text(r, x + 16, y + 34 + i * 17))
  }

  function updateHover(p: p5) {
    hovered = null
    if (p.mouseX < 0 || p.mouseY < 0 || p.mouseX > p.width || p.mouseY > p.height) return
    const wx = (p.mouseX - cam.x) / cam.z
    const wy = (p.mouseY - cam.y) / cam.z
    for (const n of [...nodes].sort((a, b) => depth(b) - depth(a))) {
      const b = nodeBox(n)
      if (wx > b.x && wx < b.x + b.w && wy > b.y && wy < b.y + b.h) { hovered = n; break }
    }
  }

  // ---------------------------------------------------------------- p5 setup

  let pendingScene: MapMachine[] | null = null
  sketch = new p5((p: p5) => {
    p.setup = () => {
      p.createCanvas(container.clientWidth, 200)
      p.pixelDensity(Math.min(2, window.devicePixelRatio || 1))
      if (pendingScene) buildScene(pendingScene)
      fitView(false)
    }
    p.draw = () => draw(p)
  }, container)
  // Only width changes come from the page; the height follows the machines.
  const resize = new ResizeObserver(() => {
    if (!sketch.width || container.clientWidth === sketch.width) return
    sketch.resizeCanvas(container.clientWidth, sketch.height)
    fitView(false)
  })
  resize.observe(container)

  return {
    update(machines, isStale) {
      stale = isStale
      if (!sketch.width) pendingScene = machines
      else buildScene(machines)
    },
    setLayout(name) {
      layoutName = name
      applyLayout(false)
    },
    remove() {
      resize.disconnect()
      sketch.remove()
    },
  }
}
