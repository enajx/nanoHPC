/**
 * Browser test of the built website with fixture cluster data.
 *
 * Run from src/nanohpc/website-source/:
 *   npm ci && npm run build      # builds ../website (src/nanohpc/website)
 *   npx playwright install chromium   # once per machine
 *   npm test
 *
 * The built site is copied into a temporary folder and served by a small static server under /hpc/, the
 * way the front node's nginx serves it, so the test proves that every URL is relative to the page.
 * site.json, data/status.json, machines.md, docs.md, and policy.md are real fixture files served over HTTP;
 * docs.md and policy.md are the built templates with their placeholders filled in, as the deploy does.
 * Grafana is mocked inside the browser (page.route): its dashboard pages and its query API return fixture
 * replies, so the embedded dashboards themselves are not verified here.
 */
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { createServer, type Server } from 'node:http'
import { tmpdir } from 'node:os'
import { extname, join, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { expect, test, type Page, type Route } from '@playwright/test'

const built = fileURLToPath(new URL('../../website/', import.meta.url))
// Not /cluster/ (the default path): an absolute /cluster/ URL left in the site would then fail.
const prefix = '/hpc/'
const site = {
  cluster_name: 'mylab',
  logo: 'logo.svg',
  login_address: 'login.mylab.example.org',
  home_quota_soft_gb: 250,
  home_quota_hard_gb: 300,
  scratch_cleanup_days: 21,
}
const gib = 1024 ** 3

/** A status.json as the status collector writes it: a front node, a GPU machine, a CPU-only machine, and storage. */
function snapshot(): object {
  const now = Math.floor(Date.now() / 1000)
  const disks = [{ mount: '/', total_bytes: 500 * gib, used_bytes: 120 * gib, available_bytes: 380 * gib },
    { mount: '/scratch', total_bytes: 2000 * gib, used_bytes: 100 * gib, available_bytes: 1900 * gib }]
  const specs = (cores: number, ram: number, gpus: { index: string; model: string; memory_bytes: number }[]) => ({
    collected_at: now, os: 'Ubuntu 24.04.3 LTS', kernel: '6.8.0-85-generic', cpu_model: 'AMD EPYC 7313',
    cpu_cores: cores, cpu_threads: cores * 2, ram_bytes: ram * gib, gpu_count: gpus.length, gpus,
    driver: gpus.length ? '580.95.05' : null, cuda_driver: gpus.length ? '13.0' : null, cuda_toolkits: gpus.length ? ['12.8'] : [],
    uptime_seconds: 3 * 86400 + 7200, pending_updates: 0, updates_checked_at: now, needs_restart: false, disks,
  })
  const a6000 = ['0', '1', '2', '3'].map(index => ({ index, model: 'NVIDIA RTX A6000', memory_bytes: 48 * gib }))
  return {
    generated_at: new Date().toISOString(),
    refresh_seconds: 30,
    accounting_start: '2026-01-01T00:00:00',
    running_jobs: 1,
    pending_jobs: 1,
    average_wait_seconds_30d: 754.5,
    total_gpus: 4,
    allocated_gpus: 2,
    jobs: [
      { id: '101', user: 'alice', state: 'RUNNING', gpus: 2, node: 'gpu1', priority: 2500, seconds: 3725 },
      { id: '102', user: 'bob', state: 'PENDING', gpus: 1, node: '', priority: 1200, seconds: 95 },
    ],
    nodes: [
      { name: 'front', role: 'Front node', health: 'Healthy', health_details: [], gpu_usage: 'Not applicable' },
      { name: 'gpu1', role: 'Compute', health: 'Healthy', health_details: [], gpu_usage: 'Active', specs: specs(32, 256, a6000), total_gpus: 4, available_gpus: 2 },
      { name: 'cpu1', role: 'Compute', health: 'Healthy', health_details: [], gpu_usage: 'Not applicable', specs: specs(16, 64, []), total_gpus: 0, available_gpus: 0 },
      { name: 'store', role: 'Storage', health: 'Healthy', health_details: [], gpu_usage: 'Not applicable', specs: specs(8, 32, []) },
    ],
    ranking: [
      { user: 'alice', gpu_hours: 120.25, decayed_gpu_hours: 40.5, fairshare: 0.25 },
      { user: 'bob', gpu_hours: 12.5, decayed_gpu_hours: 3.25, fairshare: 0.75 },
    ],
    users: [
      { user: 'alice', gpu_hours_7d: 20.5, gpu_hours_30d: 80.25, gpu_hours_365d: 120.25, home: { used_bytes: 260 * gib, soft_bytes: 250 * gib, hard_bytes: 300 * gib } },
      { user: 'bob', gpu_hours_7d: 1.5, gpu_hours_30d: 4.5, gpu_hours_365d: 12.5, home: { used_bytes: 10 * gib, soft_bytes: 250 * gib, hard_bytes: 300 * gib } },
    ],
    priorities: [{ job_id: '102', user: 'bob', priority: 1200, fairshare: 1100, age: 100 }],
    policies: [
      { name: 'interactive: maximum runtime', value: '08:00:00' },
      { name: 'interactive: default memory per CPU', value: '8192 MiB' },
      { name: 'main: maximum runtime', value: '1-00:00:00' },
      { name: 'main: default memory per CPU', value: '8192 MiB' },
      { name: 'main: GPU defaults', value: 'DefCpuPerGPU=4' },
      { name: 'normal: running + pending jobs per user', value: '30' },
      { name: 'PriorityWeightFairShare', value: '10000' },
    ],
    partitions: [
      { name: 'interactive', default: false, max_time: '08:00:00', nodes: 'gpu1' },
      { name: 'main', default: true, max_time: '1-00:00:00', nodes: 'gpu1,cpu1' },
    ],
  }
}

/** Fill in the docs.md and policy.md placeholders the way the deploy does. */
function filled(template: string): string {
  return template.replace(/\{\{(\w+)\}\}/g, (_, name: string) => String(site[name as keyof typeof site]))
}

const types: Record<string, string> = {
  '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.md': 'text/markdown',
  '.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.woff': 'font/woff', '.sh': 'text/plain',
}
let folder = ''
let server: Server
let origin = ''

test.beforeAll(async () => {
  folder = mkdtempSync(join(tmpdir(), 'nanohpc-website-'))
  const root = join(folder, prefix.slice(1, -1))
  cpSync(built, root, { recursive: true })
  mkdirSync(join(root, 'data'))
  writeFileSync(join(root, 'site.json'), JSON.stringify(site))
  writeFileSync(join(root, 'data', 'status.json'), JSON.stringify(snapshot()))
  writeFileSync(join(root, 'machines.md'), '# mylab machines\n\n| Machine | Health |\n| --- | --- |\n| gpu1 | Healthy |\n| cpu1 | Healthy |\n')
  writeFileSync(join(root, 'logo.svg'), '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10" fill="#36c"/></svg>')
  for (const name of ['docs.md', 'policy.md']) writeFileSync(join(root, name), filled(readFileSync(join(built, name), 'utf-8')))
  // A static server like the front node's nginx: files under /cluster/, index.html for the folder itself.
  server = createServer((request, response) => {
    const path = new URL(request.url ?? '/', 'http://localhost').pathname
    const file = resolve(folder, '.' + (path.endsWith('/') ? path + 'index.html' : path))
    if (!path.startsWith(prefix) || !file.startsWith(root + sep) || !existsSync(file) || !statSync(file).isFile()) {
      response.writeHead(404).end()
      return
    }
    response.writeHead(200, { 'Content-Type': types[extname(file)] ?? 'application/octet-stream' }).end(readFileSync(file))
  })
  await new Promise<void>(done => server.listen(0, '127.0.0.1', done))
  const address = server.address()
  if (address === null || typeof address === 'string') throw new Error('The test server has no port')
  origin = `http://127.0.0.1:${address.port}`
})

test.afterAll(async () => {
  await new Promise<void>(done => server.close(() => done()))
  rmSync(folder, { recursive: true, force: true })
})

/** Mocked Grafana: dashboards are a stub page; the query API answers the GPU chart and the cluster map. */
async function mockGrafana(page: Page): Promise<void> {
  await page.route(`**${prefix}grafana/d/**`, route => route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>Dashboard</title><p>Grafana dashboard (test stub)</p>' }))
  await page.route(`**${prefix}grafana/api/ds/query`, (route: Route) => {
    const body = route.request().postDataJSON() as { queries: { refId: string }[] }
    const now = Date.now()
    const times = Array.from({ length: 7 * 24 }, (_, hour) => now - (7 * 24 - hour) * 3600 * 1000)
    const frame = (labels: Record<string, string>, values: number[]) => ({ schema: { fields: [{}, { labels }] }, data: { values: [times.slice(-values.length), values] } })
    const results: Record<string, { frames: object[] }> = {}
    for (const { refId } of body.queries) {
      if (refId === 'A') results.A = { frames: [frame({ node: 'gpu1' }, times.map((_, index) => index % 4))] }
      else if (refId === 'B') results.B = { frames: [frame({ node: 'gpu1' }, times.map(() => 4))] }
      else if (refId === 'busy') results.busy = { frames: ['0', '1'].map(gpu => frame({ machine: 'gpu1', gpu }, [55])) }
      else results[refId] = { frames: [frame({ machine: 'gpu1' }, [12])] }
    }
    return route.fulfill({ json: { results } })
  })
}

/** Record every request and console error, so the test can check them at the end. */
function watch(page: Page): { requests: string[]; errors: string[] } {
  const seen = { requests: [] as string[], errors: [] as string[] }
  page.on('request', request => seen.requests.push(request.url()))
  page.on('console', message => { if (message.type() === 'error') seen.errors.push(message.text()) })
  page.on('pageerror', error => seen.errors.push(error.message))
  return seen
}

/** No request leaves the served prefix, and nothing goes to another site. */
function expectInsidePrefix(requests: string[]): void {
  const outside = requests.filter(url => {
    const parsed = new URL(url)
    if (parsed.protocol === 'data:' || parsed.protocol === 'blob:') return false
    return parsed.origin !== origin || !parsed.pathname.startsWith(prefix)
  })
  expect(outside).toEqual([])
}

test('the site renders every tab from the fixture data under a non-root path', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await mockGrafana(page)
  const seen = watch(page)
  await page.goto(`${origin}${prefix}`)

  // Brand and title from site.json, logo from next to index.html.
  await expect(page.locator('.brand-title')).toHaveText('mylab')
  await expect(page).toHaveTitle('mylab')
  const logo = page.locator('img.brand-mark')
  await expect(logo).toHaveAttribute('src', 'logo.svg')
  await expect.poll(() => logo.evaluate(image => (image as HTMLImageElement).naturalWidth)).toBeGreaterThan(0)

  // Overview: summary, compute machines only, GPU chart, current jobs.
  await expect(page.getByText('Updates every 30s')).toBeVisible()
  await expect(page.locator('.stat').filter({ hasText: 'GPUs allocated' })).toContainText('2 / 4')
  const machineRows = page.locator('.machine-list tbody tr')
  await expect(machineRows).toHaveCount(2)
  await expect(machineRows.nth(0)).toContainText('gpu1')
  await expect(machineRows.nth(1)).toContainText('cpu1')
  await expect(page.locator('.gpu-allocation-chart .recharts-area').first()).toBeVisible()
  await expect(page.getByRole('table', { name: 'Running jobs' })).toContainText('alice')
  await expect(page.getByRole('table', { name: 'Top of queue' })).toContainText('bob')

  const nav = page.getByRole('navigation', { name: 'Cluster navigation' })
  expect(await nav.getByRole('link').allInnerTexts()).toEqual(['Overview', 'How to', 'Jobs', 'Machines', 'Users', 'Cluster usage', 'Cluster policy'])

  // How to: the cluster's own values and live partitions.
  await nav.getByRole('link', { name: 'How to', exact: true }).click()
  await expect(page.getByRole('heading', { name: '1. SSH into mylab', exact: true })).toBeVisible()
  await expect(page.locator('.instruction.panel')).toHaveCount(5)
  await expect(page.locator('.instruction').filter({ hasText: 'HostName login.mylab.example.org' })).toHaveCount(1)
  await expect(page.getByText('soft quota of 250 GB and a hard quota of 300 GB', { exact: false })).toBeVisible()
  await expect(page.getByText('main (default): 24 hours, gpu1,cpu1', { exact: true }).first()).toBeVisible()
  const docsLink = page.getByRole('link', { name: 'Open How to in Markdown' })
  const docs = await page.request.get(new URL(await docsLink.getAttribute('href') ?? '', page.url()).href)
  expect(docs.ok()).toBe(true)
  expect(await docs.text()).toContain('HostName login.mylab.example.org')

  // Jobs: the queue and queue history dashboards.
  await nav.getByRole('link', { name: 'Jobs', exact: true }).click()
  await expect(page.locator('iframe')).toHaveCount(2)
  expect(await page.locator('iframe').evaluateAll(frames => frames.map(frame => new URL((frame as HTMLIFrameElement).src).pathname)))
    .toEqual([`${prefix}grafana/d/nanohpc-queue`, `${prefix}grafana/d/nanohpc-queue-history`])

  // Machines: a card per compute machine, including the CPU-only one; the map can be turned on.
  await nav.getByRole('link', { name: 'Machines', exact: true }).click()
  await expect(page.locator('.machine-spec')).toHaveCount(2)
  const cpuCard = page.getByRole('region', { name: 'cpu1 specs' })
  await expect(cpuCard).toContainText('Not applicable')
  await expect(page.getByRole('region', { name: 'gpu1 specs' })).toContainText('4× NVIDIA RTX A6000')
  await page.getByRole('button', { name: 'Show cluster map' }).click()
  await expect(page.locator('.cluster-map-canvas canvas')).toBeVisible()
  await expect(page.locator('.cluster-map-canvas')).toHaveAttribute('aria-label', /gpu1: 2 of 4 GPUs allocated/)
  await page.getByRole('button', { name: 'Show cluster map' }).click()

  // Users: ranking, one card per user, and the usage dashboard.
  await nav.getByRole('link', { name: 'Users', exact: true }).click()
  await expect(page.locator('.user-spec')).toHaveCount(2)
  await expect(page.getByRole('region', { name: 'alice stats' })).toContainText('At soft quota')
  await expect(page.getByRole('cell', { name: '120.250', exact: true })).toBeVisible()
  expect(new URL(await page.locator('iframe[title="GPU usage history"]').evaluate(frame => (frame as HTMLIFrameElement).src)).pathname)
    .toBe(`${prefix}grafana/d/nanohpc-usage`)

  // Cluster usage: the machine metrics dashboard.
  await nav.getByRole('link', { name: 'Cluster usage', exact: true }).click()
  const metrics = new URL(await page.locator('iframe[title="Machine and GPU metrics"]').evaluate(frame => (frame as HTMLIFrameElement).src))
  expect(metrics.pathname).toBe(`${prefix}grafana/d/nanohpc-machines`)
  expect(metrics.searchParams.get('var-gpu_group')).toBe('0')
  // and the long-term history: the daily summaries kept for 5 years.
  const history = new URL(await page.locator('iframe[title="Long-term history (daily summaries, kept 5 years)"]').evaluate(frame => (frame as HTMLIFrameElement).src))
  expect(history.pathname).toBe(`${prefix}grafana/d/nanohpc-history`)
  expect(history.searchParams.get('from')).toBe('now-1y')

  // Cluster policy: one card per partition, default first, then the shared rows.
  await nav.getByRole('link', { name: 'Cluster policy', exact: true }).click()
  await expect(page.locator('.policies h2')).toHaveText(['main (default)', 'interactive', 'All partitions'])
  await expect(page.locator('.policies').first()).toContainText('1-00:00:00')
  const policy = await page.request.get(`${origin}${prefix}policy.md`)
  expect(await policy.text()).toContain('# mylab cluster policy')

  expectInsidePrefix(seen.requests)
  expect(seen.errors).toEqual([])
})

test('without a logo the brand shows a generic mark and the cluster name', async ({ page }) => {
  await mockGrafana(page)
  await page.route(`**${prefix}site.json`, route => route.fulfill({ json: { ...site, logo: null } }))
  const seen = watch(page)
  await page.goto(`${origin}${prefix}`)
  await expect(page.locator('.brand-title')).toHaveText('mylab')
  await expect(page.locator('svg.generic-mark')).toBeVisible()
  await expect(page.locator('img.brand-mark')).toHaveCount(0)
  expectInsidePrefix(seen.requests)
  expect(seen.errors).toEqual([])
})
