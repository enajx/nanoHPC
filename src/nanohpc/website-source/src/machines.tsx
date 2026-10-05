export type MachineSpecs = {
  collected_at: number | null
  os: string | null
  kernel: string | null
  cpu_model: string | null
  cpu_cores: number | null
  cpu_threads: number | null
  ram_bytes: number | null
  gpu_count: number | null
  gpus: { index: string; model: string; memory_bytes: number }[]
  unified_memory_gb?: number
  driver: string | null
  cuda_driver: string | null
  cuda_toolkits: string[] | null
  uptime_seconds: number | null
  pending_updates: number | null
  updates_checked_at: number | null
  needs_restart: boolean | null
  disks: { mount: string; total_bytes: number; used_bytes: number | null; available_bytes: number | null }[]
}
export type Machine = { name: string; role: string; health?: string; health_details?: string[]; gpu_usage?: string; fpga_usage_percent?: number; total_gpus?: number; available_gpus?: number | null; specs?: MachineSpecs }

/** Use binary storage units without turning unavailable values into zero. */
export function size(bytes: number | null | undefined): string {
  if (bytes == null) return 'Unknown'
  if (bytes === 0) return '0 B'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  const power = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  return `${(bytes / 1024 ** power).toFixed(power === 0 ? 0 : 1)} ${units[power]}`
}

/** Keep uptime compact and omit invented measurements. */
function uptime(seconds: number | null | undefined): string {
  return seconds == null ? 'Unknown' : `${Math.floor(seconds / 86400)}d ${Math.floor(seconds % 86400 / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`
}

/** Group identical GPU descriptions without hiding mixed hardware. */
function gpuGroups(gpus: MachineSpecs['gpus'], describe: (gpu: MachineSpecs['gpus'][number]) => string): string {
  const counts = new Map<string, number>()
  for (const gpu of gpus) {
    const label = describe(gpu)
    counts.set(label, (counts.get(label) ?? 0) + 1)
  }
  return [...counts].map(([label, count]) => `${count}× ${label}`).join(', ')
}

/** Use whole GiB for compact hardware capacity rows. */
function wholeGib(bytes: number | null | undefined): string {
  return bytes == null ? 'Unknown' : `${Math.round(bytes / 1024 ** 3)} GiB`
}

/** Show only the root filesystem and its used fraction. */
function rootDisk(specs: MachineSpecs | undefined): string {
  const root = specs?.disks.find(disk => disk.mount === '/')
  if (!root || root.used_bytes == null || root.total_bytes <= 0) return 'Unknown'
  return `${size(root.used_bytes)} / ${size(root.total_bytes)} (${Math.round(100 * root.used_bytes / root.total_bytes)}%)`
}

/** Display one labelled measurement. */
function Detail({ label, value }: { label: string; value: string | number | null | undefined }) {
  return <div><dt>{label}</dt><dd>{value ?? 'Unknown'}</dd></div>
}

/** Keep each compute machine's measured status and specifications below its charts. */
export function MachineCards({ nodes, stale }: { nodes: Machine[]; stale: boolean }) {
  return <section className="machine-specs"><h2>Machines</h2><div className="spec-card-grid">{nodes.filter(node => node.role === 'Compute').map(node => {
    const s = stale ? undefined : node.specs
    const completeGpus = s?.gpu_count != null && s.gpus.length === s.gpu_count
    const models = s?.gpu_count === 0 ? '0' : completeGpus ? gpuGroups(s.gpus, gpu => gpu.model) : s?.gpu_count != null ? `${s.gpu_count} GPUs` : 'Unknown'
    const memory = s?.gpu_count === 0 ? 'Not applicable' : completeGpus ? gpuGroups(s.gpus, gpu => wholeGib(gpu.memory_bytes)) : 'Unknown'
    const installedCuda = s?.cuda_toolkits == null ? 'Unknown' : s.cuda_toolkits.join(', ') || 'Not installed'
    return <section key={node.name} id={`machine-${node.name}`} className="panel machine-spec" aria-label={`${node.name} specs`}>
      <div className="panel-heading"><h2>{node.name}</h2><div className="machine-flags"><span className={`pill state-${(stale ? 'Unknown' : node.health ?? 'Unknown').toLowerCase()}`}>{stale ? 'Unknown' : node.health ?? 'Unknown'}</span>{s?.pending_updates != null && s.pending_updates > 0 && <span className="pill state-warning update-badge">Needs update</span>}</div></div>
      <div className="spec-columns">
        <section><h3>Status</h3><dl>
          <Detail label="Uptime" value={uptime(s?.uptime_seconds)}/>
          <Detail label="Needs update" value={s?.pending_updates == null ? 'Unknown' : s.pending_updates > 0 ? 'Yes' : 'No'}/>
          <Detail label="Needs restart" value={s?.needs_restart == null ? 'Unknown' : s.needs_restart ? 'Yes' : 'No'}/>
          <Detail label="HD used" value={rootDisk(s)}/>
          {node.fpga_usage_percent != null && <Detail label="FPGA usage" value={stale ? 'Unknown' : `${node.fpga_usage_percent}%`}/>}
        </dl></section>
        <section><h3>Hardware</h3><dl>
          <Detail label="GPUs" value={models}/>{s?.unified_memory_gb != null ? <Detail label="Unified memory" value={`${s.unified_memory_gb} GB`}/> : <Detail label="GPU memory" value={memory}/>}
          <Detail label="CPU cores" value={s?.cpu_cores}/><Detail label="RAM" value={wholeGib(s?.ram_bytes)}/>
        </dl></section>
        <section><h3>Software</h3><dl>
          <Detail label="OS / kernel" value={`${s?.os?.split(' ')[0] ?? 'Unknown'} · ${s?.kernel ?? 'Unknown'}`}/>
          <Detail label="NVIDIA / CUDA" value={`${s?.driver ?? 'Unknown'} · ${installedCuda}`}/>
        </dl></section>
      </div>
    </section>
  })}</div></section>
}
