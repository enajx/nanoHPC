import { cpSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const source = fileURLToPath(new URL('../../website/', import.meta.url))
const target = fileURLToPath(new URL('../../website-demo/', import.meta.url))
const gib = 1024 ** 3
const site = {
  cluster_name: 'nanohpc-demo', logo: null, login_address: 'login.demo.example.org',
  home_quota_soft_gb: 250, home_quota_hard_gb: 300, scratch_cleanup_days: 21, demo: true,
}
const disks = [{ mount: '/', total_bytes: 500 * gib, used_bytes: 120 * gib, available_bytes: 380 * gib },
  { mount: '/scratch', total_bytes: 2000 * gib, used_bytes: 720 * gib, available_bytes: 1280 * gib }]
const specs = (cores, ram, gpuCount) => ({
  collected_at: 0, os: 'Ubuntu 24.04 LTS', kernel: '6.8.0', cpu_model: cores > 16 ? 'AMD EPYC' : 'AMD Threadripper',
  cpu_cores: cores, cpu_threads: cores * 2, ram_bytes: ram * gib, gpu_count: gpuCount,
  gpus: Array.from({ length: gpuCount }, (_, index) => ({ index: String(index), model: 'NVIDIA H100', memory_bytes: 80 * gib })),
  driver: gpuCount ? 'sample' : null, cuda_driver: gpuCount ? 'sample' : null, cuda_toolkits: [],
  uptime_seconds: 3 * 86400, pending_updates: 0, updates_checked_at: 0, needs_restart: false, disks,
})
const node = (name, role, cores, ram, gpus, available) => ({
  name, role, health: 'Healthy', health_details: [], gpu_usage: gpus ? 'Active' : 'Not applicable',
  specs: specs(cores, ram, gpus), total_gpus: gpus, available_gpus: available,
})
const snapshot = {
  generated_at: '2026-01-01T00:00:00Z', refresh_seconds: 30, accounting_start: '2026-01-01T00:00:00Z',
  running_jobs: 3, pending_jobs: 2, average_wait_seconds_30d: 745,
  total_gpus: 12, allocated_gpus: 7,
  jobs: [
    { id: '101', user: 'alex', state: 'RUNNING', gpus: 3, node: 'gpu1', priority: 2500, seconds: 3725 },
    { id: '102', user: 'blair', state: 'RUNNING', gpus: 2, node: 'gpu2', priority: 2100, seconds: 6250 },
    { id: '103', user: 'casey', state: 'RUNNING', gpus: 2, node: 'gpu3', priority: 1900, seconds: 900 },
    { id: '104', user: 'alex', state: 'PENDING', gpus: 1, node: '', priority: 1400, seconds: 400 },
    { id: '105', user: 'drew', state: 'PENDING', gpus: 1, node: '', priority: 1100, seconds: 140 },
  ],
  nodes: [node('front', 'Front node', 16, 64, 0, 0), node('cpu1', 'Compute', 32, 128, 0, 0),
    node('gpu1', 'Compute', 32, 256, 4, 1), node('gpu2', 'Compute', 32, 256, 4, 2),
    node('gpu3', 'Compute', 32, 256, 4, 2), node('storage', 'Storage', 8, 32, 0, 0)],
  ranking: [
    { user: 'alex', gpu_hours: 120.25, decayed_gpu_hours: 40.5, fairshare: 0.25 },
    { user: 'blair', gpu_hours: 84.5, decayed_gpu_hours: 32, fairshare: 0.45 },
    { user: 'casey', gpu_hours: 37.5, decayed_gpu_hours: 13, fairshare: 0.7 },
    { user: 'drew', gpu_hours: 12.5, decayed_gpu_hours: 3.25, fairshare: 0.9 },
  ],
  users: ['alex', 'blair', 'casey', 'drew'].map((user, index) => ({
    user, gpu_hours_7d: 25 - index * 5, gpu_hours_30d: 80 - index * 16, gpu_hours_365d: 120 - index * 30,
    home: { used_bytes: (35 + index * 25) * gib, soft_bytes: 250 * gib, hard_bytes: 300 * gib },
  })),
  priorities: [{ job_id: '104', user: 'alex', priority: 1400, fairshare: 1100, age: 300 },
    { job_id: '105', user: 'drew', priority: 1100, fairshare: 900, age: 200 }],
  policies: [
    { name: 'main: maximum runtime', value: '1-00:00:00' },
    { name: 'main: default memory per CPU', value: '8192 MiB' },
    { name: 'interactive: maximum runtime', value: '08:00:00' },
    { name: 'normal: running + pending jobs per user', value: '30' },
  ],
  partitions: [
    { name: 'main', default: true, max_time: '1-00:00:00', nodes: 'gpu1,gpu2,gpu3,cpu1' },
    { name: 'interactive', default: false, max_time: '08:00:00', nodes: 'gpu1,gpu2,gpu3' },
  ],
}

/** Copy the deploy bundle and add only invented data for static hosting. */
rmSync(target, { recursive: true, force: true })
cpSync(source, target, { recursive: true })
mkdirSync(`${target}/data`, { recursive: true })
writeFileSync(`${target}/site.json`, JSON.stringify(site, null, 2))
writeFileSync(`${target}/data/status.json`, JSON.stringify(snapshot, null, 2))
writeFileSync(`${target}/machines.md`, '# Demo machines\n\nAll machines and measurements are fictional.\n')
for (const name of ['docs.md', 'policy.md']) {
  const template = readFileSync(`${target}/${name}`, 'utf8')
  writeFileSync(`${target}/${name}`, template.replace(/\{\{(\w+)\}\}/g, (_, key) => String(site[key])))
}
writeFileSync(`${target}/.nojekyll`, '')
