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

test('static demo lets visitors browse all pages and interact with charts without Grafana', async ({ page }) => {
  const failed: string[] = []
  const outside: string[] = []
  page.on('pageerror', error => failed.push(error.message))
  page.on('response', response => { if (!response.ok()) failed.push(`${response.status()} ${response.url()}`) })
  page.on('request', request => {
    const url = new URL(request.url())
    if (url.origin !== origin || !url.pathname.startsWith(prefix) || url.pathname.includes('grafana')) outside.push(request.url())
  })
  await page.goto(`${origin}${prefix}`)
  await expect(page.getByText('Sample data', { exact: true })).toBeVisible()
  await expect(page.getByText('Data is stale')).toHaveCount(0)
  await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible()
  const guide = readFileSync(`${built}/docs.md`, 'utf8')
  expect(guide).toContain('Host nanohpc-demo')
  await expect(page.locator('.gpu-allocation-chart .recharts-area').first()).toBeVisible()
  await page.getByRole('group', { name: 'Time range' }).getByRole('button', { name: '24h' }).click()
  await expect(page.getByRole('button', { name: '24h' })).toHaveAttribute('aria-pressed', 'true')
  const nav = page.getByRole('navigation', { name: 'Cluster navigation' })
  for (const name of ['How to', 'Jobs', 'Machines', 'Users', 'Cluster usage', 'Cluster policy']) {
    await nav.getByRole('link', { name, exact: true }).click()
    await expect(page.getByRole('heading', { name, exact: true }).first()).toBeVisible()
  }
  await nav.getByRole('link', { name: 'Jobs', exact: true }).click()
  await expect(page.getByRole('table', { name: 'Sample jobs' }).locator('tbody tr')).toHaveCount(5)
  await expect(page.locator('.demo-dashboard .recharts-wrapper').first()).toBeVisible()
  await page.getByRole('group', { name: 'Dashboard time range' }).first().getByRole('button', { name: '24h' }).click()
  await expect(page.getByRole('group', { name: 'Dashboard time range' }).first().getByRole('button', { name: '24h' })).toHaveAttribute('aria-pressed', 'true')
  await page.goto(`${origin}${prefix}#queue?state=PENDING`)
  await expect(page.getByRole('table', { name: 'Sample jobs' }).locator('tbody tr')).toHaveCount(2)
  await nav.getByRole('link', { name: 'Machines', exact: true }).click()
  await page.getByRole('button', { name: 'Show cluster map' }).click()
  await expect(page.getByRole('img', { name: /Cluster map:/ })).toBeVisible()
  expect(await page.locator('iframe').count()).toBe(0)
  expect(outside).toEqual([])
  expect(failed).toEqual([])
})
