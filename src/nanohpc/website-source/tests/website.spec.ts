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
    average_wait_seconds_1d: 90,
    average_wait_seconds_7d: 3600,
    average_wait_seconds_30d: 754.5,
    total_gpus: 4,
    allocated_gpus: 2,
    jobs: [
      { id: '101', user: 'alice', state: 'RUNNING', gpus: 2, node: 'gpu1', priority: 2500, seconds: 3725, time_limit_minutes: 2160 },
      { id: '102', user: 'bob', state: 'PENDING', gpus: 1, node: '', priority: 1200, seconds: 95, time_limit_minutes: null },
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
      { name: 'interactive: GPUs per job (most)', value: 'gres/gpu=2' },
      { name: 'interactive: simultaneous resources per user', value: 'gres/gpu=2' },
      { name: 'main: maximum runtime', value: '1-00:00:00' },
      { name: 'main: GPUs per job (most)', value: 'gres/gpu=4' },
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

test('Overview map, compact layout, booked time, and Users ranking work in the served site', async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 })
  await mockGrafana(page)
  await page.goto(`${origin}${prefix}`)
  const overview = page.locator('main')
  await expect(overview.getByRole('heading', { name: 'Overview' })).toBeAttached()
  await expect(overview.getByRole('heading', { name: 'Overview' })).toHaveCSS('width', '1px')
  await expect(overview.locator('footer .freshness')).toContainText('Updates every 30s')
  const card = overview.locator('.overview-machines')
  const views = card.getByRole('group', { name: 'Machines view' })
  await expect(views.getByRole('button', { name: 'Cluster Map' })).toHaveAttribute('aria-pressed', 'true')
  await expect(card.getByRole('img', { name: /Cluster map:/ })).toBeVisible()
  await expect(card.getByRole('img', { name: /Cluster map:/ })).toHaveAttribute('aria-label', /front: front node/)
  expect(await card.locator('.cluster-map-canvas').evaluate(element => element.getBoundingClientRect().height)).toBeLessThanOrEqual(340)
  const chart = overview.locator('.gpu-allocation-chart')
  const cardWidth = await card.evaluate(element => element.getBoundingClientRect().width)
  const chartWidth = await chart.evaluate(element => element.getBoundingClientRect().width)
  expect(cardWidth / chartWidth).toBeGreaterThan(0.9)
  expect(cardWidth / chartWidth).toBeLessThan(1.1)
  await views.getByRole('button', { name: 'List view' }).click()
  await expect(card.locator('tbody tr')).toHaveCount(2)
  await expect(card.locator('.cluster-map')).toHaveCount(0)
  await page.reload()
  await expect(page.locator('.overview-machines tbody tr')).toHaveCount(2)
  await page.getByRole('navigation', { name: 'Cluster navigation' }).getByRole('link', { name: 'Machines' }).click()
  await expect(page.locator('main > .cluster-map')).toBeVisible()
  await page.getByRole('button', { name: 'Show cluster map' }).click()
  await page.getByRole('navigation', { name: 'Cluster navigation' }).getByRole('link', { name: 'Overview' }).click()
  await expect(page.locator('.overview-machines tbody tr')).toHaveCount(2)
  await expect(page.getByRole('table', { name: 'Running jobs' })).toContainText('1 day 12 hr')
  await expect(page.getByRole('table', { name: 'Top of queue' })).toContainText('No limit')
  await page.getByRole('navigation', { name: 'Cluster navigation' }).getByRole('link', { name: 'Users' }).click()
  const users = page.locator('.users-top')
  await expect(users.getByRole('heading', { name: 'GPU usage history per user' })).toBeVisible()
  await expect(users.locator('.user-ranking thead th')).toHaveText(['Rank', 'User', 'GPU-hours', 'Fair-share'])
  await expect(users.locator('.user-ranking tbody tr').first()).toContainText('120')
  await expect(users.locator('.user-ranking tbody tr').first()).toContainText('0.25')
  await expect(page.getByRole('region', { name: 'alice stats' })).toContainText('20')
})

