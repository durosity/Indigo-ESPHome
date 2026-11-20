# -*- coding: utf-8 -*-
"""
One ESPHome board (an "ESPHome Board" core device in Indigo).

Owns the aioesphomeapi client and ReconnectLogic, keeps the board's entities
in sync with Indigo child devices, and translates pushed entity states into
Indigo state updates. Everything in here runs on the plugin's asyncio loop
thread except send(), which is called from Indigo's thread and hops onto the
loop safely.
"""

import asyncio
import inspect
import json
import math
import re
import time
from collections import deque
from datetime import datetime

import indigo  # noqa  (provided by the Indigo plugin host)

import aioesphomeapi
from aioesphomeapi import (
    APIClient,
    BluetoothProxyFeature,
    ColorMode,
    InvalidAuthAPIError,
    InvalidEncryptionKeyAPIError,
    LogLevel,
    ReconnectLogic,
    RequiresEncryptionAPIError,
)

# Real ESC sequences, plus the literal "\033[" / "\u001b[" text some build output carries
ANSI_RE = re.compile(r"(?:\x1b|\\033|\\x1b|\\u001b)\[[0-9;]*[A-Za-z]")

# aioesphomeapi class name -> (kind, Indigo deviceTypeId, label)
ENTITY_SPECS = {
    "SensorInfo": ("sensor", "espSensor", "Sensor"),
    "BinarySensorInfo": ("binary_sensor", "espBinarySensor", "Binary Sensor"),
    "TextSensorInfo": ("text_sensor", "espTextSensor", "Text Sensor"),
    "SwitchInfo": ("switch", "espSwitch", "Switch"),
    "ButtonInfo": ("button", "espButton", "Button"),
    "LightInfo": ("light", "espLight", "Light"),
    "CoverInfo": ("cover", "espCover", "Cover"),
    "ValveInfo": ("valve", "espValve", "Valve"),
    "FanInfo": ("fan", "espFan", "Fan"),
    "LockInfo": ("lock", "espLock", "Lock"),
    "NumberInfo": ("number", "espNumber", "Number"),
    "SelectInfo": ("select", "espSelect", "Select"),
    "TextInfo": ("text", "espText", "Text"),
    "ClimateInfo": ("climate", "espClimate", "Climate"),
    "EventInfo": ("event", "espEvent", "Event"),
}
CHILD_TYPE_IDS = {spec[1] for spec in ENTITY_SPECS.values()}

# LightColorCapability bit flags
CAP_BRIGHTNESS, CAP_WHITE, CAP_CT, CAP_CWWW, CAP_RGB = 2, 4, 8, 16, 32

LOG_LEVELS = {
    "error": LogLevel.LOG_LEVEL_ERROR,
    "warn": LogLevel.LOG_LEVEL_WARN,
    "info": LogLevel.LOG_LEVEL_INFO,
    "debug": LogLevel.LOG_LEVEL_DEBUG,
}

LOCK_STATE_TEXT = {0: "none", 1: "locked", 2: "unlocked", 3: "jammed",
                   4: "locking", 5: "unlocking", 6: "opening", 7: "open"}
OPERATION_TEXT = {0: "idle", 1: "opening", 2: "closing"}


def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def enum_name(value):
    if value is None:
        return ""
    name = getattr(value, "name", None)
    return name.lower() if name else str(value)


def bad_float(value):
    return value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value)))


def pct(value):
    """0.0-1.0 -> 0-100 int."""
    try:
        return max(0, min(100, int(round(float(value) * 100))))
    except (TypeError, ValueError):
        return 0


def step_decimals(step):
    text = f"{float(step or 1):g}"
    return len(text.split(".")[1]) if "." in text and "e" not in text else 0


def light_caps(entity, api_version):
    try:
        modes = entity.supported_color_modes_compat(api_version)
    except Exception:
        modes = getattr(entity, "supported_color_modes", None) or []
    caps = 0
    for mode in modes:
        caps |= int(mode)
    return caps


def image(name):
    """indigo.kStateImageSel member by name, or None if this Indigo lacks it."""
    return getattr(indigo.kStateImageSel, name, None)


