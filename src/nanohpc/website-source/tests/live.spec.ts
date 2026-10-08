/**
 * Live check of a deployed front node: real nginx, real Grafana, real Content Security Policy. Nothing is mocked.
 *
 * Run from src/nanohpc/website-source/ (after `npm ci` and `npx playwright install chromium`):
 *   NANOHPC_LIVE_URL=https://cluster.example.org/cluster/ \
 *   NANOHPC_LIVE_HOST_RULES="MAP cluster.example.org:443 127.0.0.1:18443" \
 *   npm run test:live
 *
 * NANOHPC_LIVE_URL       the site root, with its trailing slash (required).
 * NANOHPC_LIVE_HOST_RULES Chromium --host-resolver-rules, to reach the site through an SSH tunnel (optional).
 * The certificate may come from a test authority: HTTPS errors are ignored (see playwright.live.config.ts).
 *
 * It visits every tab and fails, with a list, on: any response from the site's origin with status 400 or more
 * (mainly Grafana routes that nginx's allowlist refuses), failed requests, console errors, Content Security
 * Policy violations in the page or its frames, Grafana dashboards without a rendered panel or with an error panel,
 * and page data that differs from the live site.json and status.json.
 */
import { expect, test, type Frame, type Page } from '@playwright/test'

const liveUrl = process.env.NANOHPC_LIVE_URL ?? ''
const tabs = [
  { hash: 'overview', title: 'Overview', dashboards: [] },
  { hash: 'docs', title: 'How to', dashboards: [] },
  { hash: 'queue', title: 'Jobs', dashboards: ['Running Jobs and Queue', 'Queue history'] },
  { hash: 'machines', title: 'Machines', dashboards: [] },
  { hash: 'users', title: 'Users', dashboards: ['GPU usage history per user'] },
  { hash: 'usage', title: 'Cluster usage', dashboards: ['Machine and GPU metrics'] },
  { hash: 'policy', title: 'Cluster policy', dashboards: [] },
]
// Grafana 11 and later draw each dashboard panel as a scene panel with data-viz-panel-key; the panel frame
// (PanelChrome) carries the "Panel header <title>" test id. Error panels show the "Panel status error" test id.
const panelSelector = '[data-viz-panel-key], section[data-testid^="data-testid Panel header"]'
const panelErrorSelector = '[data-testid="data-testid Panel status error"]'

type Problems = { responses: string[]; failedRequests: string[]; console: string[]; dashboards: string[]; data: string[] }

/** Record every problem the browser reports, from the page and from its Grafana frames. */
async function watch(page: Page, origin: string): Promise<Problems> {
  const problems: Problems = { responses: [], failedRequests: [], console: [], dashboards: [], data: [] }
  const context = page.context()
  // Every frame reports CSP violations to the console, where they are collected with the other console errors.
  await context.addInitScript(() => {
    document.addEventListener('securitypolicyviolation', event => {
      console.error(`CSP violation: ${event.violatedDirective} blocked ${event.blockedURI || 'inline content'} in ${event.documentURI}`)
    })
  })
  // Status 400 and above fails, whatever the route. Grafana's frontend-metrics answers 204, which is not a failure.
  context.on('response', response => {
    if (new URL(response.url()).origin === origin && response.status() >= 400) {
      problems.responses.push(`${response.status()} ${response.request().method()} ${response.url()}`)
    }
  })
  // Requests cancelled by the page (a tab change removes a frame, Grafana cancels a superseded query) are not errors.
  context.on('requestfailed', request => {
    const error = request.failure()?.errorText ?? 'unknown error'
    if (error !== 'net::ERR_ABORTED') problems.failedRequests.push(`${error} ${request.method()} ${request.url()}`)
  })
  page.on('console', message => {
    const text = message.text()
    if (message.type() === 'error' || /Content Security Policy/i.test(text)) {
      problems.console.push(`[${message.type()}] ${text} (${message.location().url})`)
    }
  })
  page.on('pageerror', error => problems.console.push(`[page error] ${error.message}`))
  return problems
}

/** Wait up to 30 s for a dashboard frame to draw panels, then note missing or failed panels. */
async function checkDashboard(page: Page, name: string, problems: Problems): Promise<void> {
  const frameElement = page.locator(`iframe[title="${name}"]`)
  await expect(frameElement).toHaveCount(1)
  const frame: Frame | null = await (await frameElement.elementHandle())!.contentFrame()
  if (frame === null) {
    problems.dashboards.push(`${name}: the iframe has no document`)
    return
  }
  const panels = frame.locator(panelSelector)
  const appeared = await expect.poll(() => panels.count(), { timeout: 30_000 }).toBeGreaterThan(0).then(() => true, () => false)
  if (!appeared) {
    problems.dashboards.push(`${name}: no Grafana panel rendered within 30 s (${frame.url()})`)
    return
  }
  // Let the panels' first queries finish before judging them.
  await page.waitForTimeout(5_000)
  const states = await panels.evaluateAll((elements, errorSelector) => elements.map(element => ({
    title: element.querySelector('h2')?.textContent?.trim() || element.getAttribute('data-testid') || element.getAttribute('data-viz-panel-key') || 'untitled panel',
    failed: element.querySelector(errorSelector) !== null,
  })), panelErrorSelector)
  for (const panel of states.filter(state => state.failed)) problems.dashboards.push(`${name}: panel "${panel.title}" shows an error`)
  if (!states.some(state => !state.failed)) problems.dashboards.push(`${name}: no panel rendered without an error`)
}

