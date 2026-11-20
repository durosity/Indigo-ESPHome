# -*- coding: utf-8 -*-
"""
Bluetooth room presence from ESPHome boards running `bluetooth_proxy`.

Each board passes on raw BLE advertisements. A "Bluetooth Tracker" device in
Indigo describes one thing to follow:

  * mac      - a fixed-address tag (Tile, Nut, many cheap beacons)
  * irk      - an iPhone / Apple Watch / modern phone using rotating private
               addresses; resolved with its Identity Resolving Key
  * ibeacon  - anything broadcasting an iBeacon UUID (optionally major/minor)

Per board the RSSI is smoothed; the tracker's room is the board hearing it
loudest, with hysteresis so it doesn't flap between neighbouring rooms.
"""

import base64
import binascii
import time

import indigo  # noqa

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:          # cryptography ships with aioesphomeapi, but be defensive
    Cipher = None

EMA_ALPHA = 0.35          # smoothing for RSSI
FRESH_SECONDS = 20        # a board's reading counts towards "nearest room" for this long
RSSI_WRITE_STEP = 3       # only rewrite the rssi state when it moves this much...
RSSI_WRITE_EVERY = 30     # ...or this many seconds have passed


def mac_str(addr):
    return ":".join(f"{(addr >> (8 * i)) & 0xFF:02X}" for i in range(5, -1, -1))


def parse_irk(text):
    """Accept 32 hex chars (Bluetooth order) or base64 (as exported from Apple keychains).
    Returns both byte orders, because tools disagree about which way round IRKs are shown."""
    text = (text or "").strip().replace(" ", "").replace(":", "")
    raw = None
    if len(text) == 32:
        try:
            raw = binascii.unhexlify(text)
        except binascii.Error:
            raw = None
    if raw is None:
        try:
            raw = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            raw = None
    if raw is None or len(raw) != 16:
        raise ValueError("an IRK is 32 hex characters or a 24-character base64 value")
    return [raw, raw[::-1]]


def rpa_hash(irk, prand):
    block = b"\x00" * 13 + prand.to_bytes(3, "big")
    enc = Cipher(algorithms.AES(irk), modes.ECB()).encryptor()
    out = enc.update(block) + enc.finalize()
    return int.from_bytes(out[-3:], "big")


def is_rpa(addr):
    return (addr >> 46) & 0b11 == 0b01


def ibeacon_of(data):
    """Return (uuid, major, minor) if the advertisement carries an iBeacon frame."""
    i = 0
    n = len(data)
    while i < n:
        length = data[i]
        if length == 0 or i + length >= n:
            break
        ad_type = data[i + 1] if i + 1 < n else None
        payload = data[i + 2:i + 1 + length]
        if ad_type == 0xFF and len(payload) >= 25 and payload[0:4] == b"\x4c\x00\x02\x15":
            uuid = payload[4:20].hex()
            uuid = f"{uuid[0:8]}-{uuid[8:12]}-{uuid[12:16]}-{uuid[16:20]}-{uuid[20:32]}"
            major = int.from_bytes(payload[20:22], "big")
            minor = int.from_bytes(payload[22:24], "big")
            return uuid, major, minor
        i += 1 + length
    return None


class Tracker:
    def __init__(self, dev):
        props = dev.pluginProps
        self.dev_id = dev.id
        self.name = dev.name
        self.kind = props.get("idType", "mac")
        ident = (props.get("identifier", "") or "").strip()
        self.mac = ident.upper().replace("-", ":") if self.kind == "mac" else ""
        self.irks = parse_irk(ident) if self.kind == "irk" else []
        self.uuid = ident.lower() if self.kind == "ibeacon" else ""
        self.major = _opt_int(props.get("major"))
        self.minor = _opt_int(props.get("minor"))
        self.away_after = max(15, _opt_int(props.get("awayTimeout")) or 180)
        self.switch_db = max(0, _opt_int(props.get("switchMargin")) or 5)
        self.readings = {}      # board id -> [ema_rssi, last_seen, room]
        self.room = "" if dev.states.get("room", "") in ("", "away") else dev.states.get("room")
        self.present = bool(dev.states.get("onOffState", False))
        self.last_rssi_written = None
        self.last_rssi_time = 0.0
        self.last_seen = 0.0


def _opt_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


