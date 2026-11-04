import { test, expect, type APIRequestContext } from '@playwright/test'

async function resetGame(request: APIRequestContext) {
  const current = await (await request.get('/api/state')).json()
  const response = await request.post('/api/control', { data: {
    action: current.phase === 'estop' ? 'clear_estop' : 'reset',
    expected_session_id: current.session_id, expected_revision: current.revision,
  } })
  expect(response.ok()).toBeTruthy()
}

test.beforeEach(async ({ request }) => {
  await resetGame(request)
  const response = await request.post('/api/config', { data: {
    mode: 'demo', players: 3, green_min: 1, green_max: 1,
    red_seconds: 20, settle_seconds: .6, game_seconds: 120,
    motion_threshold: .045, hold_seconds: .2, auto_eliminate: false,
  } })
  expect(response.ok()).toBeTruthy()
})

test.afterEach(async ({ request }) => {
  await resetGame(request)
  await request.post('/api/config', { data: {
    mode: 'demo', players: 3, green_min: 3, green_max: 6,
    red_seconds: 5, settle_seconds: 1.2, game_seconds: 120,
    motion_threshold: .045, hold_seconds: .45, auto_eliminate: false,
  } })
})

test('blocked browser storage does not crash the console', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.addInitScript(() => {
    Object.defineProperty(Storage.prototype, 'getItem', {
      value() { throw new DOMException('Storage blocked', 'SecurityError') },
    })
  })
  await page.goto('/')
  await expect(page.getByRole('button', { name: '开始比赛' })).toBeEnabled()
  expect(errors).toEqual([])
})

test('settings opened in one window cannot overwrite a newer session from another', async ({ page, context, request }) => {
  await page.goto('/')
  await expect(page.getByRole('button', { name: '开始比赛' })).toBeEnabled()
  await page.getByRole('button', { name: '比赛设置', exact: true }).click()
  await page.getByLabel('玩家人数', { exact: true }).fill('1')
  const second = await context.newPage()
  await second.goto('/')
  await expect(second.getByRole('button', { name: '开始比赛' })).toBeEnabled()
  await second.getByRole('button', { name: '比赛设置', exact: true }).click()
  await second.getByLabel('玩家人数', { exact: true }).fill('4')
  await second.getByRole('button', { name: '保存比赛设置', exact: true }).click()
  await expect(second.locator('.player-card')).toHaveCount(4)
  await expect(page.locator('.player-card')).toHaveCount(4)
  // Old form was made for a different session and must be invalidated.
  await expect(page.getByRole('dialog')).toHaveCount(0)
  expect((await (await request.get('/api/state')).json()).config.players).toBe(4)
  await page.getByRole('button', { name: '比赛设置', exact: true }).click()
  await expect(page.getByLabel('玩家人数', { exact: true })).toHaveValue('4')
  await page.getByRole('button', { name: '关闭设置', exact: true }).click()
  await second.close()
})

test('a timed out resume cannot release a later pause', async ({ page, request }) => {
  let lateBody: Record<string, unknown> | undefined
  await page.route('**/api/control', async route => {
    const body = route.request().postDataJSON()
    if (body.action === 'resume' && !lateBody) {
      lateBody = body
      await route.abort('timedout')
    } else await route.continue()
  })
  await page.goto('/')
  await page.getByRole('button', { name: '开始比赛' }).click()
  await page.getByRole('button', { name: '暂停比赛' }).click()
  await page.getByRole('button', { name: '恢复比赛' }).click()
  await expect(page.getByRole('button', { name: '恢复比赛' })).toBeEnabled()
  await page.getByRole('button', { name: '恢复比赛' }).click()
  await expect(page.getByRole('button', { name: '暂停比赛' })).toBeVisible()
  await page.getByRole('button', { name: '暂停比赛' }).click()
  await expect(page.getByRole('heading', { name: '中场暂停' })).toBeVisible()
  expect(lateBody).toBeTruthy()
  expect((await request.post('/api/control', { data: lateBody })).status()).toBe(409)
  expect((await (await request.get('/api/state')).json()).phase).toBe('paused')
})

