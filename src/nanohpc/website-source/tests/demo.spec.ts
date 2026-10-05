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
    ['Users', ['GPU usage history']],
    ['Cluster usage', ['Machine and GPU metrics']],
  ] as const) {
    await nav.getByRole('link', { name: section, exact: true }).click()
    for (const title of titles) {
      const frame = page.locator(`iframe[title="${title}"]`)
      await expect(frame).toHaveAttribute('src', /^https:\/\/snapshots\.raintank\.io\/dashboard\/snapshot\/[A-Za-z0-9]+\?theme=light&kiosk$/)
    }
  }
})

test('Machines table shows compute states, home speeds, and GPU availability', async ({ page }) => {
  await page.goto(`${origin}${prefix}#machines`)
  const table = page.locator('.machine-list table')
  await expect(table.locator('thead th')).toHaveText(['Machine', 'Health', 'State', 'Speed /home', 'GPUs'])
  await expect(table.locator('tbody tr')).toHaveCount(3)
  await expect(table.getByRole('link', { name: 'front', exact: true })).toHaveCount(0)
  await expect(table.locator('tbody tr').filter({ hasText: 'Threadripper' })).toContainText('Idle')
  await expect(table.locator('tbody tr').filter({ has: page.getByRole('link', { name: 'H200', exact: true }) })).toContainText('MB/s')
  await expect(table.getByRole('link', { name: 'H100', exact: true })).toHaveCount(0)
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
  await expect(page.locator('.brand-title')).toHaveText('nanoHPC')
  await expect(page.locator('.brand-sub')).toHaveText('lightweight Slurm cluster and monitoring tool')
  await expect(page.getByText('Public demo with fictional machines')).toHaveCount(0)
  await expect(page.getByText('Sample data', { exact: true })).toHaveCount(0)
  await expect(page.getByText('Updates every 30s')).toBeVisible()
  await expect(page.locator('.freshness small')).toContainText('Last update')
  const firstUpdate = await page.locator('.freshness small').textContent()
  await page.clock.fastForward(30_000)
  await expect(page.locator('.freshness small')).not.toHaveText(firstUpdate ?? '')
  await expect(page.getByText('Data is stale')).toHaveCount(0)
  await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible()
  const guide = readFileSync(`${built}/docs.md`, 'utf8')
  expect(guide).toMatch(/^# nanoHPC documentation/m)
  expect(guide).toContain('Host nanoHPC')
  const demoSnapshot = JSON.parse(readFileSync(`${built}/data/status.json`, 'utf8'))
  const dgx = demoSnapshot.nodes.find((node: { name: string }) => node.name === 'Nvidia DGX')
  expect(dgx.specs.unified_memory_gb).toBe(128)
  expect(dgx.specs.gpus[0].memory_bytes).toBe(128 * 1024 ** 3)
  await expect(page.locator('.gpu-allocation-chart .recharts-area').first()).toBeVisible()
  const curve = await page.locator('.gpu-allocation-chart .recharts-area path').first().getAttribute('d') ?? ''
  const curveSegments = (curve.match(/C/g) ?? []).length
  expect(curveSegments).toBeGreaterThan(40)
  expect(curveSegments).toBeLessThan(150)
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
  await expect(page.getByRole('button', { name: '24h' })).toHaveAttribute('aria-pressed', 'true')
  const nav = page.getByRole('navigation', { name: 'Cluster navigation' })
  for (const name of ['How to', 'Jobs', 'Machines', 'Users', 'Cluster usage', 'Cluster policy']) {
    await nav.getByRole('link', { name, exact: true }).click()
    await expect(page.getByRole('heading', { name, exact: true }).first()).toBeVisible()
  }
  await nav.getByRole('link', { name: 'How to', exact: true }).click()
  await expect(page.getByRole('heading', { name: "Do's and don'ts" })).toBeVisible()
  await page.getByRole('tablist', { name: 'Shared vs scratch' }).getByRole('tab', { name: 'Scratch mode' }).click()
  await expect(page.getByRole('tablist', { name: 'Scratch mode' }).getByRole('tab', { name: 'Basic' })).toHaveAttribute('aria-selected', 'true')
  for (const name of ['Home space', 'GPU software', 'Caches']) await expect(page.getByRole('tab', { name })).toBeVisible()
  await nav.getByRole('link', { name: 'Machines', exact: true }).click()
  for (const name of ['H200', 'Threadripper', 'Nvidia DGX']) await expect(page.getByText(name, { exact: true }).first()).toBeVisible()
  await expect(page.locator('.machine-list')).not.toContainText('FPGA')
  await expect(page.getByRole('region', { name: 'Nvidia DGX specs' })).toContainText('Unified memory')
  await expect(page.getByRole('region', { name: 'Nvidia DGX specs' })).toContainText('128 GB')
  await nav.getByRole('link', { name: 'Users', exact: true }).click()
  for (const name of ['Alice', 'Bob', 'Mike']) await expect(page.getByText(name, { exact: true }).first()).toBeVisible()
  await nav.getByRole('link', { name: 'Cluster policy', exact: true }).click()
  for (const name of ['main (default)', 'interactive', 'Login on front', 'All partitions', 'Queue ranking', 'Partitions']) {
    await expect(page.getByRole('heading', { name, exact: true }).first()).toBeVisible()
  }
  await expect(page.locator('.partition-summary')).toContainText('Nvidia DGX')
  await expect(page.locator('.partition-summary')).not.toContainText('FPGA')
  await nav.getByRole('link', { name: 'Jobs', exact: true }).click()
  await expect(page.locator('iframe[title="Running Jobs and Queue"]')).toBeVisible()
  await expect(page.locator('iframe[title="Queue history"]')).toBeVisible()
  await page.goto(`${origin}${prefix}#queue?state=PENDING`)
  await expect(page.getByRole('table', { name: 'Sample jobs' }).locator('tbody tr')).toHaveCount(3)
  await nav.getByRole('link', { name: 'Machines', exact: true }).click()
  await expect(page.getByRole('img', { name: /Cluster map:/ })).toBeVisible()
  await expect(page.getByRole('group', { name: 'Cluster map layout' }).getByRole('button')).toHaveText(['Default', 'Partitions', 'Geographic'])
  await page.getByRole('button', { name: 'Geographic', exact: true }).click()
  await expect(page.getByRole('img', { name: /Cluster map:/ })).toHaveAttribute('aria-label', /LAB A: Threadripper, H200; LAB B: Nvidia DGX/)
  await page.getByRole('button', { name: 'Partitions', exact: true }).click()
  await expect(page.getByRole('img', { name: /Cluster map:/ })).toHaveAttribute('aria-label', /INTERACTIVE \+ MAIN:/)
  await expect(page.getByRole('img', { name: /Cluster map:/ })).toBeVisible()
  await expect(page.getByRole('img', { name: /Cluster map:/ })).not.toHaveAttribute('aria-label', /FPGA/)
  expect(await page.locator('iframe').count()).toBe(0)
  expect(outside).toEqual([])
  expect(failed).toEqual([])
})
