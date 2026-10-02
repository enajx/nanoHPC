import { defineConfig } from '@playwright/test'

// Live check of a deployed front node (`npm run test:live`); see tests/live.spec.ts for the environment variables.
// NANOHPC_LIVE_HOST_RULES, when set, is passed to Chromium so the site's host name reaches a tunnel on this machine.
const hostRules = process.env.NANOHPC_LIVE_HOST_RULES
export default defineConfig({
  testDir: 'tests',
  testMatch: 'live.spec.ts',
  reporter: 'list',
  timeout: 300_000,
  use: {
    browserName: 'chromium',
    // The test cluster's certificate comes from a test authority (Pebble or a test certificate).
    ignoreHTTPSErrors: true,
    viewport: { width: 1440, height: 900 },
    launchOptions: { args: hostRules ? [`--host-resolver-rules=${hostRules}`] : [] },
  },
})