test('delayed review serializes normal controls and cannot overwrite emergency stop', async ({ page }) => {
  let releaseReview!: () => void
  let reviewReceived!: () => void
  const held = new Promise<void>(resolve => { releaseReview = resolve })
  const received = new Promise<void>(resolve => { reviewReceived = resolve })
  await page.route('**/api/evidence/*/review', async route => {
    const response = await route.fetch()
    reviewReceived()
    await held
    await route.fulfill({ response })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '开始比赛' }).click()
  await expect(page.getByRole('button', { name: '模拟偷动' })).toBeEnabled({ timeout: 20_000 })
  await page.getByRole('button', { name: '模拟偷动' }).click()
  await page.getByRole('link', { name: '证据复核', exact: true }).click()
  await page.getByRole('button', { name: '撤销判罚', exact: true }).click()
  await received
  await page.getByRole('link', { name: '比赛现场', exact: true }).click()
  await expect(page.getByRole('button', { name: '重置', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: '暂停比赛' })).toBeDisabled()
  await expect(page.getByRole('button', { name: '停止', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: '停止', exact: true }).click()
  await expect(page.getByRole('heading', { name: '已停止运行' })).toBeVisible()
  releaseReview()
  await expect(page.getByRole('button', { name: '解除停止并重置' })).toBeEnabled()
  await expect(page.getByRole('heading', { name: '已停止运行' })).toBeVisible()
  await page.getByRole('button', { name: '解除停止并重置' }).click()
  await expect(page.getByRole('button', { name: '开始比赛' })).toBeEnabled()
})

test('hung image requests are marked stale and retried while state stays connected', async ({ page }) => {
  let count = 0
  let releaseImage!: () => void
  const held = new Promise<void>(resolve => { releaseImage = resolve })
  await page.route('**/api/frame.jpg?*', async route => {
    count++
    if (count === 2) {
      await held
      await route.abort().catch(() => {})
    } else await route.continue()
  })
  await page.goto('/')
  await expect(page.getByText('裁判台已连接')).toBeVisible()
  await expect(page.getByText('画面传输中断', { exact: true })).toBeVisible({ timeout: 8000 })
  await expect(page.getByText('暂时没有收到画面', { exact: true })).toBeVisible()
  await expect(page.getByText('裁判台已连接')).toBeVisible()
  releaseImage()
  await expect.poll(() => count).toBeGreaterThan(2)
  await expect(page.getByText('画面就绪', { exact: true })).toBeVisible()
})

test('state connection recovers and a reset closes a stale winner dialog', async ({ page, request }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '开始比赛' }).click()
  await page.locator('.player-card').first().getByRole('button').click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await resetGame(request)
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await page.route('**/api/state', route => route.abort('failed'))
  await expect(page.getByText('连接已中断', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '开始比赛' })).toBeDisabled()
  await page.unroute('**/api/state')
  await expect(page.getByText('裁判台已连接')).toBeVisible()
  await expect(page.getByRole('button', { name: '开始比赛' })).toBeEnabled()
})

test('real demo backend: start, evidence review, winner, report and reset', async ({ page, request }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('/')
  await expect(page.getByText('裁判台已连接')).toBeVisible()
  await expect(page.getByRole('button', { name: '开始比赛' })).toBeEnabled()
  await page.screenshot({ path: '../test-results/desktop-lobby.png', fullPage: true, animations: 'disabled' })
  await page.getByRole('button', { name: '开始比赛' }).click()
  await expect(page.getByRole('button', { name: '模拟偷动' })).toBeEnabled({ timeout: 20_000 })
  await page.getByRole('button', { name: '模拟偷动' }).click()
  await expect(page.locator('.evidence-card')).toHaveCount(1)
  await page.getByRole('link', { name: '证据复核', exact: true }).click()
  await expect(page.getByAltText('本次判罚的基准画面')).toBeVisible()
  await page.screenshot({ path: '../test-results/desktop-evidence.png', fullPage: true, animations: 'disabled' })
  await page.getByRole('link', { name: '证据复核', exact: true }).click()
  await page.getByRole('button', { name: '撤销判罚', exact: true }).click()
  await expect(page.locator('.player-card').first()).toContainText('正在比赛')
  await page.getByRole('radio', { name: '全部', exact: true }).check()
  await expect(page.locator('.evidence-status')).toHaveText('已撤销')
  await page.getByRole('link', { name: '比赛现场', exact: true }).click()
  await page.getByLabel('选择演示偷动的玩家').selectOption('2')
  await page.getByRole('button', { name: '模拟偷动' }).click()
  await page.getByRole('link', { name: '证据复核', exact: true }).click()
  await expect(page.getByRole('button', { name: '确认偷动 · 淘汰' })).toBeVisible()
  await page.getByRole('button', { name: '确认偷动 · 淘汰' }).click()
  await expect(page.locator('.player-card').nth(1)).toContainText('已淘汰')
  await page.getByRole('link', { name: '比赛现场', exact: true }).click()
  await page.locator('.player-card').first().getByRole('button').click()
  await page.getByRole('button', { name: '确认到达', exact: true }).click()
  await expect(page.getByRole('heading', { name: '本局结束', exact: true })).toBeVisible()
  await expect(page.locator('.player-card').first()).toContainText('已到终点')
  await page.getByRole('link', { name: '证据复核', exact: true }).click()
  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: '导出 JSON' }).click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toMatch(/^spark-freeze-.*\.json$/)
  await download.saveAs('../test-results/browser-report.json')
  const state = await (await request.get('/api/state')).json()
  expect(state.evidence.map((e: { status: string }) => e.status)).toEqual(['dismissed', 'confirmed'])
  await page.getByRole('link', { name: '比赛现场', exact: true }).click()
  await page.getByRole('button', { name: '重置，准备下一局' }).click()
  await expect(page.getByRole('button', { name: '开始比赛' })).toBeEnabled()
  expect(errors).toEqual([])
})

