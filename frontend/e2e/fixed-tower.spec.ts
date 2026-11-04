import { test, expect } from '@playwright/test'

test('fixed three-slot automatic flow needs one start and no feed/support actions',async({page})=>{
  let phase='idle',placed=0,polls=0
  const actions:string[]=[]
  const snapshot=()=>({enabled:true,session:'a'.repeat(32),revision:0,phase,label:phase==='completed'?'所选结构已完成':'正在吸取并叠放',layer:placed,placed,levels:3,structure:'tower3',agent_fresh:true,agent:{ready:true,stopped:phase==='idle',progress:'模拟视觉核验',slots_ready:true,slot_version:'fixture-v1',sources:[[310,100,-43.04],[190,220,-43.04],[175,100,-43.04]],source_age:.1},events:[],error:null})
  await page.addInitScript(()=>sessionStorage.setItem('spark-blocks-token','synthetic-control-token'))
  await page.route('**/api/health',r=>r.fulfill({json:{project:'spark-blocks',fixed_tower:true}}))
  await page.route('**/api/fixed-tower/**',async r=>{
    const path=new URL(r.request().url()).pathname
    if(path.endsWith('/state')){
      if(phase==='building'&&++polls%3===0){placed++;if(placed===3)phase='completed'}
      return r.fulfill({json:snapshot()})
    }
    if(path.endsWith('/heartbeat'))return r.fulfill({json:{ok:true}})
    if(path.endsWith('/control')){
      const body=r.request().postDataJSON();actions.push(body.action)
      expect(body).toMatchObject({action:'start',automatic:true,structure:'tower3',levels:3,checked:true})
      phase='building';return r.fulfill({json:snapshot()})
    }
    return r.fulfill({status:404})
  })
  await page.goto('/')
  await expect(page.getByRole('heading',{name:'三块摆好，一键搭塔'})).toBeVisible()
  await page.getByRole('checkbox').check()
  await page.getByRole('button',{name:'开始搭建所选结构'}).click()
  await expect(page.getByRole('status').filter({hasText:'所选结构已完成'})).toBeVisible()
  await expect(page.getByText('3 / 3 块已核验放置')).toBeVisible()
  expect(actions).toEqual(['start'])
})

test('uncommissioned fixed slots keep start disabled even with three detections',async({page})=>{
  await page.addInitScript(()=>sessionStorage.setItem('spark-blocks-token','synthetic-control-token'))
  await page.route('**/api/health',r=>r.fulfill({json:{project:'spark-blocks',fixed_tower:true}}))
  await page.route('**/api/fixed-tower/state',r=>r.fulfill({json:{enabled:true,session:'a'.repeat(32),revision:0,phase:'idle',label:'准备搭建固定结构',layer:0,placed:0,levels:3,agent_fresh:true,agent:{ready:true,stopped:true,slots_ready:false,source_note:'三个固定料位尚未验收',sources:[[310,100,-43.04],[190,220,-43.04],[175,100,-43.04]],source_age:.1},events:[],error:null}}))
  await page.route('**/api/fixed-tower/heartbeat',r=>r.fulfill({json:{ok:true}}))
  await page.goto('/')
  await page.getByRole('checkbox').check()
  await expect(page.getByRole('button',{name:'开始搭建所选结构'})).toBeDisabled()
  await expect(page.getByRole('radio')).toHaveCount(1)
})

test('fixed controller ignores a delayed start reply after emergency stop',async({page})=>{
  let phase='idle',revision=0,startSeen=false,stopSeen=false
  const snapshot=()=>({enabled:true,session:'a'.repeat(32),revision,phase,label:phase==='stopped'?'停止已锁定':'准备搭建固定结构',layer:0,placed:0,levels:3,structure:'tower3',agent_fresh:true,agent:{ready:true,stopped:true,progress:'模拟执行端',slots_ready:true,slot_version:'fixture-v1',sources:[[302,181,-43.04],[153,235,-43.04],[213,135,-43.04]],source_age:.1},events:[],error:null})
  let releaseStart!:()=>void
  const held=new Promise<void>(r=>{releaseStart=r})
  await page.addInitScript(()=>sessionStorage.setItem('spark-blocks-token','synthetic-control-token'))
  await page.route('**/api/health',r=>r.fulfill({json:{project:'spark-blocks',fixed_tower:true}}))
  await page.route('**/api/fixed-tower/**',async r=>{
    const path=new URL(r.request().url()).pathname
    if(path.endsWith('/state'))return r.fulfill({json:snapshot()})
    if(path.endsWith('/heartbeat'))return r.fulfill({json:{ok:true}})
    if(path.endsWith('/control')){
      const data=r.request().postDataJSON()
      if(data.action==='start'){
        startSeen=true;const old={...snapshot(),phase:'awaiting_feed',label:'等待放入一块积木',revision:1}
        await held;return r.fulfill({json:old})
      }
      if(data.action==='stop'){
        stopSeen=true;phase='stopped';revision=2;return r.fulfill({json:snapshot()})
      }
    }
    return r.fulfill({status:404})
  })
  await page.goto('/')
  await page.getByRole('checkbox').check()
  await page.getByRole('button',{name:'开始搭建所选结构'}).click()
  await expect.poll(()=>startSeen).toBe(true)
  await page.getByRole('button',{name:'紧急停止'}).click()
  await expect.poll(()=>stopSeen).toBe(true)
  releaseStart()
  await expect(page.getByRole('status').filter({hasText:'停止已锁定'})).toBeVisible()
  await expect(page.getByRole('button',{name:'动作已锁定，等待现场检查'})).toBeDisabled()
})
