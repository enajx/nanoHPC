import { cpSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const source = fileURLToPath(new URL('../../website/', import.meta.url))
const target = fileURLToPath(new URL('../../website-demo/', import.meta.url))
const gib = 1024 ** 3
const site = {
  cluster_name: 'nanoHPC', logo: null, login_address: 'login.demo.example.org',
  home_quota_soft_gb: 250, home_quota_hard_gb: 300, scratch_cleanup_days: 21, demo: true,
}
const disks = [{ mount: '/', total_bytes: 500 * gib, used_bytes: 120 * gib, available_bytes: 380 * gib },
  { mount: '/scratch', total_bytes: 2000 * gib, used_bytes: 720 * gib, available_bytes: 1280 * gib }]
const gigabitBytesPerSecond = 125e6
const speeds = {
  front: { home_small_write: null, internet_download: null },
  H100: { home_small_write: gigabitBytesPerSecond, internet_download: 133e6 },
  H200: { home_small_write: gigabitBytesPerSecond, internet_download: 169e6 },
  B200: { home_small_write: gigabitBytesPerSecond, internet_download: 143e6 },
  Threadripper: { home_small_write: gigabitBytesPerSecond, internet_download: 99e6 },
  'Nvidia DGX': { home_small_write: gigabitBytesPerSecond, internet_download: 95e6 },
}
const specs = (cores, ram, gpuCount, gpuModel, cpuModel, unifiedMemoryGb) => ({
  collected_at: 0, os: 'Ubuntu 24.04 LTS', kernel: '6.8.0', cpu_model: cpuModel,
  cpu_cores: cores, cpu_threads: cores * 2, ram_bytes: ram * gib, gpu_count: gpuCount,
  gpus: Array.from({ length: gpuCount }, (_, index) => ({ index: String(index), model: gpuModel, memory_bytes: gpuModel === 'NVIDIA B200' ? 192 * gib : gpuModel === 'NVIDIA H200' ? 141 * gib : gpuModel === 'NVIDIA DGX' ? 128 * gib : 80 * gib })),
  driver: gpuCount ? 'sample' : null, cuda_driver: gpuCount ? 'sample' : null, cuda_toolkits: [],
  uptime_seconds: 3 * 86400, pending_updates: 0, updates_checked_at: 0, needs_restart: false, disks,
  ...(unifiedMemoryGb === null ? {} : { unified_memory_gb: unifiedMemoryGb }),
})
const node = (name, role, cores, ram, gpus, available, gpuModel, cpuModel, unifiedMemoryGb) => ({
  name, role, health: 'Healthy', health_details: [], gpu_usage: gpus ? available === gpus ? 'Idle' : 'Active' : 'Not applicable',
  specs: { ...specs(cores, ram, gpus, gpuModel, cpuModel, unifiedMemoryGb), speeds: speeds[name] }, total_gpus: gpus, available_gpus: available,
})
const snapshot = {
  generated_at: '2026-01-01T00:00:00Z', refresh_seconds: 30, accounting_start: '2026-01-01T00:00:00Z',
  running_jobs: 4, pending_jobs: 2, average_wait_seconds_30d: 745,
  total_gpus: 13, allocated_gpus: 9,
  jobs: [
    { id: '101', user: 'Alice', state: 'RUNNING', gpus: 3, node: 'H100', priority: 2500, seconds: 3725 },
    { id: '102', user: 'Bob', state: 'RUNNING', gpus: 3, node: 'H200', priority: 2100, seconds: 6250 },
    { id: '103', user: 'Mike', state: 'RUNNING', gpus: 1, node: 'Nvidia DGX', priority: 1900, seconds: 900 },
    { id: '104', user: 'Alice', state: 'PENDING', gpus: 1, node: '', priority: 1400, seconds: 400 },
    { id: '105', user: 'Mike', state: 'PENDING', gpus: 1, node: '', priority: 1100, seconds: 140 },
    { id: '106', user: 'Mike', state: 'RUNNING', gpus: 2, node: 'B200', priority: 1800, seconds: 1350 },
  ],
  nodes: [node('front', 'Front node', 16, 64, 0, 0, '', 'AMD EPYC', null),
    node('H100', 'Compute', 32, 256, 4, 1, 'NVIDIA H100', 'AMD EPYC', null),
    node('H200', 'Compute', 32, 256, 4, 1, 'NVIDIA H200', 'AMD EPYC', null),
    node('B200', 'Compute', 64, 512, 4, 2, 'NVIDIA B200', 'AMD EPYC', null),
    node('Threadripper', 'Compute', 32, 128, 0, 0, '', 'AMD Threadripper', null),
    node('Nvidia DGX', 'Compute', 20, 128, 1, 0, 'NVIDIA DGX', 'NVIDIA CPU', 128)],
  ranking: [
    { user: 'Alice', gpu_hours: 120.25, decayed_gpu_hours: 40.5, fairshare: 0.25 },
    { user: 'Bob', gpu_hours: 84.5, decayed_gpu_hours: 32, fairshare: 0.45 },
    { user: 'Mike', gpu_hours: 37.5, decayed_gpu_hours: 13, fairshare: 0.7 },
  ],
  users: ['Alice', 'Bob', 'Mike'].map((user, index) => ({
    user, gpu_hours_7d: 25 - index * 5, gpu_hours_30d: 80 - index * 16, gpu_hours_365d: 120 - index * 30,
    home: { used_bytes: (35 + index * 25) * gib, soft_bytes: 250 * gib, hard_bytes: 300 * gib },
  })),
  priorities: [{ job_id: '104', user: 'Alice', priority: 1400, fairshare: 1100, age: 300 },
    { job_id: '105', user: 'Mike', priority: 1100, fairshare: 900, age: 200 }],
  policies: [
    { name: 'training: maximum runtime', value: '1-00:00:00' },
    { name: 'training: default memory per CPU', value: '8192 MiB' },
    { name: 'training: GPU defaults', value: 'DefCpuPerGPU=4' },
    { name: 'training: usage billing', value: 'CPU=0,Mem=0,GRES/gpu=1' },
    { name: 'training: machines per job', value: '1' },
    { name: 'training: GPUs per job (most)', value: '4' },
    { name: 'inference: maximum runtime', value: '08:00:00' },
    { name: 'inference: default memory per CPU', value: '8192 MiB' },
    { name: 'inference: GPUs per job (most)', value: '1' },
    { name: 'inference: simultaneous resources per user', value: 'gres/gpu=1' },
    { name: 'Login on front: CPU cores per user', value: '4' },
    { name: 'Login on front: memory per user', value: '8 GiB' },
    { name: 'normal: running + pending jobs per user', value: '100' },
    { name: 'PriorityWeightFairShare', value: '10000' },
    { name: 'PriorityWeightAge', value: '2000' },
  ],
  partitions: [
    { name: 'training', default: true, max_time: '1-00:00:00', nodes: 'H100,H200,B200' },
    { name: 'inference', default: false, max_time: '08:00:00', nodes: 'Threadripper,Nvidia DGX' },
  ],
}
for (const machine of snapshot.nodes) {
  machine.partitions = snapshot.partitions.filter(partition => partition.nodes.split(',').includes(machine.name)).map(partition => partition.name)
  machine.building = machine.role === 'Front node' ? null : ['H100', 'H200', 'B200', 'Threadripper'].includes(machine.name) ? 'Lab A' : 'Lab B'
}

/** Copy the deploy bundle and add only invented data for static hosting. */
rmSync(target, { recursive: true, force: true })
cpSync(source, target, { recursive: true })
mkdirSync(`${target}/data`, { recursive: true })
writeFileSync(`${target}/site.json`, JSON.stringify(site, null, 2))
writeFileSync(`${target}/data/status.json`, JSON.stringify(snapshot, null, 2))
writeFileSync(`${target}/machines.md`, '# Machines\n\n| Machine | Type | GPUs |\n| --- | --- | ---: |\n| H100 | GPU | 4 |\n| H200 | GPU | 4 |\n| B200 | GPU | 4 |\n| Threadripper | CPU | 0 |\n| Nvidia DGX | 128 GB unified memory | 1 |\n')
for (const name of ['docs.md', 'policy.md']) {
  const template = readFileSync(`${target}/${name}`, 'utf8')
  writeFileSync(`${target}/${name}`, template.replace(/\{\{(\w+)\}\}/g, (_, key) => String(site[key])))
}
writeFileSync(`${target}/.nojekyll`, '')