test('a fresh stop can cancel an in-flight clear-stop request', async ({ page }) => {
  let releaseClear!: () => void
  let clearReceived!: () => void
  let intercepted = false
  const held = new Promise<void>(resolve => { releaseClear = resolve })
  const received = new Promise<void>(resolve => { clearReceived = resolve })
  await page.route('**/api/control', async route => {
    if (route.request().postDataJSON().action === 'clear_estop' && !intercepted) {
      intercepted = true
      clearReceived()
      await held
    }
    await route.continue()
  })
  await page.goto('/')
  await page.getByRole('button', { name: '停止', exact: true }).click()
  await expect(page.getByRole('heading', { name: '已停止运行' })).toBeVisible()
  await page.getByRole('button', { name: '解除停止并重置' }).click()
  await received
  try {
    await expect(page.getByRole('button', { name: '停止', exact: true })).toBeEnabled()
    await page.getByRole('button', { name: '停止', exact: true }).click()
    await page.getByRole('link', { name: '证据复核', exact: true }).click()
    await expect(page.getByText('再次确认紧急停止，先前的解除请求已失效。', { exact: true })).toBeVisible()
    await page.getByRole('link', { name: '比赛现场', exact: true }).click()
    const rejected = page.waitForResponse(response => response.url().endsWith('/api/control')
      && response.request().postDataJSON().action === 'clear_estop')
    releaseClear()
    expect((await rejected).status()).toBe(409)
    await expect(page.getByRole('heading', { name: '已停止运行' })).toBeVisible()
    await page.getByRole('button', { name: '解除停止并重置' }).click()
    await expect(page.getByRole('button', { name: '开始比赛' })).toBeEnabled()
  } finally {
    releaseClear()
  }
})

test('pause, resume and latched stop', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '开始比赛' }).click()
  await page.getByRole('button', { name: '暂停比赛' }).click()
  await expect(page.getByRole('heading', { name: '中场暂停' })).toBeVisible()
  await page.getByRole('button', { name: '恢复比赛' }).click()
  await expect(page.getByRole('button', { name: '暂停比赛' })).toBeVisible()
  await page.getByRole('button', { name: '结束', exact: true }).click()
  await expect(page.getByRole('heading', { name: '本局结束', exact: true })).toBeVisible()
  await page.getByRole('link', { name: '比赛现场', exact: true }).click()
  await page.getByRole('button', { name: '重置，准备下一局' }).click()
  await page.getByRole('button', { name: '开始比赛' }).click()
  await page.getByRole('button', { name: '停止', exact: true }).click()
  await expect(page.getByRole('heading', { name: '已停止运行' })).toBeVisible()
  await expect(page.getByRole('button', { name: '重置', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: '解除停止并重置' }).click()
  await expect(page.getByRole('button', { name: '开始比赛' })).toBeEnabled()
})

test('mobile layout and real settings persistence', async ({ page, request }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await expect(page.getByText('裁判台已连接')).toBeVisible()
  await page.getByRole('button', { name: '比赛设置', exact: true }).click()
  await page.getByLabel('玩家人数', { exact: true }).fill('2')
  await page.getByRole('button', { name: '保存比赛设置', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '开始比赛' })).toBeEnabled()
  await expect(page.locator('.player-card')).toHaveCount(2)
  expect((await (await request.get('/api/state')).json()).config.players).toBe(2)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy()
  await page.screenshot({ path: '../test-results/mobile-lobby.png', fullPage: true, animations: 'disabled' })
  await page.getByRole('button', { name: '了解动作幅度' }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.getByRole('button', { name: '明白了，回到比赛' }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
})