class ESPBoard:
    def __init__(self, plugin, dev):
        self.plugin = plugin
        self.logger = plugin.logger
        self.dev_id = dev.id
        self.name = dev.name
        props = dev.pluginProps
        self.host = props.get("address", "").strip()
        try:
            self.port = int(props.get("port") or 6053)
        except ValueError:
            self.port = 6053
        self.password = props.get("password", "") or ""
        self.noise_psk = (props.get("encryptionKey", "") or "").strip() or None
        self.forward_logs = props.get("forwardLogs", "off")
        self.config_override = (props.get("dashboardConfiguration", "") or "").strip()
        self.known_node_name = dev.states.get("nodeName", "") or ""
        try:
            self.grace_seconds = max(0, int(props.get("graceSeconds", "30") or 0))
        except ValueError:
            self.grace_seconds = 30
        self.share_states = bool(props.get("shareStates", False))
        self.allow_control = bool(props.get("allowControl", False))
        self.ble_room = (props.get("bleRoom", "") or "").strip() or dev.name

        self.client = None
        self.reconnect = None
        self.connected = False          # API link is up right now
        self.reported_online = False    # what Indigo has been told (lags drops by the grace period)
        self.device_info = None
        self.api_version = None
        self._unsub_logs = None
        self._log_level_subscribed = None
        self._unsub_ble = None
        self._last_error = None
        self._offline_handle = None
        self._stopping = False
        self._dropped_at = None
        self._drop_times = deque(maxlen=200)
        self.disconnect_count = int(dev.states.get("disconnectCount", 0) or 0)

        self.live_log = deque(maxlen=800)   # (seq, time, level, text) for the setup page
        self.live_log_seq = 0
        self.live_until = 0.0

        self.ha_subscriptions = set()   # (entity_id, attribute) the board asked Indigo for

        self.services = {}          # service name -> UserService
        self.key_map = {}           # (device_id, key) -> indigo dev id
        self.dev_cache = {}         # indigo dev id -> device object (for state writes)
        self.dev_kind = {}          # indigo dev id -> kind
        self.entity_info = {}       # indigo dev id -> EntityInfo
        self.orphans = set()        # indigo dev ids no longer in firmware
        self.errored = set()        # indigo dev ids currently showing an error
        self.force_create = False   # next sync creates devices even if auto-create is off

        self.db_config = ""         # Device Builder configuration filename once matched
        self.update_available = False

    # ------------------------------------------------------------------ #
    # Lifecycle (loop thread)
    # ------------------------------------------------------------------ #
    @property
    def node_name(self):
        if self.device_info is not None:
            return self.device_info.name
        if self.known_node_name:
            return self.known_node_name
        if self.host.endswith(".local"):
            return self.host[:-6]
        return ""

    async def start(self):
        self.client = APIClient(
            self.host,
            self.port,
            self.password,
            client_info="Indigo ESPHome plugin",
            noise_psk=self.noise_psk,
        )
        self.reconnect = ReconnectLogic(
            client=self.client,
            on_connect=self._on_connect,
            on_disconnect=self._on_disconnect,
            on_connect_error=self._on_connect_error,
            name=self.node_name or None,
        )
        self._core_states([{"key": "status", "value": "Connecting"}], image("PowerOff"))
        await self.reconnect.start()

    async def stop(self):
        self._stopping = True
        self.connected = False
        self._cancel_offline_timer()
        try:
            if self.reconnect is not None:
                await self.reconnect.stop()
            if self.client is not None:
                await self.client.disconnect(force=True)
        except Exception:
            self.logger.debug(f"{self.name}: error during disconnect", exc_info=True)
        self.reported_online = False
        self.mark_children_offline("board stopped")

    async def _on_connect(self):
        try:
            self.device_info = await self.client.device_info()
            entities, services = await self.client.list_entities_services()
            self.api_version = self.client.api_version
            self.connected = True
            self._last_error = None
            brief = self._dropped_at is not None and self._offline_handle is not None
            outage = time.monotonic() - self._dropped_at if self._dropped_at else 0
            self._cancel_offline_timer()
            self._update_core_info(len(entities))
            self.sync_entities(entities, services)
            self.client.subscribe_states(self._on_state)
            self._log_level_subscribed = None
            self.update_log_subscription()
            if self.share_states:
                self.client.subscribe_home_assistant_states(self._on_ha_state_sub, self._on_ha_state_request)
            if self.allow_control:
                self.client.subscribe_service_calls(self._on_service_call)
            self._start_ble()
            self.reported_online = True
            if brief:
                self.logger.info(f"{self.name}: reconnected after a {outage:.0f}s drop")
            else:
                self.logger.info(
                    f"{self.name}: connected ({self.device_info.name}, ESPHome "
                    f"{self.device_info.esphome_version}, {len(self.key_map)} entities)"
                )
            self._dropped_at = None
            self.plugin.apply_dashboard_to_board(self)
        except Exception:
            self.logger.exception(f"{self.name}: error while setting up connection")
            raise

    async def _on_disconnect(self, expected_disconnect):
        self.connected = False
        self._unsub_logs = None
        self._unsub_ble = None
        self.ha_subscriptions.clear()
        self.plugin.ble.forget_scanner(self)
        now = time.monotonic()
        self._dropped_at = now
        self._drop_times.append(now)
        self.disconnect_count += 1
        self._core_states([{"key": "disconnectCount", "value": self.disconnect_count},
                           {"key": "dropsLast24h", "value": self._drops_24h()},
                           {"key": "lastDisconnect", "value": ts()},
                           {"key": "status", "value": "Reconnecting"}])
        # Only disconnects the plugin started itself skip the grace period. A board closing the
        # link cleanly (e.g. rebooting after an OTA install) is "expected" to the library, but is
        # exactly the kind of short gap the grace period exists for.
        if self._stopping or self.grace_seconds == 0 or not self.reported_online:
            self._go_offline(self._stopping)
        else:
            self.logger.debug(f"{self.name}: link dropped, waiting {self.grace_seconds}s before reporting offline")
            self._offline_handle = asyncio.get_running_loop().call_later(
                self.grace_seconds, self._go_offline, False)

    def _go_offline(self, expected):
        self._offline_handle = None
        if self.connected:
            return
        self.reported_online = False
        (self.logger.debug if expected else self.logger.warning)(
            f"{self.name}: disconnected" + ("" if expected else
                                           f" (no reconnection within {self.grace_seconds}s)" if self.grace_seconds else ""))
        self._core_states(
            [{"key": "connected", "value": False}, {"key": "status", "value": "Disconnected"}],
            image("PowerOff"),
        )
        self.mark_children_offline("board offline")
        self.plugin.fire_board_triggers(self, "offline")

    def _cancel_offline_timer(self):
        if self._offline_handle is not None:
            self._offline_handle.cancel()
            self._offline_handle = None

    def _drops_24h(self):
        cutoff = time.monotonic() - 86400
        return sum(1 for t in self._drop_times if t >= cutoff)

    async def _on_connect_error(self, err):
        if isinstance(err, RequiresEncryptionAPIError):
            msg = "board requires an API encryption key"
        elif isinstance(err, InvalidEncryptionKeyAPIError):
            msg = "API encryption key is wrong"
        elif isinstance(err, InvalidAuthAPIError):
            msg = "API password is wrong"
        else:
            msg = str(err) or err.__class__.__name__
        if msg != self._last_error:
            # Only log each distinct failure once, so an unplugged board doesn't flood the log
            fatal = isinstance(err, (RequiresEncryptionAPIError, InvalidEncryptionKeyAPIError, InvalidAuthAPIError))
            (self.logger.error if fatal else self.logger.debug if self._offline_handle else self.logger.warning)(
                f"{self.name}: cannot connect - {msg}")
            self._last_error = msg
        if self._offline_handle is None:   # don't show an error while still inside the grace period
            self._core_states([{"key": "status", "value": "Error" if "key" in msg or "password" in msg else "Unreachable"}],
                              image("SensorTripped"))

    def ota_target(self):
        return getattr(self.client, "connected_address", None) or self.host

    # ------------------------------------------------------------------ #
    # Core device states
    # ------------------------------------------------------------------ #
    def _core_dev(self):
        try:
            return indigo.devices[self.dev_id]
        except KeyError:
            return None

    def _core_states(self, states, img=None):
        dev = self._core_dev()
        if dev is None:
            return
        dev.updateStatesOnServer(states)
        if img is not None:
            dev.updateStateImageOnServer(img)

    def _update_core_info(self, entity_count):
        di = self.device_info
        api = self.api_version
        self.known_node_name = di.name
        states = [
            {"key": "connected", "value": True},
            {"key": "status", "value": "Connected"},
            {"key": "nodeName", "value": di.name or ""},
            {"key": "friendlyName", "value": di.friendly_name or ""},
            {"key": "boardModel", "value": di.model or ""},
            {"key": "boardMaker", "value": di.manufacturer or ""},
            {"key": "macAddress", "value": di.mac_address or ""},
            {"key": "firmwareVersion", "value": di.esphome_version or ""},
            {"key": "compilationTime", "value": di.compilation_time or ""},
            {"key": "projectName", "value": di.project_name or ""},
            {"key": "projectVersion", "value": di.project_version or ""},
            {"key": "apiVersion", "value": f"{api.major}.{api.minor}" if api else ""},
            {"key": "connectedAddress", "value": self.ota_target() or ""},
            {"key": "entityCount", "value": entity_count},
            {"key": "lastConnected", "value": ts()},
            {"key": "dropsLast24h", "value": self._drops_24h()},
            {"key": "bluetoothProxy", "value": self.ble_capable()},
        ]
        if not self.reported_online:
            states.append({"key": "connectedSince", "value": ts()})
        self._core_states(states, image("PowerOn"))

    def apply_dashboard(self, db_device):
        """Update Device Builder states from a devices/list entry (or None if unmatched)."""
        dev = self._core_dev()
        if dev is None:
            return
        fw = (self.device_info.esphome_version if self.device_info else dev.states.get("firmwareVersion", "")) or ""
        if db_device is None:
            self.db_config = ""
            self.update_available = False
            new = {"dbConfiguration": "", "dbVersion": "", "updateAvailable": False, "pendingChanges": False}
        else:
            self.db_config = db_device.get("configuration", "")
            current = db_device.get("current_version", "") or ""
            self.update_available = bool(db_device.get("update_available")) or bool(fw and current and fw != current)
            new = {
                "dbConfiguration": self.db_config,
                "dbVersion": current,
                "updateAvailable": self.update_available,
                "pendingChanges": bool(db_device.get("has_pending_changes")),
            }
        changed = [{"key": k, "value": v} for k, v in new.items() if dev.states.get(k) != v]
        if changed:
            dev.updateStatesOnServer(changed)

    def set_build_state(self, status, result=None):
        states = [{"key": "buildStatus", "value": status}]
        if result is not None:
            states += [{"key": "lastBuildResult", "value": result}, {"key": "lastBuildTime", "value": ts()}]
        self._core_states(states)

    # ------------------------------------------------------------------ #
    # Entity sync (loop thread)
    # ------------------------------------------------------------------ #
    def _existing_children(self):
        found = {}
        for dev in indigo.devices.iter("self"):
            if dev.deviceTypeId in CHILD_TYPE_IDS and dev.pluginProps.get("coreId") == str(self.dev_id):
                found[dev.pluginProps.get("entityUid", "")] = dev
        return found

    async def resync(self, force_create=False):
        if not self.connected:
            self.logger.warning(f"{self.name}: not connected, cannot sync entities")
            return
        self.force_create = force_create
        entities, services = await self.client.list_entities_services()
        self.sync_entities(entities, services)

    def sync_entities(self, entities, services):
        self.services = {s.name: s for s in services}
        sub_names = {d.device_id: d.name for d in (getattr(self.device_info, "devices", None) or [])}
        existing = self._existing_children()
        auto_create = self.plugin.pluginPrefs.get("autoCreateEntities", True) or self.force_create
        include_disabled = self.plugin.pluginPrefs.get("includeDisabledEntities", False)

        self.key_map.clear()
        self.dev_cache.clear()
        self.dev_kind.clear()
        self.entity_info.clear()
        created = 0
        skipped = 0

        for entity in entities:
            spec = ENTITY_SPECS.get(type(entity).__name__)
            if spec is None:
                self.logger.debug(f"{self.name}: unsupported entity type {type(entity).__name__} ({entity.name})")
                continue
            kind, type_id, label = spec
            did = getattr(entity, "device_id", 0) or 0
            uid = f"{kind}:{did}:{entity.key}"
            dev = existing.pop(uid, None)
            if dev is None:
                if entity.disabled_by_default and not include_disabled:
                    continue
                if not auto_create:
                    skipped += 1
                    continue
                dev = self._create_child(entity, kind, type_id, label, uid, sub_names)
                if dev is None:
                    continue
                created += 1
            else:
                dev = self._refresh_child_props(dev, kind, entity)
            self.key_map[(did, entity.key)] = dev.id
            self.dev_cache[dev.id] = dev
            self.dev_kind[dev.id] = kind
            self.entity_info[dev.id] = entity
            self.orphans.discard(dev.id)
            self._apply_static_states(dev, kind, entity)

        for dev in existing.values():
            if dev.id not in self.orphans:
                self.logger.warning(f'"{dev.name}": entity no longer exists in {self.name} firmware')
                self.orphans.add(dev.id)
            dev.updateStateOnServer("online", False)
            dev.setErrorStateOnServer("removed")

        self.force_create = False
        if created:
            self.logger.info(f"{self.name}: created {created} new Indigo device(s)")
        if skipped:
            self.logger.info(f"{self.name}: {skipped} new entities not created (auto-create is off - use Sync Entities)")
        self._core_states([{"key": "orphanCount", "value": len(self.orphans)},
                           {"key": "serviceCount", "value": len(self.services)}])

    def _create_child(self, entity, kind, type_id, label, uid, sub_names):
        di = self.device_info
        base = entity.name or di.friendly_name or di.name
        sub = sub_names.get(getattr(entity, "device_id", 0) or 0)
        if sub and not base.startswith(sub):
            base = f"{sub} {base}"
        name = self.plugin.unique_device_name(f"{self.name} - {base}")
        props = {
            "coreId": str(self.dev_id),
            "entityUid": uid,
            "entityKey": str(entity.key),
            "entityDeviceId": str(getattr(entity, "device_id", 0) or 0),
            "address": getattr(entity, "object_id", "") or str(entity.key),
        }
        props.update(self._entity_props(kind, entity))
        try:
            dev = indigo.device.create(
                indigo.kProtocol.Plugin,
                address=props["address"],
                name=name,
                description=f"ESPHome {label} on {di.name}",
                deviceTypeId=type_id,
                props=props,
                folder=self.plugin.folder_for_board(self),
            )
            self.logger.info(f'Created "{dev.name}" ({label})')
            return dev
        except Exception:
            self.logger.exception(f"{self.name}: failed to create device for {entity.name}")
            return None

    def _refresh_child_props(self, dev, kind, entity):
        props = dev.pluginProps
        wanted = self._entity_props(kind, entity)
        wanted["entityKey"] = str(entity.key)
        if any(props.get(k) != v for k, v in wanted.items()):
            for k, v in wanted.items():
                props[k] = v
            dev.replacePluginPropsOnServer(props)
            dev = indigo.devices[dev.id]
        return dev

    def _entity_props(self, kind, e):
        """Type-specific pluginProps, recomputed on every sync so firmware changes flow through."""
        p = {}
        if kind == "sensor":
            p.update(SupportsSensorValue=True, SupportsOnState=False, SupportsStatusRequest=False,
                     unit=e.unit_of_measurement or "", decimals=str(max(0, min(6, e.accuracy_decimals or 0))),
                     deviceClass=e.device_class or "")
        elif kind == "binary_sensor":
            p.update(SupportsSensorValue=False, SupportsOnState=True, SupportsStatusRequest=False,
                     deviceClass=e.device_class or "")
        elif kind in ("switch", "button"):
            p.update(SupportsOnState=True, SupportsStatusRequest=False)
        elif kind == "light":
            caps = light_caps(e, self.api_version)
            rgb, white, ct = bool(caps & CAP_RGB), bool(caps & CAP_WHITE), bool(caps & (CAP_CT | CAP_CWWW))
            p.update(SupportsStatusRequest=False, SupportsColor=rgb or white or ct, SupportsRGB=rgb,
                     SupportsWhite=white, SupportsWhiteTemperature=ct, colorCaps=str(caps),
                     effectsJson=json.dumps([x for x in (e.effects or []) if x and x.lower() != "none"]))
            if ct:
                p["WhiteTemperatureMin"] = str(int(1e6 / e.max_mireds)) if e.max_mireds else "2000"
                p["WhiteTemperatureMax"] = str(int(1e6 / e.min_mireds)) if e.min_mireds else "6500"
        elif kind == "cover":
            p.update(SupportsStatusRequest=False, supportsPosition=bool(e.supports_position),
                     supportsTilt=bool(e.supports_tilt), supportsStop=bool(e.supports_stop))
        elif kind == "valve":
            p.update(SupportsStatusRequest=False, supportsPosition=bool(e.supports_position),
                     supportsStop=bool(e.supports_stop))
        elif kind == "fan":
            p.update(SupportsStatusRequest=False,
                     speedCount=str(e.supported_speed_count if e.supports_speed else 0),
                     supportsOscillation=bool(e.supports_oscillation), supportsDirection=bool(e.supports_direction),
                     presetsJson=json.dumps(list(e.supported_preset_modes or [])))
        elif kind == "lock":
            p.update(SupportsOnState=True, IsLockSubType=True, SupportsStatusRequest=False,
                     supportsOpen=bool(e.supports_open), requiresCode=bool(e.requires_code))
        elif kind == "number":
            p.update(unit=e.unit_of_measurement or "", minValue=str(e.min_value), maxValue=str(e.max_value),
                     step=str(e.step), decimals=str(step_decimals(e.step)))
        elif kind == "select":
            p.update(optionsJson=json.dumps(list(e.options or [])))
        elif kind == "climate":
            modes = [enum_name(m) for m in (e.supported_modes or [])]
            two_point = bool(e.supports_two_point_target_temperature)
            p.update(NumTemperatureInputs="1" if e.supports_current_temperature else "0",
                     NumHumidityInputs="1" if e.supports_current_humidity else "0",
                     SupportsHeatSetpoint=any(m in modes for m in ("heat", "heat_cool", "auto")),
                     SupportsCoolSetpoint=any(m in modes for m in ("cool", "heat_cool", "auto")),
                     SupportsHvacOperationMode=True,
                     SupportsHvacFanMode=bool(e.supported_fan_modes),
                     ShowCoolHeatEquipmentStateUI=bool(e.supports_action),
                     SupportsStatusRequest=False,
                     supportedModes=",".join(modes), twoPoint=two_point,
                     hasSwing=len(e.supported_swing_modes or []) > 1,
                     presetsJson=json.dumps([enum_name(x) for x in (e.supported_presets or [])] +
                                            list(e.supported_custom_presets or [])),
                     fanModesJson=json.dumps([enum_name(x) for x in (e.supported_fan_modes or [])] +
                                             list(e.supported_custom_fan_modes or [])))
        elif kind == "event":
            p.update(eventTypesJson=json.dumps(list(e.event_types or [])))
        return p

    def _apply_static_states(self, dev, kind, e):
        states = [{"key": "online", "value": True}]
        if kind == "sensor":
            states.append({"key": "unit", "value": e.unit_of_measurement or ""})
        elif kind == "number":
            states += [{"key": "minValue", "value": e.min_value}, {"key": "maxValue", "value": e.max_value},
                       {"key": "step", "value": e.step}, {"key": "unit", "value": e.unit_of_measurement or ""}]
        elif kind == "select":
            states.append({"key": "options", "value": ", ".join(e.options or [])})
        dev.updateStatesOnServer(states)
        if getattr(dev, "errorState", "") or dev.id in self.errored:
            dev.setErrorStateOnServer(None)
            self.errored.discard(dev.id)
        if kind == "sensor":
            img = {"temperature": "TemperatureSensor", "humidity": "HumiditySensor",
                   "illuminance": "LightSensor", "power": "EnergyMeterOn",
                   "energy": "EnergyMeterOn"}.get(e.device_class or "")
            if img and image(img) is not None:
                dev.updateStateImageOnServer(image(img))

    def mark_children_offline(self, reason):
        for dev_id, dev in list(self.dev_cache.items()):
            if dev_id in self.orphans:
                continue
            try:
                dev.updateStateOnServer("online", False)
                dev.setErrorStateOnServer(reason)
                self.errored.add(dev_id)
            except Exception:
                pass

    def forget_device(self, dev_id):
        for k, v in list(self.key_map.items()):
            if v == dev_id:
                del self.key_map[k]
        for store in (self.dev_cache, self.dev_kind, self.entity_info):
            store.pop(dev_id, None)
        self.orphans.discard(dev_id)
        self.errored.discard(dev_id)

    # ------------------------------------------------------------------ #
    # State updates (loop thread)
    # ------------------------------------------------------------------ #
    def _err(self, dev, msg):
        if dev.id not in self.errored:
            dev.setErrorStateOnServer(msg)
            self.errored.add(dev.id)

    def _clear_error(self, dev):
        if dev.id in self.errored:
            dev.setErrorStateOnServer(None)
            self.errored.discard(dev.id)

    def _write(self, dev, states):
        states.append({"key": "lastUpdate", "value": ts()})
        dev.updateStatesOnServer(states)
        self._clear_error(dev)

    def _on_state(self, state):
        dev_id = self.key_map.get((getattr(state, "device_id", 0) or 0, state.key))
        if dev_id is None:
            return
        dev = self.dev_cache.get(dev_id)
        kind = self.dev_kind.get(dev_id)
        handler = getattr(self, f"_st_{kind}", None)
        if dev is None or handler is None:
            return
        try:
            handler(dev, state)
        except Exception:
            self.logger.exception(f'"{dev.name}": error applying state {state}')

    def _st_sensor(self, dev, s):
        if s.missing_state or bad_float(s.state):
            self._err(dev, "unavailable")
            return
        dec = int(dev.pluginProps.get("decimals", "1"))
        unit = dev.pluginProps.get("unit", "")
        value = round(float(s.state), dec)
        ui = f"{value:.{dec}f}" + (f" {unit}" if unit else "")
        self._write(dev, [{"key": "sensorValue", "value": value, "uiValue": ui, "decimalPlaces": dec}])

    def _st_binary_sensor(self, dev, s):
        if s.missing_state:
            self._err(dev, "unavailable")
            return
        on = bool(s.state)
        self._write(dev, [{"key": "onOffState", "value": on}])
        cls = dev.pluginProps.get("deviceClass", "")
        img = None
        if cls in ("motion", "occupancy", "presence"):
            img = "MotionSensorTripped" if on else "MotionSensor"
        elif cls in ("door", "garage_door", "opening"):
            img = "DoorSensorOpened" if on else "DoorSensorClosed"
        elif cls == "window":
            img = "WindowSensorOpened" if on else "WindowSensorClosed"
        if img and image(img) is not None:
            dev.updateStateImageOnServer(image(img))

    def _st_text_sensor(self, dev, s):
        if s.missing_state:
            self._err(dev, "unavailable")
            return
        self._write(dev, [{"key": "value", "value": str(s.state)}])

    def _st_switch(self, dev, s):
        self._write(dev, [{"key": "onOffState", "value": bool(s.state)}])

    def _st_light(self, dev, s):
        props = dev.pluginProps
        on = bool(s.state)
        if not on:
            level = 0
        elif int(props.get("colorCaps", "0")) & CAP_BRIGHTNESS:
            level = max(1, pct(s.brightness))
        else:
            level = 100
        try:
            mode_text = ColorMode(int(s.color_mode)).name.lower()
        except (ValueError, TypeError):
            mode_text = str(s.color_mode)
        effect = s.effect if s.effect and s.effect.lower() != "none" else ""
        states = [{"key": "brightnessLevel", "value": level},
                  {"key": "colorMode", "value": mode_text},
                  {"key": "effect", "value": effect}]
        if props.get("SupportsRGB"):
            states += [{"key": "redLevel", "value": pct(s.red)},
                       {"key": "greenLevel", "value": pct(s.green)},
                       {"key": "blueLevel", "value": pct(s.blue)}]
        if props.get("SupportsWhite"):
            states.append({"key": "whiteLevel", "value": pct(s.white)})
        if props.get("SupportsWhiteTemperature") and s.color_temperature:
            states.append({"key": "whiteTemperature", "value": int(round(1e6 / s.color_temperature))})
        self._write(dev, states)

    def _st_cover(self, dev, s):
        states = [{"key": "brightnessLevel", "value": pct(s.position)},
                  {"key": "operation", "value": OPERATION_TEXT.get(int(s.current_operation), "unknown")}]
        if dev.pluginProps.get("supportsTilt"):
            states.append({"key": "tiltLevel", "value": pct(s.tilt)})
        self._write(dev, states)

    def _st_valve(self, dev, s):
        self._write(dev, [{"key": "brightnessLevel", "value": pct(s.position)},
                          {"key": "operation", "value": OPERATION_TEXT.get(int(s.current_operation), "unknown")}])

    def _st_fan(self, dev, s):
        count = int(dev.pluginProps.get("speedCount", "0") or 0)
        on = bool(s.state)
        if not on:
            level = 0
        elif count:
            level = max(1, int(round((s.speed_level or count) / count * 100)))
        else:
            level = 100
        index = 0 if level == 0 else (1 if level <= 33 else 2 if level <= 66 else 3)
        self._write(dev, [{"key": "speedLevel", "value": level}, {"key": "speedIndex", "value": index},
                          {"key": "oscillating", "value": bool(s.oscillating)},
                          {"key": "direction", "value": enum_name(s.direction)},
                          {"key": "preset", "value": s.preset_mode or ""}])

    def _st_lock(self, dev, s):
        value = int(s.state)
        text = LOCK_STATE_TEXT.get(value, "unknown")
        self._write(dev, [{"key": "onOffState", "value": value == 1}, {"key": "lockState", "value": text}])
        if value == 3:
            self._err(dev, "jammed")

    def _st_number(self, dev, s):
        if s.missing_state or bad_float(s.state):
            self._err(dev, "unavailable")
            return
        dec = int(dev.pluginProps.get("decimals", "0"))
        unit = dev.pluginProps.get("unit", "")
        value = round(float(s.state), dec)
        ui = f"{value:.{dec}f}" + (f" {unit}" if unit else "")
        self._write(dev, [{"key": "value", "value": value, "uiValue": ui, "decimalPlaces": dec}])

    def _st_select(self, dev, s):
        if s.missing_state:
            self._err(dev, "unavailable")
            return
        self._write(dev, [{"key": "value", "value": str(s.state)}])

    _st_text = _st_select

    def _st_climate(self, dev, s):
        mode = enum_name(s.mode)
        action = enum_name(s.action)
        hvac = {"off": indigo.kHvacMode.Off, "heat": indigo.kHvacMode.Heat, "cool": indigo.kHvacMode.Cool,
                "heat_cool": indigo.kHvacMode.HeatCool, "auto": indigo.kHvacMode.HeatCool}.get(mode, indigo.kHvacMode.Off)
        props = dev.pluginProps
        states = [{"key": "hvacOperationMode", "value": hvac},
                  {"key": "mode", "value": mode}, {"key": "action", "value": action},
                  {"key": "fanModeText", "value": (s.custom_fan_mode or enum_name(s.fan_mode))
                   if props.get("SupportsHvacFanMode") else ""},
                  {"key": "swingMode", "value": enum_name(s.swing_mode) if props.get("hasSwing") else ""},
                  {"key": "preset", "value": s.custom_preset or enum_name(s.preset)},
                  {"key": "hvacHeaterIsOn", "value": action == "heating"},
                  {"key": "hvacCoolerIsOn", "value": action == "cooling"},
                  {"key": "hvacFanIsOn", "value": action == "fan"}]
        if props.get("NumTemperatureInputs") == "1" and not bad_float(s.current_temperature):
            states.append({"key": "temperatureInput1", "value": round(s.current_temperature, 1)})
        if props.get("NumHumidityInputs") == "1" and not bad_float(s.current_humidity):
            states.append({"key": "humidityInput1", "value": round(s.current_humidity)})
        if props.get("twoPoint"):
            if not bad_float(s.target_temperature_low):
                states.append({"key": "setpointHeat", "value": round(s.target_temperature_low, 1)})
            if not bad_float(s.target_temperature_high):
                states.append({"key": "setpointCool", "value": round(s.target_temperature_high, 1)})
        elif not bad_float(s.target_temperature):
            target = round(s.target_temperature, 1)
            states += [{"key": "setpointHeat", "value": target}, {"key": "setpointCool", "value": target}]
        if props.get("SupportsHvacFanMode"):
            fan_on = enum_name(s.fan_mode) == "on"
            states.append({"key": "hvacFanMode", "value": indigo.kFanMode.AlwaysOn if fan_on else indigo.kFanMode.Auto})
        self._write(dev, states)

    def _st_event(self, dev, s):
        self._write(dev, [{"key": "lastEventType", "value": s.event_type},
                          {"key": "lastEventTime", "value": ts()}])
        self.plugin.fire_event_triggers(dev, s.event_type)

    def _st_button(self, dev, s):
        pass  # buttons have no state

    # ------------------------------------------------------------------ #
    # Device logs
    # ------------------------------------------------------------------ #
    def wanted_log_level(self):
        """Most verbose level anyone currently wants: the Indigo log setting, or debug while the page watches."""
        levels = []
        if self.forward_logs in LOG_LEVELS:
            levels.append(self.forward_logs)
        if self.live_until > time.monotonic():
            levels.append("debug")
        order = ["error", "warn", "info", "debug"]
        return max(levels, key=order.index) if levels else None

    def update_log_subscription(self):
        """(Re)subscribe to board logs at the level needed. Loop thread."""
        if not self.connected or self.client is None:
            return
        wanted = self.wanted_log_level()
        if wanted == self._log_level_subscribed:
            return
        if self._unsub_logs is not None:
            try:
                self._unsub_logs()
            except Exception:
                pass
            self._unsub_logs = None
        if wanted is not None:
            self._unsub_logs = self.client.subscribe_logs(self._on_log, log_level=LOG_LEVELS[wanted])
        elif self._log_level_subscribed is not None:
            # Dropping the callback isn't enough - the board keeps sending until told LOG_LEVEL_NONE
            try:
                self.client.subscribe_logs(lambda _msg: None, log_level=LogLevel.LOG_LEVEL_NONE)()
            except Exception:
                pass
        self._log_level_subscribed = wanted

    def watch_logs(self, seconds=90):
        self.live_until = time.monotonic() + seconds
        self.update_log_subscription()

    def _on_log(self, msg):
        try:
            text = ANSI_RE.sub("", msg.message.decode("utf-8", "replace")).strip()
        except Exception:
            return
        if not text:
            return
        level = int(getattr(msg, "level", 3))
        if self.live_until > time.monotonic():
            self.live_log_seq += 1
            self.live_log.append((self.live_log_seq, datetime.now().strftime("%H:%M:%S"), level, text))
        wanted = {"error": 1, "warn": 2, "info": 3, "debug": 5}.get(self.forward_logs)
        if wanted is not None and level <= wanted:
            log = self.logger.error if level == 1 else self.logger.warning if level == 2 else self.logger.info
            log(f"[{self.name}] {text}")

    # ------------------------------------------------------------------ #
    # Indigo link: board <-> Indigo states and actions (loop thread)
    # ------------------------------------------------------------------ #
    def _on_ha_state_sub(self, entity_id, attribute):
        key = (entity_id, attribute or None)
        if key in self.ha_subscriptions:
            return
        self.ha_subscriptions.add(key)
        self.plugin.link.board_subscribed(self, entity_id, attribute or None)

    def _on_ha_state_request(self, entity_id, attribute):
        self.plugin.link.push_one(self, entity_id, attribute or None)

    def _on_service_call(self, call):
        self.plugin.link.handle_call(self, call)

    def send_ha_state(self, entity_id, attribute, value):
        if self.connected and self.client is not None:
            try:
                self.client.send_home_assistant_state(entity_id, attribute, value)
            except Exception:
                self.logger.debug(f"{self.name}: could not send {entity_id}", exc_info=True)

    # ------------------------------------------------------------------ #
    # Bluetooth presence (loop thread)
    # ------------------------------------------------------------------ #
    def ble_capable(self):
        flags = int(getattr(self.device_info, "bluetooth_proxy_feature_flags", 0) or 0) if self.device_info else 0
        return bool(flags & int(BluetoothProxyFeature.RAW_ADVERTISEMENTS))

    def _start_ble(self):
        if not self.ble_capable() or not self.plugin.ble.wanted():
            return
        try:
            self._unsub_ble = self.client.subscribe_bluetooth_le_raw_advertisements(self._on_ble)
            self.logger.debug(f"{self.name}: listening for Bluetooth advertisements (room: {self.ble_room})")
        except Exception:
            self.logger.debug(f"{self.name}: Bluetooth subscription failed", exc_info=True)

    def ensure_ble(self):
        if self.connected and self._unsub_ble is None:
            self._start_ble()

    def _on_ble(self, response):
        try:
            self.plugin.ble.ingest(self, response.advertisements)
        except Exception:
            self.logger.debug(f"{self.name}: error handling Bluetooth data", exc_info=True)

    # ------------------------------------------------------------------ #
    # Commands (called from Indigo's thread)
    # ------------------------------------------------------------------ #
    def send(self, method, dev, *args, **kwargs):
        if not self.connected or self.client is None:
            self.logger.warning(f'"{dev.name}": {self.name} is not connected, command not sent')
            return False
        props = dev.pluginProps
        key = int(props["entityKey"])
        did = int(props.get("entityDeviceId") or 0)
        if did:
            kwargs["device_id"] = did

        async def _invoke():
            result = getattr(self.client, method)(key, *args, **kwargs)
            if inspect.isawaitable(result):
                await result

        try:
            self.plugin.run_coro(_invoke(), timeout=10)
            if self.plugin.debug:
                self.logger.debug(f'"{dev.name}": {method}({args}, {kwargs})')
            return True
        except Exception as err:
            self.logger.error(f'"{dev.name}": {method} failed - {err}')
            return False

    async def execute_service(self, service_name, raw_args):
        service = self.services.get(service_name)
        if service is None:
            raise ValueError(f"API action '{service_name}' not found on {self.name}")
        data = {}
        for arg in service.args:
            if arg.name not in raw_args:
                if getattr(arg, "optional", False):
                    continue
                raise ValueError(f"missing argument '{arg.name}'")
            data[arg.name] = coerce_arg(int(arg.type), raw_args[arg.name])
        return await self.client.execute_service(service, data)


def _to_bool(v):
    return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")


def coerce_arg(arg_type, value):
    """Convert a user-supplied value to the type an ESPHome API action argument expects."""
    scalar = {0: _to_bool, 1: lambda v: int(float(v)), 2: float, 3: str}
    if arg_type in scalar:
        return scalar[arg_type](value)
    items = value if isinstance(value, list) else [x.strip() for x in str(value).split(",") if x.strip()]
    return [scalar[arg_type - 4](x) for x in items]
