"""Speaker details that PipeWire doesn't expose.

* ``AirPlayBrowser`` reads the ``_airplay._tcp`` mDNS records to learn each
  speaker's model and whether HomePods are stereo-paired in Apple Home.
* ``SonosManager`` wraps SoCo to read and change Sonos stereo pairs.

Both are optional: if zeroconf or soco are missing, Roomcast still works
with plain names.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger("roomcast")

try:
    from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf
except ImportError:  # pragma: no cover
    Zeroconf = None

try:
    import soco
except ImportError:  # pragma: no cover
    soco = None

APPLE_MODELS = {
    "AudioAccessory1,1": "HomePod",
    "AudioAccessory1,2": "HomePod",
    "AudioAccessory5,1": "HomePod mini",
    "AudioAccessory6,1": "HomePod (2nd generation)",
}


def friendly_model(info: dict) -> str:
    model = info.get("model", "")
    maker = info.get("manufacturer", "")
    if model in APPLE_MODELS:
        return APPLE_MODELS[model]
    if model.startswith("AppleTV"):
        return "Apple TV"
    if maker and maker.lower() not in model.lower():
        return f"{maker} {model}".strip()
    return model or "AirPlay speaker"


def is_sonos(info: dict) -> bool:
    return info.get("manufacturer", "").lower() == "sonos"


ACCESS_OK = "ok"
ACCESS_FORBIDDEN = "forbidden"  # HomePod/Apple TV limited to people in the Home
ACCESS_PASSWORD = "password"  # speaker wants an AirPlay password


def probe_access(ip: str, port: int = 7000) -> str | None:
    """Ask a speaker whether it will accept AirPlay from this computer.

    Sends one RTSP OPTIONS request, the same first step every AirPlay
    sender makes. It doesn't start playback or interrupt the speaker.
    Blocking; run it in a worker thread.
    """
    import socket

    try:
        with socket.create_connection((ip, port), timeout=3) as s:
            s.sendall(b"OPTIONS * RTSP/1.0\r\nCSeq: 1\r\nUser-Agent: Roomcast\r\n\r\n")
            s.settimeout(3)
            status = s.recv(512).decode(errors="replace").split("\r\n", 1)[0]
    except OSError:
        return None
    parts = status.split()
    code = parts[1] if len(parts) > 1 else ""
    if code == "200":
        return ACCESS_OK
    if code == "403":
        return ACCESS_FORBIDDEN
    if code == "401":
        return ACCESS_PASSWORD
    return None


class AirPlayBrowser:
    """Collects mDNS TXT records keyed by host name ("Kitchen.local")."""

    def __init__(self, on_change):
        self.on_change = on_change
        self.by_host: dict[str, dict] = {}
        self.by_ip: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._zc = None
        if Zeroconf is None:
            return
        try:
            self._zc = Zeroconf()
            self._browser = ServiceBrowser(
                self._zc, "_airplay._tcp.local.", handlers=[self._handler]
            )
        except Exception:  # noqa: BLE001
            log.exception("mDNS browsing unavailable")
            self._zc = None

    def _handler(self, zeroconf, service_type, name, state_change):
        if state_change is ServiceStateChange.Removed:
            return
        threading.Thread(
            target=self._resolve, args=(zeroconf, service_type, name), daemon=True
        ).start()

    def _resolve(self, zc, service_type, name):
        info = zc.get_service_info(service_type, name, 3000)
        if not info:
            return
        txt = {}
        for k, v in (info.properties or {}).items():
            try:
                txt[k.decode()] = (v or b"").decode(errors="replace")
            except AttributeError:
                continue
        txt["_name"] = name.split("._airplay")[0]
        host = (info.server or "").rstrip(".")
        with self._lock:
            if host:
                self.by_host[host.lower()] = txt
            for ip in info.parsed_addresses():
                self.by_ip[ip] = txt
        self.on_change()

    def lookup(self, key: str, ip: str) -> dict:
        with self._lock:
            return self.by_host.get(key.lower()) or self.by_ip.get(ip) or {}

    def close(self):
        if self._zc:
            self._zc.close()


class SonosZone:
    def __init__(self, player, partner=None):
        self.player = player  # the visible (coordinator) speaker
        self.partner = partner  # hidden half of a stereo pair, if any
        self.name = player.player_name
        self.ip = player.ip_address
        try:
            self.model = player.speaker_info.get("model_name", "Sonos")
        except Exception:  # noqa: BLE001
            self.model = "Sonos"

    @property
    def is_pair(self) -> bool:
        return self.partner is not None


class SonosManager:
    available = soco is not None

    def discover(self) -> list[SonosZone]:
        """Blocking; call from a worker thread."""
        if soco is None:
            return []
        players = soco.discover(timeout=4) or set()
        if not players:
            return []
        any_player = next(iter(players))
        zones = []
        all_players = list(any_player.all_zones)
        for p in sorted(all_players, key=lambda p: p.player_name):
            if not p.is_visible:
                continue
            partner = None
            cmap = getattr(p, "_channel_map", None)
            if cmap and not getattr(p, "has_satellites", False):
                for other in all_players:
                    if other is not p and getattr(other, "_channel_map", None) == cmap:
                        partner = other
                        break
            zones.append(SonosZone(p, partner))
        return zones

    @staticmethod
    def create_pair(left: SonosZone, right: SonosZone):
        """Bond two Sonos speakers. The left one becomes the visible unit."""
        left.player.create_stereo_pair(right.player)

    @staticmethod
    def separate(zone: SonosZone):
        zone.player.separate_stereo_pair()
