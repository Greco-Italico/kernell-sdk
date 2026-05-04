from __future__ import annotations
import urllib.request
import json
import time
from pathlib import Path
from importlib.metadata import version

CACHE_FILE = Path.home() / ".kernell" / "version_cache.json"
CACHE_TTL = 3600  # 1 hour

class VersionManager:
    def __init__(self, package="kernell-os-sdk"):
        self.package = package

    def current_version(self) -> str:
        try:
            return version(self.package)
        except Exception:
            return "dev"

    def _get_cached_latest(self) -> str | None:
        try:
            if CACHE_FILE.exists():
                data = json.loads(CACHE_FILE.read_text())
                if time.time() - data.get("timestamp", 0) < CACHE_TTL:
                    return data.get("latest_version")
        except Exception:
            pass
        return None

    def _set_cached_latest(self, ver: str):
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps({
                "latest_version": ver,
                "timestamp": time.time()
            }))
        except Exception:
            pass

    def latest_version(self) -> str:
        cached = self._get_cached_latest()
        if cached:
            return cached

        try:
            req = urllib.request.Request(f"https://pypi.org/pypi/{self.package}/json")
            with urllib.request.urlopen(req, timeout=2) as response:
                data = json.loads(response.read().decode())
                latest = data["info"]["version"]
                self._set_cached_latest(latest)
                return latest
        except Exception:
            return self.current_version()

    def has_update(self) -> bool:
        curr = self.current_version()
        if curr == "dev":
            return False
        latest = self.latest_version()
        return curr != latest

    def get_changelog(self) -> list[str]:
        if self.has_update():
            return [
                "Improved routing efficiency and dynamic fallback",
                "New policy model integration (Qwen3-0.6B)",
                "Full interactive CLI and local Web Dashboard",
                "Real baseline benchmark comparisons"
            ]
        return []
