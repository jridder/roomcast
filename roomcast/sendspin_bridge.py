"""Sendspin bridge: plays PipeWire audio on Sendspin speakers.

Sendspin is the streaming protocol from Music Assistant / the Open Home
Foundation. Speakers such as the FutureProofHomes Satellite1 (ESPHome)
use it. This process:

* finds speakers through their ``_sendspin._tcp`` mDNS records;
* gives each one a PipeWire sink named ``sendspin_sink.<key>``, so the
  service treats it like any AirPlay speaker (outputs, pairs, routing);
* captures that sink's audio with ``pw-record`` and pushes it to the
  speaker through an aiosendspin server.

The capture stream is passive, so it only runs while something actually
plays to the speaker. When it stops, the stream to the speaker stops too.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import signal
import sys

from aiosendspin.models.types import ConnectionReason, PlayerCommand
from aiosendspin.noise import Identity, InMemoryServerPairingStore
from aiosendspin.server import (
    AudioFormat,
    ClientAddedEvent,
    ClientConnectedEvent,
    ClientDisconnectedEvent,
    SendspinServer,
)
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from . import config, pw

log = logging.getLogger("roomcast.sendspin")

SERVICE = "_sendspin._tcp.local."
RATE, CHANNELS = 48000, 2
FORMAT = AudioFormat(sample_rate=RATE, bit_depth=16, channels=CHANNELS)
CHUNK = RATE // 50 * CHANNELS * 2  # 20 ms of s16 stereo
IDLE_STOP_S = 1.5  # stop the speaker's stream after this much silence
SERVER_PORT = 8937
IDENTITY_FILE = config.CONFIG_DIR / "sendspin-identity"


def load_identity() -> Identity:
    try:
        return Identity.from_private_bytes(IDENTITY_FILE.read_bytes())
    except (OSError, ValueError):
        ident = Identity.generate()
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        IDENTITY_FILE.write_bytes(ident.private_bytes)
        IDENTITY_FILE.chmod(0o600)
        return ident


def sink_key(instance: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", instance)


class Speaker:
    def __init__(self, bridge: Bridge, key: str, url: str, ip: str, name: str):
        self.bridge = bridge
        self.key = key
        self.url = url
        self.ip = ip
        self.name = name
        self.client_id: str | None = None
        self.sink_created = False
        self.capture: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.stream = None
        self.hw_volume = False  # slider drives the speaker's own volume
        self.last_dev: int | None = None
        self.last_sink: float | None = None
        self.delay_ms = 0  # extra hold-back to line up with AirPlay speakers
        self.stream_delay_ms = 0  # the delay the running stream was started with

    @property
    def sink_name(self) -> str:
        return pw.SENDSPIN_PREFIX + self.key

    def client(self):
        if self.client_id is None:
            return None
        c = self.bridge.server.get_client(self.client_id)
        return c if c is not None and c.connection is not None else None

    def player(self):
        client = self.client()
        if client is None:
            return None
        roles = client.roles_by_family("player")
        return roles[0] if roles else None

    def supports_volume(self) -> bool:
        role = self.player()
        return role is not None and PlayerCommand.VOLUME in role.state_supported_commands

    async def ensure_sink(self):
        if self.sink_created:
            return
        props = {
            "sendspin.ip": self.ip,
            "sendspin.url": self.url,
            "device.product.name": "Sendspin speaker",
        }
        # The speaker reports what it supports in its first state message,
        # shortly after connecting.
        for _ in range(30):
            if self.client() is None or self.supports_volume():
                break
            await asyncio.sleep(0.1)
        if self.sink_created:
            return
        if self.supports_volume():
            # Send full-scale audio and let the slider set the speaker's own
            # volume instead, like AirPlay speakers. Scaling the audio here
            # as well would leave the speaker at its own (often 100%) level.
            self.hw_volume = True
            props["monitor.channel-volumes"] = "false"
        await asyncio.to_thread(pw.create_speaker_sink, self.sink_name, self.name, props)
        self.sink_created = True
        self.task = asyncio.create_task(self.pump())
        log.info("added speaker %s (%s)", self.name, self.url)

    async def start_capture(self):
        self.capture = await asyncio.create_subprocess_exec(
            "pw-record", "--raw", "--target", self.sink_name,
            "-P", "{ stream.capture.sink=true node.passive=true node.dont-fallback=true "
                  "node.dont-reconnect=false media.role=Music "
                  f"node.name=roomcast.capture.{self.key} application.name=Roomcast }}",
            "--rate", str(RATE), "--channels", str(CHANNELS), "--format", "s16", "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def pump(self):
        """Read the sink's audio and push it to the speaker while it flows."""
        # Give the new sink a moment to appear before the capture targets it.
        await asyncio.sleep(1.0)
        while True:
            await self.start_capture()
            assert self.capture.stdout is not None
            try:
                while True:
                    try:
                        data = await asyncio.wait_for(
                            self.capture.stdout.readexactly(CHUNK), IDLE_STOP_S
                        )
                    except TimeoutError:
                        self.stop_stream()
                        continue
                    await self.push(data)
            except asyncio.IncompleteReadError:
                log.warning("capture for %s ended; restarting", self.name)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("streaming to %s failed", self.name)
            finally:
                self.stop_stream()
                with contextlib.suppress(ProcessLookupError):
                    self.capture.kill()
            await asyncio.sleep(2)

    async def push(self, data: bytes):
        client = self.client()
        if client is None:
            # Not connected: ask the speaker to take us as its playback server.
            if not self.bridge.dialing.get(self.url):
                self.bridge.dialing[self.url] = True
                self.bridge.server.connect_to_client(
                    self.url, connection_reason=ConnectionReason.PLAYBACK,
                    retry_initial_connection=True,
                )
            return
        if self.stream is not None and self.stream_delay_ms != self.delay_ms:
            self.stop_stream()  # restart on the new timeline
        play_start = None
        if self.stream is None:
            log.info("playing to %s (delay %d ms)", self.name, self.delay_ms)
            self.stream = client.group.start_stream()
            self.stream.set_live_source(True)
            self.stream_delay_ms = self.delay_ms
            if self.delay_ms:
                # Anchor the first chunk later than the library would; every
                # following chunk continues from there, so the whole stream
                # plays delay_ms later.
                lead = getattr(self.stream, "_min_send_ahead_us", lambda: 1_100_000)()
                play_start = self.stream.now_us() + lead + self.delay_ms * 1000
        try:
            # The capture already delivers audio in real time, so no extra
            # pacing: the speaker plays it about a second behind, steadily.
            self.stream.prepare_audio(data, FORMAT)
            await self.stream.commit_audio(play_start_us=play_start)
        except Exception:  # noqa: BLE001 - stream was stopped under us; restart next chunk
            log.info("stream to %s restarted", self.name)
            self.stream = None

    def stop_stream(self):
        if self.stream is not None:
            log.info("stopped playing to %s", self.name)
            with contextlib.suppress(Exception):
                self.stream.stop()
            self.stream = None

    async def close(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        self.stop_stream()
        if self.capture and self.capture.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.capture.kill()
        if self.sink_created:
            await asyncio.to_thread(pw.destroy_by_name, self.sink_name)


class Bridge:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.server = SendspinServer(
            self.loop,
            load_identity(),
            "Roomcast",
            pairing_store=InMemoryServerPairingStore(),
            # Satellite1 firmware so far speaks the pre-encryption protocol.
            allow_unencrypted=True,
        )
        self.speakers: dict[str, Speaker] = {}  # by url
        self.dialing: dict[str, bool] = {}
        self.server.add_event_listener(self.on_event)

    def load_delays(self):
        m = config.mtime()
        if m == getattr(self, "_cfg_mtime", None):
            return
        self._cfg_mtime = m
        delays = config.load().get("delays", {})
        for spk in self.speakers.values():
            spk.delay_ms = int(delays.get("sendspin:" + spk.key, 0) or 0)

    async def sync_volumes(self):
        """Keep each sink's volume slider and its speaker's own volume in step."""
        while True:
            await asyncio.sleep(1.0)
            self.load_delays()
            speakers = [s for s in self.speakers.values() if s.sink_created and s.hw_volume]
            if not speakers:
                continue
            try:
                graph = await asyncio.to_thread(pw.dump)
            except Exception:  # noqa: BLE001
                continue
            for spk in speakers:
                role = spk.player()
                node = graph.node_by_name(spk.sink_name)
                if role is None or node is None or node.volume is None:
                    continue
                dev = int(role.volume)
                if spk.last_dev is None:
                    # First sight: show the speaker's current volume on the slider.
                    await asyncio.to_thread(pw.set_volume, node.id, dev / 100)
                    spk.last_dev, spk.last_sink = dev, dev / 100
                elif abs(node.volume - (spk.last_sink or 0)) > 0.005:
                    target = max(0, min(100, round(node.volume * 100)))
                    role.set_volume(target)
                    spk.last_dev, spk.last_sink = target, node.volume
                elif dev != spk.last_dev:
                    # Changed on the speaker or from another app.
                    await asyncio.to_thread(pw.set_volume, node.id, dev / 100)
                    spk.last_dev, spk.last_sink = dev, dev / 100

    def on_event(self, server, event):
        cid = getattr(event, "client_id", None)
        if isinstance(event, (ClientAddedEvent, ClientConnectedEvent)) and cid:
            url = server._client_urls.get(cid)  # noqa: SLF001 - no public mapping
            spk = self.speakers.get(url)
            if spk is None:
                return
            spk.client_id = cid
            self.dialing.pop(url, None)
            client = server.get_client(cid)
            if client is not None and getattr(client, "name", None):
                spk.name = client.name
            asyncio.ensure_future(spk.ensure_sink())
        elif isinstance(event, ClientDisconnectedEvent) and cid:
            for spk in self.speakers.values():
                if spk.client_id == cid:
                    spk.stop_stream()

    def on_service(self, zeroconf, service_type, name, state_change):
        if state_change is ServiceStateChange.Removed:
            return
        asyncio.ensure_future(self.resolve(zeroconf, service_type, name))

    async def resolve(self, zc, service_type, name):
        info = AsyncServiceInfo(service_type, name)
        if not await info.async_request(zc, 3000):
            return
        addrs = [a for a in info.parsed_addresses() if ":" not in a] or info.parsed_addresses()
        if not addrs or not info.port:
            return
        props = {k.decode(): (v or b"").decode(errors="replace") for k, v in info.properties.items()}
        path = props.get("path") or "/sendspin"
        host = f"[{addrs[0]}]" if ":" in addrs[0] else addrs[0]
        url = f"ws://{host}:{info.port}{path}"
        instance = name.removesuffix("." + service_type)
        key = sink_key(instance)
        if any(s.key == key for s in self.speakers.values()):
            return
        spk = Speaker(self, key, url, addrs[0], instance)
        spk.delay_ms = int(config.load().get("delays", {}).get("sendspin:" + key, 0) or 0)
        self.speakers[url] = spk
        log.info("found Sendspin speaker %s at %s", instance, url)
        self.server.connect_to_client(url, retry_initial_connection=True)
        # If the speaker is busy with another server, still offer it after a while.
        self.loop.call_later(10, lambda: asyncio.ensure_future(spk.ensure_sink()))

    async def run(self):
        await self.server.start_server(port=SERVER_PORT, discover_clients=False)
        self.volume_task = asyncio.create_task(self.sync_volumes())
        self.zc = AsyncZeroconf()
        self.browser = AsyncServiceBrowser(self.zc.zeroconf, [SERVICE], handlers=[self.on_service])
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            self.loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        log.info("stopping")
        for spk in list(self.speakers.values()):
            await spk.close()
        await self.browser.async_cancel()
        await self.zc.async_close()
        await self.server.close()


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s %(message)s")
    for noisy in ("aiosendspin",):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    async def run():
        await Bridge().run()

    asyncio.run(run())


if __name__ == "__main__":
    main()
