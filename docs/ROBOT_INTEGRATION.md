# 机器人集成说明

当前固定站流程不需要底盘导航。ROS 图像适配器位于 ros/spark_blocks_bridge/scripts/fixed_tower_agent.py，订阅 Astra RGB 图像并与 HTTP 后端交换任务。FastAPI 和 Vue 均不是 ROS 节点。

原移动搬运桥接保留于 blocks_bridge_node.py 和 launch/blocks.launch，可在独立 catkin 工作空间构建，用 dry_run=true 检查读链路；此分支不等于固定站流程的实物验收。

```bash
source /opt/ros/noetic/setup.bash
mkdir -p ~/blocks_ws/src
# 将 ros/spark_blocks_bridge 复制到已配置厂家依赖的工作空间 src 下
cd ~/blocks_ws
catkin_make
source devel/setup.bash
roslaunch spark_blocks_bridge blocks.launch backend_url:=http://<PC_IP>:8768 dry_run:=true
```

固定站控制台源在 robot_console/。所有文件须同版放在独立部署目录，相机标定 thefile.txt 和 private/fixed-site.json 必须由现场已有、实测配置提供。控制台只有一个串口所有者，业务适配器通过 Unix socket 调用；不与厂商的其他串口进程同时运行。依赖 Python 3、pyserial、numpy、opencv、requests 和 ROS cv_bridge。控制令牌与桥接令牌由私有环境变量注入，不提交 Git。

网页适配器读取 SPARK_FIXED_BASE、SPARK_FIXED_BACKEND、SPARK_BRIDGE_TOKEN。本仓库不提供自动解除硬件锁或无人值守启动运动的脚本。先核对相机位置、送料区、每层实际高度和完整路径，再由现场人员组织验收。

机械臂位姿通过 P2200 读取关节反馈后，以 M2221 换算 XYZ。M2222 可做固件逆运动学查询，结果只说明采样点可达。G0 为位置运动，M2231 控制气泵；查询应答或已发开泵命令不能证明实际吸附。模板配置保持未验收，历史成功落点不能覆盖整个工作区。
