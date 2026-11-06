"""Roomcast background service.

* Runs PipeWire's AirPlay (RAOP) discovery module in a child ``pipewire``
  process, so every speaker on the network shows up as a sink.
* Keeps one virtual sink per configured output ("roomcast.<id>"). Apps and
  GNOME Settings see these as ordinary sound devices.
* Links each output's monitor channels to its speakers: stereo speakers get
  L->L and R->R, the left half of a pair gets L on both inputs, the right
  half gets R on both inputs.

The virtual sinks are never torn down while the service runs, so adding or
removing speakers never makes a playing app jump to the computer's own
speakers.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import config, pw

log = logging.getLogger("roomcastd")

DISCOVERY_CONF = """\
context.properties = {{
    application.name = "{app}"
    log.level = 1
}}
context.spa-libs = {{
    audio.convert.* = audioconvert/libspa-audioconvert
    support.*       = support/libspa-support
}}
context.modules = [
    {{ name = libpipewire-module-rt flags = [ ifexists nofail ] }}
    {{ name = libpipewire-module-protocol-native }}
    {{ name = libpipewire-module-client-node }}
    {{ name = libpipewire-module-adapter }}
    {{ name = libpipewire-module-raop-discover
      args = {{
        stream.rules = [
          {{ matches = [ {{ raop.ip = "~.*" }} ]
            actions = {{ create-stream = {{ }} }} }}
        ]
      }}
    }}
]
"""


MODULE_DIRS = (
    "/usr/lib/x86_64-linux-gnu/pipewire-0.3",
    "/usr/lib/aarch64-linux-gnu/pipewire-0.3",
    "/usr/lib64/pipewire-0.3",
    "/usr/lib/pipewire-0.3",
)
RAOP_SINK = "libpipewire-module-raop-sink.so"


def find_module_dir() -> Path | None:
    for d in MODULE_DIRS:
        if (Path(d) / RAOP_SINK).exists():
            return Path(d)
    for p in Path("/usr/lib").glob(f"*/pipewire-0.3/{RAOP_SINK}"):
        return p.parent
    return None


def prepare_module_dir(runtime: Path) -> tuple[Path | None, str]:
    """Build a private PipeWire module directory for the discovery process.

    HomePods answer "403 Forbidden" to any AirPlay sender whose User-Agent
    isn't an Apple one, and PipeWire's RAOP module always sends
    "PipeWire/<version>". This makes a copy of that module with the string
    swapped for an "iTunes/..." one of the same length. Every other module is
    a symlink to the system copy, and the system files stay untouched. The
    copy is rebuilt on each start, so PipeWire upgrades are picked up.

    Returns (module dir or None to use the system one, user agent in use).
    """
    import re

    sysdir = find_module_dir()
    if sysdir is None:
        return None, "PipeWire"
    data = (sysdir / RAOP_SINK).read_bytes()
    matches = list(re.finditer(rb"PipeWire/[0-9][0-9.]*\x00", data))
    if len(matches) != 1:
        log.warning("couldn't find PipeWire's AirPlay user agent; HomePods may refuse to play")
        return None, "PipeWire"
    old = matches[0].group(0)[:-1]
    version = old[len(b"PipeWire/"):]
    new = (b"iTunes/" + version + b"0" * 16)[: len(old)]

    moddir = runtime / "modules"
    if moddir.exists():
        for p in moddir.iterdir():
            p.unlink()
    moddir.mkdir(parents=True, exist_ok=True)
    for p in sysdir.iterdir():
        if p.name != RAOP_SINK:
            (moddir / p.name).symlink_to(p)
    (moddir / RAOP_SINK).write_bytes(data.replace(old + b"\x00", new + b"\x00"))
    agent = new.decode()
    log.info("AirPlay user agent: %s", agent)
    return moddir, agent


class Daemon:
    def __init__(self):
        self.child: subprocess.Popen | None = None
        self.child_started = 0.0
        self.cfg = config.load()
        self.cfg_mtime = config.mtime()
        self.running = True
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "roomcast"
        runtime.mkdir(parents=True, exist_ok=True)
        self.conf_path = runtime / "discovery.conf"
        self.runtime = runtime

    # ----- discovery child --------------------------------------------

    def ensure_discovery(self):
        if self.child and self.child.poll() is None:
            return
        if self.child is not None:
            log.warning("discovery process exited with %s; restarting", self.child.returncode)
            if time.monotonic() - self.child_started < 5:
                return  # back off; try again on a later tick
        self.conf_path.write_text(DISCOVERY_CONF.format(app=pw.DISCOVERY_APP_NAME))
        env = dict(os.environ)
        try:
            moddir, agent = prepare_module_dir(self.runtime)
        except OSError:
            log.exception("couldn't prepare AirPlay module")
            moddir, agent = None, "PipeWire"
        if moddir is not None:
            env["PIPEWIRE_MODULE_DIR"] = str(moddir)
        (self.runtime / "user-agent").write_text(agent)
        self.child = subprocess.Popen(
            ["pipewire", "-c", str(self.conf_path)],
            stdin=subprocess.DEVNULL,
            env=env,
        )
        self.child_started = time.monotonic()
        log.info("started speaker discovery (pid %s)", self.child.pid)

    def stop_discovery(self):
        if self.child and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(5)
            except subprocess.TimeoutExpired:
                self.child.kill()

    # ----- reconcile ---------------------------------------------------

    def reload_config(self):
        m = config.mtime()
        if m != self.cfg_mtime:
            self.cfg_mtime = m
            self.cfg = config.load()
            log.info("configuration reloaded")
            return True
        return False

    def reconcile(self, g: pw.Graph) -> bool:
        """Make the graph match the config. Returns True if anything changed."""
        changed = False
        wanted = {o["id"]: o for o in config.all_outputs(self.cfg)}
        existing = g.outputs()

        for oid, node in existing.items():
            out = wanted.get(oid)
            if out is None:
                log.info("removing output %s", node.description)
                pw.destroy(node.id)
                changed = True
            elif node.description != out["name"] and node.state != "running":
                # Renames need a new node; only do it while nothing is playing.
                log.info("renaming output %s -> %s", node.description, out["name"])
                pw.destroy(node.id)
                changed = True

        for oid, out in wanted.items():
            if oid not in existing:
                log.info("creating output %s", out["name"])
                pw.create_output_sink(oid, out["name"])
                changed = True

        if changed:
            return True  # links are done on the next pass, once nodes exist

        speakers = g.speakers()
        desired: set[tuple[int, int]] = set()
        managed_ports: set[int] = set()
        for oid, node in existing.items():
            out = wanted.get(oid)
            if out is None:
                continue
            src = {"FL": node.out_ports.get("monitor_FL"), "FR": node.out_ports.get("monitor_FR")}
            managed_ports.update(p for p in src.values() if p)
            if not all(src.values()):
                continue
            for key, role in config.flatten_members(self.cfg, out):
                spk = speakers.get(key)
                if spk is None:
                    continue
                desired |= _port_pairs(src, spk.node.in_ports, role)

        current = {
            (l.out_port, l.in_port): l.id
            for l in g.links.values()
            if l.out_port in managed_ports
        }
        for pair, link_id in current.items():
            if pair not in desired:
                pw.unlink(link_id)
                changed = True
        for out_port, in_port in desired - current.keys():
            pw.link(out_port, in_port)
            changed = True
        return changed

    def teardown(self):
        try:
            g = pw.dump()
            for node in g.outputs().values():
                pw.destroy(node.id)
        except Exception:  # noqa: BLE001 - best effort on shutdown
            log.exception("teardown failed")
        self.stop_discovery()

    def run(self):
        def stop(*_):
            self.running = False

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGHUP, lambda *_: setattr(self, "cfg_mtime", -1))
        log.info("Roomcast service starting")
        while self.running:
            try:
                self.ensure_discovery()
                self.reload_config()
                g = pw.dump()
                if self.reconcile(g):
                    time.sleep(0.3)
                    continue
            except Exception:  # noqa: BLE001 - keep the service alive
                log.exception("reconcile failed")
            time.sleep(1.0)
        log.info("Roomcast service stopping")
        self.teardown()


def _port_pairs(src: dict, dst: dict, role: str) -> set[tuple[int, int]]:
    if not dst:
        return set()
    if role == "left":
        return {(src["FL"], p) for p in dst.values()}
    if role == "right":
        return {(src["FR"], p) for p in dst.values()}
    if "FL" in dst and "FR" in dst:
        return {(src["FL"], dst["FL"]), (src["FR"], dst["FR"])}
    return {(s, p) for s in src.values() for p in dst.values()}  # mono speaker


def main():
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s"
    )
    Daemon().run()


if __name__ == "__main__":
    main()