test('served cluster Overview fits a 1440 by 900 desktop', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(`${origin}${prefix}`)
  await expect(page.locator('.overview-machines .cluster-map-canvas canvas')).toBeVisible()
  await expect(page.locator('.gpu-allocation-chart')).toBeVisible()
  await expect(page.getByRole('table', { name: 'Running jobs' })).toBeVisible()
  await expect(page.locator('main footer')).toBeVisible()
  const pageSize = await page.evaluate(() => ({ height: document.documentElement.scrollHeight, width: document.documentElement.scrollWidth }))
  expect(pageSize.height).toBeLessThanOrEqual(900)
  expect(pageSize.width).toBeLessThanOrEqual(1440)
})

test('Partitions map uses one trunk and one branch turn per machine', async ({ page }) => {
  await mockGrafana(page)
  await page.goto(`${origin}${prefix}#machines`)
  await page.getByRole('group', { name: 'Cluster map layout' }).getByRole('button', { name: 'Partitions' }).click()
  const raw = await page.locator('.cluster-map-canvas').getAttribute('data-pipes')
  expect(raw).not.toBeNull()
  const pipes = JSON.parse(raw ?? '[]') as { from: string; to: string; tiles: [number, number][] }[]
  expect(pipes.length).toBe(3)
  expect(pipes.map(pipe => pipe.from)).toEqual(['front', 'front', 'front'])
  expect(pipes.every(pipe => pipe.tiles.length <= 5)).toBe(true)
  expect(pipes.every(pipe => pipe.tiles[1][0] === pipes[0].tiles[1][0])).toBe(true)
})

