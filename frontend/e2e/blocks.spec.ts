import { test, expect } from '@playwright/test'

test.beforeEach(async ({ request }) => {
  let state = await (await request.get('/api/blocks/state')).json()
  if (!['idle', 'completed', 'estop', 'paused'].includes(state.phase)) {
    state = await (await request.post('/api/blocks/control', { data: { action: 'estop', session: state.session, revision: state.revision } })).json()
  }
  state = await (await request.post('/api/blocks/control', { data: { action: ['estop', 'paused'].includes(state.phase) ? 'recover' : 'reset', session: state.session, revision: state.revision } })).json()
  if (state.mode !== 'demo') state = await (await request.post('/api/blocks/control', { data: { action: 'mode', mode: 'demo', session: state.session, revision: state.revision } })).json()
  await request.put('/api/blocks/layout', { data: { session: state.session, revision: state.revision, layout: { name: '三层蓝色积木塔', color: 'blue', cells: [0, 1, 2].map(layer => ({ row: 1, col: 1, layer })) } } })
})

test('default tower completes synthetic sequence and exports honest report', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '把想法，搭起来。' })).toBeVisible()
  await expect(page.getByText('合成演示 · 不控制机器人')).toBeVisible()
  await page.screenshot({ path: '../test-results/blocks-desktop.png', fullPage: true })
  await page.getByRole('button', { name: '运行搭建演示' }).click()
  await expect(page.getByRole('heading', { name: '布局搭建完成' })).toBeVisible({ timeout: 25000 })
  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('link', { name: '导出记录' }).click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe('spark-blocks-report.json')
  const state = await (await page.request.get('/api/blocks/state')).json()
  expect(state.placed.map((p: { verification: string }) => p.verification)).toEqual(['simulated', 'simulated', 'simulated'])
  expect(errors).toEqual([])
})

test('support editing persists layout and natural language only proposes', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: 'L3 顶层', exact: true }).click()
  await page.getByRole('button', { name: '第3层 第1行 第1列', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('下一层')
  await page.getByRole('button', { name: '搭建助手' }).click()
  await page.getByLabel('文字或语音指令').fill('搭两层塔')
  await page.getByRole('button', { name: '生成布局草案' }).click()
  await expect(page.getByText('有未保存修改')).toBeVisible()
  await expect(page.getByRole('button', { name: '运行搭建演示' })).toBeDisabled()
  await page.getByRole('button', { name: '保存目标' }).click()
  await page.reload()
  await expect(page.getByText('2 层蓝色积木塔', { exact: true })).toBeVisible()
  expect((await (await page.request.get('/api/blocks/state')).json()).phase).toBe('idle')
})

test('emergency stop locks and requires explicit recovery', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '运行搭建演示' }).click()
  await page.getByRole('button', { name: '紧急停止' }).click()
  await expect(page.getByRole('button', { name: '解除锁定并重置' })).toBeDisabled()
  await page.waitForTimeout(1200)
  expect((await (await page.request.get('/api/blocks/state')).json()).phase).toBe('estop')
  await page.getByLabel('我已完成现场检查和复位').check()
  await page.getByRole('button', { name: '解除锁定并重置' }).click()
  await expect(page.getByRole('button', { name: '运行搭建演示' })).toBeEnabled()
})

test('robot mode cannot start offline; assistant remains read-only', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '真实机器人', exact: true }).click()
  await expect(page.getByRole('button', { name: '开始实机搭建' })).toBeDisabled()
  await page.getByRole('button', { name: '现场与检查' }).click()
  await expect(page.getByText('等待机器人上传相机画面')).toBeVisible()
  await page.getByRole('button', { name: '搭建助手' }).click()
  await page.getByLabel('文字或语音指令').fill('现在进行到哪一步？')
  await page.getByRole('button', { name: '询问任务与搭建建议' }).click()
  await expect(page.locator('.answer-text')).toContainText('实机任务')
  await page.getByRole('button', { name: '合成演示', exact: true }).click()
})

test('phone layout is usable without horizontal overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '把想法，搭起来。' })).toBeVisible()
  await page.getByRole('button', { name: '控制口令', exact: true }).click()
  await expect(page.getByLabel('控制口令', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '保存口令' }).click()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy()
  await page.screenshot({ path: '../test-results/blocks-mobile.png', fullPage: true })
  await page.getByRole('button', { name: '搭建助手' }).click()
  await expect(page.getByLabel('文字或语音指令')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy()
})

