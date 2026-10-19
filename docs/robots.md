# Robots: Autonet agents with a body

Status: MVP built (2026-10-19), sim only, E2E 10/10. Design decided in conversation with the
owner; this file is the ledger. Nothing here touches contracts.

## The three layers

A robot runs at three speeds, and Autonet owns only two seams of them.

| Layer | Speed | Who owns it |
|-------|-------|-------------|
| Body control (proprioception, motor policies) | 100 to 1000 Hz | Off the shelf: ROS 2, `ros2_control`, Nav2, open VLAs |
| Directives (safety packs) | Same rate as the commands they filter | Autonet: governed, hash-pinned packs enforced on the robot |
| Agent (goals, judgment, escalation) | Seconds | Autonet: the existing agent loop, unchanged |

The agent sets goals and calls skills. It never closes a motor loop.

## Decisions

1. **The robot runs its own daemon, as a sponsored dependent of the household
   daemon** (`docs/sponsored_inference.md`). All judgment goes through the
   sponsor: the sponsor agent is the cognitive backstop and the escalation
   target. The household is the liable entity. The robot holds no provider
   credentials, so a compromised robot has no key to steal.
2. **Sponsor unreachable means packs-only safe stop.** No judgment available,
   no motion: the robot stops and holds.
3. **Directives are safety packs, and a pack is an ordinary pinned tool.** It
   is authored, bonded, reviewed and adopted like any tool, so a pack that
   fails and leads to harm slashes its author's bond. The packs a jurisdiction
   requires are its **standards**.
4. **Packs run on the robot, below the agent, at the actuation boundary.**
   Every command source (Nav2, a teleop, the agent through its connector) only
   *proposes* motion. Only the filter's output reaches the motors.
5. **Fail closed.** A missing or tampered required pack, a pack that raises or
   runs too long, stale sensor data, or a stale sponsor heartbeat all produce
   a stop.
6. **Polling is for robots in shared space only.** A DAO (or anyone the robot
   answers to) can send a random challenge. The robot answers before a deadline
   with a signed statement over its recent worklog (rosbag2 MCAP chunks), the
   challenge and the active pack digests. Faking that worklog in time is harder
   than sending the real one, and witnesses sharing the space can cross-check
   it. Private digital daemons are never polled: what an agent does in the
   privacy of its own daemon is not the network's business.
7. **Identity in public space**: household and robot 0x addresses on the body
   (printed, QR) plus a signed broadcast beacon, Remote ID style. Not in the
   MVP.
8. **Public services** are DAOs under the state DAO (fractal block), running
   their own daemons and fleets. Subordinates are funded by transfer proposals
   or tranche drips with a halt vote; trustless projects are for peers. Not in
   the MVP.

## The seam: ROS 2

ROS 2 is the de facto standard for open robot stacks (not universal: Tesla and
Figure run their own). Autonet plugs in at three points:

- **Skills as tools.** `ros-mcp-server` (PyPI `ros-mcp`) exposes topics,
  services and actions over rosbridge as MCP tools. The daemon takes it as an
  ordinary MCP connector. No new daemon code.
- **Safety filter node** (`robot/ros/safety_filter.py`). Subscribes to the
  proposed command topic and the sensors, runs the required packs, publishes
  the filtered command to the topic the motors listen on, and publishes every
  verdict on `/safety/events`. The pack logic itself is plain stdlib Python
  (`atn/robot/packs.py`) so the same code runs in the daemon and on the robot.
- **Worklog.** `ros2 bag record` with MCAP storage, split into short chunks.
  The challenge response hashes the chunks that overlap the window.

## Pack contract

A pack is a pinned tool whose code defines

```python
def check(obs: dict, cmd: dict) -> dict:
    # obs: {"stamp": float, "scan": {"front_min": float, ...}, ...}
    # cmd: {"linear": float, "angular": float}
    return {"verdict": "pass" | "clip" | "stop", "cmd": {...}, "reason": str}
```

