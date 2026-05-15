#!/usr/bin/env python3
"""
Roborock Qrevo Plus joystick / keyboard driver.

  First run:  python qrevo_drive.py --email you@example.com
  Later runs: python qrevo_drive.py

Controls (gamepad):
  Left stick       drive (forward / turn)
  A                start full clean        (exits drive)
  Y                pause                   (exits drive)
  X                spot clean              (exits drive)
  Back / View      return to dock          (exits drive)
  Start / Menu     stop                    (exits drive)
  D-pad Down       empty dustbin into dock (exits drive)
  D-pad Left       dock + wash mop         (exits drive)
  D-pad Up         find me (beep)
  D-pad Right      toggle mop on / off (water flow)
  LB / RB          fan power down / up
  B / Circle       quit

Controls (keyboard, fallback): WASD / arrows + ESC or Q to quit.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import math
import sys
from pathlib import Path

import pygame
from pygame._sdl2 import controller as sdl_controller

from roborock.data.containers import UserData
from roborock.devices.device_manager import UserParams, create_device_manager
from roborock.roborock_typing import RoborockCommand as RC
from roborock.web_api import RoborockApiClient

# --- Roborock manual-control protocol limits ---
MAX_VEL_MPS   = 0.3            # ±0.3 m/s
MAX_OMEGA_DEG = 180            # ±180 deg/s
TICK_HZ       = 8              # send rate; 8 Hz sits inside cloud RTT
DEADZONE      = 0.08

CREDS_PATH = Path.home() / ".roborock_creds.json"

# SDL game-controller named buttons & axes — platform-independent.
BTN_A      = pygame.CONTROLLER_BUTTON_A
BTN_B      = pygame.CONTROLLER_BUTTON_B
BTN_X      = pygame.CONTROLLER_BUTTON_X
BTN_Y      = pygame.CONTROLLER_BUTTON_Y
BTN_LB     = pygame.CONTROLLER_BUTTON_LEFTSHOULDER
BTN_RB     = pygame.CONTROLLER_BUTTON_RIGHTSHOULDER
BTN_BACK   = pygame.CONTROLLER_BUTTON_BACK
BTN_START  = pygame.CONTROLLER_BUTTON_START
BTN_DUP    = pygame.CONTROLLER_BUTTON_DPAD_UP
BTN_DDOWN  = pygame.CONTROLLER_BUTTON_DPAD_DOWN
BTN_DLEFT  = pygame.CONTROLLER_BUTTON_DPAD_LEFT
BTN_DRIGHT = pygame.CONTROLLER_BUTTON_DPAD_RIGHT
AX_LX      = pygame.CONTROLLER_AXIS_LEFTX
AX_LY      = pygame.CONTROLLER_AXIS_LEFTY

FAN_POWERS = [101, 102, 103, 104]
FAN_NAMES  = {101: "quiet", 102: "balanced", 103: "turbo", 104: "max"}
# Water box modes — 200 = mop off, 202 = mop on (medium flow).
WATER_OFF  = 200
WATER_ON   = 202


# ---------------------------------------------------------------------------
# Credential cache
# ---------------------------------------------------------------------------
def load_creds() -> tuple[str, UserData] | None:
    if not CREDS_PATH.exists():
        return None
    blob = json.loads(CREDS_PATH.read_text())
    return blob["email"], UserData.from_dict(blob["user_data"])


def save_creds(email: str, user_data: UserData) -> None:
    CREDS_PATH.write_text(
        json.dumps({"email": email, "user_data": dataclasses.asdict(user_data)})
    )
    CREDS_PATH.chmod(0o600)


async def login(email_arg: str | None) -> tuple[str, UserData]:
    cached = load_creds()
    if cached and (email_arg is None or cached[0] == email_arg):
        print(f"Using cached login for {cached[0]}")
        return cached

    email = email_arg or input("Email: ").strip()
    web = RoborockApiClient(username=email)
    await web.request_code()
    code = input(f"Code sent to {email}: ").strip()
    user_data = await web.code_login(code)
    save_creds(email, user_data)
    print("Login saved to", CREDS_PATH)
    return email, user_data


# ---------------------------------------------------------------------------
# Device selection
# ---------------------------------------------------------------------------
async def pick_device(email: str, user_data: UserData):
    mgr = await create_device_manager(UserParams(username=email, user_data=user_data))
    devices = await mgr.get_devices()
    v1 = [d for d in devices if d.v1_properties]
    if not v1:
        sys.exit("No V1 vacuums found on this account.")
    if len(v1) == 1:
        return v1[0]
    for i, d in enumerate(v1):
        print(f"  [{i}] {d.name}")
    return v1[int(input("Pick device: "))]


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------
def dz(x: float) -> float:
    return 0.0 if abs(x) < DEADZONE else x


def read_pad(ctrl) -> tuple[float, float, bool]:
    """ctrl is a pygame._sdl2.controller.Controller. Axes are int16 (-32768..32767)."""
    pygame.event.pump()
    fwd  = -dz(ctrl.get_axis(AX_LY) / 32768.0)   # stick up = forward
    turn = -dz(ctrl.get_axis(AX_LX) / 32768.0)   # stick right = right turn
    quit_now = bool(ctrl.get_button(BTN_B))
    return fwd, turn, quit_now


def read_keys() -> tuple[float, float, bool]:
    pygame.event.pump()
    k = pygame.key.get_pressed()
    fwd  = (1 if k[pygame.K_w] or k[pygame.K_UP]    else 0) \
         - (1 if k[pygame.K_s] or k[pygame.K_DOWN]  else 0)
    turn = (1 if k[pygame.K_d] or k[pygame.K_RIGHT] else 0) \
         - (1 if k[pygame.K_a] or k[pygame.K_LEFT]  else 0)
    quit_now = k[pygame.K_ESCAPE] or k[pygame.K_q]
    # also drain QUIT events so the pygame window can be closed cleanly
    for ev in pygame.event.get():
        if ev.type == pygame.QUIT:
            quit_now = True
    return float(fwd), float(turn), quit_now


class EdgeDetector:
    """Edge-triggered button detection (fires once per press, not while held)."""
    def __init__(self):
        self.prev: dict[int, bool] = {}

    def button(self, ctrl, btn: int) -> bool:
        cur = bool(ctrl.get_button(btn))
        was = self.prev.get(btn, False)
        self.prev[btn] = cur
        return cur and not was


# Actions that take the vacuum out of manual control. Pressing one releases
# RC mode, sends the command, and exits the drive loop — you don't drive while
# the vacuum is auto-cleaning, docking, washing, etc.
EXIT_ACTIONS: dict[str, tuple[str, "RC", list | None]] = {
    "A":     ("start full clean",        RC.APP_START,              None),
    "Y":     ("pause",                   RC.APP_PAUSE,              None),
    "X":     ("spot clean",              RC.APP_SPOT,               None),
    "BACK":  ("return to dock",          RC.APP_CHARGE,             None),
    "START": ("stop",                    RC.APP_STOP,               None),
    "DOWN":  ("empty dustbin into dock", RC.APP_START_COLLECT_DUST, None),
    "LEFT":  ("dock + wash mop",         RC.START_WASH_THEN_CHARGE, None),
}


def poll_gamepad_actions(ctrl, edge: EdgeDetector, fan_idx: list[int], mop_on: list[bool]
                        ) -> tuple[str, str, list | None] | None:
    """Return (label, RC command, params) on an action press, else None.

    Mutates fan_idx[0] / mop_on[0] in place when those settings cycle.
    """
    exit_buttons = (
        ("A",     BTN_A),
        ("Y",     BTN_Y),
        ("X",     BTN_X),
        ("BACK",  BTN_BACK),
        ("START", BTN_START),
        ("DOWN",  BTN_DDOWN),
        ("LEFT",  BTN_DLEFT),
    )
    for name, btn in exit_buttons:
        if edge.button(ctrl, btn) and name in EXIT_ACTIONS:
            label, command, params = EXIT_ACTIONS[name]
            return ("EXIT:" + label, command, params)

    if edge.button(ctrl, BTN_DUP):
        return ("find me (beep)", RC.FIND_ME, None)

    if edge.button(ctrl, BTN_DRIGHT):
        mop_on[0] = not mop_on[0]
        flow = WATER_ON if mop_on[0] else WATER_OFF
        return (f"mop: {'on' if mop_on[0] else 'off'}", RC.SET_WATER_BOX_CUSTOM_MODE, [flow])

    if edge.button(ctrl, BTN_LB):
        fan_idx[0] = max(0, fan_idx[0] - 1)
        power = FAN_POWERS[fan_idx[0]]
        return (f"fan power: {FAN_NAMES[power]}", RC.SET_CUSTOM_MODE, [power])

    if edge.button(ctrl, BTN_RB):
        fan_idx[0] = min(len(FAN_POWERS) - 1, fan_idx[0] + 1)
        power = FAN_POWERS[fan_idx[0]]
        return (f"fan power: {FAN_NAMES[power]}", RC.SET_CUSTOM_MODE, [power])

    return None


# ---------------------------------------------------------------------------
# Drive loop
# ---------------------------------------------------------------------------
async def reset_state(cmd) -> None:
    """Clear any stuck manual-control / cleaning state from a prior killed run."""
    print("Resetting vacuum state...")
    for label, c in (("RC_END", RC.APP_RC_END), ("STOP", RC.APP_STOP)):
        try:
            await asyncio.wait_for(cmd.send(c), timeout=8)
        except Exception as e:
            print(f"  ({label} -> {type(e).__name__}: {e})")
    await asyncio.sleep(0.5)


async def drive(device, ctrl):
    cmd = device.v1_properties.command
    await reset_state(cmd)
    print("Entering manual control mode...")
    await cmd.send(RC.APP_RC_START)
    await asyncio.sleep(1.5)        # vacuum needs a beat to switch state
    if ctrl:
        print("Driving. See header for button map. B/Circle to quit.\n")
    else:
        print("Driving. ESC or Q to quit (focus the pygame window).\n")

    seq = 0
    period = 1.0 / TICK_HZ
    read = (lambda: read_pad(ctrl)) if ctrl else read_keys
    edge = EdgeDetector() if ctrl else None
    fan_idx = [1]   # balanced
    mop_on  = [True]   # vacuum boots with mop active by default
    pending_exit_action: tuple[str, "RC", list | None] | None = None

    try:
        while True:
            fwd, turn, quit_now = read()
            if quit_now:
                break

            if ctrl and edge is not None:
                action = poll_gamepad_actions(ctrl, edge, fan_idx, mop_on)
                if action is not None:
                    label, command, params = action
                    if label.startswith("EXIT:"):
                        pending_exit_action = (label[5:], command, params)
                        break
                    print(f"\n→ {label}")
                    try:
                        await (cmd.send(command, params) if params else cmd.send(command))
                    except Exception as e:
                        print(f"  ({label} failed: {e})")

            seq += 1
            velocity  = fwd * MAX_VEL_MPS
            omega_deg = turn * MAX_OMEGA_DEG
            params = {
                "omega":    round(math.radians(omega_deg), 2),
                "velocity": round(velocity, 2),
                "duration": int(period * 1000) + 400,   # overlap so we don't stutter
                "seqnum":   seq,
            }
            await cmd.send(RC.APP_RC_MOVE, [params])

            sys.stdout.write(
                f"\rvel={velocity:+.2f} m/s  omega={omega_deg:+4.0f}°/s  seq={seq:>5}"
            )
            sys.stdout.flush()

            await asyncio.sleep(period)
    finally:
        print("\nReleasing manual control...")
        try:
            await cmd.send(RC.APP_RC_END)
        except Exception as e:
            print("Cleanup warning:", e)

        if pending_exit_action is not None:
            label, command, params = pending_exit_action
            await asyncio.sleep(0.8)  # let RC release land before issuing the next mode
            print(f"→ {label}")
            try:
                await cmd.send(command, params) if params else await cmd.send(command)
            except Exception as e:
                print(f"  ({label} failed: {e})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", help="Roborock account email (only needed for first login)")
    ap.add_argument("--keyboard", action="store_true", help="Force keyboard mode")
    ap.add_argument("--logout", action="store_true", help="Forget cached credentials and exit")
    args = ap.parse_args()

    if args.logout:
        if CREDS_PATH.exists():
            CREDS_PATH.unlink()
            print("Forgot cached credentials.")
        return

    pygame.init()
    sdl_controller.init()

    ctrl = None
    if not args.keyboard and sdl_controller.get_count() > 0 and sdl_controller.is_controller(0):
        ctrl = sdl_controller.Controller(0)
        print(f"Gamepad: {ctrl.name}")
    else:
        # Keyboard input requires a focused pygame window
        pygame.display.set_mode((420, 220))
        pygame.display.set_caption("Roborock Drive — WASD/arrows, ESC/Q to quit")
        if args.keyboard:
            print("Keyboard mode forced.")
        else:
            print("No gamepad detected — falling back to keyboard mode.")

    email, user_data = await login(args.email)
    device = await pick_device(email, user_data)
    print(f"Connected to: {device.name}\n")

    await drive(device, ctrl)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