for (const failure of ['pending', 'failed'] as const) {
  test(`old robot status expires when polling is ${failure}`, async ({ page, request }) => {
    const initial = await (await request.get('/api/blocks/state')).json()
    const robot = { ...initial, mode: 'robot', phase: 'awaiting_pick', phase_label: '等待确认积木已吸稳',
      armed: true, bridge_fresh: true, readiness: null,
      bridge: { dry_run: false, slots: ['1-1-0'], sources: 1, camera_age: .05, scan_age: .05, odom_age: .05, stopped: true },
      camera: { fresh: true, sequence: 1, boxes: [] } }
    let first = true
    let broken = false
    let restored = false
    let delayed = false
    let release: () => void = () => {}
    const gate = new Promise<void>(resolve => { release = resolve })
    await page.route('**/api/blocks/operator/heartbeat', route => route.fulfill({ json: { ok: true } }))
    await page.route('**/api/blocks/frame.jpg*', route => route.fulfill({ status: 204 }))
    await page.route('**/api/blocks/state', async route => {
      if (first || restored) { first = false; await route.fulfill({ json: robot }); return }
      broken = true
      if (failure === 'pending' && !delayed) {
        delayed = true
        await gate
        await route.fulfill({ json: robot })
        return
      }
      await route.abort('failed')
    })
    try {
      await page.goto('/')
      await page.getByRole('button', { name: '现场与检查' }).click()
      await expect(page.getByText('画面新鲜', { exact: true })).toBeVisible()
      await expect.poll(() => broken).toBeTruthy()
      await expect(page.getByText('画面新鲜', { exact: true })).toBeHidden({ timeout: 1800 })
      await expect(page.getByText('在线 · 实机', { exact: true })).toBeHidden()
      await expect(page.getByRole('button', { name: '确认吸稳并抬起，继续返回' })).toBeDisabled()
      await expect(page.getByRole('button', { name: '紧急停止' })).toBeEnabled()
      if (failure === 'pending') {
        const lateResponse = page.waitForResponse(response => response.url().endsWith('/api/blocks/state'))
        // Ensure the held response itself has exceeded the one-second budget.
        await page.waitForTimeout(1000)
        release()
        await lateResponse
        await expect(page.getByRole('alert')).toContainText('状态响应已过期')
        await expect(page.getByText('画面新鲜', { exact: true })).toBeHidden()
      }
      restored = true
      await expect(page.getByText('画面新鲜', { exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: '确认吸稳并抬起，继续返回' })).toBeEnabled()
    } finally { release() }
  })
}

test('delayed start response cannot replace a later emergency stop', async ({ page }) => {
  let release: () => void = () => {}
  let held = false
  const gate = new Promise<void>(resolve => { release = resolve })
  await page.route('**/api/blocks/control', async route => {
    if (route.request().postDataJSON().action !== 'start') { await route.continue(); return }
    const response = await route.fetch()
    held = true
    await gate
    await route.fulfill({ response })
  })
  try {
    await page.goto('/')
    await page.getByRole('button', { name: '运行搭建演示' }).click()
    await expect.poll(() => held).toBeTruthy()
    await page.getByRole('button', { name: '紧急停止' }).click()
    await expect(page.locator('.recovery-panel')).toBeVisible()
    // Prevent a later poll from masking an incorrect stale-response overwrite.
    await page.route('**/api/blocks/state', route => route.abort('failed'))
    const lateResponse = page.waitForResponse(response => response.url().endsWith('/api/blocks/control') && response.request().postDataJSON().action === 'start')
    release()
    await lateResponse
    await page.waitForTimeout(150)
    await expect(page.locator('.recovery-panel')).toBeVisible()
    await expect(page.getByRole('button', { name: '运行搭建演示' })).toBeDisabled()
  } finally { release() }
})

