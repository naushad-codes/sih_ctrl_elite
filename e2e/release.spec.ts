import { expect, test, type Page, type APIRequestContext } from '@playwright/test'

const api = 'http://127.0.0.1:8010'

async function loginApi(request: APIRequestContext, role: 'hq' | 'station' | 'technical', station = 'maitri') {
  const username = role === 'hq' ? 'admin_hq' : role === 'station' ? 'admin_station' : 'admin_tech'
  const response = await request.post(`${api}/auth/login`, { data: { username, password: '123', role, station } })
  expect(response.ok()).toBeTruthy()
  return response.json() as Promise<{ token: string; username: string; role: string; station: string }>
}

async function loginUi(page: Page, role: 'hq' | 'station' | 'technical', station = 'Maitri Station') {
  await page.goto('/')
  await page.getByRole('button', { name: new RegExp(station) }).click()
  await page.getByRole('button', { name: 'Next' }).click()
  const roleName = role === 'hq' ? 'HQ Operations Analyst' : role === 'station' ? 'Station Operations Officer' : 'Technical Engineer'
  await page.getByRole('button', { name: new RegExp(roleName) }).click()
  await page.getByRole('button', { name: 'Continue' }).click()
  await page.getByLabel('Password').fill('123')
  await page.getByRole('button', { name: /Sign In/ }).click()
  await expect(page.locator('.authenticated-topbar')).toBeVisible()
  return roleName
}

async function createQueuedOperation(request: APIRequestContext, assignedRole: 'station' | 'technical' = 'station') {
  const hq = await loginApi(request, 'hq')
  const revisionResponse = await request.get(`${api}/api/operations`, { headers: { Authorization: `Bearer ${hq.token}` } })
  const { current_revision: revision } = await revisionResponse.json()
  const key = `e2e-${Date.now()}-${Math.random().toString(16).slice(2)}`
  const createdResponse = await request.post(`${api}/api/operations`, {
    headers: { Authorization: `Bearer ${hq.token}`, 'Idempotency-Key': key },
    data: {
      operation_type: 'INSPECTION_REQUEST', title: `E2E CHP inspection ${key.slice(-5)}`,
      description: 'Browser validation of the human inspection workflow.', priority: 'P1', assigned_role: assignedRole,
      base_revision: revision, model_version: 'E2E fixture',
      metadata: { asset: 'CHP', requested_checks: 'Availability and visible condition' },
    },
  })
  expect(createdResponse.ok()).toBeTruthy()
  const operation = await createdResponse.json()
  const queuedResponse = await request.post(`${api}/api/operations/${operation.id}/queue`, { headers: { Authorization: `Bearer ${hq.token}` }, data: {} })
  expect(queuedResponse.ok()).toBeTruthy()
  return { operation, hq }
}

test('all three roles authenticate into the shared shell and preserve role navigation', async ({ page }) => {
  for (const role of ['hq', 'station', 'technical'] as const) {
    await loginUi(page, role)
    await expect(page.getByText('MAITRI', { exact: false }).first()).toBeVisible()
    if (role === 'hq') {
      await expect(page.getByRole('button', { name: 'COMMAND CENTER', exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: 'DIGITAL TWIN', exact: true })).toBeVisible()
    } else if (role === 'station') {
      await expect(page.getByRole('button', { name: 'INBOX', exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: 'SYNC CENTER', exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: 'COMMAND CENTER', exact: true })).toHaveCount(0)
    } else {
      await expect(page.getByRole('button', { name: 'ASSIGNED WORK' })).toBeVisible()
      await expect(page.getByRole('button', { name: 'COMMAND CENTER', exact: true })).toHaveCount(0)
    }
    await page.getByRole('button', { name: /SIGN OUT/i }).click()
  }
})

