"""One-shot: release any stuck RC session and stop the vacuum."""
import asyncio, json, dataclasses
from pathlib import Path
from roborock.data.containers import UserData
from roborock.devices.device_manager import UserParams, create_device_manager
from roborock.roborock_typing import RoborockCommand as RC

async def main():
    blob = json.loads((Path.home() / ".roborock_creds.json").read_text())
    email = blob["email"]
    ud = UserData.from_dict(blob["user_data"])
    mgr = await create_device_manager(UserParams(username=email, user_data=ud))
    devices = [d for d in await mgr.get_devices() if d.v1_properties]
    dev = devices[0]
    cmd = dev.v1_properties.command
    print(f"Resetting {dev.name}...")
    for label, c in (("RC_END", RC.APP_RC_END), ("STOP", RC.APP_STOP)):
        try:
            await asyncio.wait_for(cmd.send(c), timeout=8)
            print(f"  {label} OK")
        except Exception as e:
            print(f"  {label} -> {type(e).__name__}: {e}")
    print("done")

asyncio.run(main())
