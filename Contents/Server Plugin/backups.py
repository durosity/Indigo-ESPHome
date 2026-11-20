# -*- coding: utf-8 -*-
"""
Dated copies of each board's YAML (and secrets.yaml, when the plugin can read
the Device Builder's config folder directly).

  <root>/<config stem>/20261104-211423.yaml            nightly / manual snapshot
  <root>/<config stem>/20261104-211423-installed.yaml  taken after a successful install
  <root>/_secrets/20261104-030000.yaml
"""

import hashlib
import os
import re
from datetime import datetime

NAME_RE = re.compile(r"^(\d{8}-\d{6})(-installed)?\.yaml$")


class BackupStore:
    def __init__(self, root, keep=30):
        self.root = root
        self.keep = max(5, keep)

    def _dir(self, cfg):
        stem = os.path.splitext(os.path.basename(cfg))[0]
        if not stem or stem.startswith("."):
            raise ValueError("bad configuration name")
        return os.path.join(self.root, stem)

    def list(self, cfg):
        folder = self._dir(cfg)
        if not os.path.isdir(folder):
            return []
        out = []
        for name in sorted(os.listdir(folder), reverse=True):
            m = NAME_RE.match(name)
            if m:
                stamp = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
                out.append({"id": name, "time": stamp.strftime("%Y-%m-%d %H:%M"), "installed": bool(m.group(2)),
                            "size": os.path.getsize(os.path.join(folder, name))})
        return out

    def read(self, cfg, backup_id):
        if not NAME_RE.match(backup_id):
            raise ValueError("bad backup id")
        with open(os.path.join(self._dir(cfg), backup_id), encoding="utf-8") as fh:
            return fh.read()

    def last_installed(self, cfg):
        for item in self.list(cfg):
            if item["installed"]:
                return self.read(cfg, item["id"])
        return None

    def save(self, cfg, content, installed=False):
        """Save if different from the newest copy of the same kind. Returns the id or None."""
        folder = self._dir(cfg)
        os.makedirs(folder, exist_ok=True)
        same_kind = [b for b in self.list(cfg) if b["installed"] == installed]
        if same_kind and _digest(self.read(cfg, same_kind[0]["id"])) == _digest(content):
            return None
        name = datetime.now().strftime("%Y%m%d-%H%M%S") + ("-installed" if installed else "") + ".yaml"
        with open(os.path.join(folder, name), "w", encoding="utf-8") as fh:
            fh.write(content)
        self._prune(cfg)
        return name

    def _prune(self, cfg):
        folder = self._dir(cfg)
        items = self.list(cfg)
        for kind in (True, False):
            for old in [b for b in items if b["installed"] == kind][self.keep:]:
                os.remove(os.path.join(folder, old["id"]))


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
