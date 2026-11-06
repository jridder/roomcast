"""Thin helpers around the PipeWire command line tools.

Everything here works on a snapshot of the graph taken with ``pw-dump``.
Changes are made with ``pw-cli``, ``pw-link``, ``pw-metadata`` and ``wpctl``,
so nothing links against libpipewire directly.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
from dataclasses import dataclass, field

OUTPUT_PREFIX = "roomcast."
DISCOVERY_APP_NAME = "Roomcast speaker discovery"

# raop_sink.<host>.<ip>.<port>, e.g. raop_sink.Office-Left.local.192.168.3.177.7000
_RAOP_RE = re.compile(
    r"^raop_sink\.(?P<host>.+?)\.(?P<ip>\d+(?:\.\d+){3}|[0-9A-Fa-f]*:[0-9A-Fa-f:]*)\.(?P<port>\d+)$"
)


@dataclass
class Node:
    id: int
    props: dict
    state: str = ""
    volume: float | None = None  # 0..1 on the cubic scale wpctl and GNOME use
    mute: bool = False
    in_ports: dict = field(default_factory=dict)  # channel -> port id
    out_ports: dict = field(default_factory=dict)  # port name -> port id

    @property
    def name(self) -> str:
        return self.props.get("node.name", "")

    @property
    def description(self) -> str:
        return (
            self.props.get("node.description")
            or self.props.get("application.name")
            or self.props.get("node.nick")
            or self.name
        )

    @property
    def media_class(self) -> str:
        return self.props.get("media.class", "")


@dataclass
class Speaker:
    """A network speaker exposed by the RAOP (AirPlay) discovery module."""

    key: str  # stable mDNS host name, e.g. "Office-Left.local"
    ip: str
    node: Node

    @property
    def name(self) -> str:
        return self.node.description


@dataclass
class Link:
    id: int
    out_node: int
    out_port: int
    in_node: int
    in_port: int


class Graph:
    def __init__(self, objects: list):
        self.nodes: dict[int, Node] = {}
        self.links: dict[int, Link] = {}
        self.clients: list[dict] = []
        self.default_sink_name: str | None = None
        self.configured_sink_name: str | None = None
        self.targets: dict[int, str] = {}  # stream id -> target.object
        ports = []
        for o in objects:
            t = o.get("type", "")
            info = o.get("info") or {}
            if t.endswith(":Node"):
                n = Node(o["id"], info.get("props") or {}, info.get("state") or "")
                props_params = (info.get("params") or {}).get("Props") or []
                for p in props_params:
                    vols = p.get("channelVolumes")
                    if vols:
                        lin = sum(vols) / len(vols)
                        n.volume = round(math.pow(max(lin, 0.0), 1 / 3), 3)
                    if "mute" in p:
                        n.mute = bool(p["mute"])
                self.nodes[n.id] = n
            elif t.endswith(":Port"):
                ports.append(o)
            elif t.endswith(":Link"):
                self.links[o["id"]] = Link(
                    o["id"],
                    info.get("output-node-id"),
                    info.get("output-port-id"),
                    info.get("input-node-id"),
                    info.get("input-port-id"),
                )
            elif t.endswith(":Client"):
                self.clients.append(info.get("props") or {})
            elif t.endswith(":Metadata"):
                self._read_metadata(o)
        for o in ports:
            info = o.get("info") or {}
            props = info.get("props") or {}
            node = self.nodes.get(props.get("node.id"))
            if not node:
                continue
            if info.get("direction") == "input":
                ch = props.get("audio.channel") or props.get("port.name", "")
                node.in_ports[ch] = o["id"]
            else:
                node.out_ports[props.get("port.name", "")] = o["id"]

    def _read_metadata(self, o):
        props = o.get("props") or {}
        if props.get("metadata.name") != "default":
            return
        for item in o.get("metadata") or []:
            key, value, subject = item.get("key"), item.get("value"), item.get("subject")
            if key in ("default.audio.sink", "default.configured.audio.sink"):
                name = value.get("name") if isinstance(value, dict) else None
                if key == "default.audio.sink":
                    self.default_sink_name = name
                else:
                    self.configured_sink_name = name
            elif key == "target.object" and subject is not None:
                if isinstance(value, dict):
                    value = value.get("name") or value.get("value")
                self.targets[subject] = str(value)

    # ----- queries -----------------------------------------------------

    def node_by_name(self, name: str) -> Node | None:
        for n in self.nodes.values():
            if n.name == name:
                return n
        return None

    def sinks(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.media_class == "Audio/Sink"]

    def speakers(self) -> dict[str, Speaker]:
        """Discovered AirPlay speakers keyed by host name (IPv4 preferred)."""
        result: dict[str, Speaker] = {}
        for n in self.sinks():
            m = _RAOP_RE.match(n.name)
            if not m:
                continue
            key, ip = m.group("host"), m.group("ip")
            prev = result.get(key)
            if prev is None or (":" in prev.ip and ":" not in ip):
                result[key] = Speaker(key, ip, n)
        return result

    def outputs(self) -> dict[str, Node]:
        """Roomcast virtual output sinks keyed by output id."""
        return {
            n.name[len(OUTPUT_PREFIX):]: n
            for n in self.sinks()
            if n.name.startswith(OUTPUT_PREFIX)
        }

    def local_sinks(self) -> list[Node]:
        return [
            n for n in self.sinks()
            if not n.name.startswith(OUTPUT_PREFIX) and not _RAOP_RE.match(n.name)
        ]

    def app_streams(self) -> list[Node]:
        out = []
        for n in self.nodes.values():
            if n.media_class != "Stream/Output/Audio":
                continue
            # Internal streams created by PipeWire modules (combine, loopback, ...)
            if n.props.get("node.virtual") or n.props.get("stream.monitor"):
                continue
            if n.name.startswith(("output.", "roomcast")):
                continue
            out.append(n)
        return sorted(out, key=lambda n: n.id)

    def linked_sink(self, node: Node) -> Node | None:
        for link in self.links.values():
            if link.out_node == node.id:
                target = self.nodes.get(link.in_node)
                if target is not None:
                    return target
        return None

    def links_from_node(self, node_id: int) -> list[Link]:
        return [l for l in self.links.values() if l.out_node == node_id]

    def links_to_node(self, node_id: int) -> list[Link]:
        return [l for l in self.links.values() if l.in_node == node_id]

    def discovery_running(self) -> bool:
        return any(c.get("application.name") == DISCOVERY_APP_NAME for c in self.clients)


# ----- reading ---------------------------------------------------------


def dump() -> Graph:
    out = subprocess.run(["pw-dump", "--no-colors"], capture_output=True, text=True, timeout=10)
    return Graph(json.loads(out.stdout or "[]"))


def parse_dump(text: str) -> Graph:
    return Graph(json.loads(text or "[]"))


# ----- writing ---------------------------------------------------------


def _run(*args: str) -> bool:
    try:
        r = subprocess.run(list(args), capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _spa_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def create_output_sink(output_id: str, description: str) -> bool:
    props = {
        "factory.name": "support.null-audio-sink",
        "node.name": OUTPUT_PREFIX + output_id,
        "node.description": description,
        "media.class": "Audio/Sink",
        "audio.position": "[ FL FR ]",
        "monitor.channel-volumes": "true",
        # Links out of the monitor ports are passive: while no app plays to
        # this output, it and its speakers suspend instead of streaming
        # silence to them (which would also hold the HomePods/Sonos busy).
        "node.passive": "out",
        "object.linger": "true",
        "device.icon-name": "audio-speakers",
        "roomcast.output": "true",
    }
    body = " ".join(
        f"{k}={v if v.startswith('[') or v in ('true', 'false') else _spa_str(v)}"
        for k, v in props.items()
    )
    return _run("pw-cli", "create-node", "adapter", "{ " + body + " }")


def destroy(object_id: int) -> bool:
    return _run("pw-cli", "destroy", str(object_id))


def link(out_port: int, in_port: int) -> bool:
    return _run("pw-link", str(out_port), str(in_port))


def unlink(link_id: int) -> bool:
    return _run("pw-link", "-d", str(link_id))


def set_volume(node_id: int, volume: float) -> bool:
    return _run("wpctl", "set-volume", str(node_id), f"{max(0.0, min(volume, 1.0)):.3f}")


def set_mute(node_id: int, mute: bool) -> bool:
    return _run("wpctl", "set-mute", str(node_id), "1" if mute else "0")


def set_default_sink(node_id: int) -> bool:
    return _run("wpctl", "set-default", str(node_id))


def route_stream(stream_id: int, sink: Node | None) -> bool:
    """Send a playing stream to a sink, or back to the default when None.

    The target is written as the sink's object.serial with type Spa:Id:
    that is the form WirePlumber stores, so the app goes back to the same
    place the next time it plays. A node name moves the stream but isn't
    remembered.
    """
    if sink is not None:
        serial = sink.props.get("object.serial")
        if serial is not None:
            return _run("pw-metadata", str(stream_id), "target.object", str(serial), "Spa:Id")
        return _run("pw-metadata", str(stream_id), "target.object", sink.name)
    return _run("pw-metadata", "-d", str(stream_id), "target.object")