class BLEPresence:
    def __init__(self, plugin):
        self.plugin = plugin
        self.logger = plugin.logger
        self.trackers = {}          # dev id -> Tracker
        self.by_mac = {}
        self.rpa_cache = {}         # address -> tracker dev id or None
        self.adverts_seen = 0

    # ---- tracker registry (called from Indigo's thread; mutations hop to the loop) ----
    def wanted(self):
        return bool(self.trackers)

    def add(self, dev):
        try:
            tracker = Tracker(dev)
        except ValueError as err:
            self.logger.error(f'"{dev.name}": {err}')
            dev.setErrorStateOnServer("bad identifier")
            return
        if tracker.irks and Cipher is None:
            self.logger.error(f'"{dev.name}": IRK tracking needs the cryptography package')
            return

        def _apply():
            self.trackers[dev.id] = tracker
            self._rebuild()
            for board in self.plugin.boards.values():
                board.ensure_ble()
        self.plugin.loop.call_soon_threadsafe(_apply)

    def remove(self, dev_id):
        def _apply():
            self.trackers.pop(dev_id, None)
            self._rebuild()
        self.plugin.loop.call_soon_threadsafe(_apply)

    def _rebuild(self):
        self.by_mac = {t.mac: t.dev_id for t in self.trackers.values() if t.mac}
        self.rpa_cache.clear()

    def forget_scanner(self, board):
        for t in self.trackers.values():
            t.readings.pop(board.dev_id, None)

    # ---- matching (loop thread) ----
    def _match(self, addr, data):
        mac = mac_str(addr)
        if mac in self.by_mac:
            return self.by_mac[mac]
        if is_rpa(addr):
            if addr in self.rpa_cache:
                return self.rpa_cache[addr]
            prand, hsh = addr >> 24, addr & 0xFFFFFF
            found = None
            for t in self.trackers.values():
                if any(rpa_hash(k, prand) == hsh for k in t.irks):
                    found = t.dev_id
                    break
            if len(self.rpa_cache) > 4000:
                self.rpa_cache.clear()
            self.rpa_cache[addr] = found
            if found is not None:
                return found
        if data and any(t.uuid for t in self.trackers.values()):
            beacon = ibeacon_of(bytes(data))
            if beacon:
                uuid, major, minor = beacon
                for t in self.trackers.values():
                    if t.uuid == uuid and (t.major is None or t.major == major) and \
                            (t.minor is None or t.minor == minor):
                        return t.dev_id
        return None

    def ingest(self, board, adverts):
        if not self.trackers:
            return
        now = time.monotonic()
        touched = set()
        for adv in adverts:
            self.adverts_seen += 1
            dev_id = self._match(adv.address, adv.data)
            if dev_id is None:
                continue
            t = self.trackers[dev_id]
            reading = t.readings.get(board.dev_id)
            if reading is None:
                t.readings[board.dev_id] = [float(adv.rssi), now, board.ble_room]
            else:
                reading[0] = EMA_ALPHA * adv.rssi + (1 - EMA_ALPHA) * reading[0]
                reading[1] = now
                reading[2] = board.ble_room
            t.last_seen = now
            touched.add(dev_id)
        for dev_id in touched:
            self._evaluate(self.trackers[dev_id], now)

    def tick(self):
        """Periodic: mark trackers away once they've not been heard for their timeout."""
        now = time.monotonic()
        for t in self.trackers.values():
            if t.present and now - t.last_seen > t.away_after:
                self._evaluate(t, now)

    def _evaluate(self, t, now):
        fresh = {bid: r for bid, r in t.readings.items() if now - r[1] <= FRESH_SECONDS}
        present = now - t.last_seen <= t.away_after if t.last_seen else False
        room = t.room
        rssi = None
        if fresh:
            best_id = max(fresh, key=lambda b: fresh[b][0])
            best_rssi, _, best_room = fresh[best_id]
            current = next((r for r in fresh.values() if r[2] == t.room), None)
            if current is None or best_rssi - current[0] >= t.switch_db:
                room = best_room
                rssi = best_rssi
            else:
                rssi = current[0]
        elif not present:
            room = ""
        states = []
        if room != t.room:
            states.append({"key": "room", "value": room or "away"})
            if room:
                states.append({"key": "previousRoom", "value": t.room or "away"})
            t.room = room
        if present != t.present:
            states.append({"key": "onOffState", "value": present})
            t.present = present
        if rssi is not None and (t.last_rssi_written is None or abs(rssi - t.last_rssi_written) >= RSSI_WRITE_STEP
                                 or now - t.last_rssi_time >= RSSI_WRITE_EVERY):
            states.append({"key": "rssi", "value": round(rssi)})
            heard = ", ".join(f"{r[2]} {round(r[0])}" for r in sorted(fresh.values(), key=lambda r: -r[0]))
            states.append({"key": "heardBy", "value": heard})
            states.append({"key": "lastSeen", "value": time.strftime("%Y-%m-%d %H:%M:%S")})
            t.last_rssi_written = rssi
            t.last_rssi_time = now
        if not states:
            return
        try:
            dev = indigo.devices[t.dev_id]
        except KeyError:
            return
        dev.updateStatesOnServer(states)
        if any(s["key"] in ("room", "onOffState") for s in states):
            self.logger.info(f'"{t.name}": {"in " + t.room if t.present and t.room else "away"}')