test('HQ runs CHP Unavailable in Scenario Lab and creates a linked inspection request', async ({ page, request }) => {
  await loginUi(page, 'hq')
  await page.getByRole('button', { name: 'SCENARIO LAB', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'SCENARIO LAB' })).toBeVisible()
  await expect(page.getByLabel('Preset')).toHaveValue('CHP Unavailable')
  await page.getByRole('button', { name: 'RUN SCENARIO · SAVE RESULT' }).click()
  const resultMeta = page.locator('.scenario-result-meta')
  await expect(resultMeta).toContainText('v0.2.0-causal-accounting', { timeout: 15000 })
  const scenarioId = (await resultMeta.innerText()).match(/SC-[A-Z0-9-]+/)?.[0]
  expect(scenarioId).toBeTruthy()
  await page.getByRole('button', { name: /VIEW IN 3D DIGITAL TWIN/ }).click()
  await expect(page.getByRole('heading', { name: /MAITRI.*DIGITAL TWIN/ })).toBeVisible()
  await expect(page.locator('.projection-context')).toContainText('CHP Unavailable')
  await expect(page.getByRole('button', { name: 'Inspect chp component' })).toBeVisible()
  await page.screenshot({ path: 'test-results/visual-hq-digital-twin-chp-unavailable.png', fullPage: true })
  await expect(page.locator('.twin-component-list')).toContainText('UNAVAILABLE')
  await page.getByRole('button', { name: /Combined Heat & Power/ }).click()
  await expect(page.locator('.twin-inspector-primary')).toContainText('UNAVAILABLE')
  await expect(page.locator('.twin-inspector-primary')).toContainText('SCENARIO PROJECTION')
  await page.getByRole('button', { name: /RUN OR REVIEW SCENARIO/ }).click()
  await page.getByRole('button', { name: /CREATE LINKED REQUEST/ }).click()
  await expect(page.getByRole('heading', { name: 'Create a human-reviewed request' })).toBeVisible()
  await expect(page.getByLabel('Operation type')).toHaveValue('INSPECTION_REQUEST')
  await expect(page.getByLabel('Scenario ID')).toHaveValue(scenarioId!)
  const title = `CHP inspection for ${scenarioId}`
  await page.getByLabel('Title').fill(title)
  await page.getByRole('button', { name: /Create Draft · Pending Sync/ }).click()
  await expect(page.getByText(/saved as DRAFT/)).toBeVisible()
  await expect(page.getByRole('heading', { name: title })).toBeVisible()
  await page.getByRole('button', { name: /Queue · pending sync/ }).click()
  await expect(page.getByText(/QUEUED/).first()).toBeVisible()
  await page.getByRole('button', { name: 'COMMAND CENTER', exact: true }).click()
  await page.getByRole('button', { name: 'SCENARIO LAB', exact: true }).click()
  await expect(resultMeta).toContainText(scenarioId!)

  const hq = await loginApi(request, 'hq')
  const scenariosResponse = await request.get(`${api}/hq/scenarios/maitri`, { headers: { Authorization: `Bearer ${hq.token}` } })
  expect(scenariosResponse.ok()).toBeTruthy()
  expect((await scenariosResponse.json()).some((item: { id: string; name: string }) => item.id === scenarioId && item.name === 'CHP Unavailable')).toBeTruthy()
  const operationsResponse = await request.get(`${api}/api/operations`, { headers: { Authorization: `Bearer ${hq.token}` } })
  expect((await operationsResponse.json()).operations.some((item: { title: string; status: string; scenario_id: string }) => item.title === title && item.status === 'QUEUED' && item.scenario_id === scenarioId)).toBeTruthy()
})

test('Digital Twin keeps an inspectable 2D model when WebGL initialization is unavailable', async ({ page }) => {
  await page.addInitScript(() => {
    const original = HTMLCanvasElement.prototype.getContext
    HTMLCanvasElement.prototype.getContext = function (type: string, ...args: any[]) {
      if (type.includes('webgl')) return null
      return original.call(this, type as any, ...args as any)
    }
  })
  await loginUi(page, 'hq')
  await page.getByRole('button', { name: 'DIGITAL TWIN', exact: true }).click()
  await expect(page.locator('.twin-fallback')).toBeVisible()
  await expect(page.locator('.twin-fallback')).toContainText('2D FALLBACK')
  await expect(page.locator('.cc-twin-large')).toBeVisible()
})

