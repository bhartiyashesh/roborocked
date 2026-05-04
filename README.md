# Qrevo Drive

Joystick / keyboard remote control for a Roborock Qrevo Plus, using
[`python-roborock`](https://github.com/Python-roborock/python-roborock) and pygame.

## Setup (Mac Mini M4 Pro, one-time)

```bash
cd qrevo-drive
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

If you're using a Bluetooth gamepad (PS5 DualSense, Xbox, 8BitDo Pro 2, Joy-Cons,
etc.), pair it under **System Settings → Bluetooth** before running. macOS exposes
it to pygame automatically — no driver needed.

## Run

First time, pass your Roborock account email so the script can request a login code:

```bash
python qrevo_drive.py --email you@example.com
```

You'll get a 6-digit code by email; paste it into the terminal. After that the
script caches your token at `~/.roborock_creds.json` (chmod 600), and subsequent
runs are zero-prompt:

```bash
python qrevo_drive.py
```

To force keyboard mode even with a gamepad attached:

```bash
python qrevo_drive.py --keyboard
```

To wipe the cached credentials:

```bash
python qrevo_drive.py --logout
```

## Controls

| | Gamepad | Keyboard |
|---|---|---|
| Forward / back | Left stick Y | `W` `S` or `↑` `↓` |
| Turn left / right | Left stick X | `A` `D` or `←` `→` |
| Quit | `B` / Circle | `ESC` or `Q` |

In keyboard mode a small pygame window opens — keep it focused for keys to register.

## What's happening under the hood

- `app_rc_start` puts the Qrevo into manual-control state (cancels any active job).
- The drive loop sends `app_rc_move` at 8 Hz with `(omega, velocity, duration, seqnum)`.
  - `omega` is in radians/sec, capped at ±π (±180°/s) — protocol limit.
  - `velocity` is m/s, capped at ±0.3 — protocol limit.
  - `seqnum` increments each tick. The Qrevo drops out-of-order packets.
  - `duration` is a hold time in ms; we send slightly longer than the tick period
    so movement stays smooth across cloud-RTT jitter.
- `app_rc_end` returns the vacuum to idle.

Cliff sensors, bumpers, and structured-light obstacle avoidance stay active in
manual mode — you can't drive it down the stairs.

## Latency

Commands route through Roborock's cloud, which adds ~200–500 ms each way
depending on your region. If you want crisp control, the `python-roborock`
library also supports a direct LAN MQTT connection — pull the local key out
of the cached `user_data` blob and use the local channel. That gets you under
30 ms.

## Notes

- The vacuum stops the cleaning job when you start manual control.
- If the connection blips, just re-run the script. It re-issues `app_rc_start`.
- If the vacuum stops responding to stick input, it likely hit the obstacle
  avoidance limit. Back off and try a different angle.
