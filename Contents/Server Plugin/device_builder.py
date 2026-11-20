# -*- coding: utf-8 -*-
"""
Minimal client for the ESPHome Device Builder (ESPHome 2026.6+ dashboard).

Talks to the multiplexed /ws API:
  -> {"command": "...", "message_id": "...", "args": {...}}
  <- {"message_id": "...", "event": "output"|"result", "data": ...}   (streams)
  <- {"message_id": "...", "result": ...}                             (completion)
  <- {"message_id": "...", "error_code": "...", "details": "..."}     (failure)

The legacy /compile and /upload endpoints still exist but are deprecated, so
this uses the new protocol only.
"""

import asyncio
import itertools
import json
import re

import aiohttp


class DeviceBuilderError(Exception):
    pass


_DONE = object()


class DeviceBuilderClient:
    def __init__(self, logger, url, username="", password=""):
        self.logger = logger
        self.base_url = url.strip().rstrip("/")
        self.ws_url = re.sub(r"^http", "ws", self.base_url, count=1) + "/ws"
        self.username = username or ""
        self.password = password or ""
        self.server_info = {}
        self._session = None
        self._ws = None
        self._reader = None
        self._connect_lock = asyncio.Lock()
        self._ids = itertools.count(1)
        self._pending = {}   # message_id -> Future
        self._streams = {}   # message_id -> Queue

    # ------------------------------------------------------------------ #
    async def close(self):
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._reader is not None:
            self._reader.cancel()
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._ws = self._reader = self._session = None

    async def _ensure_connected(self):
        async with self._connect_lock:
            if self._ws is not None and not self._ws.closed:
                return
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            try:
                self._ws = await self._session.ws_connect(self.ws_url, heartbeat=30, max_msg_size=0)
                self.server_info = await self._ws.receive_json(timeout=15)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
                raise DeviceBuilderError(f"cannot reach Device Builder at {self.base_url} ({err})") from err
            self._reader = asyncio.get_running_loop().create_task(self._read_loop())
            if self.server_info.get("requires_auth"):
                if not self.username:
                    raise DeviceBuilderError("Device Builder requires a username and password (set them in plugin config)")
                try:
                    await self._request("auth/login", {"username": self.username, "password": self.password}, 30)
                except Exception as err:
                    await self._ws.close()
                    raise DeviceBuilderError(f"Device Builder login failed ({err})") from err

    async def _read_loop(self):
        ws = self._ws
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                mid = data.get("message_id", "")
                stream = self._streams.get(mid)
                if "event" in data:
                    if stream is not None:
                        stream.put_nowait((data.get("event"), data.get("data")))
                    continue
                future = self._pending.pop(mid, None)
                if "error_code" in data:
                    err = DeviceBuilderError(f"{data.get('error_code')}: {data.get('details', '')}".rstrip(": "))
                    if future is not None and not future.done():
                        future.set_exception(err)
                    if stream is not None:
                        stream.put_nowait(("error", err))
                else:
                    if future is not None and not future.done():
                        future.set_result(data.get("result"))
                    if stream is not None:
                        stream.put_nowait((_DONE, data.get("result")))
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self.logger.debug(f"Device Builder connection error: {err}")
        finally:
            lost = DeviceBuilderError("connection to Device Builder lost")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(lost)
            self._pending.clear()
            for queue in self._streams.values():
                queue.put_nowait(("error", lost))

    async def _request(self, command, args, timeout):
        mid = f"indigo-{next(self._ids)}"
        future = asyncio.get_running_loop().create_future()
        self._pending[mid] = future
        await self._ws.send_json({"command": command, "message_id": mid, "args": args or {}})
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(mid, None)

    # ------------------------------------------------------------------ #
    async def command(self, command, args=None, timeout=60):
        await self._ensure_connected()
        return await self._request(command, args, timeout)

    async def stream(self, command, args, on_line):
        """Run a streaming command; calls on_line for each output line, returns the 'result' event data."""
        await self._ensure_connected()
        mid = f"indigo-{next(self._ids)}"
        queue = asyncio.Queue()
        self._streams[mid] = queue
        final = None
        try:
            await self._ws.send_json({"command": command, "message_id": mid, "args": args or {}})
            while True:
                event, data = await queue.get()
                if event == "output":
                    on_line(data if isinstance(data, str) else str(data))
                elif event == "result":
                    final = data
                elif event == "error":
                    raise data
                elif event is _DONE:
                    return final
        finally:
            self._streams.pop(mid, None)

    # ------------------------------------------------------------------ #
    async def list_devices(self):
        result = await self.command("devices/list") or {}
        return result.get("configured", []) or []

    async def follow_job(self, job_id, on_line):
        result = await self.stream("firmware/follow_job", {"job_id": job_id}, on_line) or {}
        return result

    async def get_jobs(self, configuration):
        return await self.command("firmware/get_jobs", {"configuration": configuration}) or []

    # ------------------------------------------------------------------ #
    # New-board workflow
    # ------------------------------------------------------------------ #
    async def search_boards(self, query="", limit=40):
        result = await self.command("boards/get_boards", {"query": query or None, "limit": limit}) or {}
        out = []
        for b in result.get("boards", []) or []:
            out.append({"id": b.get("id", ""), "name": b.get("name", ""),
                        "manufacturer": b.get("manufacturer", ""), "generic": bool(b.get("is_generic")),
                        "featured": bool(b.get("featured"))})
        return out

    async def create_device(self, name, friendly_name="", board_id="", ssid="", psk=""):
        args = {"name": name, "friendly_name": friendly_name or None, "board_id": board_id or None,
                "ssid": ssid or "", "psk": psk or ""}
        return await self.command("devices/create", args, timeout=120) or {}

    async def get_config(self, configuration):
        return await self.command("devices/get_config", {"configuration": configuration})

    async def update_config(self, configuration, content, expected=None):
        args = {"configuration": configuration, "content": content}
        if expected is not None:
            args["expected"] = expected
        await self.command("devices/update_config", args)

    async def get_encryption_key(self, configuration):
        result = await self.command("devices/get_encryption_key", {"configuration": configuration}) or {}
        return result.get("key", "") or ""

    async def download_factory_binary(self, configuration, dest_dir):
        """Fetch the first-flash image (ESP32 factory .bin, else firmware.bin). Returns local path."""
        import os
        binaries = await self.command("firmware/get_binaries", {"configuration": configuration}) or []
        if not binaries:
            raise DeviceBuilderError("no firmware built for this configuration")
        files = [b.get("file", "") for b in binaries]
        choice = next((f for f in files if "factory" in f), None) or \
            next((f for f in files if f.endswith(".bin") and "ota" not in f), None)
        if not choice:
            raise DeviceBuilderError(f"no flashable binary found (have: {', '.join(files)})")
        token = await self.command("firmware/download_token", {"configuration": configuration, "file": choice})
        url = f"{self.base_url}/api/firmware/download"
        async with self._session.get(url, params={"token": token["token"]},
                                     timeout=aiohttp.ClientTimeout(total=120)) as resp:
            if resp.status != 200:
                raise DeviceBuilderError(f"firmware download failed (HTTP {resp.status})")
            data = await resp.read()
        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, token.get("filename") or choice)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    # ------------------------------------------------------------------ #
    # Secrets (values are write-only: the Device Builder never returns them)
    # ------------------------------------------------------------------ #
    async def secret_names(self):
        return await self.command("config/get_secrets") or []

    async def set_wifi(self, ssid, password):
        await self.command("config/set_wifi_credentials", {"ssid": ssid, "password": password})

    async def set_secret(self, key, value):
        return await self.command("config/set_secret", {"key": key, "value": value, "overwrite": True}) or {}
