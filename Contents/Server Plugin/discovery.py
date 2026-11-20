# -*- coding: utf-8 -*-
"""Find ESPHome boards on the local network by mDNS (_esphomelib._tcp)."""

import asyncio

SERVICE = "_esphomelib._tcp.local."


async def discover(seconds=4.0):
    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

    names = set()

    def on_change(zeroconf, service_type, name, state_change):
        if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            names.add(name)

    azc = AsyncZeroconf()
    browser = AsyncServiceBrowser(azc.zeroconf, [SERVICE], handlers=[on_change])
    try:
        await asyncio.sleep(seconds)
        found = []
        for name in sorted(names):
            info = AsyncServiceInfo(SERVICE, name)
            if not await info.async_request(azc.zeroconf, 1500):
                continue
            props = {k.decode(errors="replace"): (v.decode(errors="replace") if v else "")
                     for k, v in (info.properties or {}).items()}
            addresses = info.parsed_scoped_addresses()
            ipv4 = [a for a in addresses if ":" not in a]
            node = name[: -len(SERVICE) - 1] if name.endswith("." + SERVICE) else name.split(".")[0]
            found.append({
                "node": node,
                "friendlyName": props.get("friendly_name", "") or node,
                "address": (ipv4 or addresses or [""])[0],
                "host": f"{node}.local",
                "port": info.port or 6053,
                "mac": props.get("mac", "").upper(),
                "version": props.get("version", ""),
                "platform": props.get("platform", ""),
                "board": props.get("board", ""),
                "encrypted": "api_encryption" in props or "api_encryption_supported" in props,
            })
        return found
    finally:
        await browser.async_cancel()
        await azc.async_close()
