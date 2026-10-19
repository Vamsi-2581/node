"""Safety pack: stop before hitting anything in front, cap speed.

Pure and deterministic: no imports beyond json/sys, no I/O inside check().
Usable as a normal tool too: JSON {"obs": ..., "cmd": ...} on stdin, the
verdict as JSON on stdout.
"""
import json
import sys

STOP_DISTANCE_M = 0.45      # nothing closer than this ahead while moving forward
SLOW_DISTANCE_M = 1.0       # inside this, forward speed scales down to zero
MAX_LINEAR_MPS = 0.5
MAX_ANGULAR_RPS = 1.5


def check(obs, cmd):
    linear = float(cmd.get("linear", 0.0))
    angular = float(cmd.get("angular", 0.0))
    front = (obs.get("scan") or {}).get("front_min")
    if front is None:
        return {"verdict": "stop", "cmd": {"linear": 0.0, "angular": 0.0},
                "reason": "no front range reading"}
    front = float(front)
    if linear > 0 and front <= STOP_DISTANCE_M:
        return {"verdict": "stop", "cmd": {"linear": 0.0, "angular": 0.0},
                "reason": f"obstacle {front:.2f} m ahead"}
    out_linear = max(-MAX_LINEAR_MPS, min(MAX_LINEAR_MPS, linear))
    if out_linear > 0 and front < SLOW_DISTANCE_M:
        span = SLOW_DISTANCE_M - STOP_DISTANCE_M
        out_linear = min(out_linear,
                         MAX_LINEAR_MPS * (front - STOP_DISTANCE_M) / span)
    out_angular = max(-MAX_ANGULAR_RPS, min(MAX_ANGULAR_RPS, angular))
    if (out_linear, out_angular) == (linear, angular):
        return {"verdict": "pass", "cmd": {"linear": linear, "angular": angular},
                "reason": ""}
    return {"verdict": "clip",
            "cmd": {"linear": out_linear, "angular": out_angular},
            "reason": f"limited to {out_linear:.2f} m/s, {out_angular:.2f} rad/s"}


if __name__ == "__main__":
    args = json.load(sys.stdin)
    print(json.dumps(check(args.get("obs") or {}, args.get("cmd") or {})))
