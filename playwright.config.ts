import { defineConfig, devices } from '@playwright/test'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const apiPort = 8010
const webPort = 5174
const testDatabase = join(tmpdir(), `pratibimb-e2e-${process.pid}.db`)
const testEdgeDatabase = join(tmpdir(), `pratibimb-edge-e2e-${process.pid}.db`)

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 45_000,
  expect: { timeout: 10_000 },
  reporter: [['list'], ['html', { outputFolder: 'playwright-report', open: 'never' }]],
  use: {
    baseURL: `http://127.0.0.1:${webPort}`,
    ...devices['Desktop Chrome'],
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: [
    {
      command: `python -m uvicorn backend.main:app --host 127.0.0.1 --port ${apiPort}`,
      url: `http://127.0.0.1:${apiPort}/stations`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: { PRATIBIMB_DB_PATH: testDatabase, PRATIBIMB_CORS_ORIGINS: 'http://127.0.0.1:5174', PRATIBIMB_DEMO_MODE: '1', PRATIBIMB_GATEWAY_TOKEN: 'e2e-gateway-token' },
    },
    {
      command: 'python -m uvicorn backend.edge_service:app --host 127.0.0.1 --port 8011',
      url: 'http://127.0.0.1:8011/health',
      reuseExistingServer: false,
      timeout: 60_000,
      env: { PRATIBIMB_EDGE_DB_PATH: testEdgeDatabase, PRATIBIMB_EDGE_TOKEN: 'e2e-edge-token', PRATIBIMB_GATEWAY_TOKEN: 'e2e-gateway-token', PRATIBIMB_HQ_GATEWAY_URL: `http://127.0.0.1:${apiPort}/api/gateway`, PRATIBIMB_CORS_ORIGINS: 'http://127.0.0.1:5174' },
    },
    {
      command: `npm run dev -- --host 127.0.0.1 --port ${webPort}`,
      url: `http://127.0.0.1:${webPort}`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: { VITE_API_URL: `http://127.0.0.1:${apiPort}`, VITE_EDGE_API_URL: 'http://127.0.0.1:8011', VITE_EDGE_TOKEN: 'e2e-edge-token' },
    },
  ],
})
