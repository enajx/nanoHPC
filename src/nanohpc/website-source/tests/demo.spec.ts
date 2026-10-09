import { existsSync, readFileSync, statSync } from 'node:fs'
import { createServer, type Server } from 'node:http'
import { extname, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { expect, test } from '@playwright/test'

const built = resolve(fileURLToPath(new URL('../../website-demo/', import.meta.url)))
const prefix = '/nanohpc-demo/'
let server: Server
let origin: string

test.beforeAll(async () => {
  server = createServer((request, response) => {
    const pathname = new URL(request.url ?? '/', 'http://localhost').pathname
    const file = resolve(built, '.' + pathname.slice(prefix.length - 1) + (pathname.endsWith('/') ? 'index.html' : ''))
    if (!pathname.startsWith(prefix) || !file.startsWith(built + sep) || !existsSync(file) || !statSync(file).isFile()) {
      response.writeHead(404).end()
      return
    }
    const types: Record<string, string> = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.md': 'text/markdown' }
    response.writeHead(200, { 'Content-Type': types[extname(file)] ?? 'application/octet-stream' }).end(readFileSync(file))
  })
  await new Promise<void>(done => server.listen(0, '127.0.0.1', done))
  const address = server.address()
  if (!address || typeof address === 'string') throw new Error('No demo server address')
  origin = `http://127.0.0.1:${address.port}`
})

test.afterAll(async () => {
  await new Promise<void>(done => server.close(() => done()))
})

test('the four demo dashboards embed fixed Grafana snapshots', async ({ page }) => {
  await page.route('https://snapshots.raintank.io/dashboard/snapshot/**', route => route.fulfill({ contentType: 'text/html', body: '<h1>Grafana sample snapshot</h1>' }))
  await page.goto(`${origin}${prefix}`)
  const nav = page.getByRole('navigation', { name: 'Cluster navigation' })
  for (const [section, titles] of [
    ['Jobs', ['Running Jobs and Queue', 'Queue history']],
    ['Users', ['GPU usage history per user']],
    ['Cluster usage', ['Machine and GPU metrics']],
  ] as const) {
    await nav.getByRole('link', { name: section, exact: true }).click()
    for (const title of titles) {
      const frame = page.locator(`iframe[title="${title}"]`)
      await expect(frame).toHaveAttribute('src', /^https:\/\/snapshots\.raintank\.io\/dashboard\/snapshot\/[A-Za-z0-9]+\?theme=dark&kiosk$/)
    }
  }
})

test('Machines table shows machine states, demo link speeds, and GPU availability', async ({ page }) => {
  await page.goto(`${origin}${prefix}`)
  await page.getByRole('group', { name: 'Machines view' }).getByRole('button', { name: 'List view' }).click()
  await expect(page.locator('.machine-list table thead th').nth(3)).toHaveText('Link Speed')
  await expect(page.locator('.machine-list table tbody tr td:nth-child(4)')).toHaveText(Array(5).fill('1 Gb/s'))
  await page.goto(`${origin}${prefix}#machines`)
  const table = page.locator('.machine-list table')
  await expect(table.locator('thead th')).toHaveText(['Machine', 'Health', 'State', 'Link Speed', 'GPUs'])
  await expect(table.locator('tbody tr')).toHaveCount(6)
  await expect(table.locator('tbody tr th a')).toHaveText(['front', 'H100', 'H200', 'B200', 'Threadripper', 'DGX'])
  await expect(table.getByRole('link', { name: 'front', exact: true })).toHaveCount(1)
  await expect(table.locator('tbody tr').filter({ hasText: 'Threadripper' })).toContainText('Idle')
  await expect(table.locator('tbody tr td:nth-child(4)')).toHaveText(Array(6).fill('1 Gb/s'))
  await expect(table.getByRole('link', { name: 'B200', exact: true })).toHaveCount(1)
})

test('demo GPU history changes with the selected time range', async ({ page }) => {
  await page.goto(`${origin}${prefix}`)
  const chart = page.locator('.gpu-allocation-chart')
  const ranges = chart.getByRole('group', { name: 'Time range' })
  const curve = chart.locator('.recharts-area path').last()
  await ranges.getByRole('button', { name: '30d' }).click()
  await expect(chart.getByRole('heading', { name: 'Cluster Usage (30 days)' })).toBeVisible()
  await expect(ranges.getByRole('button', { name: '30d' })).toHaveAttribute('aria-pressed', 'true')
  await expect(curve).toBeVisible()
  const month = await curve.getAttribute('d')
  await ranges.getByRole('button', { name: '7d' }).click()
  await expect(chart.getByRole('heading', { name: 'Cluster Usage (7 days)' })).toBeVisible()
  await expect(ranges.getByRole('button', { name: '7d' })).toHaveAttribute('aria-pressed', 'true')
  await expect(curve).not.toHaveAttribute('d', month ?? '')
  const week = await curve.getAttribute('d')
  await ranges.getByRole('button', { name: '24h' }).click()
  await expect(chart.getByRole('heading', { name: 'Cluster Usage (24 hours)' })).toBeVisible()
  await expect(curve).not.toHaveAttribute('d', week ?? '')
  await expect(ranges.getByRole('button', { name: '24h' })).toHaveAttribute('aria-pressed', 'true')
})

test('demo waiting time uses its selected period', async ({ page }) => {
  await page.goto(`${origin}${prefix}`)
  const card = page.locator('.waiting-card')
  const periods = card.getByRole('group', { name: 'Waiting time period' })
  await expect(card).toContainText('7 min')
  await periods.getByRole('button', { name: '24h' }).click()
  await expect(card).toContainText('3 min')
  await periods.getByRole('button', { name: '30d' }).click()
  await expect(card).toContainText('12 min')
})

test('Overview map updates its sample measurements after demo data loads', async ({ page }) => {
  await page.goto(`${origin}${prefix}`)
  const map = page.locator('.overview-machines .cluster-map-canvas')
  await expect(map).toHaveAttribute('aria-label', /H100: 3 of 4 GPUs allocated.*shared home \d+ requests per second/)
  await expect(map).not.toHaveAttribute('aria-label', /GPU busy unknown/)
})

test('demo header and Machines page fit a 320px phone', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 720 })
  await page.goto(origin + prefix)
  await expect(page.getByRole('button', { name: 'Change accent color' })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320)
  await page.goto(origin + prefix + '#machines')
  await expect(page.locator('.machine-list')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320)
})

