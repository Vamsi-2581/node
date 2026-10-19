# Simulated robot for the robot MVP: TurtleBot3 + Nav2 in Gazebo (headless),
# rosbridge for the agent connector, MCAP rosbags for the worklog.
FROM ros:jazzy-ros-base

RUN apt-get update && apt-get install -y --no-install-recommends \
      ros-jazzy-nav2-bringup \
      ros-jazzy-nav2-minimal-tb3-sim \
      ros-jazzy-rosbridge-server \
      ros-jazzy-rosbag2-storage-mcap \
      python3-yaml \
    && rm -rf /var/lib/apt/lists/*

ENV TURTLEBOT3_MODEL=waffle
WORKDIR /robot
