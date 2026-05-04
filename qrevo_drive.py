#!/usr/bin/env python3
"""
Roborock Qrevo Plus joystick / keyboard driver.

  First run:  python qrevo_drive.py --email you@example.com
  Later runs: python qrevo_drive.py

Controls:
  Gamepad   left stick = forward + turn,  B/Circle button = quit
  Keyboard  WASD or arrows,               ESC or Q       = quit
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


def read_pad(joy) -> tuple[float, float, bool]:
    pygame.event.pump()
    fwd  = -dz(joy.get_axis(1))   # stick up = forward
    turn = -dz(joy.get_axis(0))   # stick right = right turn
    quit_now = bool(joy.get_button(1))  # B / Circle
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


# ---------------------------------------------------------------------------
# Drive loop
# ---------------------------------------------------------------------------
async def drive(device, joy):
    cmd = device.v1_properties.command
    print("Entering manual control mode...")
    await cmd.send(RC.APP_RC_START)
    await asyncio.sleep(1.5)        # vacuum needs a beat to switch state
    print("Driving. Quit with",
          "B/Circle button.\n" if joy else "ESC or Q (focus the pygame window).\n")

    seq = 0
    period = 1.0 / TICK_HZ
    read = (lambda: read_pad(joy)) if joy else read_keys

    try:
        while True:
            fwd, turn, quit_now = read()
            if quit_now:
                break

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
    pygame.joystick.init()

    joy = None
    if not args.keyboard and pygame.joystick.get_count() > 0:
        joy = pygame.joystick.Joystick(0)
        joy.init()
        print(f"Gamepad: {joy.get_name()}")
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

    await drive(device, joy)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