test('Offline and Unknown map machines stay transparent with their labels and pipes', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await mockGrafana(page)
  let health: Record<string, string> = {}
  let old = false
  let sample = 0
  await page.route(`**${prefix}data/status.json`, route => {
    const data = snapshot() as { generated_at: string; nodes: { name: string; health: string }[] }
    data.nodes.forEach(node => { node.health = health[node.name] ?? 'Healthy' })
    if (old) data.generated_at = new Date(Date.now() - 10 * 60 * 1000).toISOString()
    return route.fulfill({ json: data })
  })
  const solidPixels = async (): Promise<{ ink: number; white: number }> => {
    await page.goto(`${origin}${prefix}?map-fade=${sample++}#machines`)
    const canvas = page.locator('.cluster-map-canvas canvas')
    await expect(canvas).toBeVisible()
    await page.mouse.move(0, 0)
    await page.waitForTimeout(2300)
    return canvas.evaluate(element => {
      const surface = element as HTMLCanvasElement
      const top = Math.ceil(48 * surface.width / surface.clientWidth)
      const pixels = surface.getContext('2d')!.getImageData(0, top, surface.width, surface.height - top).data
      let ink = 0
      let white = 0
      for (let i = 0; i < pixels.length; i += 4) {
        if (pixels[i + 3] > 250 && Math.abs(pixels[i] - 23) + Math.abs(pixels[i + 1] - 35) + Math.abs(pixels[i + 2] - 34) < 10) ink++
        if (pixels[i + 3] > 250 && pixels[i] > 250 && pixels[i + 1] > 250 && pixels[i + 2] > 250) white++
      }
      return { ink, white }
    })
  }
  const healthy = await solidPixels()
  health = { cpu1: 'Offline' }
  const offline = await solidPixels()
  // With this fixture at 1440px, cpu1's stack is centered at (937, 516).
  await page.mouse.move(937, 516)
  await page.waitForTimeout(200)
  const hoverPixels = await page.locator('.cluster-map-canvas canvas').evaluate(element => {
    const canvas = element as HTMLCanvasElement
    const rect = canvas.getBoundingClientRect()
    const scale = canvas.width / rect.width
    const count = (left: number, top: number, width: number, height: number) => {
      const pixels = canvas.getContext('2d')!.getImageData(
        Math.floor((left - rect.left) * scale), Math.floor((top - rect.top) * scale),
        Math.ceil(width * scale), Math.ceil(height * scale),
      ).data
      let ink = 0
      for (let i = 0; i < pixels.length; i += 4) {
        if (pixels[i + 3] > 250 && Math.abs(pixels[i] - 23) + Math.abs(pixels[i + 1] - 35) + Math.abs(pixels[i + 2] - 34) < 10) ink++
      }
      return ink
    }
    return { stack: count(908, 472, 55, 21), tooltip: count(960, 510, 168, 100) }
  })
  expect(hoverPixels.tooltip, 'the Offline machine should still show its hover details').toBeGreaterThan(200)
  expect(hoverPixels.stack, 'the hovered Offline stack should remain transparent').toBeLessThan(40)
  health = { cpu1: 'Offline', gpu1: 'Unknown' }
  const unknown = await solidPixels()
  health = { front: 'Unknown', gpu1: 'Unknown', cpu1: 'Unknown', store: 'Unknown' }
  const allUnknown = await solidPixels()
  health = {}
  old = true
  const stale = await solidPixels()
  expect(offline.ink, 'Offline machine, label, and pipe should lose solid ink').toBeLessThan(healthy.ink * 0.85)
  expect(offline.white, 'Offline pipe rail and label should lose solid white').toBeLessThan(healthy.white * 0.85)
  expect(unknown.ink, 'Unknown machine, label, and pipe should also lose solid ink').toBeLessThan(offline.ink * 0.82)
  expect(Math.abs(stale.ink - allUnknown.ink), 'stale data should fade every machine').toBeLessThan(allUnknown.ink * 0.08)
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
  await expect(page.locator('main footer')).toContainText('mylab runs on nanoHPC')
  await page.getByRole('button', { name: 'Settings' }).click()
  await expect(page.getByRole('dialog', { name: 'Settings' })).toContainText('read-only cluster monitor')
  await page.getByRole('button', { name: 'Close settings' }).click()
  await expect(page.getByRole('dialog', { name: 'Settings' })).toHaveCount(0)

  // Overview: summary, compute machines, GPU chart, current jobs.
  await expect(page.getByText('Updates every 30s')).toBeVisible()
  await expect(page.locator('.stat').filter({ hasText: 'GPUs allocated' })).toContainText('2 / 4')
  await page.getByRole('group', { name: 'Machines view' }).getByRole('button', { name: 'List view' }).click()
  const machineRows = page.locator('.machine-list tbody tr')
  await expect(page.locator('.machine-list thead th').nth(3)).toHaveText('Speed /home | Internet')
  await expect(machineRows).toHaveCount(2)
  await expect(machineRows.nth(0)).toContainText('gpu1')
  await expect(machineRows.nth(1)).toContainText('cpu1')
  await expect(machineRows.nth(1)).toContainText('Idle')
  await expect(machineRows.nth(0)).toContainText('Unknown')
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
  await expect(page.getByRole('heading', { name: 'Terminal: type these commands after saving job.sh' })).toBeVisible()
  await expect(page.getByText('We recommend submitting from a git worktree:', { exact: false })).toHaveCount(0)
  const submitCard = page.locator('.instruction.panel').filter({ has: page.getByRole('heading', { name: '2. Submit a job' }) })
  await expect(submitCard.getByRole('heading', { name: 'job.sh: save this file in your project' })).toBeVisible()
  const referenceSection = page.locator('.user-guide > section').first()
  await expect(referenceSection.getByRole('heading')).toHaveText('Jobs examples')
  expect(await referenceSection.evaluate(section => getComputedStyle(section).borderBottomWidth)).toBe('0px')
  expect(await referenceSection.getByRole('heading').evaluate(heading => getComputedStyle(heading).fontSize)).toBe('28px')
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

  // Machines: a card per configured machine; the map opens by default.
  await nav.getByRole('link', { name: 'Machines', exact: true }).click()
  await expect(page.locator('.machine-spec')).toHaveCount(4)
  const cpuCard = page.getByRole('region', { name: 'cpu1 specs' })
  await expect(cpuCard).toContainText('Not applicable')
  await expect(page.getByRole('region', { name: 'gpu1 specs' })).toContainText('4× NVIDIA RTX A6000')
  await expect(page.locator('.cluster-map-canvas canvas')).toBeVisible()
  await expect(page.locator('.cluster-map-canvas')).toHaveAttribute('aria-label', /gpu1: 2 of 4 GPUs allocated/)
  await page.getByRole('button', { name: 'Show cluster map' }).click()

  // Users: ranking, one card per user, and the usage dashboard.
  await nav.getByRole('link', { name: 'Users', exact: true }).click()
  await expect(page.locator('.user-spec')).toHaveCount(2)
  await expect(page.getByRole('region', { name: 'alice stats' })).toContainText('At soft quota')
  await expect(page.getByRole('cell', { name: '120', exact: true })).toBeVisible()
  expect(new URL(await page.locator('iframe[title="GPU usage history per user"]').evaluate(frame => (frame as HTMLIFrameElement).src)).pathname)
    .toBe(`${prefix}grafana/d/nanohpc-usage`)

  // Cluster usage: the machine metrics dashboard.
  await nav.getByRole('link', { name: 'Cluster usage', exact: true }).click()
  const metrics = new URL(await page.locator('iframe[title="Machine and GPU metrics"]').evaluate(frame => (frame as HTMLIFrameElement).src))
  expect(metrics.pathname).toBe(`${prefix}grafana/d/nanohpc-machines`)
  expect(metrics.searchParams.get('var-gpu_group')).toBe('0')
  await expect(page.locator('iframe')).toHaveCount(1)

  // Cluster policy: one card per partition, default first, then the shared rows.
  await nav.getByRole('link', { name: 'Cluster policy', exact: true }).click()
  await expect(page.locator('.policies h2')).toHaveText(['main (default)', 'interactive', 'All partitions'])
  await expect(page.locator('.policies').first()).toContainText('1-00:00:00')
  const policy = await page.request.get(`${origin}${prefix}policy.md`)
  expect(await policy.text()).toContain('# mylab cluster policy')

  expectInsidePrefix(seen.requests)
  expect(seen.errors).toEqual([])
})

