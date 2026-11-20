# -*- coding: utf-8 -*-
"""
Two-way link between ESPHome boards and Indigo.

Boards read Indigo using ESPHome's standard `homeassistant` platforms - the
plugin answers in Home Assistant's place:

    sensor:
      - platform: homeassistant
        entity_id: device.123456789          # Indigo device id (or device.bedroom_lamp by name)
        attribute: brightnessLevel           # any device state; without it you get on/off
                                             # (or the value, for devices with no on/off state)
      - platform: homeassistant
        entity_id: variable.house_mode       # Indigo variable by name (or variable.123)

Boards act on Indigo with `homeassistant.action` / `homeassistant.event`:

    on_press:
      - homeassistant.action:
          action: indigo.run_action_group
          data: {group: "Goodnight"}
      - homeassistant.event:
          event: esphome.bedside_button      # ESPHome requires the esphome. prefix; fires "ESPHome Board Event"

Both directions are opt-in per board (Share Indigo states / Allow control).
"""

import json
import re

import indigo  # noqa

SLUG_RE = re.compile(r"[^a-z0-9]+")
ACTIONS = ("indigo.run_action_group", "indigo.turn_on", "indigo.turn_off", "indigo.toggle",
           "indigo.set_brightness", "indigo.set_variable", "indigo.log")


def slug(name):
    return SLUG_RE.sub("_", str(name).lower()).strip("_")


def format_value(value):
    """Render an Indigo value the way ESPHome's homeassistant platforms parse it."""
    if value is None:
        return "unavailable"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, float):
        return f"{value:g}"
    text = str(value)
    low = text.strip().lower()
    if low in ("true", "false"):
        return "on" if low == "true" else "off"
    return text