test('static demo lets visitors browse all pages and view Grafana snapshots', async ({ page }) => {
  const failed: string[] = []
  const outside: string[] = []
  page.on('pageerror', error => failed.push(error.message))
  page.on('response', response => { if (!response.ok()) failed.push(`${response.status()} ${response.url()}`) })
  page.on('request', request => {
    const url = new URL(request.url())
    if (url.origin !== origin && url.origin !== 'https://snapshots.raintank.io') outside.push(request.url())
  })
  await page.route('https://snapshots.raintank.io/dashboard/snapshot/**', route => route.fulfill({ contentType: 'text/html', body: '<h1>Grafana sample snapshot</h1>' }))
  await page.clock.install({ time: new Date('2026-10-05T12:34:00') })
  await page.goto(`${origin}${prefix}`)
  const brand = page.locator('header .brand')
  const mark = brand.getByRole('img', { name: 'nanoHPC mark' })
  await expect(mark).toBeVisible()
  await expect.poll(() => mark.evaluate(image => (image as HTMLImageElement).naturalWidth)).toBeGreaterThan(0)
  await expect(brand.getByRole('img', { name: 'nanoHPC', exact: true })).toBeVisible()
  await expect(brand.locator('.brand-sub')).toHaveCount(0)
  await expect(page.getByText('Public demo with fictional machines')).toHaveCount(0)
  await expect(page.getByText('Sample data', { exact: true })).toHaveCount(0)
  await expect(page.getByText('Updates every 30s')).toBeVisible()
  await expect(page.locator('.freshness small')).toContainText('Last update')
  const firstUpdate = await page.locator('.freshness small').textContent()
  await page.clock.fastForward(30_000)
  await expect(page.locator('.freshness small')).not.toHaveText(firstUpdate ?? '')
  await expect(page.getByText('Data is stale')).toHaveCount(0)
  await expect(page.getByRole('heading', { name: 'Overview' })).toHaveCSS('width', '1px')
  const guide = readFileSync(`${built}/docs.md`, 'utf8')
  expect(guide).toMatch(/^# nanoHPC documentation/m)
  expect(guide).toContain('Host nanoHPC')
  const demoSnapshot = JSON.parse(readFileSync(`${built}/data/status.json`, 'utf8'))
  const dgx = demoSnapshot.nodes.find((node: { name: string }) => node.name === 'DGX')
  expect(demoSnapshot.total_gpus).toBe(14)
  expect(demoSnapshot.allocated_gpus).toBe(12)
  await expect(page.locator('.stat-grid')).toContainText('12 / 14')
  expect(dgx.total_gpus).toBe(2)
  expect(dgx.specs.gpus).toHaveLength(2)
  expect(demoSnapshot.jobs.filter((job: { state: string }) => job.state === 'RUNNING').reduce((sum: number, job: { gpus: number }) => sum + job.gpus, 0)).toBe(12)
  expect(demoSnapshot.nodes.reduce((sum: number, node: { total_gpus: number; available_gpus: number }) => sum + node.total_gpus - node.available_gpus, 0)).toBe(12)
  expect(dgx.specs.unified_memory_gb).toBe(128)
  expect(dgx.specs.gpus[0].memory_bytes).toBe(128 * 1024 ** 3)
  await expect(page.locator('.gpu-allocation-chart .recharts-area').first()).toBeVisible()
  await expect(page.locator('.gpu-allocation-chart .recharts-area')).toHaveCount(5)
  const curve = await page.locator('.gpu-allocation-chart .recharts-area path').first().getAttribute('d') ?? ''
  expect((curve.match(/C/g) ?? []).length).toBeGreaterThan(8)
  await expect(page.getByRole('button', { name: 'About nanoHPC' })).toBeVisible()
  await page.getByRole('button', { name: 'About nanoHPC' }).click()
  await expect(page.getByRole('dialog', { name: 'About nanoHPC' })).toBeVisible()
  await expect(page.getByRole('dialog', { name: 'About nanoHPC' }).getByRole('link', { name: 'User documentation' })).toHaveAttribute('href', '#docs')
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog', { name: 'About nanoHPC' })).toHaveCount(0)
  const settings = page.getByRole('button', { name: 'Settings' })
  await expect(settings).toBeVisible()
  await settings.click()
  const settingsDialog = page.getByRole('dialog', { name: 'Settings' })
  await expect(settingsDialog).toContainText('read-only cluster monitor')
  await expect(settingsDialog).toContainText('Settings cannot be changed here.')
  await page.keyboard.press('Escape')
  await expect(settingsDialog).toHaveCount(0)
  await expect(page.locator('main footer').getByRole('link', { name: 'nanoHPC' })).toHaveAttribute('href', 'https://github.com/enajx/nanoHPC')
  await expect(page.locator('main footer')).toContainText('This demo runs on nanoHPC')
  const accents = page.getByRole('button', { name: 'Change accent color' })
  await expect(page.locator('html')).toHaveAttribute('data-accent', 'coral')
  await accents.click()
  await expect(page.locator('html')).toHaveAttribute('data-accent', 'sand')
  await accents.click()
  await expect(page.locator('html')).toHaveAttribute('data-accent', 'coral')
  await page.getByRole('group', { name: 'Time range' }).getByRole('button', { name: '24h' }).click()
  await expect(page.getByRole('group', { name: 'Time range' }).getByRole('button', { name: '24h' })).toHaveAttribute('aria-pressed', 'true')
  const nav = page.getByRole('navigation', { name: 'Cluster navigation' })
  for (const name of ['How to', 'Jobs', 'Machines', 'Users', 'Cluster usage', 'Cluster policy']) {
    await nav.getByRole('link', { name, exact: true }).click()
    await expect(page.getByRole('heading', { name, exact: true }).first()).toBeVisible()
  }
  await nav.getByRole('link', { name: 'How to', exact: true }).click()
  await expect(page.getByRole('heading', { name: "Do's and don'ts" })).toBeVisible()
  await page.getByRole('tablist', { name: 'Shared vs scratch' }).getByRole('tab', { name: 'Scratch mode' }).click()
  await expect(page.getByRole('tablist', { name: 'Scratch mode' }).getByRole('tab', { name: 'Basic' })).toHaveAttribute('aria-selected', 'true')
  for (const name of ['Home space', 'GPU software', 'Containers', 'Interactive notebooks', 'Caches']) await expect(page.getByRole('tab', { name })).toBeVisible()
  const notebook = await page.request.get(`${origin}${prefix}job-examples/interactive-notebook.sh`)
  expect(notebook.ok()).toBe(true)
  await nav.getByRole('link', { name: 'Machines', exact: true }).click()
  for (const name of ['H100', 'H200', 'B200', 'Threadripper', 'DGX']) await expect(page.getByText(name, { exact: true }).first()).toBeVisible()
  await expect(page.locator('.machine-list')).not.toContainText('FPGA')
  await expect(page.getByRole('region', { name: 'DGX specs' })).toContainText('Unified memory')
  await expect(page.getByRole('region', { name: 'DGX specs' })).toContainText('128 GB')
  await nav.getByRole('link', { name: 'Users', exact: true }).click()
  for (const name of ['Alice', 'Bob', 'Mike']) await expect(page.getByText(name, { exact: true }).first()).toBeVisible()
  await nav.getByRole('link', { name: 'Cluster policy', exact: true }).click()
  for (const name of ['training (default)', 'inference', 'Login on front', 'All partitions', 'Queue ranking', 'Partitions']) {
    await expect(page.getByRole('heading', { name, exact: true }).first()).toBeVisible()
  }
  await expect(page.locator('.partition-summary')).toContainText('DGX')
  await expect(page.locator('.partition-summary')).not.toContainText('FPGA')
  await nav.getByRole('link', { name: 'Jobs', exact: true }).click()
  await expect(page.locator('iframe[title="Running Jobs and Queue"]')).toBeVisible()
  await expect(page.locator('iframe[title="Queue history"]')).toBeVisible()
  await page.goto(`${origin}${prefix}#queue?state=PENDING`)
  await expect(page.getByRole('table', { name: 'Sample jobs' }).locator('tbody tr')).toHaveCount(2)
  await nav.getByRole('link', { name: 'Machines', exact: true }).click()
  await expect(page.getByRole('img', { name: /Cluster map:/ })).toBeVisible()
  await expect(page.getByRole('group', { name: 'Cluster map layout' }).getByRole('button')).toHaveText(['Default', 'Partitions', 'Geographic'])
  await page.getByRole('button', { name: 'Geographic', exact: true }).click()
  await expect(page.getByRole('img', { name: /Cluster map:/ })).toHaveAttribute('aria-label', /LAB A: H100, H200, B200, Threadripper; LAB B: DGX/)
  await page.getByRole('button', { name: 'Partitions', exact: true }).click()
  await expect(page.getByRole('img', { name: /Cluster map:/ })).toHaveAttribute('aria-label', /TRAINING: H100, H200, B200; INFERENCE: Threadripper, DGX/)
  await expect(page.getByRole('img', { name: /Cluster map:/ })).toBeVisible()
  await expect(page.getByRole('img', { name: /Cluster map:/ })).not.toHaveAttribute('aria-label', /FPGA/)
  expect(await page.locator('iframe').count()).toBe(0)
  expect(outside).toEqual([])
  expect(failed).toEqual([])
})

test('demo header fits a 320px phone', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 640 })
  await page.route('https://snapshots.raintank.io/dashboard/snapshot/**', route => route.fulfill({ contentType: 'text/html', body: '<h1>Grafana sample snapshot</h1>' }))
  await page.goto(`${origin}${prefix}`)
  const brand = page.locator('header .brand')
  await expect(brand.getByRole('img', { name: 'nanoHPC mark' })).toBeVisible()
  await expect(brand.getByRole('img', { name: 'nanoHPC', exact: true })).toBeVisible()
  const wordmark = await brand.locator('.brand-logo').boundingBox()
  const buttons = await page.locator('header .header-buttons').boundingBox()
  expect(wordmark).not.toBeNull()
  expect(buttons).not.toBeNull()
  expect(wordmark!.x + wordmark!.width).toBeLessThan(buttons!.x)
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320)
})

test('demo partition map keeps machine names clear of other names and servers', async ({ page }) => {
  await page.goto(`${origin}${prefix}#machines`)
  await page.getByRole('button', { name: 'Partitions', exact: true }).click()
  const boxes = await page.locator('.cluster-map-canvas').getAttribute('data-label-boxes')
  expect(boxes).not.toBeNull()
  type Box = { name: string; x: number; y: number; w: number; h: number; server: { x: number; y: number; w: number; h: number } }
  const labels = JSON.parse(boxes ?? '[]') as Box[]
  expect(labels).toHaveLength(6)
  const overlap = (a: Box, b: Pick<Box, 'x' | 'y' | 'w' | 'h'>) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h
  for (const a of labels) for (const b of labels) if (a !== b) {
    expect(overlap(a, b), `${a.name} label covers ${b.name} label`).toBe(false)
    expect(overlap(a, b.server), `${a.name} label covers ${b.name} server`).toBe(false)
  }
})