test('station can receive, accept, work offline, report manually and synchronize without duplicate events', async ({ page, request, browser }) => {
  const { operation, hq } = await createQueuedOperation(request)
  await loginUi(page, 'station')
  await expect(page.getByRole('button', { name: new RegExp(operation.title) })).toBeVisible()
  await page.getByRole('button', { name: new RegExp(operation.title) }).click()
  await page.getByRole('button', { name: 'ACCEPT REQUEST' }).click()
  await page.getByRole('button', { name: 'START WORK' }).click()
  await page.getByRole('button', { name: 'SYNC CENTER', exact: true }).click()
  await page.getByRole('button', { name: 'SIMULATE OFFLINE' }).click()
  await page.getByRole('button', { name: 'INBOX', exact: true }).click()
  await page.getByRole('button', { name: new RegExp(operation.title) }).click()
  await page.getByRole('button', { name: 'REPORT OBSERVATION' }).click()
  await page.getByLabel('Metric').selectOption({ label: 'CHP Availability' })
  await page.getByLabel('Value').fill('Available after visual inspection')
  await page.getByLabel('Unit').fill('manual assessment')
  await page.getByLabel('Observation time').fill('2026-10-04T12:00')
  await page.getByLabel('Method').fill('Manual inspection')
  await page.getByRole('button', { name: /SAVE · MANUAL HUMAN ACTION/ }).click()
  await expect(page.getByRole('status')).toContainText('PENDING SYNC')
  await page.getByRole('button', { name: 'REPORT COMPLETE' }).click()
  await page.getByLabel('Outcome summary').fill('CHP availability reported after visual inspection.')
  await page.getByRole('button', { name: /SAVE · MANUAL HUMAN ACTION/ }).click()
  await page.getByRole('button', { name: 'SYNC CENTER', exact: true }).click()
  await expect(page.locator('.sync-center-head .sync-pill')).toContainText('2 PENDING')
  await page.getByRole('button', { name: 'SIMULATE RECONNECT · RUN SYNC' }).click()
  await expect(page.locator('.sync-center-head .sync-pill')).not.toContainText('PENDING', { timeout: 15000 })

  const station = await loginApi(request, 'station')
  const operationResponse = await request.get(`${api}/api/operations/${operation.id}`, { headers: { Authorization: `Bearer ${station.token}` } })
  expect(operationResponse.ok()).toBeTruthy()
  const synchronized = await operationResponse.json()
  expect(synchronized.status).toBe('REPORTED_COMPLETE')
  expect(synchronized.station_observations.some((item: { source_type: string }) => item.source_type === 'MANUAL')).toBeTruthy()
  const timeline = synchronized.timeline.map((item: { event_type: string }) => item.event_type)
  expect(timeline).toContain('STATION_OUTCOME_REPORTED')
  const after = await request.get(`${api}/api/operations/${operation.id}`, { headers: { Authorization: `Bearer ${hq.token}` } })
  expect((await after.json()).timeline.filter((item: { event_type: string }) => item.event_type === 'STATION_OUTCOME_REPORTED')).toHaveLength(1)

  const hqContext = await browser.newContext()
  const pageHq = await hqContext.newPage()
  await loginUi(pageHq, 'hq')
  await pageHq.getByRole('button', { name: 'RECONCILIATION', exact: true }).click()
  await pageHq.locator('.recon-ready article')
    .filter({ hasText: operation.title })
    .getByRole('button', { name: /CREATE PROJECTION CHECK/ }).click()
  await expect(pageHq.getByText(/Awaiting explicit HQ review/)).toBeVisible()
  await expect(pageHq.getByRole('button', { name: new RegExp(operation.title) })).toBeVisible()
  await expect(pageHq.locator('.comparison-status')).toBeVisible()
  await pageHq.getByLabel('Review outcome').selectOption('INSUFFICIENT_EVIDENCE')
  await pageHq.getByLabel('Review notes').fill('Browser validation records a human review decision.')
  await pageHq.getByRole('button', { name: 'SAVE HQ REVIEW' }).click()
  await expect(pageHq.getByText(/HQ REVIEW · INSUFFICIENT_EVIDENCE/)).toBeVisible()
  await pageHq.getByRole('button', { name: 'REMOTE OPERATIONS', exact: true }).click()
  await pageHq.getByTitle('Refresh station and operation state').click()
  await pageHq.getByRole('button', { name: new RegExp(operation.title) }).first().click()
  await pageHq.getByRole('button', { name: /Close operation/ }).click()
  await expect(pageHq.getByRole('button', { name: /CLOSED/ }).first()).toBeVisible()
  const reviewed = await request.get(`${api}/api/operations/${operation.id}`, { headers: { Authorization: `Bearer ${hq.token}` } })
  expect((await reviewed.json()).status).toBe('CLOSED')
  await hqContext.close()
})

