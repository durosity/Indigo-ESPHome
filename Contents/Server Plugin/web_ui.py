# -*- coding: utf-8 -*-
"""
Board setup page, served by the Indigo Web Server.

  GET  /message/<pluginId>/ui/            -> the page (web_ui.html)
  GET  /message/<pluginId>/api/?op=...    -> JSON reads
  POST /message/<pluginId>/api/?op=...    -> JSON writes (JSON request body)

Indigo authenticates every request before it reaches the plugin, so the page
is only reachable by someone already logged in to this Indigo server.
"""

import json
import os
import re

import indigo  # noqa

import usb_flash
from discovery import discover

PAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_ui.html")
NODE_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,22}[a-z0-9])?$")
JOB_MODES = ("validate", "compile", "install", "usb")


def _reply(status, content, content_type="application/json"):
    reply = indigo.Dict()
    reply["status"] = status
    reply["headers"] = indigo.Dict({"Content-Type": f"{content_type}; charset=utf-8", "Cache-Control": "no-store"})
    reply["content"] = content
    return reply


def _json(status, payload):
    return _reply(status, json.dumps(payload))


def _request_args(props):
    """Merge query args, form params and a JSON body, whichever IWS supplied."""
    args = {}
    query = props.get("url_query_args") or {}
    try:
        args.update({k: v for k, v in dict(query).items()})
    except (TypeError, ValueError):
        pass
    for key in ("body_params",):
        value = props.get(key)
        if value:
            try:
                args.update(dict(value))
            except (TypeError, ValueError):
                pass
    for key in ("request_body", "body"):
        raw = props.get(key)
        if not raw:
            continue
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        if isinstance(raw, str):
            try:
                body = json.loads(raw)
            except ValueError:
                continue
        else:
            try:
                body = dict(raw)
            except (TypeError, ValueError):
                continue
        if isinstance(body, dict):
            args.update(body)
        break
    return args


def serve_page(plugin, action):
    try:
        with open(PAGE_PATH, encoding="utf-8") as fh:
            return _reply(200, fh.read(), "text/html")
    except OSError as err:
        return _reply(500, f"Cannot load page: {err}", "text/plain")


def serve_api(plugin, action):
    props = action.props
    method = str(props.get("incoming_request_method", "GET")).upper()
    args = _request_args(props)
    op = str(args.get("op", ""))
    try:
        handler = ROUTES.get((method, op))
        if handler is None:
            if plugin.debug:
                plugin.logger.debug(f"Board setup page: unknown {method} {op!r}, props keys {list(props.keys())}")
            return _json(404, {"error": f"unknown request {method} {op}"})
        if plugin.builder is None and op not in ("status", "ports", "logs", "discover", "add_discovered"):
            return _json(409, {"error": "Enable the Device Builder integration in the plugin's configuration first"})
        return _json(200, handler(plugin, args))
    except ValueError as err:
        return _json(400, {"error": str(err)})
    except Exception as err:   # Device Builder errors, timeouts...
        plugin.logger.debug("Board setup page request failed", exc_info=True)
        return _json(502, {"error": str(err) or err.__class__.__name__})


# ---------------------------------------------------------------------- #
def _call(plugin, coro, timeout=60):
    return plugin.run_coro(coro, timeout=timeout)


def _job_summary(job, with_lines=False):
    out = {k: job[k] for k in ("id", "cfg", "mode", "status", "note", "created", "port")}
    if with_lines:
        out["lines"] = list(job["lines"])
    return out