test('Cluster usage links select one or several machines in the embedded dashboard', async ({ page }) => {
  await mockGrafana(page)
  for (const [hash, selected] of [
    ['#usage', []],
    ['#usage?machine=front', ['front']],
    ['#usage?machine=gpu1&machine=cpu1', ['gpu1', 'cpu1']],
  ] as const) {
    await page.goto(`${origin}${prefix}${hash}`)
    const frame = page.locator('iframe[title="Machine and GPU metrics"]')
    await expect(frame).toBeVisible()
    const source = new URL(await frame.getAttribute('src') ?? '', page.url())
    expect(source.searchParams.getAll('var-machine')).toEqual(selected)
    expect(source.searchParams.get('var-gpu_group')).toBe('0')
  }
})

test('Overview waiting time switches between the three measured periods', async ({ page }) => {
  await mockGrafana(page)
  await page.goto(`${origin}${prefix}`)
  const card = page.locator('.waiting-card')
  const periods = card.getByRole('group', { name: 'Waiting time period' })
  await expect(card).toContainText('1 hr 0 min')
  await periods.getByRole('button', { name: '24h' }).click()
  await expect(card).toContainText('2 min')
  await expect(periods.getByRole('button', { name: '24h' })).toHaveAttribute('aria-pressed', 'true')
  await periods.getByRole('button', { name: '30d' }).click()
  await expect(card).toContainText('13 min')
})

test('Machines lists every configured machine with its role while Overview stays compact', async ({ page }) => {
  await mockGrafana(page)
  await page.goto(`${origin}${prefix}`)
  await page.getByRole('group', { name: 'Machines view' }).getByRole('button', { name: 'List view' }).click()
  await expect(page.locator('.machine-list thead th')).toHaveText(['Machine', 'Health', 'State', 'Speed /home | Internet', 'GPUs'])
  await expect(page.locator('.machine-list tbody tr')).toHaveCount(2)
  await page.goto(`${origin}${prefix}#machines`)
  await expect(page.locator('.machine-list thead th')).toHaveText(['Machine', 'Node type', 'Health', 'State', 'Speed /home | Internet', 'GPUs'])
  const rows = page.locator('.machine-list tbody tr')
  await expect(rows).toHaveCount(4)
  await expect(rows.locator('th')).toContainText(['front', 'gpu1', 'cpu1', 'store'])
  await expect(rows.locator('td:nth-child(2)')).toHaveText(['Front node', 'Compute', 'Compute', 'Storage'])
  await expect(rows.locator('.role-tag')).toHaveCount(0)
  await expect(page.locator('.machine-spec')).toHaveCount(4)
})