class IndigoLink:
    def __init__(self, plugin):
        self.plugin = plugin
        self.logger = plugin.logger
        self.watch = {}          # ("device"|"variable", indigo id) -> {(board_id, entity_id, attribute)}
        self.last_sent = {}      # (board_id, entity_id, attribute) -> value
        self.subscribed = {"device": False, "variable": False}
        self._denied_logged = set()

    # ------------------------------------------------------------------ #
    # Resolving entity ids
    # ------------------------------------------------------------------ #
    def resolve(self, entity_id):
        try:
            domain, obj = entity_id.split(".", 1)
        except ValueError:
            return None
        if domain == "device":
            collection = indigo.devices
        elif domain == "variable":
            collection = indigo.variables
        else:
            return None
        if obj.isdigit():
            try:
                return domain, collection[int(obj)].id
            except KeyError:
                return None
        for item in collection.iter():
            if slug(item.name) == obj:
                return domain, item.id
        return None

    def current_value(self, domain, item_id, attribute):
        if domain == "variable":
            try:
                return format_value(indigo.variables[item_id].value)
            except KeyError:
                return "unavailable"
        try:
            dev = indigo.devices[item_id]
        except KeyError:
            return "unavailable"
        if attribute:
            if attribute in dev.states:
                return format_value(dev.states[attribute])
            if attribute in ("name", "enabled", "errorState"):
                return format_value(getattr(dev, attribute, None))
            return "unavailable"
        # Home Assistant convention: the main state is on/off where the device has one;
        # numbers like brightness are read with `attribute:` (e.g. brightnessLevel)
        for key in ("onOffState", "sensorValue", "speedLevel", "brightnessLevel"):
            if key in dev.states and dev.states[key] is not None:
                return format_value(dev.states[key])
        display = getattr(dev, "displayStateId", None)
        return format_value(dev.states.get(display)) if display else "unavailable"

    # ------------------------------------------------------------------ #
    # Indigo -> board
    # ------------------------------------------------------------------ #
    def board_subscribed(self, board, entity_id, attribute):
        target = self.resolve(entity_id)
        if target is None:
            self.logger.warning(f"{board.name}: asked for {entity_id} but no matching Indigo "
                                f"{entity_id.split('.')[0]} was found (use its id, e.g. device.123456)")
            board.send_ha_state(entity_id, attribute, "unavailable")
            return
        self.watch.setdefault(target, set()).add((board.dev_id, entity_id, attribute))
        domain = target[0]
        if not self.subscribed[domain]:
            (indigo.devices if domain == "device" else indigo.variables).subscribeToChanges()
            self.subscribed[domain] = True
        self.logger.debug(f"{board.name}: following {entity_id}" + (f" [{attribute}]" if attribute else ""))
        self._send(board, entity_id, attribute, self.current_value(*target, attribute), force=True)

    def push_one(self, board, entity_id, attribute):
        target = self.resolve(entity_id)
        value = self.current_value(*target, attribute) if target else "unavailable"
        self._send(board, entity_id, attribute, value, force=True)

    def indigo_changed(self, domain, item_id):
        """Called from Indigo's thread on deviceUpdated / variableUpdated."""
        subs = self.watch.get((domain, item_id))
        if not subs:
            return
        updates = []
        for board_id, entity_id, attribute in list(subs):
            updates.append((board_id, entity_id, attribute, self.current_value(domain, item_id, attribute)))
        self.plugin.loop.call_soon_threadsafe(self._deliver, updates)

    def _deliver(self, updates):
        for board_id, entity_id, attribute, value in updates:
            board = self.plugin.boards.get(board_id)
            if board is not None and (entity_id, attribute) in board.ha_subscriptions:
                self._send(board, entity_id, attribute, value)

    def _send(self, board, entity_id, attribute, value, force=False):
        key = (board.dev_id, entity_id, attribute)
        if not force and self.last_sent.get(key) == value:
            return
        self.last_sent[key] = value
        board.send_ha_state(entity_id, attribute, value)

    def forget_board(self, board_id):
        for subs in self.watch.values():
            for item in [x for x in subs if x[0] == board_id]:
                subs.discard(item)
        for key in [k for k in self.last_sent if k[0] == board_id]:
            del self.last_sent[key]

    # ------------------------------------------------------------------ #
    # Board -> Indigo
    # ------------------------------------------------------------------ #
    def handle_call(self, board, call):
        data = dict(call.data or {})
        data.update({k: str(v) for k, v in dict(call.data_template or {}).items()})
        name = call.service
        if call.is_event:
            self.plugin.board_event(board, name, data)
            return
        if not board.allow_control:
            if name not in self._denied_logged:
                self.logger.warning(f'{board.name}: ignored {name} - turn on "Allow this board to control Indigo" '
                                    f"in the board's settings")
                self._denied_logged.add(name)
            self._respond(board, call, False, "control not allowed")
            return
        try:
            self._run(board, name, data)
            self._respond(board, call, True)
        except Exception as err:
            self.logger.error(f"{board.name}: {name} failed - {err}")
            self._respond(board, call, False, str(err))

    def _respond(self, board, call, ok, error=""):
        if getattr(call, "wants_response", False) or getattr(call, "call_id", 0):
            try:
                board.client.send_homeassistant_action_response(call.call_id, ok, error)
            except Exception:
                pass

    def _find(self, collection, ref, kind):
        ref = str(ref or "").strip()
        if not ref:
            raise ValueError(f"no {kind} given")
        if ref.isdigit():
            try:
                return collection[int(ref)]
            except KeyError:
                pass
        if ref in collection:
            return collection[ref]
        for item in collection.iter():
            if slug(item.name) == slug(ref):
                return item
        raise ValueError(f'{kind} "{ref}" not found')

    def _run(self, board, name, data):
        if name == "indigo.run_action_group":
            group = self._find(indigo.actionGroups, data.get("group") or data.get("name"), "action group")
            indigo.actionGroup.execute(group.id)
            self.logger.info(f'{board.name}: ran action group "{group.name}"')
        elif name in ("indigo.turn_on", "indigo.turn_off", "indigo.toggle"):
            dev = self._find(indigo.devices, data.get("device"), "device")
            {"indigo.turn_on": indigo.device.turnOn, "indigo.turn_off": indigo.device.turnOff,
             "indigo.toggle": indigo.device.toggle}[name](dev.id)
            self.logger.info(f'{board.name}: {name.split(".")[1].replace("_", " ")} "{dev.name}"')
        elif name == "indigo.set_brightness":
            dev = self._find(indigo.devices, data.get("device"), "device")
            level = max(0, min(100, int(float(data.get("level", data.get("brightness", 100))))))
            indigo.dimmer.setBrightness(dev.id, value=level)
            self.logger.info(f'{board.name}: set "{dev.name}" to {level}%')
        elif name == "indigo.set_variable":
            var = self._find(indigo.variables, data.get("variable"), "variable")
            indigo.variable.updateValue(var.id, value=str(data.get("value", "")))
            self.logger.info(f'{board.name}: set variable "{var.name}"')
        elif name == "indigo.log":
            message = str(data.get("message", ""))
            level = str(data.get("level", "info")).lower()
            {"error": self.logger.error, "warning": self.logger.warning}.get(level, self.logger.info)(
                f"[{board.name}] {message}")
        else:
            raise ValueError(f"unknown action (supported: {', '.join(ACTIONS)})")

    # ------------------------------------------------------------------ #
    def summary(self):
        out = {}
        for (domain, item_id), subs in self.watch.items():
            for board_id, entity_id, attribute in subs:
                out.setdefault(board_id, []).append(entity_id + (f"[{attribute}]" if attribute else ""))
        return out


def event_data_json(data):
    try:
        return json.dumps(data, sort_keys=True)
    except (TypeError, ValueError):
        return str(data)
