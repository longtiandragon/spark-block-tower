# SPARK 蓝色积木视觉抓取与分层搭建系统

课程项目源代码。系统由 Vue 操作界面、FastAPI 任务服务、ROS 图像适配器与 SwiftPro 机械臂执行模块组成，使用 HSV 颜色分割、平面坐标标定、吸盘取放和分层任务编排。

## 当前能力

- 结构选择、相机显示、任务状态与记录导出。
- 蓝色顶面定位、固定三料位匹配、试提随动与放置视觉检查。
- 分层路线、关节反馈换算、分段到位检查及停止锁。
- 已有人工送料、支撑确认与现场修正条件下的三层实物搭建记录。固定料位自动版本的软件流程已实现，整轮无人干预实机验收仍待完成。

本仓库是当前源码首次整理公开的快照，不补造此前 Git 开发历史。旧木头人模块保留用于兼容和回归测试，当前搭塔入口由 SPARK_PROJECT=blocks 选择。

## 电脑端运行

需要 Python 3.10+ 与 Node.js/npm。Windows PowerShell：

```powershell
.\Setup.ps1
.\Start.ps1 -Port 8768
```

打开 http://127.0.0.1:8768/ 。固定结构页面缺少机器人配置时会显示未就绪。软件演示入口为 http://127.0.0.1:8768/?layout=1 ，合成演示不发送实物命令。

也可手动安装和构建：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
cd frontend
npm ci
npm run build
cd ..
.\Start.ps1 -Port 8768
```

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
cd frontend
npm run build
npm run test:e2e
```

整理时工作区 Python 回归为 480 passed、11 subtests passed。公开快照复核为 479 passed、1 skipped、11 subtests passed；跳过项依赖未分发的历史相机照片。前端类型检查与生产构建通过，16项浏览器流程测试通过。另有一条 Starlette/httpx 弃用提醒及Vite打包体积提示。软件测试使用模拟输入，不代表机器人实体可靠性。

## 目录

|目录|内容|
|---|---|
|backend/spark_freeze|HTTP API、固定结构状态机、布局与演示|
|frontend/src|Vue 操作界面与 Three.js 展示|
|ros/spark_blocks_bridge|ROS 适配、视觉、路径与执行器|
|robot_console|独占串口控制台及其依赖源文件|
|config|保留未验收标志的配置示例|
|tests、ros/*/tests、frontend/e2e|软件回归|

## 机器人接入

机器人侧使用现有 ROS Noetic、Astra 图像驱动与 SwiftPro 固件接口，具体设备和标定不可由源码推断。参见 docs/ROBOT_INTEGRATION.md。仓库不包含现场口令、实际启用配置、相机原始数据或私有运行记录。不得将示例配置标记为已验收后直接运行。

## 来源和辅助工具

吸盘取放与平面标定思路参考课程仓储例程，ROS 与 SwiftPro 接口属于原平台。增量包括任务状态机、网页控制、反馈复核、视觉检查、租约与证据绑定。使用 OpenAI Codex 辅助代码编写、调试、测试和材料整理。课程/第三方代码权利归原作者；本次公开展示未另行授予开源许可证。