test('machine labels explain health, speed, and waiting updates', async ({ page }) => {
  await mockGrafana(page)
  const data = snapshot() as { nodes: { name: string; specs?: object }[] }
  await page.route(`**${prefix}data/status.json`, route => route.fulfill({ json: {
    ...data, nodes: data.nodes.map(node => node.name === 'gpu1' ? {
      ...node, specs: { ...node.specs, pending_updates: 3, speeds: {
        home_large_read: 125, home_large_write: 118, home_small_read: 114, home_small_write: 112, internet_download: 95,
      }, speeds_measured_at: Date.now() / 1000 },
    } : node),
  } }))
  await page.goto(`${origin}${prefix}`)
  await page.getByRole('group', { name: 'Machines view' }).getByRole('button', { name: 'List view' }).click()
  const row = page.locator('.machine-list tbody tr').filter({ hasText: 'gpu1' })
  await row.getByRole('button', { name: 'Healthy' }).click()
  await expect(page.getByRole('dialog', { name: 'gpu1: Healthy' })).toContainText('No health problems reported')
  await page.getByRole('button', { name: 'Close' }).click()
  await expect(row.getByRole('button', { name: '/home speed' })).toContainText('112 MB/s')
  await expect(row.getByRole('button', { name: 'Internet speed' })).toContainText('95 MB/s')
  await row.getByRole('button', { name: '/home speed' }).click()
  await expect(page.getByRole('dialog', { name: 'gpu1: /home speed' })).toContainText('One large file: read 125 MB/s')
  await page.keyboard.press('Escape')
  await page.goto(`${origin}${prefix}#machines`)
  const card = page.getByRole('region', { name: 'gpu1 specs' })
  await card.getByRole('button', { name: 'Needs update' }).click()
  await expect(page.getByRole('dialog', { name: 'gpu1: Needs update' })).toContainText('package update')
})

test('/home speed box color follows the shown small-file write speed', async ({ page }) => {
  await mockGrafana(page)
  const data = snapshot() as { nodes: { name: string; specs?: object }[] }
  let smallWrite = 0.9
  let largeRead = 125
  await page.route(`**${prefix}data/status.json`, route => route.fulfill({ json: {
    ...data, nodes: data.nodes.map(node => node.name === 'gpu1' ? {
      ...node, specs: { ...node.specs, speeds: {
        home_large_read: largeRead, home_small_write: smallWrite, internet_download: 95,
      } },
    } : node),
  } }))
  for (const [index, [write, read, color]] of ([[0.9, 125, 'state-problem'], [1, 125, 'state-warning'], [10, 0, 'state-healthy']] as const).entries()) {
    smallWrite = write
    largeRead = read
    if (index === 0) await page.goto(`${origin}${prefix}#machines`)
    else await page.reload()
    const row = page.locator('.machine-list tbody tr').filter({ hasText: 'gpu1' })
    const box = row.getByRole('button', { name: '/home speed' })
    await expect(box).toContainText(`${Math.round(write)} MB/s`)
    await expect(box).toHaveClass(new RegExp(`\\b${color}\\b`))
  }
})

test('the guide covers supported containers and interactive notebooks without claiming unsupported queue rules', async ({ page }) => {
  await mockGrafana(page)
  await page.goto(`${origin}${prefix}#docs`)
  await expect(page.getByRole('heading', { name: 'Other settings' })).toBeVisible()
  await expect(page.getByRole('tab', { name: 'Containers' })).toBeVisible()
  await page.getByRole('tab', { name: 'Containers' }).click()
  await expect(page.getByText('apptainer exec --nv', { exact: false })).toBeVisible()
  await page.getByRole('tab', { name: 'Interactive notebooks' }).click()
  await expect(page.locator('.user-guide code').filter({ hasText: 'interactive-notebook.sh' }).first()).toBeVisible()
  await expect(page.locator('.user-guide code').filter({ hasText: 'scp interactive-notebook.sh' }).first()).toBeVisible()
  const docs = await page.request.get(`${origin}${prefix}docs.md`)
  expect(await docs.text()).toContain('Apptainer')
  expect(await docs.text()).toContain('scp interactive-notebook.sh')
  expect(await docs.text()).not.toContain('Every job must state --time')
  const helper = await page.request.get(`${origin}${prefix}job-examples/interactive-notebook.sh`)
  expect(helper.ok()).toBe(true)
})