test('an older layout draft cannot overwrite another windows saved layout', async ({ page, request }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '一字排列', exact: true }).click()
  const state = await (await request.get('/api/blocks/state')).json()
  const otherLayout = { name: '另一窗口的两层塔', color: 'blue', cells: [0, 1].map(layer => ({ row: 1, col: 1, layer })) }
  const response = await request.put('/api/blocks/layout', { data: { session: state.session, revision: state.revision, layout: otherLayout } })
  expect(response.ok()).toBeTruthy()
  await expect(page.locator('.mission-strip')).toContainText(otherLayout.name)
  await page.getByRole('button', { name: '保存目标', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('任务已变化')
  expect((await (await request.get('/api/blocks/state')).json()).layout.name).toBe(otherLayout.name)
  await page.getByRole('button', { name: '放弃草案并同步', exact: true }).click()
  await expect(page.getByRole('textbox', { name: '布局名称' })).toHaveValue(otherLayout.name)
  await page.getByRole('button', { name: '一字排列', exact: true }).click()
  await page.getByRole('button', { name: '保存目标', exact: true }).click()
  await expect(page.locator('.mission-strip')).toContainText('蓝色积木直线')
})

test('turning voice off immediately cancels queued browser speech', async ({ page }) => {
  await page.addInitScript(() => {
    const events: string[] = []
    Object.assign(window, { speechEvents: events })
    Object.defineProperty(window, 'speechSynthesis', { configurable: true, value: {
      cancel: () => events.push('cancel'), speak: () => events.push('speak'),
    } })
    Object.defineProperty(window, 'SpeechSynthesisUtterance', { configurable: true, value: class { lang = ''; rate = 1 } })
  })
  await page.goto('/')
  await expect(page.getByRole('button', { name: '运行搭建演示' })).toBeEnabled()
  const toggle = page.getByRole('button', { name: '切换浏览器语音播报' })
  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-pressed', 'true')
  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-pressed', 'false')
  expect(await page.evaluate(() => (window as unknown as { speechEvents: string[] }).speechEvents.slice(-1))).toEqual(['cancel'])
})

test('late assistant response is discarded after an emergency stop', async ({ page }) => {
  let release: () => void = () => {}
  let held = false
  const gate = new Promise<void>(resolve => { release = resolve })
  await page.route('**/api/blocks/assistant', async route => {
    const response = await route.fetch()
    held = true
    await gate
    await route.fulfill({ response })
  })
  try {
    await page.goto('/')
    await page.getByRole('button', { name: '搭建助手', exact: true }).click()
    await page.getByRole('button', { name: '询问任务与搭建建议' }).click()
    await expect.poll(() => held).toBeTruthy()
    await page.getByRole('button', { name: '紧急停止' }).click()
    await expect(page.locator('.recovery-panel')).toBeVisible()
    release()
    await expect(page.getByRole('status')).toContainText('已丢弃过期的助手回答')
    await expect(page.locator('.answer-text')).toContainText('告诉我想搭什么')
  } finally { release() }
})

test('browser speech failure cannot prevent emergency stop', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, 'speechSynthesis', { configurable: true, value: {
      cancel: () => { throw new Error('audio disconnected') },
      speak: () => { throw new Error('audio disconnected') },
    } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '运行搭建演示' }).click()
  await page.getByRole('button', { name: '紧急停止' }).click()
  await expect(page.locator('.recovery-panel')).toBeVisible()
  expect((await (await page.request.get('/api/blocks/state')).json()).phase).toBe('estop')
})


test('stationary workspace can enable start without a fabricated map pose', async ({ page, request }) => {
  const initial = await (await request.get('/api/blocks/state')).json()
  const robot = { ...initial, mode: 'robot', home: null, workspace_recorded: true,
    bridge_fresh: true, readiness: null,
    bridge: { operation_mode: 'stationary', dry_run: false, slots: ['1-1-0','1-1-1','1-1-2'],
      sources: 3, camera_age: .05, scan_age: .05, odom_age: .05, stopped: true } }
  await page.route('**/api/blocks/state', route => route.fulfill({ json: robot }))
  await page.route('**/api/blocks/operator/heartbeat', route => route.fulfill({ json: { ok: true } }))
  await page.goto('/')
  await page.locator('.start-check input').check()
  await expect(page.getByRole('button', { name: '开始实机搭建' })).toBeEnabled()
  await page.getByRole('button', { name: '现场与检查' }).click()
  await expect(page.getByRole('button', { name: '确认固定工作区' })).toBeVisible()
  await expect(page.getByText(/固定工作区模式：底盘与相机保持不动/)).toBeVisible()
})
