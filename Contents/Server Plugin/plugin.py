# -*- coding: utf-8 -*-
"""
ESPHome for Indigo, by Durosity. MIT licence.

Native-API control of ESPHome boards (all common entity types), plus a board
setup page and firmware validate / compile / OTA / USB install through the
ESPHome Device Builder.
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import threading
import time
from collections import deque
from datetime import datetime

try:
    import indigo
except ImportError:
    pass

import aioesphomeapi
from aioesphomeapi import ClimateFanMode, ClimateMode, ClimatePreset, ClimateSwingMode, FanDirection, LockCommand

from esp_board import ANSI_RE, CHILD_TYPE_IDS, ESPBoard, ts as _now
from device_builder import DeviceBuilderClient, DeviceBuilderError
import usb_flash
import web_ui
from backups import BackupStore
from ble_presence import BLEPresence, parse_irk
from discovery import discover
from indigo_link import IndigoLink, event_data_json

CORE_TYPE = "espCore"
TRACKER_TYPE = "espBleTracker"


def _float(value, default=None):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _json_list(text):
    try:
        value = json.loads(text or "[]")
        return value if isinstance(value, list) else []
    except ValueError:
        return []


class Plugin(indigo.PluginBase):

    def __init__(self, plugin_id, plugin_display_name, plugin_version, plugin_prefs):
        super().__init__(plugin_id, plugin_display_name, plugin_version, plugin_prefs)
        self.debug = bool(plugin_prefs.get("showDebugInfo", False))
        self.indigo_log_handler.setLevel(logging.DEBUG if self.debug else logging.INFO)
        lib_logger = logging.getLogger("aioesphomeapi")
        lib_logger.addHandler(self.plugin_file_handler)
        lib_logger.setLevel(logging.DEBUG if plugin_prefs.get("libraryDebug", False) else logging.WARNING)

        self.loop = None
        self.loop_thread = None
        self.boards = {}              # core dev id -> ESPBoard
        self.triggers = {}            # trigger id -> trigger
        self.builder = None           # DeviceBuilderClient
        self.db_devices = []          # last devices/list result
        self.build_lock = None        # asyncio.Lock, created on the loop
        self.jobs = {}                # job id -> job dict (builds started from actions or the web page)
        self._poll_task = None
        self._housekeeping_task = None
        self._db_last_error = None
        self.link = IndigoLink(self)
        self.ble = BLEPresence(self)
        self.backups = None

    # ====================================================================== #
    # Startup / shutdown and the asyncio loop
    # ====================================================================== #
    def startup(self):
        try:
            from importlib.metadata import version
            lib_version = version("aioesphomeapi")
        except Exception:
            lib_version = "unknown"
        self.logger.info(f"ESPHome plugin {self.pluginVersion} (aioesphomeapi {lib_version})")

        ready = threading.Event()

        def _run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.build_lock = asyncio.Lock()
            ready.set()
            self.loop.run_forever()
            self.loop.close()

        self.loop_thread = threading.Thread(target=_run, name="ESPHomeLoop", daemon=True)
        self.loop_thread.start()
        ready.wait(5)
        self.backups = BackupStore(self._backup_root(), self._int_pref("backupKeep", 30))
        self._configure_builder()
        self.loop.call_soon_threadsafe(self._start_housekeeping)

    def shutdown(self):
        for task in (self._poll_task, self._housekeeping_task):
            if task is not None:
                self.loop.call_soon_threadsafe(task.cancel)
        for board in list(self.boards.values()):
            try:
                self.run_coro(board.stop(), timeout=5)
            except Exception:
                pass
        self.boards.clear()
        if self.builder is not None:
            try:
                self.run_coro(self.builder.close(), timeout=5)
            except Exception:
                pass
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.loop_thread is not None:
            self.loop_thread.join(5)

    def run_coro(self, coro, timeout=None):
        """Schedule a coroutine on the plugin loop. Waits for the result if timeout is given."""
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout) if timeout is not None else future

    def _background(self, coro, what):
        future = self.run_coro(coro)

        def _done(f):
            try:
                f.result()
            except Exception as err:
                self.logger.error(f"{what} failed: {err}")
                if self.debug:
                    self.logger.debug(what, exc_info=err)
        future.add_done_callback(_done)
        return future

    # ====================================================================== #
    # Plugin preferences
    # ====================================================================== #
    def validatePrefsConfigUi(self, values):
        errors = indigo.Dict()
        if values.get("dbEnabled"):
            url = values.get("dbUrl", "").strip()
            if not re.match(r"^https?://[^/\s]+", url):
                errors["dbUrl"] = "Enter the Device Builder URL, e.g. http://localhost:6052"
        try:
            if int(values.get("dbPollMinutes", "60")) < 5:
                raise ValueError
        except ValueError:
            errors["dbPollMinutes"] = "Enter a whole number of minutes, 5 or more"
        for f in ("autoUpdateStart", "autoUpdateEnd"):
            if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", values.get(f, "03:00").strip()):
                errors[f] = "Use 24-hour HH:MM, e.g. 03:00"
        try:
            if int(values.get("backupKeep", "30")) < 5:
                raise ValueError
        except ValueError:
            errors["backupKeep"] = "Keep at least 5 copies"
        if errors:
            return False, values, errors
        return True, values

    def closedPrefsConfigUi(self, values, user_cancelled):
        if user_cancelled:
            return
        self.debug = bool(values.get("showDebugInfo", False))
        self.indigo_log_handler.setLevel(logging.DEBUG if self.debug else logging.INFO)
        logging.getLogger("aioesphomeapi").setLevel(
            logging.DEBUG if values.get("libraryDebug", False) else logging.WARNING)
        self.backups = BackupStore(self._backup_root(), self._int_pref("backupKeep", 30))
        self._configure_builder()

    def _configure_builder(self):
        async def _swap():
            if self._poll_task is not None:
                self._poll_task.cancel()
                self._poll_task = None
            if self.builder is not None:
                await self.builder.close()
                self.builder = None
            self.db_devices = []
            if self.pluginPrefs.get("dbEnabled", False):
                self.builder = DeviceBuilderClient(
                    self.logger, self.pluginPrefs.get("dbUrl", "http://localhost:6052"),
                    self.pluginPrefs.get("dbUsername", ""), self.pluginPrefs.get("dbPassword", ""))
                self._poll_task = asyncio.get_running_loop().create_task(self._dashboard_poller())
            for board in self.boards.values():
                board.apply_dashboard(self.find_db_device(board))
        self._background(_swap(), "Device Builder setup")

    # ====================================================================== #
    # Devices
    # ====================================================================== #
    def deviceStartComm(self, dev):
        dev.stateListOrDisplayStateIdChanged()
        if dev.deviceTypeId == TRACKER_TYPE:
            props = dev.pluginProps
            if props.get("SupportsOnState") is not True or props.get("SupportsSensorValue") is not False:
                props["SupportsOnState"] = True
                props["SupportsSensorValue"] = False
                props["SupportsStatusRequest"] = False
                dev.replacePluginPropsOnServer(props)
                dev = indigo.devices[dev.id]
            self.ble.add(dev)
            return
        if dev.deviceTypeId == CORE_TYPE:
            if not dev.pluginProps.get("address", "").strip():
                dev.updateStateOnServer("status", "Not configured")
                return
            board = ESPBoard(self, dev)
            self.boards[dev.id] = board
            self._background(board.start(), f"{dev.name}: start")

    def deviceStopComm(self, dev):
        if dev.deviceTypeId == TRACKER_TYPE:
            self.ble.remove(dev.id)
            return
        if dev.deviceTypeId == CORE_TYPE:
            board = self.boards.pop(dev.id, None)
            self.link.forget_board(dev.id)
            if board is not None:
                try:
                    self.run_coro(board.stop(), timeout=10)
                except Exception:
                    self.logger.debug(f"{dev.name}: stop timed out", exc_info=True)
            dev.updateStatesOnServer([{"key": "connected", "value": False}, {"key": "status", "value": "Stopped"}])
            dev.updateStateImageOnServer(indigo.kStateImageSel.PowerOff)

    def deviceDeleted(self, dev):
        if dev.deviceTypeId in CHILD_TYPE_IDS:
            board = self.boards.get(int(dev.pluginProps.get("coreId", 0) or 0))
            if board is not None:
                self.loop.call_soon_threadsafe(board.forget_device, dev.id)
        super().deviceDeleted(dev)

    def didDeviceCommPropertyChange(self, orig, new):
        if new.deviceTypeId not in (CORE_TYPE, TRACKER_TYPE):
            return False   # child props are managed by the plugin and never need a restart
        if new.deviceTypeId == TRACKER_TYPE:
            return orig.pluginProps != new.pluginProps
        keys = ("address", "port", "password", "encryptionKey", "forwardLogs", "dashboardConfiguration",
                "graceSeconds", "shareStates", "allowControl", "bleRoom")
        return any(orig.pluginProps.get(k) != new.pluginProps.get(k) for k in keys)

    def validateDeviceConfigUi(self, values, type_id, dev_id):
        errors = indigo.Dict()
        if type_id == CORE_TYPE:
            if not values.get("address", "").strip():
                errors["address"] = "Enter the board's IP address or hostname (e.g. kitchen.local)"
            try:
                port = int(values.get("port", "6053") or 6053)
                if not 0 < port < 65536:
                    raise ValueError
            except ValueError:
                errors["port"] = "Enter a valid port (normally 6053)"
            key = values.get("encryptionKey", "").strip()
            if key:
                import base64
                try:
                    if len(base64.b64decode(key, validate=True)) != 32:
                        raise ValueError
                except Exception:
                    errors["encryptionKey"] = "The API encryption key should be the 44-character base64 value from your YAML"
            cfg = values.get("dashboardConfiguration", "").strip()
            if cfg and not cfg.endswith((".yaml", ".yml")):
                values["dashboardConfiguration"] = cfg + ".yaml"
            try:
                if not 0 <= int(values.get("graceSeconds", "30") or 0) <= 3600:
                    raise ValueError
            except ValueError:
                errors["graceSeconds"] = "Enter 0-3600 seconds (0 = report drops immediately)"
        elif type_id == TRACKER_TYPE:
            kind = values.get("idType", "mac")
            ident = values.get("identifier", "").strip()
            if kind == "mac":
                if not re.match(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$", ident):
                    errors["identifier"] = "Enter a MAC address like AA:BB:CC:DD:EE:FF"
                else:
                    values["identifier"] = ident.upper().replace("-", ":")
            elif kind == "irk":
                try:
                    parse_irk(ident)
                except ValueError as err:
                    errors["identifier"] = str(err)
            else:
                if not re.match(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", ident):
                    errors["identifier"] = "Enter the iBeacon UUID, e.g. FDA50693-A4E2-4FB1-AFCF-C6EB07647825"
                for f in ("major", "minor"):
                    v = values.get(f, "").strip()
                    if v and (not v.isdigit() or int(v) > 65535):
                        errors[f] = "0-65535, or blank for any"
        if errors:
            return False, values, errors
        return True, values

    def unique_device_name(self, name):
        candidate, n = name, 2
        while candidate in indigo.devices:
            candidate = f"{name} ({n})"
            n += 1
        return candidate

    def folder_for_board(self, board):
        core = indigo.devices[board.dev_id]
        if self.pluginPrefs.get("entityFolderMode", "board") != "board":
            return core.folderId
        folder_name = core.name
        if folder_name not in indigo.devices.folders:
            indigo.devices.folder.create(folder_name)
        return indigo.devices.folders[folder_name].id

    def _board_for(self, dev):
        core_id = dev.id if dev.deviceTypeId == CORE_TYPE else int(dev.pluginProps.get("coreId", 0) or 0)
        board = self.boards.get(core_id)
        if board is None:
            self.logger.warning(f'"{dev.name}": its ESPHome board is disabled or missing')
        return board

    # ====================================================================== #
    # Standard device actions
    # ====================================================================== #
    def actionControlDevice(self, action, dev):
        board = self._board_for(dev)
        if board is None:
            return
        a = action.deviceAction
        K = indigo.kDeviceAction
        t = dev.deviceTypeId

        if t == "espSwitch":
            if a in (K.TurnOn, K.TurnOff, K.Toggle):
                state = (a == K.TurnOn) if a != K.Toggle else not dev.onState
                board.send("switch_command", dev, state)

        elif t == "espButton":
            if a in (K.TurnOn, K.Toggle):
                if board.send("button_command", dev):
                    self.logger.info(f'"{dev.name}" pressed')
                    dev.updateStatesOnServer([{"key": "onOffState", "value": False},
                                              {"key": "lastPressed", "value": _now()}])

        elif t == "espLight":
            self._light_action(board, dev, action)

        elif t in ("espCover", "espValve"):
            method = "cover_command" if t == "espCover" else "valve_command"
            target = None
            if a == K.TurnOn:
                target = 100
            elif a == K.TurnOff:
                target = 0
            elif a == K.Toggle:
                target = 0 if dev.brightness > 0 else 100
            elif a == K.SetBrightness:
                target = action.actionValue
            elif a == K.BrightenBy:
                target = min(100, dev.brightness + action.actionValue)
            elif a == K.DimBy:
                target = max(0, dev.brightness - action.actionValue)
            if target is not None:
                if not dev.pluginProps.get("supportsPosition"):
                    target = 100 if target > 0 else 0
                board.send(method, dev, position=max(0, min(100, target)) / 100.0)

        elif t == "espLock":
            if a in (K.Lock, K.TurnOn):
                board.send("lock_command", dev, LockCommand.LOCK)
            elif a in (K.Unlock, K.TurnOff):
                board.send("lock_command", dev, LockCommand.UNLOCK)
            elif a == K.Toggle:
                board.send("lock_command", dev, LockCommand.UNLOCK if dev.onState else LockCommand.LOCK)

    def _light_action(self, board, dev, action):
        a = action.deviceAction
        K = indigo.kDeviceAction
        if a == K.TurnOn:
            board.send("light_command", dev, state=True)
        elif a == K.TurnOff:
            board.send("light_command", dev, state=False)
        elif a == K.Toggle:
            board.send("light_command", dev, state=not dev.onState)
        elif a in (K.SetBrightness, K.BrightenBy, K.DimBy):
            if a == K.SetBrightness:
                level = action.actionValue
            elif a == K.BrightenBy:
                level = dev.brightness + action.actionValue
            else:
                level = dev.brightness - action.actionValue
            level = max(0, min(100, int(level)))
            if level == 0:
                board.send("light_command", dev, state=False)
            else:
                board.send("light_command", dev, state=True, brightness=level / 100.0)
        elif a == K.SetColorLevels:
            values = action.actionValue
            kwargs = {"state": True}
            props = dev.pluginProps
            if props.get("SupportsRGB") and any(k in values for k in ("redLevel", "greenLevel", "blueLevel")):
                rgb = [float(values.get(k, dev.states.get(k, 0))) for k in ("redLevel", "greenLevel", "blueLevel")]
                peak = max(rgb)
                if peak > 0:
                    kwargs["rgb"] = tuple(c / peak for c in rgb)
                    kwargs["color_brightness"] = peak / 100.0
                else:
                    kwargs["color_brightness"] = 0.0
            if props.get("SupportsWhite") and "whiteLevel" in values:
                kwargs["white"] = float(values["whiteLevel"]) / 100.0
            if props.get("SupportsWhiteTemperature") and "whiteTemperature" in values:
                kelvin = float(values["whiteTemperature"])
                if kelvin > 0:
                    kwargs["color_temperature"] = 1e6 / kelvin
            board.send("light_command", dev, **kwargs)

    def actionControlSpeedControl(self, action, dev):
        board = self._board_for(dev)
        if board is None:
            return
        a = action.speedControlAction
        K = indigo.kSpeedControlAction
        count = int(dev.pluginProps.get("speedCount", "0") or 0)

        def set_level(level):
            level = max(0, min(100, int(level)))
            if level == 0:
                board.send("fan_command", dev, state=False)
            elif count:
                board.send("fan_command", dev, state=True, speed_level=max(1, -(-level * count // 100)))
            else:
                board.send("fan_command", dev, state=True)

        index_to_level = {0: 0, 1: 33, 2: 66, 3: 100}
        if a == K.TurnOn:
            board.send("fan_command", dev, state=True)
        elif a == K.TurnOff:
            board.send("fan_command", dev, state=False)
        elif a == K.Toggle:
            board.send("fan_command", dev, state=not dev.onState)
        elif a == K.SetSpeedLevel:
            set_level(action.actionValue)
        elif a == K.SetSpeedIndex:
            set_level(index_to_level.get(int(action.actionValue), 100))
        elif a == K.IncreaseSpeedIndex:
            set_level(index_to_level[min(3, dev.speedIndex + 1)])
        elif a == K.DecreaseSpeedIndex:
            set_level(index_to_level[max(0, dev.speedIndex - 1)])

    def actionControlThermostat(self, action, dev):
        board = self._board_for(dev)
        if board is None:
            return
        a = action.thermostatAction
        K = indigo.kThermostatAction
        props = dev.pluginProps
        supported = props.get("supportedModes", "").split(",")
        two_point = bool(props.get("twoPoint"))

        if a == K.SetHvacMode:
            H = indigo.kHvacMode
            mapping = {}
            for name, mode_text in (("Off", "off"), ("Heat", "heat"), ("Cool", "cool"), ("HeatCool", "heat_cool"),
                                    ("ProgramHeat", "heat"), ("ProgramCool", "cool"),
                                    ("ProgramHeatCool", "auto"), ("ProgramAuto", "auto")):
                if hasattr(H, name):
                    mapping[getattr(H, name)] = mode_text
            mode = mapping.get(action.actionMode, "auto")
            if mode == "heat_cool" and mode not in supported and "auto" in supported:
                mode = "auto"
            if mode not in supported:
                self.logger.warning(f'"{dev.name}" does not support {mode} mode (supports: {", ".join(supported)})')
                return
            board.send("climate_command", dev, mode=ClimateMode[mode.upper()])
            return

        if a == K.SetFanMode:
            fan = ClimateFanMode.ON if action.actionMode == indigo.kFanMode.AlwaysOn else ClimateFanMode.AUTO
            board.send("climate_command", dev, fan_mode=fan)
            return

        heat = a in (K.SetHeatSetpoint, K.IncreaseHeatSetpoint, K.DecreaseHeatSetpoint)
        current = dev.heatSetpoint if heat else dev.coolSetpoint
        if a in (K.SetHeatSetpoint, K.SetCoolSetpoint):
            target = action.actionValue
        elif a in (K.IncreaseHeatSetpoint, K.IncreaseCoolSetpoint):
            target = current + action.actionValue
        elif a in (K.DecreaseHeatSetpoint, K.DecreaseCoolSetpoint):
            target = current - action.actionValue
        else:
            return
        if two_point:
            key = "target_temperature_low" if heat else "target_temperature_high"
            board.send("climate_command", dev, **{key: float(target)})
        else:
            board.send("climate_command", dev, target_temperature=float(target))

    def actionControlUniversal(self, action, dev):
        if action.deviceAction == indigo.kUniversalAction.RequestStatus:
            self.logger.info(f'"{dev.name}": ESPHome pushes state changes live, no status request needed')

    # ====================================================================== #
    # Custom entity actions (Actions.xml)
    # ====================================================================== #
    def _dev(self, action, dev):
        return dev if dev is not None else indigo.devices[action.deviceId]

    def action_set_number(self, action, dev=None):
        dev = self._dev(action, dev)
        board = self._board_for(dev)
        value = _float(self.substitute(action.props.get("value", "")))
        if board is None or value is None:
            self.logger.error(f'"{dev.name}": a numeric value is required')
            return
        lo, hi = _float(dev.pluginProps.get("minValue")), _float(dev.pluginProps.get("maxValue"))
        if lo is not None and hi is not None and not lo <= value <= hi:
            self.logger.warning(f'"{dev.name}": {value} is outside {lo}-{hi}, the board may clamp or reject it')
        board.send("number_command", dev, value)

    def action_set_select(self, action, dev=None):
        dev = self._dev(action, dev)
        board = self._board_for(dev)
        option = self.substitute(action.props.get("optionText", "").strip()) or action.props.get("option", "")
        options = _json_list(dev.pluginProps.get("optionsJson"))
        if board is None or not option:
            return
        if options and option not in options:
            self.logger.error(f'"{dev.name}": "{option}" is not one of {options}')
            return
        board.send("select_command", dev, option)

    def action_set_text(self, action, dev=None):
        dev = self._dev(action, dev)
        board = self._board_for(dev)
        if board is not None:
            board.send("text_command", dev, self.substitute(action.props.get("value", "")))

    def action_press_button(self, action, dev=None):
        dev = self._dev(action, dev)
        board = self._board_for(dev)
        if board is not None and board.send("button_command", dev):
            dev.updateStateOnServer("lastPressed", _now())

    def action_stop_cover(self, action, dev=None):
        dev = self._dev(action, dev)
        board = self._board_for(dev)
        if board is not None:
            board.send("cover_command" if dev.deviceTypeId == "espCover" else "valve_command", dev, stop=True)

    def action_set_cover_tilt(self, action, dev=None):
        dev = self._dev(action, dev)
        board = self._board_for(dev)
        tilt = _float(self.substitute(action.props.get("tilt", "")))
        if board is None or tilt is None:
            return
        if not dev.pluginProps.get("supportsTilt"):
            self.logger.warning(f'"{dev.name}" does not support tilt')
            return
        board.send("cover_command", dev, tilt=max(0.0, min(100.0, tilt)) / 100.0)

    def action_light_transition(self, action, dev=None):
        dev = self._dev(action, dev)
        board = self._board_for(dev)
        level = _float(self.substitute(action.props.get("brightness", "")))
        seconds = _float(self.substitute(action.props.get("transition", "1")), 1.0)
        if board is None or level is None:
            return
        level = max(0.0, min(100.0, level))
        kwargs = {"state": level > 0, "transition_length": max(0.0, seconds)}
        if level > 0:
            kwargs["brightness"] = level / 100.0
        board.send("light_command", dev, **kwargs)

    def action_light_effect(self, action, dev=None):
        dev = self._dev(action, dev)
        board = self._board_for(dev)
        effect = action.props.get("effect", "")
        if board is not None and effect:
            board.send("light_command", dev, state=True, effect=effect)

    def action_fan_options(self, action, dev=None):
        dev = self._dev(action, dev)
        board = self._board_for(dev)
        if board is None:
            return
        p = action.props
        kwargs = {}
        if p.get("oscillation") in ("on", "off"):
            kwargs["oscillating"] = p["oscillation"] == "on"
        if p.get("direction") in ("forward", "reverse"):
            kwargs["direction"] = FanDirection[p["direction"].upper()]
        if p.get("preset"):
            kwargs["preset_mode"] = p["preset"]
        if kwargs:
            board.send("fan_command", dev, **kwargs)

    def action_lock_command(self, action, dev=None):
        dev = self._dev(action, dev)
        board = self._board_for(dev)
        if board is None:
            return
        command = {"lock": LockCommand.LOCK, "unlock": LockCommand.UNLOCK, "open": LockCommand.OPEN}[
            action.props.get("command", "lock")]
        if command == LockCommand.OPEN and not dev.pluginProps.get("supportsOpen"):
            self.logger.warning(f'"{dev.name}" does not support open/unlatch')
            return
        code = self.substitute(action.props.get("code", "")) or None
        board.send("lock_command", dev, command, code=code)

    def action_climate_options(self, action, dev=None):
        dev = self._dev(action, dev)
        board = self._board_for(dev)
        if board is None:
            return
        p = action.props
        kwargs = {}
        preset = p.get("preset", "")
        if preset:
            if preset.upper() in ClimatePreset.__members__:
                kwargs["preset"] = ClimatePreset[preset.upper()]
            else:
                kwargs["custom_preset"] = preset
        fan = p.get("fanMode", "")
        if fan:
            if fan.upper() in ClimateFanMode.__members__:
                kwargs["fan_mode"] = ClimateFanMode[fan.upper()]
            else:
                kwargs["custom_fan_mode"] = fan
        if p.get("swingMode", "") in ("off", "both", "vertical", "horizontal"):
            kwargs["swing_mode"] = ClimateSwingMode[p["swingMode"].upper()]
        humidity = _float(self.substitute(p.get("targetHumidity", "")))
        if humidity is not None:
            kwargs["target_humidity"] = humidity
        if kwargs:
            board.send("climate_command", dev, **kwargs)

    def action_run_api_action(self, action, dev=None):
        p = action.props
        board = self.boards.get(int(p.get("coreId", 0) or 0))
        name = p.get("service", "")
        if board is None or not board.connected:
            self.logger.warning(f"Run API action {name}: board is not connected")
            return
        raw = self.substitute(p.get("arguments", ""))
        try:
            args = _parse_args(raw)
        except ValueError as err:
            self.logger.error(f"Run API action {name}: could not read arguments - {err}")
            return

        async def _go():
            await board.execute_service(name, args)
            self.logger.info(f"{board.name}: ran API action {name}")
        self._background(_go(), f"{board.name}: API action {name}")

    # ---- core board actions ----
    def action_reconnect(self, action, dev=None):
        dev = self._dev(action, dev)
        self.logger.info(f"{dev.name}: reconnecting")
        self.deviceStopComm(dev)
        self.deviceStartComm(indigo.devices[dev.id])

    def action_sync_entities(self, action, dev=None):
        dev = self._dev(action, dev)
        board = self._board_for(dev)
        if board is not None:
            self._background(board.resync(force_create=True), f"{dev.name}: sync entities")

    def action_validate(self, action, dev=None):
        self._start_build(self._dev(action, dev), "validate")

    def action_compile(self, action, dev=None):
        self._start_build(self._dev(action, dev), "compile")

    def action_install(self, action, dev=None):
        self._start_build(self._dev(action, dev), "install")

    def action_refresh_dashboard(self, action=None, *args):
        self._background(self.refresh_dashboard(log=True), "Device Builder refresh")

    def action_update_all(self, action=None, *args):
        self._background(self.update_all_boards(), "Update all boards")

    # ====================================================================== #
    # Dynamic lists and config UI callbacks
    # ====================================================================== #
    def menu_changed(self, values, type_id="", target_id=0):
        return values

    def _target_props(self, target_id):
        try:
            return indigo.devices[int(target_id)].pluginProps
        except (KeyError, ValueError, TypeError):
            return {}

    def list_select_options(self, filter="", values=None, type_id="", target_id=0):
        return [(o, o) for o in _json_list(self._target_props(target_id).get("optionsJson"))]

    def list_light_effects(self, filter="", values=None, type_id="", target_id=0):
        return [(e, e) for e in _json_list(self._target_props(target_id).get("effectsJson"))]

    def list_fan_presets(self, filter="", values=None, type_id="", target_id=0):
        return [("", "- no change -")] + [(x, x) for x in _json_list(self._target_props(target_id).get("presetsJson"))]

    def list_climate_presets(self, filter="", values=None, type_id="", target_id=0):
        return [("", "- no change -")] + [(x, x) for x in _json_list(self._target_props(target_id).get("presetsJson"))]

    def list_climate_fan_modes(self, filter="", values=None, type_id="", target_id=0):
        return [("", "- no change -")] + [(x, x) for x in _json_list(self._target_props(target_id).get("fanModesJson"))]

    def list_event_types(self, filter="", values=None, type_id="", target_id=0):
        dev_id = (values or {}).get("deviceId", "")
        return [("", "- any event -")] + [(x, x) for x in _json_list(self._target_props(dev_id).get("eventTypesJson"))]

    def list_boards_any(self, filter="", values=None, type_id="", target_id=0):
        return [("any", "- any board -")] + [(str(d.id), d.name) for d in indigo.devices.iter("self.espCore")]

    def list_api_actions(self, filter="", values=None, type_id="", target_id=0):
        board = self.boards.get(int((values or {}).get("coreId", 0) or 0))
        if board is None or not board.services:
            return [("", "- select a connected board first -")]
        out = []
        for name, svc in sorted(board.services.items()):
            sig = ", ".join(f"{a.name}: {aioesphomeapi.UserServiceArgType(int(a.type)).name.lower()}" for a in svc.args)
            out.append((name, f"{name}({sig})"))
        return out

    def config_sync_now(self, values, type_id, dev_id):
        board = self.boards.get(dev_id)
        if board is None or not board.connected:
            self.logger.warning("Save the device and wait for it to connect before syncing")
        else:
            self._background(board.resync(force_create=True), f"{board.name}: sync entities")
        return values

    # ====================================================================== #
    # Triggers
    # ====================================================================== #
    def triggerStartProcessing(self, trigger):
        self.triggers[trigger.id] = trigger

    def triggerStopProcessing(self, trigger):
        self.triggers.pop(trigger.id, None)

    def fire_event_triggers(self, dev, event_type):
        for trig in list(self.triggers.values()):
            if trig.pluginTypeId != "espEventReceived":
                continue
            if str(trig.pluginProps.get("deviceId", "")) != str(dev.id):
                continue
            wanted = trig.pluginProps.get("eventType", "")
            if not wanted or wanted == event_type:
                indigo.trigger.execute(trig)

    def fire_build_triggers(self, board, ok):
        for trig in list(self.triggers.values()):
            if trig.pluginTypeId != "buildFinished":
                continue
            target = str(trig.pluginProps.get("deviceId", ""))
            if target not in ("", "any", str(board.dev_id)):
                continue
            want = trig.pluginProps.get("result", "any")
            if want == "any" or (want == "success") == ok:
                indigo.trigger.execute(trig)

    def fire_board_triggers(self, board, what):
        for trig in list(self.triggers.values()):
            if trig.pluginTypeId != "boardOffline":
                continue
            if str(trig.pluginProps.get("deviceId", "any")) in ("", "any", str(board.dev_id)):
                indigo.trigger.execute(trig)

    def board_event(self, board, name, data):
        """A board sent homeassistant.event (loop thread)."""
        payload = event_data_json(data)
        board._core_states([{"key": "lastBoardEvent", "value": name},
                            {"key": "lastBoardEventData", "value": payload},
                            {"key": "lastBoardEventTime", "value": _now()}])
        self.logger.info(f"{board.name}: event {name}" + (f" {payload}" if data else ""))
        for trig in list(self.triggers.values()):
            if trig.pluginTypeId != "boardEvent":
                continue
            if str(trig.pluginProps.get("deviceId", "any")) not in ("", "any", str(board.dev_id)):
                continue
            wanted = trig.pluginProps.get("eventName", "").strip()
            if not wanted or wanted == name or wanted == name.split(".", 1)[-1]:
                indigo.trigger.execute(trig)

    # ====================================================================== #
    # Indigo change feed (for boards following Indigo states)
    # ====================================================================== #
    def deviceUpdated(self, orig, new):
        super().deviceUpdated(orig, new)
        if self.link.watch:
            self.link.indigo_changed("device", new.id)

    def variableUpdated(self, orig, new):
        super().variableUpdated(orig, new)
        if self.link.watch:
            self.link.indigo_changed("variable", new.id)

    # ====================================================================== #
    # Housekeeping: log-watch expiry, Bluetooth timeouts, backups, night updates
    # ====================================================================== #
    def _int_pref(self, key, default):
        try:
            return int(str(self.pluginPrefs.get(key, default)).strip())
        except ValueError:
            return default

    def _backup_root(self):
        try:
            base = os.path.join(indigo.server.getInstallFolderPath(), "Preferences", "Plugins", self.pluginId)
        except Exception:
            base = os.path.expanduser("~/Library/Application Support/Indigo ESPHome")
        root = os.path.join(base, "backups")
        os.makedirs(root, exist_ok=True)
        return root

    def _start_housekeeping(self):
        self._housekeeping_task = asyncio.get_running_loop().create_task(self._housekeeping())

    async def _housekeeping(self):
        tick = 0
        while True:
            await asyncio.sleep(5)
            tick += 1
            try:
                self.ble.tick()
                for board in list(self.boards.values()):
                    board.update_log_subscription()
                if tick % 12 == 0:      # once a minute
                    await self._nightly_jobs()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("Housekeeping error")

    def _in_window(self, now, start, end):
        def mins(text):
            h, m = (int(x) for x in str(text).strip().split(":"))
            return h * 60 + m
        try:
            a, b, n = mins(start), mins(end), now.hour * 60 + now.minute
        except ValueError:
            return False
        return a <= n < b if a <= b else (n >= a or n < b)

    async def _nightly_jobs(self):
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if self.builder is not None and self.pluginPrefs.get("backupNightly", True) \
                and now.hour >= 2 and self.pluginPrefs.get("lastBackupDate", "") != today:
            self.pluginPrefs["lastBackupDate"] = today
            await self.backup_all()
        if self.builder is not None and self.pluginPrefs.get("autoUpdate", False) \
                and self.pluginPrefs.get("lastAutoUpdateDate", "") != today \
                and self._in_window(now, self.pluginPrefs.get("autoUpdateStart", "03:00"),
                                    self.pluginPrefs.get("autoUpdateEnd", "05:00")):
            self.pluginPrefs["lastAutoUpdateDate"] = today
            self.logger.info("Overnight update window: checking boards")
            await self.update_all_boards()

    async def backup_all(self, log=False):
        saved = 0
        try:
            devices = await self.builder.list_devices()
        except Exception as err:
            self.logger.warning(f"Backup: cannot reach Device Builder ({err})")
            return 0
        for d in devices:
            cfg = d.get("configuration", "")
            try:
                content = await self.builder.get_config(cfg)
                if content and self.backups.save(cfg, content):
                    saved += 1
            except Exception as err:
                self.logger.warning(f"Backup of {cfg} failed: {err}")
        folder = self.pluginPrefs.get("dbConfigFolder", "").strip()
        if folder:
            path = os.path.join(os.path.expanduser(folder), "secrets.yaml")
            try:
                with open(path, encoding="utf-8") as fh:
                    if self.backups.save("_secrets.yaml", fh.read()):
                        saved += 1
            except OSError as err:
                self.logger.warning(f"Backup: cannot read {path} ({err.strerror})")
        if log or saved:
            self.logger.info(f"Backup: {saved} changed file(s) saved to {self.backups.root}")
        return saved

    async def backup_installed(self, cfg):
        try:
            content = await self.builder.get_config(cfg)
            if content:
                self.backups.save(cfg, content, installed=True)
        except Exception as err:
            self.logger.debug(f"Post-install backup of {cfg} failed: {err}")

    # ---- boards found by discovery ----
    def add_discovered(self, node, address, key, name=""):
        for board in self.boards.values():
            if board.node_name == node:
                return {"created": False, "deviceId": board.dev_id, "name": board.name}
        props = {"address": address, "port": "6053", "encryptionKey": key or "", "password": "",
                 "forwardLogs": "off", "dashboardConfiguration": "", "graceSeconds": "30"}
        dev = indigo.device.create(indigo.kProtocol.Plugin, address=address,
                                   name=self.unique_device_name(name or node),
                                   description=f"ESPHome board {node}", deviceTypeId=CORE_TYPE, props=props)
        self.logger.info(f'Added ESPHome board "{dev.name}" ({address})')
        return {"created": True, "deviceId": dev.id, "name": dev.name}

    # ====================================================================== #
    # Device Builder
    # ====================================================================== #
    async def _dashboard_poller(self):
        await asyncio.sleep(5)
        while True:
            await self.refresh_dashboard()
            try:
                minutes = max(5, int(self.pluginPrefs.get("dbPollMinutes", "60")))
            except ValueError:
                minutes = 60
            await asyncio.sleep(minutes * 60)

    async def refresh_dashboard(self, log=False):
        if self.builder is None:
            if log:
                self.logger.warning("Device Builder integration is not enabled in plugin config")
            return
        try:
            self.db_devices = await self.builder.list_devices()
            self._db_last_error = None
        except Exception as err:
            if str(err) != self._db_last_error:
                self.logger.warning(f"Device Builder: {err}")
                self._db_last_error = str(err)
            return
        for board in self.boards.values():
            board.apply_dashboard(self.find_db_device(board))
        if log:
            pending = [b.name for b in self.boards.values() if b.update_available]
            self.logger.info(f"Device Builder: {len(self.db_devices)} configurations, "
                             f"{len(pending)} board(s) with an ESPHome update available"
                             + (f": {', '.join(pending)}" if pending else ""))

    def find_db_device(self, board):
        if not self.db_devices:
            return None
        if board.config_override:
            return next((d for d in self.db_devices if d.get("configuration") == board.config_override), None)
        node = board.node_name
        return next((d for d in self.db_devices if node and d.get("name") == node), None)

    def apply_dashboard_to_board(self, board):
        board.apply_dashboard(self.find_db_device(board))

    def _start_build(self, dev, mode):
        board = self._board_for(dev)
        if board is None:
            return
        if self.builder is None:
            self.logger.error("Enable the Device Builder integration in plugin config first")
            return
        self.logger.info(f"{board.name}: {mode} queued")
        self._background(self.run_build(board, mode), f"{board.name}: {mode}")

    async def run_build(self, board, mode):
        """Build for a board already in Indigo (used by actions, menus and Update All)."""
        if not board.db_config:
            await self.refresh_dashboard()
        cfg = board.db_config
        if not cfg:
            self.logger.error(f"{board.name}: no matching configuration in Device Builder "
                              f"(set the configuration file name in the board's settings)")
            return False
        job = self.new_job(cfg, mode, board=board)
        return await self.run_job(job)

    # ---- jobs: one build at a time, output kept for the web page ----
    def new_job(self, cfg, mode, board=None, port=None):
        import uuid
        job = {"id": uuid.uuid4().hex[:10], "cfg": cfg, "mode": mode, "port": port,
               "board_id": board.dev_id if board else None, "status": "queued",
               "lines": deque(maxlen=600), "note": "", "created": _now()}
        self.jobs[job["id"]] = job
        while len(self.jobs) > 20:
            self.jobs.pop(next(iter(self.jobs)))
        return job

    async def run_job(self, job):
        board = self.boards.get(job["board_id"]) if job["board_id"] else None
        label = board.name if board else job["cfg"]
        mode, cfg = job["mode"], job["cfg"]
        async with self.build_lock:
            job["status"] = "running"
            if board:
                board.set_build_state(f"{mode} running")
            log_path = self._build_log_path(cfg)
            tail = deque(maxlen=12)
            with open(log_path, "w", encoding="utf-8") as fh:
                def on_line(text):
                    # esptool / PlatformIO progress bars overwrite with \r - keep the last segment
                    line = ANSI_RE.sub("", text).split("\r")[-1].rstrip("\n")
                    if not line.strip():
                        return
                    fh.write(line + "\n")
                    tail.append(line)
                    job["lines"].append(line)
                    if self.debug:
                        self.logger.debug(f"[{label} {mode}] {line}")

                try:
                    ok, note = await self._run_build_steps(board, mode, cfg, on_line, job)
                except Exception as err:
                    on_line(f"Error: {err}")
                    ok, note = False, f" - {err}"

            job["status"] = "succeeded" if ok else "failed"
            if ok and mode in ("install", "usb"):
                await self.backup_installed(cfg)
            job["note"] = note
            if board:
                board.set_build_state("idle", ("success" if ok else "failed") + f" ({mode})")
                self.fire_build_triggers(board, ok)
            if ok:
                self.logger.info(f"{label}: {mode} succeeded{note}")
            else:
                details = "\n".join(f"    {x}" for x in list(tail)[-8:])
                self.logger.error(f"{label}: {mode} failed{note}\n{details}\n    Full log: {log_path}")
        if mode != "validate":
            await self.refresh_dashboard()
        return ok

    async def _run_build_steps(self, board, mode, cfg, on_line, job=None):
        try:
            if mode == "validate":
                result = await self.builder.stream("devices/validate", {"configuration": cfg}, on_line) or {}
                return bool(result.get("success")), ""

            if mode in ("compile", "usb"):
                started = await self.builder.command("firmware/compile", {"configuration": cfg})
                result = await self.builder.follow_job(started["job_id"], on_line)
                if result.get("status") != "completed":
                    return False, _job_note(result)
                if mode == "compile":
                    return True, ""
                return await self._usb_flash(cfg, job["port"], on_line)

            # install = compile job + dependent upload job
            if board is not None and self.pluginPrefs.get("otaTarget", "ip") == "ip":
                port = board.ota_target()
            else:
                port = "OTA"
            started = await self.builder.command("firmware/install", {"configuration": cfg, "port": port})
            first = await self.builder.follow_job(started["job_id"], on_line)
            if first.get("status") != "completed":
                return False, _job_note(first)
            if started.get("is_deferred_install") or first.get("queued_update_armed"):
                return True, " - board is offline, Device Builder will flash it when it comes back"
            if started.get("job_type") != "compile":
                return True, ""   # older fused install job
            jobs = await self.builder.get_jobs(cfg)
            upload = next((j for j in reversed(jobs) if j.get("depends_on") == started["job_id"]), None)
            if upload is None:
                return False, " - compile finished but no upload job was queued"
            on_line(f"--- uploading to {port} ---")
            second = await self.builder.follow_job(upload["job_id"], on_line)
            if second.get("status") == "completed":
                return True, f" (flashed {port})"
            return False, " - compiled OK but the upload " + (_job_note(second)[3:] or "failed")
        except DeviceBuilderError as err:
            on_line(f"Device Builder error: {err}")
            return False, f" - {err}"

    async def _usb_flash(self, cfg, port, on_line):
        if not port:
            return False, " - no USB serial port chosen"
        folder = os.path.join(os.path.dirname(self._build_log_path(cfg)), "firmware")
        on_line("--- downloading factory firmware from Device Builder ---")
        image = await self.builder.download_factory_binary(cfg, folder)
        on_line(f"--- flashing {os.path.basename(image)} to {port} (hold BOOT if it fails to connect) ---")
        loop = asyncio.get_running_loop()

        def line_threadsafe(text):
            loop.call_soon_threadsafe(on_line, text)
        try:
            await loop.run_in_executor(None, usb_flash.flash, port, image, line_threadsafe)
        except RuntimeError as err:
            return False, f" - USB flash failed: {err}"
        await asyncio.sleep(0.2)   # let queued log lines land
        return True, f" (flashed over USB on {port})"

    def core_device_for_config(self, cfg):
        for dev in indigo.devices.iter("self.espCore"):
            if cfg and dev.pluginProps.get("dashboardConfiguration", "") == cfg:
                return dev
        return None

    async def adopt(self, cfg, name=""):
        """Create (or return) the ESPHome Board device for a Device Builder configuration."""
        for board in self.boards.values():
            if board.db_config == cfg or board.config_override == cfg:
                return {"created": False, "deviceId": board.dev_id, "name": board.name}
        existing = self.core_device_for_config(cfg)
        if existing is not None:
            return {"created": False, "deviceId": existing.id, "name": existing.name}
        devices = await self.builder.list_devices()
        entry = next((d for d in devices if d.get("configuration") == cfg), None)
        if entry is None:
            raise ValueError(f"{cfg} not found in Device Builder")
        node = entry.get("name", "")
        address = entry.get("address") or f"{node}.local"
        key = await self.builder.get_encryption_key(cfg)
        dev_name = self.unique_device_name(name or entry.get("friendly_name") or node)
        props = {"address": address, "port": "6053", "encryptionKey": key, "password": "",
                 "forwardLogs": "off", "dashboardConfiguration": cfg, "graceSeconds": "30"}
        dev = indigo.device.create(indigo.kProtocol.Plugin, address=address, name=dev_name,
                                   description=f"ESPHome board {node}", deviceTypeId=CORE_TYPE, props=props)
        self.logger.info(f'Added ESPHome board "{dev.name}" ({address}) - it will connect once it is on Wi-Fi')
        return {"created": True, "deviceId": dev.id, "name": dev.name}

    def _build_log_path(self, cfg):
        try:
            folder = indigo.server.getLogsFolderPath(pluginId=self.pluginId)
        except Exception:
            folder = os.path.expanduser("~/Library/Logs")
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, f"build-{os.path.splitext(cfg)[0]}.log")

    async def update_all_boards(self):
        await self.refresh_dashboard()
        targets = [b for b in self.boards.values() if b.update_available and b.connected]
        if not targets:
            self.logger.info("All boards are up to date")
            return
        self.logger.info(f"Updating {len(targets)} board(s): {', '.join(b.name for b in targets)}")
        for board in targets:
            await self.run_build(board, "install")

    # ====================================================================== #
    # Board setup web page (Indigo Web Server)
    # ====================================================================== #
    def web_page(self, action, dev=None, callerWaitingForResult=None):
        return web_ui.serve_page(self, action)

    def web_api(self, action, dev=None, callerWaitingForResult=None):
        return web_ui.serve_api(self, action)

    def setup_page_url(self):
        base = (self.pluginPrefs.get("iwsUrl", "") or "http://localhost:8176").rstrip("/")
        return f"{base}/message/{self.pluginId}/ui/"

    # ====================================================================== #
    # Menu items
    # ====================================================================== #
    def menu_sync_all(self, *args, **kwargs):
        for board in self.boards.values():
            if board.connected:
                self._background(board.resync(force_create=True), f"{board.name}: sync entities")

    def menu_refresh_dashboard(self, *args, **kwargs):
        self.action_refresh_dashboard()

    def menu_update_all(self, *args, **kwargs):
        self.action_update_all()

    def menu_backup_now(self, *args, **kwargs):
        if self.builder is None:
            self.logger.warning("Enable the Device Builder integration first")
            return
        self._background(self.backup_all(log=True), "Backup")

    def menu_open_setup_page(self, *args, **kwargs):
        url = self.setup_page_url()
        self.logger.info(f"Board setup page: {url}")
        subprocess.Popen(["/usr/bin/open", url])

    def menu_open_dashboard(self, *args, **kwargs):
        url = self.pluginPrefs.get("dbUrl", "")
        if not url:
            self.logger.warning("Set the Device Builder URL in plugin config first")
            return
        subprocess.Popen(["/usr/bin/open", url])

    def menu_board_summary(self, *args, **kwargs):
        if not self.boards:
            self.logger.info("No ESPHome boards configured")
        for b in self.boards.values():
            di = b.device_info
            self.logger.info(
                f"{b.name}: {'connected' if b.connected else 'NOT connected'} | {b.host}:{b.port} | "
                f"node {b.node_name or '?'} | ESPHome {di.esphome_version if di else '?'} | "
                f"{len(b.key_map)} entities, {len(b.services)} API actions, {len(b.orphans)} orphaned | "
                f"encryption {'on' if b.noise_psk else 'off'} | drops {b.disconnect_count} (24h: {b._drops_24h()}) | "
                f"follows {len(b.ha_subscriptions)} Indigo item(s) | Bluetooth {'yes' if b.ble_capable() else 'no'} | "
                f"Device Builder config: {b.db_config or 'not matched'}"
                + (" | UPDATE AVAILABLE" if b.update_available else ""))

    def menu_toggle_debug(self, *args, **kwargs):
        self.debug = not self.debug
        self.pluginPrefs["showDebugInfo"] = self.debug
        self.indigo_log_handler.setLevel(logging.DEBUG if self.debug else logging.INFO)
        self.logger.info(f"Debug logging {'on' if self.debug else 'off'}")


# -------------------------------------------------------------------------- #
def _job_note(result):
    err = (result or {}).get("error")
    status = (result or {}).get("status", "")
    if err:
        return f" - {err}"
    if status and status != "completed":
        return f" - {status}"
    return ""


def _parse_args(text):
    """API action arguments: JSON object, or key=value pairs separated by ; or newlines."""
    text = (text or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("JSON arguments must be an object")
        return value
    out = {}
    for part in re.split(r"[;\n]", text):
        if not part.strip():
            continue
        if "=" not in part:
            raise ValueError(f"expected name=value, got '{part.strip()}'")
        key, value = part.split("=", 1)
        out[key.strip()] = value.strip()
    return out
