import { defineConfig } from '@playwright/test'

// Browser tests of the built site (../website, that is src/nanohpc/website); run `npm run build` first. See tests/website.spec.ts.
// tests/live.spec.ts runs only with playwright.live.config.ts (`npm run test:live`), against a deployed front node.
export default defineConfig({
  testDir: 'tests',
  testIgnore: 'live.spec.ts',
  reporter: 'list',
  use: { browserName: 'chromium' },
})