test('independent edge retains local work while HQ transport is blocked and synchronizes exactly once', async ({ page, request }) => {
  await loginUi(page, 'station')
  await page.getByRole('button', { name: 'OPEN INDEPENDENT EDGE' }).click()
  await page.screenshot({ path: 'test-results/independent-edge-workspace.png', fullPage: true })
  const { operation, hq } = await createQueuedOperation(request)
  const beforeSync = await request.get(`${api}/api/operations/${operation.id}`, { headers: { Authorization: `Bearer ${hq.token}` } })
  expect((await beforeSync.json()).status).toBe('QUEUED')
  const hqOutbox = await request.get(`${api}/api/gateway/outbox/maitri`, { headers: { 'X-Sync-Token': 'e2e-gateway-token' } })
  expect((await hqOutbox.json()).operations.some((item: { id: string }) => item.id === operation.id)).toBeTruthy()
  await page.getByRole('button', { name: /FORCE SYNC THROUGH HQ GATEWAY/ }).click()
  await expect(page.getByRole('button', { name: new RegExp(operation.title) })).toBeVisible()

  const stop = await request.post(`${api}/api/gateway/control`, { headers: { Authorization: `Bearer ${hq.token}` }, data: { status: 'OFFLINE' } })
  expect(stop.ok()).toBeTruthy()
  await page.getByRole('button', { name: new RegExp(operation.title) }).click()
  await page.getByRole('button', { name: 'ACCEPT', exact: true }).click()
  await expect(page.getByText(/STORED ON STATION EDGE · PENDING SYNC/)).toBeVisible()
  await page.getByRole('button', { name: 'START WORK' }).click()
  await page.getByLabel('Quality').selectOption('GOOD')
  await page.getByRole('button', { name: /SAVE TO LOCAL EDGE OUTBOX/ }).click()
  await page.getByRole('button', { name: 'REPORT COMPLETE' }).click()
  await expect(page.locator('.edge-outbox')).toContainText('4 PENDING')

  const hqView = await request.get(`${api}/api/operations/${operation.id}`, { headers: { Authorization: `Bearer ${hq.token}` } })
  expect((await hqView.json()).status).toBe('DELIVERED')
  const restore = await request.post(`${api}/api/gateway/control`, { headers: { Authorization: `Bearer ${hq.token}` }, data: { status: 'CONNECTED' } })
  expect(restore.ok()).toBeTruthy()
  await page.getByRole('button', { name: /FORCE SYNC THROUGH HQ GATEWAY/ }).click()
  await expect(page.getByRole('status')).toContainText('SYNC COMPLETE')

  const station = await loginApi(request, 'station')
  const synced = await request.get(`${api}/api/operations/${operation.id}`, { headers: { Authorization: `Bearer ${station.token}` } })
  const document = await synced.json()
  expect(document.status).toBe('REPORTED_COMPLETE')
  expect(document.station_observations).toHaveLength(1)
  const timeline = document.timeline.map((event: { event_type: string }) => event.event_type)
  expect(timeline.filter((event: string) => event === 'OPERATION_ACCEPTED')).toHaveLength(1)
  expect(timeline).toContain('STATION_OUTCOME_REPORTED')
  await page.getByRole('button', { name: /FORCE SYNC THROUGH HQ GATEWAY/ }).click()
  const replay = await request.get(`${api}/api/operations/${operation.id}`, { headers: { Authorization: `Bearer ${station.token}` } })
  const replayed = await replay.json()
  expect(replayed.timeline.filter((event: { event_type: string }) => event.event_type === 'OPERATION_ACCEPTED')).toHaveLength(1)
})