test('the deployed site loads every tab without refused routes, errors, or CSP violations', async ({ page }) => {
  expect(liveUrl, 'set NANOHPC_LIVE_URL to the site root, such as https://cluster.example.org/cluster/').toMatch(/^https?:\/\/.+\/$/)
  const origin = new URL(liveUrl).origin
  const problems = await watch(page, origin)
  await page.goto(liveUrl)

  // The page's data must be the live site.json and status.json, read from the browser through the same route.
  const live = await page.evaluate(async () => {
    const [site, status] = await Promise.all(['site.json', 'data/status.json'].map(url => fetch(url, { cache: 'no-store' }).then(response => response.json())))
    return { site, status } as { site: { cluster_name: string }; status: { refresh_seconds: number; nodes: { name: string; role: string }[] } }
  })
  await expect(page.locator('.brand-title')).toHaveText(live.site.cluster_name)
  await expect(page).toHaveTitle(live.site.cluster_name)
  // Fresh data says "Updates every Ns"; stale or missing data says so instead.
  const freshness = page.locator('.freshness')
  await expect(freshness).not.toContainText('Connecting', { timeout: 15_000 })
  const freshnessText = await freshness.innerText()
  if (!freshnessText.includes(`Updates every ${live.status.refresh_seconds}s`)) problems.data.push(`status.json is not current: the page says "${freshnessText.replace(/\s+/g, ' ')}"`)
  const shown = await page.locator('.machine-list .machine-link').allInnerTexts()
  const compute = live.status.nodes.filter(node => node.role === 'Compute').map(node => node.name)
  if (JSON.stringify(shown) !== JSON.stringify(compute)) problems.data.push(`machine table shows ${shown.join(', ')}, status.json has ${compute.join(', ')}`)
  if (await page.locator('.notice[role="alert"]').count()) problems.data.push(`the page warns: ${await page.locator('.notice[role="alert"]').first().innerText()}`)

  const nav = page.getByRole('navigation', { name: 'Cluster navigation' })
  for (const tab of tabs) {
    await nav.getByRole('link', { name: tab.title, exact: true }).click()
    await expect(page).toHaveURL(new RegExp(`#${tab.hash}$`))
    await expect(page.getByRole('heading', { level: 1, name: tab.title, exact: true })).toBeVisible()
    for (const name of tab.dashboards) await checkDashboard(page, name, problems)
    if (tab.hash === 'overview') {
      // The GPU chart queries Grafana's query API; on a cluster without GPUs it says so instead of drawing.
      await expect(page.locator('.gpu-allocation-chart .gpu-chart-body')).not.toContainText('Loading', { timeout: 30_000 })
      if (await page.locator('.gpu-allocation-chart [role="alert"]').count()) problems.data.push('the GPU chart says: GPU history unavailable')
    }
    if (tab.hash === 'machines') {
      const everyMachine = live.status.nodes.map(node => node.name)
      const machineRows = await page.locator('.machine-list .machine-link').allInnerTexts()
      if (JSON.stringify(machineRows) !== JSON.stringify(everyMachine)) problems.data.push(`Machines page shows ${machineRows.join(', ')}, status.json has ${everyMachine.join(', ')}`)
      // The cluster map is visible by default and also queries Grafana's query API.
      const mapToggle = page.getByRole('button', { name: 'Show cluster map' })
      await expect(mapToggle).toHaveAttribute('aria-pressed', 'true')
      await expect(page.locator('.cluster-map-canvas canvas')).toBeVisible({ timeout: 30_000 })
      await page.waitForTimeout(3_000)
      await mapToggle.click()
      await expect(page.locator('.cluster-map-canvas canvas')).toHaveCount(0)
      await mapToggle.click()
      await expect(page.locator('.cluster-map-canvas canvas')).toBeVisible({ timeout: 30_000 })
    }
    if (tab.hash === 'docs') {
      for (const file of ['docs.md', 'policy.md', 'machines.md']) {
        const text = await page.evaluate(async url => (await fetch(url, { cache: 'no-store' })).text(), file)
        if (text.includes('{{')) problems.data.push(`${file} still has template placeholders`)
      }
    }
  }

  const report = Object.entries(problems).filter(([, items]) => items.length).map(([kind, items]) => `${kind}:\n  ${items.join('\n  ')}`)
  expect(report, `problems found on ${liveUrl}`).toEqual([])
})
