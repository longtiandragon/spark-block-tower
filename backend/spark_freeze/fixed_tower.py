"""Leased operator-assisted fixed tower workflow; no actuator or SSH calls."""
import copy
import time
import uuid
import re
from pathlib import Path
from .blocks import BlockError


LABELS = dict(idle='准备搭建固定结构', awaiting_feed='等待放入一块积木', building='正在吸取并叠放',
              preparing='正在进入固定取料位', awaiting_support='等待确认本层落稳', releasing='正在释放并退出', completed='所选结构已完成', stopped='停止已锁定')

STRUCTURES = {
    'single': dict(name='单块', cells=[dict(x=0, layer=0)]),
    'tower2': dict(name='两层直塔', cells=[dict(x=0,layer=i) for i in range(2)]),
    'tower3': dict(name='三层直塔', cells=[dict(x=0,layer=i) for i in range(3)]),
    'pyramid3': dict(name='两底一顶', cells=[dict(x=-.5,layer=0),dict(x=.5,layer=0),dict(x=0,layer=1)]),
}


def logical_plan(structure):
    if structure not in STRUCTURES:
        raise BlockError('不支持该结构。')
    model=STRUCTURES[structure]
    steps=[]
    for i,cell in enumerate(model['cells']):
        supports=[j for j,p in enumerate(model['cells'][:i])
                  if p['layer']==cell['layer']-1 and abs(p['x']-cell['x'])<1]
        if cell['layer'] and not supports:
            raise BlockError('上层缺少已经完成的下层支撑。')
        steps.append(dict(index=i,layer=cell['layer'],x=cell['x'],supports=supports,
                          label=f'第 {i+1} 块：'+('底层' if not cell['layer'] else '跨缝顶块' if len(supports)>1 else f"第 {cell['layer']+1} 层")))
    return dict(structure=structure,name=model['name'],count=len(steps),
                height_levels=max(v['layer'] for v in steps)+1,steps=steps,
                assisted=True,physical_verified=False)