def op_status(plugin, args):
    status = {"builder": plugin.builder is not None, "configs": [], "jobs": [],
              "ports": usb_flash.list_serial_ports(), "error": ""}
    if plugin.builder is not None:
        try:
            devices = _call(plugin, plugin.builder.list_devices(), 30)
            plugin.db_devices = devices
        except Exception as err:
            status["error"] = str(err)
            devices = []
        by_cfg = {}
        for board in plugin.boards.values():
            cfg = board.config_override or board.db_config
            if cfg:
                by_cfg[cfg] = board
        for d in devices:
            board = by_cfg.get(d.get("configuration"))
            if board is None:
                board = next((b for b in plugin.boards.values()
                              if not b.config_override and b.node_name and b.node_name == d.get("name")), None)
            if board is None:   # in Indigo but not started (disabled, or just created)
                dev = plugin.core_device_for_config(d.get("configuration"))
                indigo_info = None if dev is None else {"deviceId": dev.id, "name": dev.name, "connected": False}
            else:
                indigo_info = {"deviceId": board.dev_id, "name": board.name, "connected": board.connected,
                               "drops24h": board._drops_24h(), "dropsTotal": board.disconnect_count,
                               "bluetooth": board.ble_capable(), "follows": len(board.ha_subscriptions),
                               "shareStates": board.share_states, "allowControl": board.allow_control}
            runtime = d.get("runtime_state") or {}
            status["configs"].append({
                "configuration": d.get("configuration", ""),
                "name": d.get("name", ""),
                "friendlyName": d.get("friendly_name", "") or d.get("name", ""),
                "platform": d.get("target_platform", ""),
                "address": d.get("address", ""),
                "reachability": runtime.get("state", "unknown"),
                "deployedVersion": runtime.get("deployed_version", ""),
                "currentVersion": d.get("current_version", ""),
                "updateAvailable": bool(d.get("update_available")),
                "pendingChanges": bool(d.get("has_pending_changes")),
                "indigo": indigo_info,
            })
        status["configs"].sort(key=lambda c: c["friendlyName"].lower())
    status["jobs"] = [_job_summary(j) for j in reversed(list(plugin.jobs.values()))][:8]
    return status


def op_ports(plugin, args):
    return {"ports": usb_flash.list_serial_ports()}


def op_boards(plugin, args):
    return {"boards": _call(plugin, plugin.builder.search_boards(str(args.get("q", "")).strip()), 30)}


def op_create(plugin, args):
    name = str(args.get("name", "")).strip().lower()
    if not NODE_NAME_RE.match(name):
        raise ValueError("Node name must be lowercase letters, numbers and hyphens (max 24), e.g. bedroom-sensor")
    board_id = str(args.get("boardId", "")).strip()
    if not board_id:
        raise ValueError("Choose the board type")
    result = _call(plugin, plugin.builder.create_device(
        name, str(args.get("friendlyName", "")).strip(), board_id,
        str(args.get("ssid", "")).strip(), str(args.get("psk", ""))), 120)
    plugin.logger.info(f"Board setup: created Device Builder configuration {result.get('configuration')}")
    return {"configuration": result.get("configuration", ""), "warning": result.get("warning") or ""}


def _cfg(args):
    cfg = str(args.get("cfg", "")).strip()
    if not cfg or "/" in cfg or cfg.startswith("."):
        raise ValueError("missing or invalid configuration name")
    return cfg


def op_yaml(plugin, args):
    return {"content": _call(plugin, plugin.builder.get_config(_cfg(args)), 30) or ""}


def op_save(plugin, args):
    content = str(args.get("content", ""))
    if not content.strip():
        raise ValueError("Refusing to save an empty configuration")
    expected = args.get("expected")
    _call(plugin, plugin.builder.update_config(_cfg(args), content, expected if isinstance(expected, str) else None), 30)
    return {"saved": True}


def op_start_job(plugin, args):
    cfg = _cfg(args)
    mode = str(args.get("mode", ""))
    if mode not in JOB_MODES:
        raise ValueError(f"mode must be one of {', '.join(JOB_MODES)}")
    port = str(args.get("port", "")).strip() or None
    if mode == "usb":
        if not port or port not in {p["device"] for p in usb_flash.list_serial_ports()}:
            raise ValueError("Choose the USB serial port the board is plugged into")
    board = next((b for b in plugin.boards.values() if cfg in (b.db_config, b.config_override)), None)
    job = plugin.new_job(cfg, mode, board=board, port=port)
    plugin.logger.info(f"Board setup: {mode} {cfg} queued")
    plugin._background(plugin.run_job(job), f"{cfg}: {mode}")
    return {"jobId": job["id"]}


def op_job(plugin, args):
    job = plugin.jobs.get(str(args.get("id", "")))
    if job is None:
        raise ValueError("unknown job")
    return _job_summary(job, with_lines=True)


def op_adopt(plugin, args):
    return _call(plugin, plugin.adopt(_cfg(args), str(args.get("name", "")).strip()), 30)


# ---------------------------------------------------------------------- #
# Wi-Fi and secrets
def op_secrets(plugin, args):
    return {"names": _call(plugin, plugin.builder.secret_names(), 30)}


def op_wifi(plugin, args):
    ssid = str(args.get("ssid", "")).strip()
    if not ssid:
        raise ValueError("Enter the Wi-Fi network name")
    password = str(args.get("password", ""))
    if password and not 8 <= len(password) <= 63:
        raise ValueError("Wi-Fi passwords are 8-63 characters")
    _call(plugin, plugin.builder.set_wifi(ssid, password), 30)
    plugin.logger.info(f"Board setup: Wi-Fi secrets updated (network {ssid})")
    return {"saved": True}


