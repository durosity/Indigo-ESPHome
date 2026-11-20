# -*- coding: utf-8 -*-
"""First-time USB flashing with esptool, run on the Mac Indigo lives on.

A Device Builder in Docker on macOS cannot see USB serial ports, so the plugin
downloads the compiled factory image and flashes it locally instead.
"""

import threading

_flash_lock = threading.Lock()   # esptool's logger is process-global; one flash at a time


def list_serial_ports():
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    ports = []
    for p in list_ports.comports():
        dev = p.device
        # macOS lists each port as /dev/tty.* and /dev/cu.*; cu.* is the one to use
        if dev.startswith("/dev/tty.") or "Bluetooth" in dev or "debug-console" in dev:
            continue
        ports.append({"device": dev, "description": p.description or "", "usb": bool(p.vid)})
    ports.sort(key=lambda x: (not x["usb"], x["device"]))
    return ports


def flash(port, image_path, on_line, baud=460800):
    """Blocking: write image_path at 0x0. Raises RuntimeError on failure."""
    try:
        import esptool
        from esptool.logger import TemplateLogger
    except ImportError as err:
        raise RuntimeError("esptool is not installed (it is listed in requirements.txt)") from err

    class _Forward(TemplateLogger):
        _last_pct = -10

        def print(self, *args, **kwargs):
            text = " ".join(str(a) for a in args).strip()
            if text:
                on_line(text)

        def note(self, message):
            on_line(f"Note: {message}")

        def warning(self, message):
            on_line(f"Warning: {message}")

        def error(self, message):
            on_line(f"Error: {message}")

        def stage(self, finish=False):
            pass

        def progress_bar(self, cur_iter, total_iters, prefix="", suffix="", bar_length=30):
            pct = int(cur_iter * 100 / total_iters) if total_iters else 100
            if pct >= self._last_pct + 10 or pct == 100:
                self._last_pct = pct
                on_line(f"Writing... {pct}%")

        def set_verbosity(self, verbosity):
            pass

    with _flash_lock:
        # `log` is a proxy to whichever logger is installed. Install ours through
        # esptool's own logger and put that back afterwards, so a second flash in
        # the same session finds esptool's logger again (not ours).
        from esp_pylib.logger import EspLog
        from esptool.logger import EsptoolLogger
        original = EspLog.instance
        if not isinstance(original, EsptoolLogger):
            original = EsptoolLogger()
        original.set_logger(_Forward())
        try:
            esptool.main(["--port", port, "--baud", str(baud), "write-flash", "-z", "0x0", image_path])
        except esptool.FatalError as err:
            raise RuntimeError(str(err).strip().splitlines()[0]) from err
        except SystemExit as err:
            if err.code not in (0, None):
                raise RuntimeError(f"esptool exited with code {err.code}") from err
        finally:
            EspLog.instance = original
