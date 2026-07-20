import asyncio
import json
import urllib.request

import websockets


BASE_URL = "http://127.0.0.1:8001"


async def main():
    token = json.loads(
        urllib.request.urlopen(f"{BASE_URL}/api/client_config", timeout=10).read()
    )["dashboard_token"]
    async with websockets.connect(f"ws://127.0.0.1:8001/ws?token={token}") as ws:
        msg = await asyncio.wait_for(ws.recv(), timeout=15)
        payload = json.loads(msg)
        print(json.dumps({"type": payload.get("type"), "bytes": len(msg)}, indent=2))


asyncio.run(main())