def op_secret(plugin, args):
    key = str(args.get("key", "")).strip()
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$", key):
        raise ValueError("Secret names use letters, numbers and underscores, e.g. ota_password")
    _call(plugin, plugin.builder.set_secret(key, str(args.get("value", ""))), 30)
    return {"saved": True}


# ---------------------------------------------------------------------- #
# Live logs from a board connected to Indigo
def _board(plugin, args):
    try:
        board = plugin.boards.get(int(args.get("deviceId", 0)))
    except (TypeError, ValueError):
        board = None
    if board is None:
        raise ValueError("That board isn't in Indigo yet - add it to Indigo to see its live logs")
    return board


def op_logs(plugin, args):
    board = _board(plugin, args)
    plugin.loop.call_soon_threadsafe(board.watch_logs, 90)
    try:
        since = int(args.get("since", 0))
    except (TypeError, ValueError):
        since = 0
    lines = [{"seq": q, "time": t, "level": lvl, "text": txt} for q, t, lvl, txt in list(board.live_log) if q > since]
    return {"connected": board.connected, "lines": lines[-400:], "last": board.live_log_seq}


# ---------------------------------------------------------------------- #
# Discovery
def op_discover(plugin, args):
    found = _call(plugin, discover(4.0), 20)
    configs = {d.get("name"): d.get("configuration") for d in (plugin.db_devices or [])}
    in_indigo = {b.node_name for b in plugin.boards.values()}
    hosts = {(d.pluginProps.get("address", "") or "").lower() for d in indigo.devices.iter("self.espCore")}
    for f in found:
        f["inIndigo"] = f["node"] in in_indigo or f["address"].lower() in hosts or f["host"].lower() in hosts
        f["configuration"] = configs.get(f["node"], "")
    return {"boards": found}


def op_add_discovered(plugin, args):
    cfg = str(args.get("cfg", "")).strip()
    if cfg:
        return _call(plugin, plugin.adopt(_cfg(args)), 30)
    node = str(args.get("node", "")).strip()
    address = str(args.get("address", "")).strip()
    if not node or not address:
        raise ValueError("missing board details")
    key = str(args.get("key", "")).strip()
    if key:
        import base64
        try:
            if len(base64.b64decode(key, validate=True)) != 32:
                raise ValueError
        except Exception:
            raise ValueError("The API encryption key should be the 44-character value from the board's YAML")
    return plugin.add_discovered(node, address, key, str(args.get("name", "")).strip())


# ---------------------------------------------------------------------- #
# Backups and the "changes since last install" view
def op_backups(plugin, args):
    return {"backups": plugin.backups.list(_cfg(args))}


def op_backup(plugin, args):
    return {"content": plugin.backups.read(_cfg(args), str(args.get("id", "")))}


def op_last_installed(plugin, args):
    return {"content": plugin.backups.last_installed(_cfg(args))}


def op_restore(plugin, args):
    cfg = _cfg(args)
    content = plugin.backups.read(cfg, str(args.get("id", "")))
    current = _call(plugin, plugin.builder.get_config(cfg), 30)
    if current:
        plugin.backups.save(cfg, current)          # so the restore itself can be undone
    _call(plugin, plugin.builder.update_config(cfg, content), 30)
    plugin.logger.info(f"Board setup: restored {cfg} from backup {args.get('id')}")
    return {"content": content}


def op_backup_now(plugin, args):
    return {"saved": _call(plugin, plugin.backup_all(log=True), 120)}


ROUTES = {
    ("GET", "status"): op_status,
    ("GET", "ports"): op_ports,
    ("GET", "boards"): op_boards,
    ("GET", "yaml"): op_yaml,
    ("GET", "job"): op_job,
    ("POST", "create"): op_create,
    ("POST", "save"): op_save,
    ("POST", "job"): op_start_job,
    ("POST", "adopt"): op_adopt,
    ("GET", "secrets"): op_secrets,
    ("POST", "wifi"): op_wifi,
    ("POST", "secret"): op_secret,
    ("GET", "logs"): op_logs,
    ("GET", "discover"): op_discover,
    ("POST", "add_discovered"): op_add_discovered,
    ("GET", "backups"): op_backups,
    ("GET", "backup"): op_backup,
    ("GET", "last_installed"): op_last_installed,
    ("POST", "restore"): op_restore,
    ("POST", "backup_now"): op_backup_now,
}
