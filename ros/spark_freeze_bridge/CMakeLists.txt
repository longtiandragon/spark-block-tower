cmake_minimum_required(VERSION 3.0.2)
project(spark_freeze_bridge)
find_package(catkin REQUIRED COMPONENTS rospy geometry_msgs nav_msgs sensor_msgs cv_bridge)
catkin_python_setup()
catkin_package()
catkin_install_python(PROGRAMS scripts/freeze_bridge_node.py
  DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION})
install(DIRECTORY launch DESTINATION ${CATKIN_PACKAGE_SHARE_DESTINATION})