class FixedTower:
    def __init__(self, data_dir, enabled=False, clock=time.monotonic):
        self.clock, self.enabled = clock, enabled
        self.path = Path(data_dir)/'fixed-tower'
        self.path.mkdir(parents=True, exist_ok=True)
        self.session, self.revision = uuid.uuid4().hex, 0
        self.phase, self.layer, self.placed = 'idle', 0, 0
        self.levels = 3
        self.structure = 'tower3'
        self.sources=[]
        self.staging=None
        self.profile_sha256=self.slot_version=None
        self.command, self.owner, self.agent = None, None, None
        self.agent_at = self.operator_at = float('-inf')
        self.events, self.error, self.receipts = [], None, []
        self.operator = None

    def busy(self):
        return self.phase in {'preparing','awaiting_feed','building','awaiting_support','releasing'}

    def stop(self, reason):
        self.phase, self.command, self.error = 'stopped', None, reason
        self.log(reason)

    def tick(self):
        if self.busy() and (self.clock()-self.agent_at > 1 or self.clock()-self.operator_at > 2):
            self.stop('机器人或操作网页心跳过期；停止并保持吸力。')
        if self.command and self.clock()-self.command['issued'] > (300 if self.sources else 180):
            self.stop('动作超时；停止并保持吸力。')

    def log(self, text):
        self.revision += 1
        self.events.append(dict(time=time.strftime('%H:%M:%S'), message=text))
        self.events = self.events[-100:]
        try:
            p = self.path/(self.session+'.json')
            tmp = p.with_suffix('.tmp')
            tmp.write_text(__import__('json').dumps(self.snapshot(), ensure_ascii=False, indent=2), encoding='utf8')
            tmp.replace(p)
        except OSError:
            self.phase, self.command, self.error = 'stopped', None, '运行记录保存失败'
            raise BlockError(self.error)

    def snapshot(self):
        return copy.deepcopy(dict(enabled=self.enabled, session=self.session, revision=self.revision,
            phase=self.phase, label=LABELS[self.phase], layer=self.layer, placed=self.placed,
            agent_fresh=self.clock()-self.agent_at <= 1, agent=self.agent, error=self.error,
            events=self.events, receipts=self.receipts, command=self.command,
            assisted=True, shape=self.structure, levels=self.levels, structure=self.structure,
            structures=STRUCTURES))

    def heartbeat(self, operator):
        self.tick()  # Expired leases cannot be resurrected by a returning browser.
        if self.busy() and operator != self.operator:
            raise BlockError('另一个网页正在操作当前搭塔任务。')
        self.operator_at = self.clock()

    def issue(self, action):
        self.command = dict(id=uuid.uuid4().hex, run=self.session, action=action, layer=self.layer, levels=self.levels, structure=self.structure, issued=self.clock(),sources=copy.deepcopy(self.sources))
        self.command['staging']=copy.deepcopy(self.staging)
        if self.sources:
            self.command.update(profile_sha256=self.profile_sha256,slot_version=self.slot_version)

    def control(self, action, session, revision, operator, checked=False, levels=3, structure=None, automatic=False):
        self.tick()
        if action == 'stop':
            self.stop('操作员请求停止；吸力保持。')
            return
        if not self.enabled:
            raise BlockError('固定塔实机入口尚未启用。')
        if session != self.session or revision != self.revision:
            raise BlockError('任务已变化，请同步后再操作。')
        if self.clock()-self.agent_at > 1 or not self.agent or not self.agent.get('ready'):
            raise BlockError('固定塔执行端离线、停止锁定或相机过期。')
        if self.clock()-self.operator_at > 2:
            raise BlockError('操作网页心跳过期。')
        if self.busy() and operator != self.operator:
            raise BlockError('当前任务属于另一个操作网页。')
        if action == 'start':
            structure = structure or {1:'single',2:'tower2',3:'tower3'}.get(levels)
            if structure not in STRUCTURES:
                raise BlockError('不支持该结构。')
            if structure not in self.agent.get('structures', ['single','tower2','tower3']):
                raise BlockError('所选结构尚未完成现场标定与实机验收，可预览但不能启动实物。')
            levels = len(STRUCTURES[structure]['cells'])
            if type(levels) is not int or levels not in (1,2,3):
                raise BlockError('仅支持已限定的单块、两层塔或三层塔。')
            if self.phase != 'idle' or not checked or not self.agent.get('stopped'):
                raise BlockError('请先确认旧塔已清空、相机固定且机械臂停稳。')
            if automatic:
                if structure!='tower3' or self.agent.get('staging') is not None:
                    raise BlockError('固定料位自动模式仅支持三层塔，禁止临时搬运。')
                if (self.agent.get('slots_ready') is not True or not self.agent.get('slot_version') or
                        not re.fullmatch('[a-f0-9]{64}',str(self.agent.get('profile_sha256','')))):
                    raise BlockError('三个固定料位及视觉核验尚未完成实机验收。')
                if len(self.agent.get('sources',[]))!=3 or self.agent.get('source_age',1e9)>.5:
                    raise BlockError('需要三个固定料位的新鲜匹配结果。')
            self.operator, self.owner = operator, self.agent['id']
            self.levels = levels
            self.structure = structure
            if automatic:
                if len(self.agent.get('sources',[]))!=3 or self.agent.get('source_age',1e9)>.5:
                    raise BlockError('需要三块积木顶面的稳定识别结果。')
                self.sources=copy.deepcopy(self.agent['sources'][:levels])
                self.staging=copy.deepcopy(self.agent.get('staging'))
                self.profile_sha256=self.agent['profile_sha256']
                self.slot_version=self.agent['slot_version']
            else:self.sources=[]
            self.phase = 'preparing'
            self.issue('prepare')
            self.log('开始固定三料位自动搭建；逐块核验抓取与放置。' if automatic else f"开始搭建{STRUCTURES[structure]['name']}；每次在同一个送料区放一块。")
        elif action == 'feed':
            if self.phase != 'awaiting_feed' or not checked or not self.agent.get('stopped'):
                raise BlockError('请先在固定送料区放好一块并撤手。')
            self.phase = 'building'
            self.issue('build')
            self.log(f'开始吸取并放置第 {self.layer+1} 块。')
        elif action == 'support':
            if self.phase != 'awaiting_support' or not checked or not self.agent.get('stopped'):
                raise BlockError('请先确认积木已由地面或下层稳定支撑。')
            self.phase = 'releasing'
            self.issue('release')
            self.log(f'已确认第 {self.layer+1} 块支撑，释放并退出。')
        elif action == 'reset':
            if self.phase != 'completed' or not checked or not self.agent.get('stopped'):
                raise BlockError('只允许完成后清空场地并开始新轮；停止故障需现场处理。')
            self.session, self.revision = uuid.uuid4().hex, self.revision+1
            self.phase, self.layer, self.placed = 'idle', 0, 0
            self.command, self.owner, self.operator, self.error = None, None, None, None
            self.events, self.receipts = [], []
            self.log('新一轮已准备；启动前请清空旧塔。')
        else:
            raise BlockError('固定塔操作无效。')

    def poll(self, data):
        self.tick()
        if self.busy() and self.owner != data['id']:
            raise BlockError('固定塔执行端已被当前会话独占。')
        self.agent, self.agent_at = copy.deepcopy(data), self.clock()
        if self.busy() and self.sources and (data.get('profile_sha256')!=self.profile_sha256 or data.get('slot_version')!=self.slot_version):
            self.stop('执行端料位配置已变化；禁止继续当前任务。')
        if self.busy() and data.get('fault'):
            self.stop(data['fault'])
        if self.busy() and self.owner != data['id']:
            self.stop('执行端发生变化。')
        return dict(run=self.session, command=copy.deepcopy(self.command),
                    armed=self.busy() and self.clock()-self.operator_at <= 2,
                    stop=self.phase == 'stopped')

    def result(self, agent, command, ok, details):
        self.tick()
        if not self.command or self.command['id'] != command or agent != self.owner or self.phase not in {'preparing','building','releasing'}:
            raise BlockError('拒绝过期、重复或其他执行端的结果。')
        action = self.command['action']
        expected_layer=self.command['layer']
        self.receipts.append(dict(command_id=command, layer=self.layer, action=action, ok=ok, details=details))
        self.command = None
        if not ok:
            self.stop('固定塔动作失败；保持吸力并现场检查。')
        elif action == 'prepare':
            self.phase = 'building' if self.sources else 'awaiting_feed'
            if self.sources:self.issue('build')
            self.log('自动取料路径已准备，开始抓取第一块。' if self.sources else '取料位已停稳，请在吸盘正下方放第一块后撤手。')
        elif action == 'build':
            if self.sources:
                proofs=[details.get('grasp',{}),details.get('placement',{})]
                valid=(details.get('automatic_released') is True and
                       details.get('profile_sha256')==self.profile_sha256 and details.get('slot_version')==self.slot_version)
                for kind,proof in zip(('grasp','placement'),proofs):
                    valid=valid and isinstance(proof,dict) and proof.get('verified') is True and proof.get('kind')==kind
                    valid=valid and proof.get('run')==self.session and proof.get('command')==command and proof.get('layer')==expected_layer
                    valid=valid and proof.get('profile_sha256')==self.profile_sha256 and bool(re.fullmatch('[a-f0-9]{32}',str(proof.get('id',''))))
                if not valid or proofs[0]['id']==proofs[1]['id']:
                    self.stop('缺少本任务本块的抓取/放置视觉证据；未计数，停止后续动作。')
                    return
                self.placed+=1
                self.layer+=1
                self.phase='completed' if self.placed==self.levels else 'building'
                if self.phase=='building':self.issue('build')
                self.log('三块抓取与放置均已核验，自动流程完成。' if self.phase=='completed' else '本块抓取与放置已核验，自动取下一块。')
            else:
                self.phase = 'awaiting_support'
                self.log('已到放置位置；动作回执不等同稳定支撑，请确认。')
        else:
            self.placed += 1
            self.layer += 1
            self.phase = 'completed' if self.placed == self.levels else 'awaiting_feed'
            if self.sources and self.phase!='completed':
                self.phase='building'
                self.issue('build')
            self.log('所选结构流程完成。' if self.placed == self.levels else '本层已释放，等待下一块送料。')