test('Cluster Usage excludes samples from a stale snapshot', async ({ page }) => {
  await mockGrafana(page)
  const expressions: string[] = []
  await page.route(`**${prefix}grafana/api/ds/query`, route => {
    const body = route.request().postDataJSON() as { queries: { refId: string; expr: string }[] }
    if (body.queries[0]?.refId !== 'A') return route.fallback()
    expressions.push(...body.queries.map(query => query.expr))
    const fresh = body.queries.every(query => query.expr.includes('time() - cluster_snapshot_timestamp_seconds < 90'))
    const times = [Date.now() - 60_000, Date.now()]
    const frame = (values: number[]) => ({ schema: { fields: [{}, { labels: { node: 'gpu1' } }] }, data: { values: [times, values] } })
    return route.fulfill({ json: { results: { A: { frames: fresh ? [] : [frame([2, 3])] }, B: { frames: fresh ? [] : [frame([4, 4])] } } } })
  })
  await page.goto(`${origin}${prefix}`)
  await expect(page.locator('.gpu-allocation-chart')).toContainText('No GPU history yet')
  expect(expressions).toHaveLength(2)
})

test('planned maintenance is distinct from a machine fault and a paused queue shows 0 / 0', async ({ page }) => {
  await mockGrafana(page)
  const data = snapshot() as { nodes: { name: string; health: string; health_details: string[] }[] }
  const nodes = data.nodes.map(node => node.name === 'gpu1' ? { ...node, health: 'Maintenance', health_details: ['The queue is paused (partitions down)'] } : node)
  await page.route(`**${prefix}data/status.json`, route => route.fulfill({ json: { ...data, nodes, queue_paused: true } }))
  await page.goto(`${origin}${prefix}`)
  await page.getByRole('group', { name: 'Machines view' }).getByRole('button', { name: 'List view' }).click()
  await expect(page.locator('.stat').filter({ hasText: 'GPUs allocated' })).toContainText('0 / 0')
  await expect(page.locator('.machine-list tbody tr').filter({ hasText: 'gpu1' })).toContainText('Scheduled maintenance')
})

test('Cluster policy shows live partition GPU and policy limits', async ({ page }) => {
  await mockGrafana(page)
  await page.goto(origin + prefix + '#policy')
  const box = page.locator('.partition-summary pre')
  await expect(box).toContainText('PARTITION')
  await expect(box).toContainText('MOST PER JOB')
  await expect(box).toContainText('main (default)')
  await expect(box).toContainText(/gpu1\s+interactive, main/)
  await expect(box).toContainText('NVIDIA RTX A6000')
  await page.setViewportSize({ width: 320, height: 720 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320)
})

test('the header and Machines page fit a 320px phone', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 720 })
  await mockGrafana(page)
  await page.goto(origin + prefix)
  await expect(page.getByRole('button', { name: 'Change accent color' })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320)
  await page.goto(origin + prefix + '#machines')
  await expect(page.locator('.machine-list')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320)
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

