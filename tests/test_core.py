"""Unit tests for the parts of Roomcast that don't need speakers or a desktop.

Run with:  python3 -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from roomcast import config, pw  # noqa: E402
from roomcast.daemon import _port_pairs  # noqa: E402


def node(i, name, desc="", media_class="Audio/Sink", **props):
    return {
        "id": i,
        "type": "PipeWire:Interface:Node",
        "info": {"state": "suspended", "props": {
            "node.name": name, "node.description": desc or name, "media.class": media_class, **props,
        }},
    }


def port(i, node_id, direction, name, channel=None):
    return {
        "id": i,
        "type": "PipeWire:Interface:Port",
        "info": {"direction": direction, "props": {
            "node.id": node_id, "port.name": name, "audio.channel": channel,
        }},
    }


class SpeakerNames(unittest.TestCase):
    def test_raop_names(self):
        for name, host, ip in [
            ("raop_sink.MB-Left.local.192.168.1.20.7000", "MB-Left.local", "192.168.1.20"),
            ("raop_sink.Sonos-74CA60A2CED0.local.10.0.0.5.7000", "Sonos-74CA60A2CED0.local", "10.0.0.5"),
            ("raop_sink.Kitchen.local.fe80::c07:b88e:e53:2bf9.7000", "Kitchen.local", "fe80::c07:b88e:e53:2bf9"),
        ]:
            m = pw._RAOP_RE.match(name)
            self.assertIsNotNone(m, name)
            self.assertEqual((m.group("host"), m.group("ip")), (host, ip))

    def test_speakers_prefers_ipv4_and_includes_sendspin(self):
        g = pw.Graph([
            node(1, "raop_sink.Kitchen.local.fe80::1:2.7000", "Kitchen"),
            node(2, "raop_sink.Kitchen.local.192.168.1.9.7000", "Kitchen"),
            node(3, "sendspin_sink.satellite1-abc", "Satellite1", **{"sendspin.ip": "192.168.1.30"}),
            node(4, "alsa_output.pci.analog-stereo", "Speakers"),
        ])
        speakers = g.speakers()
        self.assertEqual(set(speakers), {"Kitchen.local", "sendspin:satellite1-abc"})
        self.assertEqual(speakers["Kitchen.local"].ip, "192.168.1.9")
        self.assertEqual(speakers["sendspin:satellite1-abc"].kind, "sendspin")
        self.assertEqual([n.name for n in g.local_sinks()], ["alsa_output.pci.analog-stereo"])


class Routing(unittest.TestCase):
    SRC = {"FL": 10, "FR": 11}
    DST = {"FL": 20, "FR": 21}

    def test_stereo(self):
        self.assertEqual(_port_pairs(self.SRC, self.DST, "stereo"), {(10, 20), (11, 21)})

    def test_pair_halves_get_one_channel_on_both_inputs(self):
        self.assertEqual(_port_pairs(self.SRC, self.DST, "left"), {(10, 20), (10, 21)})
        self.assertEqual(_port_pairs(self.SRC, self.DST, "right"), {(11, 20), (11, 21)})

    def test_mono_speaker_gets_both_channels(self):
        self.assertEqual(_port_pairs(self.SRC, {"MONO": 30}, "stereo"), {(10, 30), (11, 30)})


class Config(unittest.TestCase):
    CFG = {
        "pairs": [{"id": "p1", "name": "Office", "left": "L.local", "right": "R.local"}],
        "outputs": [{"id": "o1", "name": "House", "members": [
            {"type": "pair", "ref": "p1"},
            {"type": "speaker", "ref": "K.local"},
            {"type": "speaker", "ref": "L.local"},  # already in the pair
            {"type": "pair", "ref": "gone"},
        ]}],
    }

    def test_flatten_dedupes_and_skips_missing(self):
        out = self.CFG["outputs"][0]
        self.assertEqual(
            config.flatten_members(self.CFG, out),
            [("L.local", "left"), ("R.local", "right"), ("K.local", "stereo")],
        )

    def test_every_pair_gets_an_output(self):
        ids = [o["id"] for o in config.all_outputs(self.CFG)]
        self.assertEqual(ids, ["o1", "pair-p1"])


class GraphParsing(unittest.TestCase):
    def test_ports_volume_default_and_targets(self):
        objs = [
            node(5, "roomcast.o1", "House"),
            port(50, 5, "output", "monitor_FL"),
            port(51, 5, "output", "monitor_FR"),
            port(52, 5, "input", "playback_FL", "FL"),
            node(6, "spotify", media_class="Stream/Output/Audio", **{"application.name": "Spotify"}),
            {"id": 9, "type": "PipeWire:Interface:Metadata", "props": {"metadata.name": "default"},
             "metadata": [
                 {"subject": 0, "key": "default.audio.sink", "value": {"name": "roomcast.o1"}},
                 {"subject": 6, "key": "target.object", "value": "123"},
             ]},
        ]
        objs[0]["info"]["params"] = {"Props": [{"channelVolumes": [0.125, 0.125]}]}
        g = pw.Graph(objs)
        out = g.outputs()["o1"]
        self.assertEqual(out.out_ports, {"monitor_FL": 50, "monitor_FR": 51})
        self.assertEqual(out.in_ports, {"FL": 52})
        self.assertAlmostEqual(out.volume, 0.5)  # cubic scale, as wpctl shows it
        self.assertEqual(g.default_sink_name, "roomcast.o1")
        self.assertEqual(g.targets, {6: "123"})
        self.assertEqual([s.name for s in g.app_streams()], ["spotify"])


if __name__ == "__main__":
    unittest.main()
