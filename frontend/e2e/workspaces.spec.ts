import { test, expect } from '@playwright/test'

test('all workspaces, settings and help fit wide, embedded and mobile screens', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('/')
  await expect(page.getByText('裁判台已连接')).toBeVisible()
  for (const width of [1440, 1024, 900, 760, 390, 360]) {
    await page.setViewportSize({ width, height: width > 600 ? 1000 : 844 })
    for (const [name, id] of [['比赛现场', 'live'], ['数字孪生', 'twin'], ['证据复核', 'review'], ['裁判助手', 'assistant']]) {
      await page.getByRole('link', { name, exact: true }).click()
      await expect(page.getByRole('link', { name, exact: true })).toHaveAttribute('aria-current', 'page')
      await expect(page).toHaveURL(new RegExp('#' + id + '$'))
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `${name} @ ${width}`).toBeTruthy()
      await expect(page.getByRole('button', { name: '停止', exact: true })).toBeEnabled()
      if (id === 'twin') {
        await expect(page.locator('.three-host canvas')).toBeVisible()
        const box = await page.locator('.three-host canvas').boundingBox()
        expect(box!.width).toBeGreaterThan(200)
        expect(box!.height).toBeGreaterThan(200)
      }
      if ([1440, 900, 390].includes(width)) {
        await page.screenshot({ path: `../test-results/ui-${id}-${width}.png`, fullPage: true, animations: 'disabled' })
      }
    }
    await page.getByRole('button', { name: '比赛设置', exact: true }).click()
    await expect(page.getByRole('dialog')).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy()
    if ([1440, 390].includes(width)) await page.screenshot({ path: `../test-results/ui-settings-${width}.png`, animations: 'disabled' })
    await page.keyboard.press('Escape')
    await expect(page.getByRole('dialog')).toHaveCount(0)
  }
  await page.getByRole('button', { name: '玩法与判罚说明' }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.screenshot({ path: '../test-results/ui-help-360.png' })
  await page.getByRole('button', { name: '明白了，回到比赛' }).click()
  expect(errors).toEqual([])
})

test('workspace links survive reload and browser history without losing assistant draft', async ({ page }) => {
  await page.goto('/#assistant')
  await expect(page.getByRole('heading', { name: '裁判助手.', exact: true })).toBeVisible()
  await page.getByLabel('想了解什么').fill('解释本场最新证据')
  await page.getByRole('link', { name: '数字孪生', exact: true }).click()
  await page.goBack()
  await expect(page.getByLabel('想了解什么')).toHaveValue('解释本场最新证据')
  await page.reload()
  await expect(page.getByRole('link', { name: '裁判助手', exact: true })).toHaveAttribute('aria-current', 'page')
  await page.getByRole('button', { name: '比赛规则', exact: true }).click()
  await expect(page.getByLabel('想了解什么')).toHaveValue('请解释当前比赛规则。')
})

test('mobile referee can switch pages, pause, stop and use keyboard evidence filters', async ({ page, request }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await page.getByRole('button', { name: '开始比赛', exact: true }).click()
  const session = (await (await request.get('/api/state')).json()).session_id
  await page.getByRole('link', { name: '裁判助手', exact: true }).click()
  await page.getByRole('button', { name: '暂停比赛', exact: true }).click()
  expect((await (await request.get('/api/state')).json()).phase).toBe('paused')
  await page.getByRole('link', { name: '数字孪生', exact: true }).click()
  await page.getByRole('button', { name: '停止', exact: true }).click()
  const stopped = await (await request.get('/api/state')).json()
  expect(stopped.session_id).toBe(session)
  expect(stopped.phase).toBe('estop')
  await page.getByRole('link', { name: '证据复核', exact: true }).click()
  await page.getByRole('radio', { name: '待复核', exact: true }).press('ArrowRight')
  await expect(page.getByRole('radio', { name: '全部', exact: true })).toBeChecked()
  await page.getByRole('link', { name: '比赛现场', exact: true }).click()
  await page.getByRole('button', { name: '解除停止并重置' }).click()
  await expect(page.getByRole('button', { name: '开始比赛' })).toBeEnabled()
})