test('monitor mode shows measured machines and login names without Slurm claims', async ({ page }) => {
  await mockGrafana(page)
  await page.route(`**${prefix}grafana/api/ds/query`, route => {
    const body = route.request().postDataJSON() as { queries: { refId: string }[] }
    const frame = (gpu: string, value: number) => ({ schema: { fields: [{}, { labels: { machine: 'gpu1', gpu } }] }, data: { values: [[Date.now()], [value]] } })
    const results = Object.fromEntries(body.queries.map(query => [query.refId, { frames: [frame('0', query.refId === 'memory' ? 8 * gib : 55), frame('1', query.refId === 'memory' ? 2 * gib : 5)] }]))
    return route.fulfill({ json: { results } })
  })
  await page.route(`**${prefix}site.json`, route => route.fulfill({ json: {
    mode: 'monitor', cluster_name: 'mylab', logo: null, login_address: 'login.mylab.example.org', users: ['alice', 'bob'],
  } }))
  await page.route(`**${prefix}data/status.json`, route => route.fulfill({ json: {
    mode: 'monitor', generated_at: new Date().toISOString(), refresh_seconds: 30, total_gpus: 2,
    nodes: [
      { name: 'host', role: 'Monitor', health: 'Healthy', health_details: [], gpu_usage: 'Not applicable', total_gpus: 0,
        specs: { gpu_count: 0, gpus: [], disks: [], speeds: { internet_download: 80 } } },
      { name: 'gpu1', role: 'Machine', health: 'Healthy', health_details: [], gpu_usage: 'Active', total_gpus: 2,
        specs: { gpu_count: 2, gpus: [{ index: '0', model: 'RTX', memory_bytes: 48 * gib }, { index: '1', model: 'RTX', memory_bytes: 48 * gib }], disks: [] } },
    ],
  } }))
  const seen = watch(page)
  await page.goto(`${origin}${prefix}`)
  await expect(page.locator('.brand-sub')).toHaveText('Machine monitor')
  await expect(page.locator('.machine-list tbody tr').filter({ hasText: 'host' })).toContainText('80 MB/s')
  const nav = page.getByRole('navigation', { name: 'Cluster navigation' })
  expect(await nav.getByRole('link').allInnerTexts()).toEqual(['Overview', 'Machines', 'Usage', 'Users'])
  await expect(page.getByRole('region', { name: 'Cluster summary' })).toContainText('2')
  await expect(page.locator('.machine-list tbody tr')).toHaveCount(2)
  await nav.getByRole('link', { name: 'Machines' }).click()
  await expect(page.locator('.machine-list thead th').nth(1)).toHaveText('Node type')
  await expect(page.locator('.machine-list tbody tr td:nth-child(2)')).toHaveText(['Monitor', 'Machine'])
  await expect(page.getByRole('region', { name: 'gpu1 specs' })).toContainText('2× RTX')
  await expect(page.getByRole('table', { name: 'GPU readings' })).toContainText('GPU 0')
  await expect(page.getByRole('table', { name: 'GPU readings' })).toContainText('55%')
  await expect(page.locator('.cluster-map-canvas canvas')).toBeVisible()
  await expect(page.locator('.cluster-map-canvas')).toHaveAttribute('aria-label', /gpu1: machine, health Healthy, GPU activity GPU 0 55%/)
  await page.getByRole('button', { name: 'Show cluster map' }).click()
  await expect(page.locator('.cluster-map-canvas')).toHaveCount(0)
  await nav.getByRole('link', { name: 'Users' }).click()
  await expect(page.getByRole('list', { name: 'Login names' })).toContainText('alice')
  await expect(page.getByRole('list', { name: 'Login names' })).toContainText('bob')
  await expect(page.locator('main')).not.toContainText(/allocated|fair.share|pending|queue|job|NFS/i)
  await page.goto(`${origin}${prefix}#usage?machine=host&machine=gpu1`)
  const metrics = page.locator('iframe[title="Machine and GPU metrics"]')
  await expect(metrics).toBeVisible()
  expect(new URL(await metrics.getAttribute('src') ?? '', page.url()).searchParams.getAll('var-machine')).toEqual(['host', 'gpu1'])
  expectInsidePrefix(seen.requests)
  expect(seen.errors).toEqual([])
})

test('monitor mode shows an empty user list', async ({ page }) => {
  await page.route(`**${prefix}site.json`, route => route.fulfill({ json: {
    mode: 'monitor', cluster_name: 'mylab', logo: null, login_address: 'login.mylab.example.org', users: [],
  } }))
  await page.route(`**${prefix}data/status.json`, route => route.fulfill({ json: {
    mode: 'monitor', generated_at: new Date().toISOString(), refresh_seconds: 30, nodes: [], total_gpus: 0,
  } }))
  await page.goto(`${origin}${prefix}#users`)
  await expect(page.getByText('No login names configured.')).toBeVisible()
})

test('monitor mode does not infer a GPU total from incomplete inventory', async ({ page }) => {
  await page.route(`**${prefix}site.json`, route => route.fulfill({ json: {
    mode: 'monitor', cluster_name: 'mylab', logo: null, login_address: 'login.mylab.example.org', users: [],
  } }))
  await page.route(`**${prefix}data/status.json`, route => route.fulfill({ json: {
    mode: 'monitor', generated_at: new Date().toISOString(), refresh_seconds: 30, total_gpus: null,
    nodes: [{ name: 'gpu1', role: 'Machine', health: 'Unknown', gpu_usage: 'Unknown', total_gpus: null }],
  } }))
  await page.goto(`${origin}${prefix}`)
  await expect(page.locator('.stat').filter({ hasText: 'GPUs installed' })).toContainText('Unknown')
})