test('technical engineer records a separate finding disposition while operation lifecycle remains independent', async ({ page, request }) => {
  const { operation } = await createQueuedOperation(request, 'technical')
  await loginUi(page, 'technical')
  await page.getByRole('button', { name: 'ASSIGNED WORK' }).click()
  await page.getByRole('button', { name: new RegExp(operation.title) }).click()
  await page.getByRole('button', { name: 'RECORD OBSERVATION' }).click()
  await page.getByLabel('Value').fill('Available after technical inspection')
  await page.getByRole('button', { name: 'SAVE MANUAL OBSERVATION' }).click()
  await page.getByRole('button', { name: /ADD TECHNICAL FINDING/i }).click()
  await page.getByLabel('Technical finding summary').fill('Visual inspection found normal external condition.')
  await page.getByRole('button', { name: /CREATE TECHNICAL FINDING/i }).click()
  await expect(page.getByText(/TECHNICAL FINDING CREATED · OPEN/)).toBeVisible()
  const engineer = await loginApi(request, 'technical')
  const context = await request.get(`${api}/api/technical/context/${operation.id}`, { headers: { Authorization: `Bearer ${engineer.token}` } })
  expect(context.ok()).toBeTruthy()
  const record = await context.json()
  expect(record.technical_findings.at(-1).status).toBe('OPEN')
  const before = record.operation.status
  await page.getByLabel('Completion summary').fill('Finding reviewed and completed.')
  await page.getByRole('button', { name: 'MARK COMPLETE' }).click()
  await expect(page.getByText(/TECHNICAL FINDING COMPLETE/)).toBeVisible()
  const updatedContext = await request.get(`${api}/api/technical/context/${operation.id}`, { headers: { Authorization: `Bearer ${engineer.token}` } })
  expect((await updatedContext.json()).technical_findings.at(-1).status).toBe('COMPLETE')
  const after = await request.get(`${api}/api/operations/${operation.id}`, { headers: { Authorization: `Bearer ${engineer.token}` } })
  expect((await after.json()).status).toBe(before)
})

test('DEMO seed and reset controls are limited to the demo HQ account', async ({ page, request }) => {
  page.on('dialog', dialog => dialog.accept())
  await loginUi(page, 'hq')
  await page.getByRole('button', { name: 'COMMAND CENTER', exact: true }).click()
  await page.getByRole('button', { name: 'DEMO CONTROL' }).click()
  await expect(page.getByText('PERSISTED USER DATA', { exact: false })).toBeVisible()
  await page.getByRole('button', { name: 'SEED DEMO' }).click()
  await expect(page.getByText('DEMO-PRATIBIMB-001')).toBeVisible()
  const hq = await loginApi(request, 'hq')
  const demo = await request.get(`${api}/api/demo/status`, { headers: { Authorization: `Bearer ${hq.token}` } })
  expect(demo.ok()).toBeTruthy()
  expect((await demo.json()).status).toBe('SEEDED')
  const reset = await request.post(`${api}/api/demo/reset`, { headers: { Authorization: `Bearer ${hq.token}` }, data: {} })
  expect(reset.ok()).toBeTruthy()
  expect((await reset.json()).status).toBe('ABSENT')
})

test('role authorization and Maitri/Bharati data isolation hold at the API boundary', async ({ request }) => {
  const { operation } = await createQueuedOperation(request)
  const maitriStation = await loginApi(request, 'station', 'maitri')
  const bharatiStation = await loginApi(request, 'station', 'bharati')
  const technical = await loginApi(request, 'technical', 'maitri')

  const stationCommandCenter = await request.get(`${api}/api/hq/command-center`, { headers: { Authorization: `Bearer ${maitriStation.token}` } })
  expect(stationCommandCenter.status()).toBe(403)
  const techCommandCenter = await request.get(`${api}/api/hq/command-center`, { headers: { Authorization: `Bearer ${technical.token}` } })
  expect(techCommandCenter.status()).toBe(403)
  const stationHqAction = await request.post(`${api}/api/operations/${operation.id}/review`, {
    headers: { Authorization: `Bearer ${maitriStation.token}` }, data: { note: 'Unauthorized role probe' },
  })
  expect(stationHqAction.status()).toBe(403)
  const crossStationOperation = await request.get(`${api}/api/operations/${operation.id}`, { headers: { Authorization: `Bearer ${bharatiStation.token}` } })
  expect(crossStationOperation.status()).toBe(404)
  const stationTechnicalFinding = await request.post(`${api}/api/technical/findings/${operation.id}`, {
    headers: { Authorization: `Bearer ${maitriStation.token}` }, data: { finding: 'Unauthorized technical route probe' },
  })
  expect(stationTechnicalFinding.status()).toBe(403)
  const technicalReconciliation = await request.get(`${api}/api/reconciliation`, { headers: { Authorization: `Bearer ${technical.token}` } })
  expect(technicalReconciliation.status()).toBe(403)
})

