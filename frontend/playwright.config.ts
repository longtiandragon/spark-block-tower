import { defineConfig } from '@playwright/test'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const port = Number(process.env.SPARK_E2E_PORT ?? 8767)
if (!Number.isInteger(port) || port < 1024 || port > 65535 || port === 8765) {
  throw new Error('E2E must use a dedicated port (1024–65535), never the user service on 8765.')
}
const baseURL = `http://127.0.0.1:${port}`
const dataDir = mkdtempSync(join(tmpdir(), 'spark-freeze-e2e-'))
const legacy = process.env.SPARK_LEGACY_E2E === '1'

export default defineConfig({
  testDir: './e2e',
  testMatch: legacy ? ['game.spec.ts', 'upgrade.spec.ts', 'workspaces.spec.ts'] : ['blocks.spec.ts', 'fixed-tower.spec.ts'],
  workers: 1,
  timeout: 45_000,
  expect: { timeout: 10_000 },
  outputDir: legacy ? '../test-results/browser-legacy' : '../test-results/browser',
  reporter: 'list',
  use: {
    baseURL,
    browserName: 'chromium',
    channel: process.platform === 'win32' ? 'msedge' : undefined,
    viewport: { width: 1440, height: 1080 },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: process.platform === 'win32'
      ? `.venv\\Scripts\\python.exe -m uvicorn spark_freeze.app:app --host 127.0.0.1 --port ${port} --no-access-log`
      : `.venv/bin/python -m uvicorn spark_freeze.app:app --host 127.0.0.1 --port ${port} --no-access-log`,
    cwd: '..',
    url: `${baseURL}/api/health`,
    reuseExistingServer: false,
    env: { SPARK_PROJECT: legacy ? 'freeze' : 'blocks', SPARK_DATA_DIR: dataDir, SPARK_BLOCKS_DATA_DIR: join(dataDir, 'blocks'), SPARK_CONTROL_TOKEN: '', SPARK_BRIDGE_TOKEN: '', SPARK_AI_DISABLED: '1' },
    timeout: 30_000,
  },
})