and also works as a normal tool (JSON `{obs, cmd}` on stdin, verdict JSON on
stdout), so agents can test and review it with `use_tool`. A pack declares no
capabilities: no network, no files, no subprocesses. The runtime refuses one
that does.

Composition: every required pack runs on every command. `stop` beats `clip`
beats `pass`; clips apply in order.

Pinning: the daemon exports each required pack as `<digest>.json` (the signed
manifest) plus `<code_digest>.py`. The filter recomputes both sha256 digests
before loading. The required set lives in the robot daemon's config for the
MVP (`robot.required_packs`); reading it from the jurisdiction's Registry
(`jurisdiction.<id>.robot_standards`) is a follow-up.

## MVP

No hardware. Everything runs against a simulated TurtleBot3 in Gazebo,
headless, in Docker (`robot/sim/`). Nav2 is not started: the agent's skills
for the MVP are raw topic publishes through ros-mcp.

| Piece | Where |
|-------|-------|
| Pack runtime (stdlib only) | `atn/robot/packs.py` |
| Reference pack: proximity stop + speed cap | `robot/packs/proximity_stop.py` |
| ROS 2 filter node | `robot/ros/safety_filter.py` |
| Worklog statement, signing, verification | `atn/robot/worklog.py` |
| Daemon side (export, heartbeat, attest) | `atn/robot/service.py`, `robot:` in config.yaml, WS `robot_status` / `robot_export_packs` / `robot_attest` (owner only) |
| Sim image and entrypoint | `robot/sim/Dockerfile`, `run.sh`, `bridge.yaml` |
| Unit tests | `tests/atn/test_robot_packs.py` |
| End to end | `scripts/local_e2e_robot.py` |

Config on the robot's daemon:

```yaml
robot:
  enabled: true
  agent_id: rover            # its key signs attestations
  required_packs: [proximity_stop]
  bundle_dir: /robot/bundle  # read by the filter node
  bag_dir: /robot/bags       # MCAP worklog chunks
```

The E2E (`python scripts/local_e2e_robot.py`, needs Docker) passes 10/10:
sim up; pack exported and loaded; driving at an obstacle is slowed then
stopped (from 1.27 m to 0.46 m, peak 0.46 m/s under a 0.5 cap); reversing
away is allowed; a tampered pack is refused and nothing moves; re-export
restores motion; a stale daemon heartbeat stops the robot; an unreachable
sponsor stops it; a challenge answer verifies, and a tampered chunk, a wrong
key and a late answer all fail; the agent's ros-mcp connector pushing forward
for 6 s is clipped and never gets closer than the stop distance.

Known limits of the MVP:

- The filter runs packs in-process, so an over-budget pack is caught after the
  fact (its command is never forwarded, but it cannot be preempted).
- Sensor age is a filter parameter: 0.5 s by default, 0.8 s in the sim
  because its lidar runs at 5 Hz with occasional missed frames. Brief stops
  from a missed frame are the intended behavior.
- On a Windows host the worklog cannot be written through the bind mount
  (bag rollover fails), so it stays on the container's disk and the E2E copies
  it out with `docker cp` before a challenge. On a real robot the daemon reads
  its own disk.
- The challenge is not yet folded into the worklog itself (publishing it on a
  topic so the next chunk carries it). The deadline is what makes faking hard
  today.
- Sponsor reachability is "the named sponsor is advertising on p2p", from the
  sponsored provider's discovery. The E2E stands it in with
  `ATN_ROBOT_SPONSOR_PROBE=assume`; a live two-daemon sponsor run is not done.
- The robot agent in the E2E is an `external` agent (it only signs). A real
  robot agent would run on `rpb` through its sponsor.

Out of the MVP: beacon, sanctions list, TPM quotes, witness cross-checks,
Registry-backed standards, public-service DAOs, Nav2 skills.