test('complete Maitri demonstration crosses HQ, station, technical and HQ review roles', async ({ page, request, browser }) => {
  test.setTimeout(90_000)
  await loginUi(page, 'hq')
  await page.getByRole('button', { name: 'COMMAND CENTER', exact: true }).click()
  await expect(page.getByText('MAITRI', { exact: false }).first()).toBeVisible()
  await page.getByRole('button', { name: 'SCENARIO LAB', exact: true }).click()
  await page.getByRole('button', { name: 'RUN SCENARIO · SAVE RESULT' }).click()
  const resultMeta = page.locator('.scenario-result-meta')
  await expect(resultMeta).toContainText('v0.2.0-causal-accounting')
  const scenarioId = (await resultMeta.innerText()).match(/SC-[A-Z0-9-]+/)?.[0]
  expect(scenarioId).toBeTruthy()
  await page.getByRole('button', { name: /CREATE LINKED REQUEST/ }).click()
  const operationTitle = `Full demo CHP inspection ${scenarioId}`
  await page.getByLabel('Title').fill(operationTitle)
  await page.getByRole('button', { name: /Create Draft · Pending Sync/ }).click()
  await page.getByRole('button', { name: /Queue · pending sync/ }).click()
  await expect(page.getByText(/QUEUED/).first()).toBeVisible()

  const hq = await loginApi(request, 'hq')
  const scenarioResponse = await request.get(`${api}/hq/scenarios/maitri/${scenarioId}`, { headers: { Authorization: `Bearer ${hq.token}` } })
  expect(scenarioResponse.ok()).toBeTruthy()
  const scenario = await scenarioResponse.json()
  const technicalTitle = `Technical review ${scenarioId}`
  await page.getByRole('button', { name: /New Operation/ }).click()
  await page.getByLabel('Operation type').selectOption('INSPECTION_REQUEST')
  await page.getByLabel('Title').fill(technicalTitle)
  await page.getByLabel('Reason and instructions').fill('DEMO · Review technical context and record a separate engineering finding.')
  await page.getByLabel('Priority').selectOption('P2')
  await page.getByLabel('Recipient role').selectOption('technical')
  await page.getByLabel('Scenario ID').fill(scenarioId!)
  await page.getByLabel('Model version').fill(scenario.model_version)
  await page.getByLabel('Area / system').fill('CHP')
  await page.getByLabel('Checks requested').fill('Technical condition review')
  await page.getByRole('button', { name: /Create Draft · Pending Sync/ }).click()
  await expect(page.getByText(/saved as DRAFT/)).toBeVisible()
  await page.getByRole('button', { name: new RegExp(technicalTitle) }).click()
  await page.getByRole('button', { name: /Queue · pending sync/ }).click()
  const allOps = await request.get(`${api}/api/operations`, { headers: { Authorization: `Bearer ${hq.token}` } })
  const technicalOperation = (await allOps.json()).operations.find((item: { title: string }) => item.title === technicalTitle)
  expect(technicalOperation).toBeTruthy()

  await page.getByRole('button', { name: /SIGN OUT/i }).click()
  await loginUi(page, 'station')
  await page.getByRole('button', { name: new RegExp(operationTitle) }).click()
  await page.getByRole('button', { name: 'ACCEPT REQUEST' }).click()
  await page.getByRole('button', { name: 'START WORK' }).click()
  await page.getByRole('button', { name: 'SYNC CENTER', exact: true }).click()
  await page.getByRole('button', { name: 'SIMULATE OFFLINE' }).click()
  await page.getByRole('button', { name: 'INBOX', exact: true }).click()
  await page.getByRole('button', { name: new RegExp(operationTitle) }).click()
  await page.getByRole('button', { name: 'REPORT OBSERVATION' }).click()
  await page.getByLabel('Metric').selectOption({ label: 'Indoor Temperature' })
  await page.getByLabel('Value').fill('-4.2')
  await page.getByLabel('Unit').fill('°C')
  await page.getByLabel('Observation time').fill('2026-10-04T12:00')
  await page.getByLabel('Method').fill('Manual indoor reading')
  await page.getByRole('button', { name: /SAVE · MANUAL HUMAN ACTION/ }).click()
  await page.getByRole('button', { name: 'REPORT COMPLETE' }).click()
  await page.getByLabel('Outcome summary').fill('CHP available on manual inspection; indoor temperature recorded separately.')
  await page.getByRole('button', { name: /SAVE · MANUAL HUMAN ACTION/ }).click()
  await page.getByRole('button', { name: 'SYNC CENTER', exact: true }).click()
  await expect(page.locator('.sync-center-head .sync-pill')).toContainText('2 PENDING')
  await page.getByRole('button', { name: 'SIMULATE RECONNECT · RUN SYNC' }).click()
  await expect(page.locator('.sync-center-head .sync-pill')).not.toContainText('PENDING', { timeout: 15000 })

  await page.getByRole('button', { name: /SIGN OUT/i }).click()
  await loginUi(page, 'technical')
  await page.getByRole('button', { name: 'ASSIGNED WORK' }).click()
  await page.getByRole('button', { name: new RegExp(technicalTitle) }).click()
  await page.getByRole('button', { name: 'RECORD OBSERVATION' }).click()
  await page.getByLabel('Value').fill('Available after technical inspection')
  await page.getByRole('button', { name: 'SAVE MANUAL OBSERVATION' }).click()
  await page.getByRole('button', { name: /ADD TECHNICAL FINDING/i }).click()
  await page.getByLabel('Technical finding summary').fill('No visible external issue was reported.')
  await page.getByRole('button', { name: /CREATE TECHNICAL FINDING/i }).click()
  await page.getByLabel('Completion summary').fill('Technical finding reviewed and completed.')
  await page.getByRole('button', { name: 'MARK COMPLETE' }).click()
  await expect(page.getByText(/TECHNICAL FINDING COMPLETE/)).toBeVisible()

  const hqContext = await browser.newContext()
  const pageHq = await hqContext.newPage()
  await loginUi(pageHq, 'hq')
  await pageHq.getByRole('button', { name: 'RECONCILIATION', exact: true }).click()
  await pageHq.locator('.recon-ready article')
    .filter({ hasText: operationTitle })
    .getByRole('button', { name: /CREATE PROJECTION CHECK/ }).click()
  await expect(pageHq.locator('.comparison-status')).toBeVisible()
  await expect(pageHq.locator('.recon-evidence-grid article').nth(1).getByText(/CHP available on manual inspection/)).toBeVisible()
  await pageHq.getByLabel('Review outcome').selectOption('DIFFERS_FROM_PROJECTION')
  await pageHq.getByLabel('Review notes').fill('The CHP unavailability was the scenario assumption; the station reported availability after inspection. Human review recorded without an automatic model verdict.')
  await pageHq.getByRole('button', { name: 'SAVE HQ REVIEW' }).click()
  await expect(pageHq.getByText(/HQ REVIEW · DIFFERS_FROM_PROJECTION/)).toBeVisible()
  await pageHq.getByRole('button', { name: 'REMOTE OPERATIONS', exact: true }).click()
  await pageHq.getByTitle('Refresh station and operation state').click()
  await pageHq.getByRole('button', { name: new RegExp(operationTitle) }).first().click()
  await pageHq.getByRole('button', { name: /Close operation/ }).click()

  const tech = await loginApi(request, 'technical')
  const technicalContext = await request.get(`${api}/api/technical/context/${technicalOperation.id}`, { headers: { Authorization: `Bearer ${tech.token}` } })
  expect(technicalContext.ok()).toBeTruthy()
  const technicalFinal = await technicalContext.json()
  expect(technicalFinal.technical_findings.at(-1).status).toBe('COMPLETE')
  expect(technicalFinal.operation.status).toBe('QUEUED')
  const operationResponse = await request.get(`${api}/api/operations`, { headers: { Authorization: `Bearer ${hq.token}` } })
  const closed = (await operationResponse.json()).operations.find((item: { title: string }) => item.title === operationTitle)
  expect(closed.status).toBe('CLOSED')
  expect(closed.latest_report.review_status).toBe('HQ_REVIEWED')
  await hqContext.close()
})

test('primary role screens render at 1440px without horizontal overflow', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  let weatherFetches = 0
  await page.route('**/api/weather/official/maitri/refresh*', async route => {
    weatherFetches += 1
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
      status: 'AVAILABLE', refreshed: true, message: null,
      observation: { id: 1, station_id: 'maitri', observation_time: '2026-10-04T10:00:00+00:00',
        temperature_c: -16.2, pressure_hpa: 958, relative_humidity_pct: 42, wind_speed: 7.1,
        wind_speed_unit: 'm/s', wind_direction_deg: 170, source: 'NCPOR',
        source_type: 'NCPOR_OFFICIAL_OBSERVATION', quality: 'GOOD', fetched_at: '2026-10-04T10:05:00+00:00' },
    }) })
  })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'PRATIBIMB' }).first()).toBeVisible()
  await page.screenshot({ path: 'test-results/visual-landing-1440.png' })
  await loginUi(page, 'hq')
  const inspect = async (name: string) => {
    await page.waitForLoadState('networkidle')
    const dimensions = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }))
    expect(dimensions.scroll, `${name} must not scroll horizontally`).toBeLessThanOrEqual(dimensions.client)
    await page.screenshot({ path: `test-results/visual-${name}-1440.png`, fullPage: true })
  }
  await page.getByRole('button', { name: 'COMMAND CENTER', exact: true }).click()
  await page.evaluate(() => { document.documentElement.style.scrollBehavior = 'auto'; document.documentElement.scrollTop = 0; document.body.scrollTop = 0; window.scrollTo(0, 0) })
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0)
  await expect(page.locator('.cc-environment')).not.toHaveAttribute('data-weather-status', 'CHECKING')
  await page.getByRole('button', { name: 'Refresh NCPOR official observation' }).click()
  await expect(page.locator('.cc-weather-grid')).toContainText('-16.2')
  expect(weatherFetches).toBeGreaterThan(0)
  const primaryPicture = await page.evaluate(() => {
    const panel = document.querySelector('.cc-outcome')?.getBoundingClientRect()
    return panel ? panel.bottom : Number.POSITIVE_INFINITY
  })
  expect(primaryPicture, 'Outcome Review should remain in the initial 1440 × 900 viewport').toBeLessThanOrEqual(900)
  await page.screenshot({ path: 'test-results/command-center-initial-1440x900.png' })
  await inspect('hq-command-center')
  await page.getByRole('button', { name: /ENVIRONMENT/ }).first().click()
  await expect(page.getByText('DATA PROVENANCE')).toBeVisible()
  await page.getByRole('button', { name: 'Close', exact: true }).click()
  await page.getByRole('button', { name: 'DIGITAL TWIN', exact: true }).click()
  await expect(page.locator('.twin-canvas, .twin-fallback').first()).toBeVisible()
  await inspect('hq-digital-twin')
  await page.getByRole('button', { name: 'SCENARIO LAB', exact: true }).click()
  await expect(page.locator('.scenario-layout')).toBeVisible()
  await inspect('hq-scenario-lab')
  await page.getByRole('button', { name: 'REMOTE OPERATIONS', exact: true }).click()
  await expect(page.locator('.rom-grid')).toBeVisible()
  await inspect('hq-remote-operations')
  await page.getByRole('button', { name: 'RECONCILIATION', exact: true }).click()
  await expect(page.locator('.recon-columns')).toBeVisible()
  await inspect('hq-reconciliation')
  await page.getByRole('button', { name: 'AUDIT', exact: true }).click()
  await expect(page.locator('.cc-audit')).toBeInViewport()
  await expect(page.locator('.cc-audit')).toContainText('IMMUTABLE HISTORY')

  await page.getByRole('button', { name: /SIGN OUT/i }).click()
  await loginUi(page, 'station')
  await expect(page.locator('.authenticated-topbar')).toBeVisible()
  await inspect('station-inbox')
  await page.getByRole('button', { name: 'STATION STATE', exact: true }).click()
  await inspect('station-state')
  await page.getByRole('button', { name: 'SYNC CENTER', exact: true }).click()
  await inspect('station-sync')

  await page.getByRole('button', { name: /SIGN OUT/i }).click()
  await loginUi(page, 'technical')
  await expect(page.locator('.authenticated-topbar')).toBeVisible()
  await inspect('technical-overview')
  await page.getByRole('button', { name: 'ASSIGNED WORK', exact: true }).click()
  await inspect('technical-work')
})

test('command center remains readable at target desktop widths', async ({ page }) => {
  for (const viewport of [{ width: 768, height: 1024 }, { width: 1024, height: 521 }, { width: 1024, height: 768 }, { width: 1280, height: 800 }, { width: 1366, height: 768 }, { width: 1440, height: 900 }, { width: 1920, height: 1080 }, { width: 2048, height: 1191 }]) {
    await page.setViewportSize(viewport)
    await loginUi(page, 'hq')
    await page.getByRole('button', { name: 'COMMAND CENTER', exact: true }).click()
    await expect(page.getByText('ATTENTION REQUIRED', { exact: false })).toBeVisible()
    const sizes = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }))
    expect(sizes.scroll).toBeLessThanOrEqual(sizes.client)
    await page.screenshot({ path: `test-results/command-center-${viewport.width}x${viewport.height}.png`, fullPage: true })
    await page.getByRole('button', { name: /SIGN OUT/i }).click()
  }
})
