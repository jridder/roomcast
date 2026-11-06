"""Roomcast GTK 4 / libadwaita interface."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import __version__, config, pw  # noqa: E402
from .devices import AirPlayBrowser, SonosManager, friendly_model, is_sonos  # noqa: E402

APP_ID = "io.github.jridder.Roomcast"
SERVICE = "roomcast.service"
POLL_MS = 1500

# Rows treat titles as Pango markup by default; speaker names like
# "Bed & Bath" would then fail to render.
def _plain(cls):
    def make(**props):
        row = cls(use_markup=False)  # must be off before any text is set
        for key, value in props.items():
            row.set_property(key.replace("_", "-"), value)
        return row

    return make


ActionRow = _plain(Adw.ActionRow)
ExpanderRow = _plain(Adw.ExpanderRow)
ComboRow = _plain(Adw.ComboRow)
SwitchRow = _plain(Adw.SwitchRow)


def run_bg(fn, *args, done=None):
    """Run fn in a thread and hand its result (or exception) to done() on the main loop."""

    def work():
        try:
            result, error = fn(*args), None
        except Exception as e:  # noqa: BLE001
            result, error = None, e
        if done:
            GLib.idle_add(done, result, error)

    threading.Thread(target=work, daemon=True).start()


def start_service() -> bool:
    if shutil.which("systemctl"):
        r = subprocess.run(
            ["systemctl", "--user", "start", SERVICE], capture_output=True, text=True
        )
        if r.returncode == 0:
            return True
    # Running from a source checkout without the unit installed.
    subprocess.Popen(
        [sys.executable, "-m", "roomcast.daemon"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True


def restart_service():
    subprocess.run(["systemctl", "--user", "restart", SERVICE], capture_output=True)


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Roomcast")
        self.set_default_size(720, 760)
        self.set_size_request(360, 400)

        self.cfg = config.load()
        self.graph: pw.Graph | None = None
        self.signature = None
        self.expanded: set[str] = set()
        self.volume_scales: dict[str, Gtk.Scale] = {}
        self.volume_touched: dict[str, float] = {}
        self.updating = False
        self.sonos = SonosManager()
        self.sonos_zones = []
        self.sonos_busy = False
        self.polling = False
        self.mdns = AirPlayBrowser(lambda: GLib.idle_add(self.on_mdns_change))

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        menu = Gio.Menu()
        menu.append("Open Sound Settings", "win.sound-settings")
        menu.append("Restart Background Service", "win.restart-service")
        menu.append("About Roomcast", "win.about")
        header.pack_end(
            Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Main Menu")
        )
        view.add_top_bar(header)

        self.banner = Adw.Banner(
            title="The Roomcast background service isn't running", button_label="Start"
        )
        self.banner.connect("button-clicked", lambda *_: self.on_start_service())
        view.add_top_bar(self.banner)

        self.page_outputs = Adw.PreferencesPage()
        self.page_speakers = Adw.PreferencesPage()
        self.page_apps = Adw.PreferencesPage()
        self.stack.add_titled_with_icon(self.page_outputs, "outputs", "Outputs", "audio-speakers-symbolic")
        self.stack.add_titled_with_icon(self.page_speakers, "speakers", "Speakers", "network-wireless-symbolic")
        self.stack.add_titled_with_icon(self.page_apps, "apps", "Apps", "view-app-grid-symbolic")
        view.set_content(self.stack)

        bar = Adw.ViewSwitcherBar(stack=self.stack)
        view.add_bottom_bar(bar)
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 560sp"))
        bp.add_setter(bar, "reveal", True)
        bp.add_setter(switcher, "visible", False)
        self.add_breakpoint(bp)

        for name, cb in (
            ("sound-settings", lambda *_: subprocess.Popen(["gnome-control-center", "sound"])),
            ("restart-service", lambda *_: run_bg(restart_service, done=lambda *_: self.poll_soon())),
            ("about", lambda *_: self.show_about()),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)

        self.groups: dict[Adw.PreferencesPage, list] = {}
        self.connect("close-request", self.on_close)
        self.poll()
        GLib.timeout_add(POLL_MS, self.poll)
        self.refresh_sonos()
        GLib.timeout_add_seconds(60, self.refresh_sonos)

    # ----- data ----------------------------------------------------------

    def poll(self):
        if self.polling:
            return True
        self.polling = True
        try:
            proc = Gio.Subprocess.new(
                ["pw-dump", "--no-colors"],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE,
            )
            proc.communicate_utf8_async(None, None, self.on_dump)
        except GLib.Error:
            self.polling = False
        return True

    def poll_soon(self, delay=600):
        GLib.timeout_add(delay, lambda: (self.poll(), False)[1])

    def on_dump(self, proc, res):
        self.polling = False
        try:
            _, out, _ = proc.communicate_utf8_finish(res)
            graph = pw.parse_dump(out)
        except (GLib.Error, ValueError):
            return
        self.graph = graph
        self.cfg = config.load()
        self.banner.set_revealed(not graph.discovery_running())
        sig = self.make_signature()
        if sig != self.signature:
            self.signature = sig
            self.rebuild()
        else:
            self.update_live()

    def on_mdns_change(self):
        self.signature = None
        return False

    def refresh_sonos(self):
        if not self.sonos.available or self.sonos_busy:
            return True
        self.sonos_busy = True

        def done(zones, error):
            self.sonos_busy = False
            if error is None:
                self.sonos_zones = zones or []
                self.signature = None
                self.poll()

        run_bg(self.sonos.discover, done=done)
        return True

    def make_signature(self):
        g = self.graph
        spk = sorted((k, s.name) for k, s in g.speakers().items())
        outs = sorted((k, n.description, n.state == "running") for k, n in g.outputs().items())
        streams = []
        for s in g.app_streams():
            sink = g.linked_sink(s)
            streams.append((s.id, s.description, s.props.get("media.name"), sink.name if sink else None, g.targets.get(s.id)))
        local = sorted((n.name, n.description) for n in g.local_sinks())
        playing = sorted(k for k, s in g.speakers().items() if s.node.state == "running")
        sonos = [(z.name, z.ip, z.is_pair) for z in self.sonos_zones]
        return json.dumps(
            [spk, outs, streams, local, playing, sonos, g.default_sink_name, self.cfg],
            sort_keys=True, default=str,
        )

    def save(self):
        config.save(self.cfg)
        self.signature = None
        self.poll_soon(400)
        self.poll_soon(1500)

    # ----- helpers -------------------------------------------------------

    def speaker_info(self, spk: pw.Speaker) -> dict:
        return self.mdns.lookup(spk.key, spk.ip)

    def speaker_model(self, spk: pw.Speaker) -> str:
        info = self.speaker_info(spk)
        return friendly_model(info) if info else "AirPlay speaker"

    def speaker_name(self, key: str) -> str:
        spk = self.graph.speakers().get(key) if self.graph else None
        if spk:
            return spk.name
        return key.removesuffix(".local").replace("-", " ")

    def apple_pairs(self) -> list[tuple[str, str]]:
        """HomePods stereo-paired in Apple Home, as (left key, right key)."""
        groups: dict[str, list[pw.Speaker]] = {}
        for spk in self.graph.speakers().values():
            info = self.speaker_info(spk)
            tsid = info.get("tsid")
            if tsid and info.get("model", "").startswith("AudioAccessory"):
                groups.setdefault(tsid, []).append(spk)
        pairs = []
        for members in groups.values():
            if len(members) != 2:
                continue
            a, b = sorted(members, key=lambda s: s.name)
            if "right" in a.name.lower() or "left" in b.name.lower():
                a, b = b, a
            pairs.append((a.key, b.key))
        return pairs

    def output_playing(self, node: pw.Node) -> bool:
        """True when an app stream is feeding this output."""
        for link in self.graph.links_to_node(node.id):
            src = self.graph.nodes.get(link.out_node)
            if src and src.state == "running":
                return True
        return False

    def sonos_zone_for(self, spk: pw.Speaker):
        for z in self.sonos_zones:
            if z.ip == spk.ip:
                return z
        return None

    def clear(self, page: Adw.PreferencesPage):
        for g in self.groups.get(page, []):
            page.remove(g)
        self.groups[page] = []

    def add_group(self, page, group):
        page.add(group)
        self.groups.setdefault(page, []).append(group)
        return group

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    def volume_scale(self, node: pw.Node) -> Gtk.Scale:
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.01)
        scale.set_draw_value(False)
        scale.set_size_request(130, -1)
        scale.set_valign(Gtk.Align.CENTER)
        scale.set_tooltip_text("Volume")
        scale.set_value(node.volume if node.volume is not None else 1.0)
        name = node.name
        pending = {"src": 0}

        def changed(s):
            if self.updating:
                return
            self.volume_touched[name] = time.monotonic()
            if pending["src"]:
                GLib.source_remove(pending["src"])

            def apply():
                pending["src"] = 0
                n = self.graph.node_by_name(name) if self.graph else None
                if n:
                    run_bg(pw.set_volume, n.id, s.get_value())
                return False

            pending["src"] = GLib.timeout_add(120, apply)

        scale.connect("value-changed", changed)
        self.volume_scales[name] = scale
        return scale

    def update_live(self):
        self.updating = True
        try:
            for name, scale in self.volume_scales.items():
                if time.monotonic() - self.volume_touched.get(name, 0) < 2.5:
                    continue
                n = self.graph.node_by_name(name)
                if n and n.volume is not None and abs(scale.get_value() - n.volume) > 0.005:
                    scale.set_value(n.volume)
        finally:
            self.updating = False

    # ----- building ------------------------------------------------------

    def rebuild(self):
        self.updating = True
        self.volume_scales = {}
        try:
            self.build_outputs()
            self.build_speakers()
            self.build_apps()
        finally:
            self.updating = False

    def build_outputs(self):
        page = self.page_outputs
        self.clear(page)
        g = self.graph
        speakers = g.speakers()
        live = g.outputs()

        group = self.add_group(page, Adw.PreferencesGroup(
            title="Outputs",
            description="Each output is a sound device on this computer. Pick it in "
            "Sound Settings to play everything there, or send single apps to it from the Apps tab.",
        ))
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="New Output", valign=Gtk.Align.CENTER)
        add.add_css_class("flat")
        add.connect("clicked", lambda *_: self.new_output_dialog())
        group.set_header_suffix(add)

        if not self.cfg["outputs"]:
            row = ActionRow(
                title="No outputs yet",
                subtitle="Create one, then tick the speakers and pairs it should play on.",
            )
            btn = Gtk.Button(label="New Output", valign=Gtk.Align.CENTER)
            btn.add_css_class("suggested-action")
            btn.connect("clicked", lambda *_: self.new_output_dialog())
            row.add_suffix(btn)
            group.add(row)
            return

        for out in self.cfg["outputs"]:
            group.add(self.output_row(out, live.get(out["id"]), speakers))

    def output_row(self, out, node, speakers):
        oid = out["id"]
        members = {(m["type"], m["ref"]) for m in out.get("members", [])}
        names = []
        for t, ref in members:
            if t == "pair":
                p = config.find(self.cfg["pairs"], ref)
                if p:
                    names.append(p["name"])
            else:
                names.append(self.speaker_name(ref))
        status = []
        status.append(", ".join(sorted(names)) if names else "No speakers selected")
        if node is None:
            status.append("Starting…")
        elif self.output_playing(node):
            status.append("Playing")
        if node and self.graph.default_sink_name == node.name:
            status.append("Default output")

        exp = ExpanderRow(title=out["name"], subtitle=" · ".join(status))
        exp.add_prefix(Gtk.Image(icon_name="audio-speakers-symbolic"))
        if node is not None:
            exp.add_suffix(self.volume_scale(node))
        exp.set_expanded(oid in self.expanded)
        exp.connect(
            "notify::expanded",
            lambda r, _p: self.expanded.add(oid) if r.get_expanded() else self.expanded.discard(oid),
        )

        def member_row(kind, ref, title, subtitle):
            row = ActionRow(title=title, subtitle=subtitle)
            check = Gtk.CheckButton(active=(kind, ref) in members, valign=Gtk.Align.CENTER)
            check.connect("toggled", lambda c: self.toggle_member(oid, kind, ref, c.get_active()))
            row.add_prefix(check)
            row.set_activatable_widget(check)
            return row

        for p in self.cfg["pairs"]:
            exp.add_row(member_row(
                "pair", p["id"], p["name"],
                f"Stereo pair · L: {self.speaker_name(p['left'])} · R: {self.speaker_name(p['right'])}",
            ))
        shown = set()
        for key, spk in sorted(speakers.items(), key=lambda kv: kv[1].name.lower()):
            shown.add(key)
            exp.add_row(member_row("speaker", key, spk.name, self.speaker_model(spk)))
        for t, ref in sorted(members):
            if t == "speaker" and ref not in shown:
                exp.add_row(member_row("speaker", ref, self.speaker_name(ref), "Not found on the network"))

        if node is not None:
            sw = SwitchRow(
                title="Default output",
                subtitle="Send all sound here, including system sounds",
                active=self.graph.default_sink_name == node.name,
            )
            sw.connect("notify::active", lambda r, _p: self.set_default(node, r.get_active()))
            exp.add_row(sw)

        rename = Adw.EntryRow(title="Name", text=out["name"], show_apply_button=True)
        rename.connect("apply", lambda r: self.rename_output(oid, r.get_text()))
        exp.add_row(rename)

        delete = Adw.ButtonRow(title="Delete Output")
        delete.add_css_class("destructive-action")
        delete.connect("activated", lambda *_: self.delete_output(out))
        exp.add_row(delete)
        return exp

    def build_speakers(self):
        page = self.page_speakers
        self.clear(page)
        g = self.graph
        speakers = g.speakers()

        # --- discovered speakers
        group = self.add_group(page, Adw.PreferencesGroup(
            title="Network Speakers",
            description="AirPlay speakers found on your network, including HomePods and Sonos.",
        ))
        if not speakers:
            row = ActionRow(
                title="Looking for speakers…",
                subtitle="Speakers must be on the same network. On HomePods, set "
                "Home app › Home Settings › Speakers & TV › Allow Speaker & TV Access "
                "to “Anyone on the Same Network”.",
            )
            row.add_prefix(Adw.Spinner())
            group.add(row)
        apple_partner = {}
        for left, right in self.apple_pairs():
            apple_partner[left], apple_partner[right] = right, left
        for key, spk in sorted(speakers.items(), key=lambda kv: kv[1].name.lower()):
            info = self.speaker_info(spk)
            bits = [self.speaker_model(spk), spk.ip]
            if key in apple_partner:
                bits.append(f"Paired with {self.speaker_name(apple_partner[key])} in Apple Home")
            zone = self.sonos_zone_for(spk)
            if zone and zone.is_pair:
                bits.append("Sonos stereo pair")
            row = ActionRow(title=spk.name, subtitle=" · ".join(bits))
            icon = "video-display-symbolic" if info.get("model", "").startswith("AppleTV") else "audio-speakers-symbolic"
            row.add_prefix(Gtk.Image(icon_name=icon))
            if spk.node.state == "running":
                playing = Gtk.Image(icon_name="media-playback-start-symbolic", tooltip_text="Playing")
                playing.add_css_class("accent")
                row.add_suffix(playing)
            row.add_suffix(self.volume_scale(spk.node))
            group.add(row)

        # --- Roomcast stereo pairs
        group = self.add_group(page, Adw.PreferencesGroup(
            title="Stereo Pairs",
            description="Play the left channel on one speaker and the right channel on "
            "another. Works with any two speakers, including HomePods.",
        ))
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="New Pair", valign=Gtk.Align.CENTER)
        add.add_css_class("flat")
        add.connect("clicked", lambda *_: self.pair_dialog())
        group.set_header_suffix(add)

        existing = {(p["left"], p["right"]) for p in self.cfg["pairs"]}
        existing |= {(r, l) for l, r in existing}
        for p in self.cfg["pairs"]:
            def side(key):
                return self.speaker_name(key) + ("" if key in speakers else " (not found)")

            row = ActionRow(title=p["name"], subtitle=f"Left: {side(p['left'])} · Right: {side(p['right'])}")
            row.add_prefix(Gtk.Image(icon_name="media-playlist-consecutive-symbolic"))
            swap = Gtk.Button(icon_name="object-flip-horizontal-symbolic", tooltip_text="Swap Left and Right", valign=Gtk.Align.CENTER)
            swap.add_css_class("flat")
            swap.connect("clicked", lambda *_, p=p: self.swap_pair(p))
            edit = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text="Edit Pair", valign=Gtk.Align.CENTER)
            edit.add_css_class("flat")
            edit.connect("clicked", lambda *_, p=p: self.pair_dialog(p))
            delete = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Delete Pair", valign=Gtk.Align.CENTER)
            delete.add_css_class("flat")
            delete.connect("clicked", lambda *_, p=p: self.delete_pair(p))
            for b in (swap, edit, delete):
                row.add_suffix(b)
            group.add(row)
        for left, right in self.apple_pairs():
            if (left, right) in existing:
                continue
            row = ActionRow(
                title=f"{self.speaker_name(left)} + {self.speaker_name(right)}",
                subtitle="Paired in Apple Home. Add it here to play these HomePods in stereo from this computer.",
            )
            row.add_prefix(Gtk.Image(icon_name="starred-symbolic"))
            btn = Gtk.Button(label="Add Pair", valign=Gtk.Align.CENTER)
            btn.connect("clicked", lambda *_, l=left, r=right: self.add_pair(self.pair_name(l, r), l, r))
            row.add_suffix(btn)
            group.add(row)
        if not self.cfg["pairs"] and not self.apple_pairs():
            group.add(ActionRow(title="No pairs yet", subtitle="Use + to pair two speakers."))

        # --- Sonos hardware pairs
        if self.sonos_zones:
            group = self.add_group(page, Adw.PreferencesGroup(
                title="Sonos Pairing",
                description="Pair or separate Sonos speakers. This changes their Sonos setup, "
                "the same as doing it in the Sonos app. A Sonos pair plays as a single stereo speaker.",
            ))
            singles = [z for z in self.sonos_zones if not z.is_pair]
            if len(singles) >= 2:
                btn = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Create Sonos Pair", valign=Gtk.Align.CENTER)
                btn.add_css_class("flat")
                btn.connect("clicked", lambda *_: self.sonos_pair_dialog(singles))
                group.set_header_suffix(btn)
            for z in self.sonos_zones:
                if z.is_pair:
                    sub = f"Stereo pair · {z.model} · {z.ip} + {z.partner.ip_address}"
                else:
                    sub = f"Single speaker · {z.model} · {z.ip}"
                row = ActionRow(title=z.name, subtitle=sub)
                row.add_prefix(Gtk.Image(icon_name="audio-speakers-symbolic"))
                if z.is_pair:
                    btn = Gtk.Button(label="Separate", valign=Gtk.Align.CENTER)
                    btn.connect("clicked", lambda *_, z=z: self.sonos_separate(z))
                    row.add_suffix(btn)
                group.add(row)

        note = self.add_group(page, Adw.PreferencesGroup())
        note.add(ActionRow(
            title="About HomePod pairs",
            subtitle="Apple only lets the Home app create HomePod stereo pairs. Roomcast pairs "
            "above do the same job from this computer, and don't change your Home setup.",
        ))

    def build_apps(self):
        page = self.page_apps
        self.clear(page)
        g = self.graph
        group = self.add_group(page, Adw.PreferencesGroup(
            title="Playing Now",
            description="Send one app to your network speakers while system sounds stay on "
            "this computer. Apps remember where you sent them the next time they play.",
        ))
        streams = g.app_streams()
        if not streams:
            group.add(ActionRow(
                title="Nothing is playing",
                subtitle="Start playing something and it will show up here.",
            ))
            return

        default = g.node_by_name(g.default_sink_name or "")
        choices: list[tuple[str | None, str]] = [
            (None, f"Default ({default.description})" if default else "Default output")
        ]
        outputs = g.outputs()
        for out in self.cfg["outputs"]:
            n = outputs.get(out["id"])
            if n:
                choices.append((n.name, out["name"]))
        for p in self.cfg["pairs"]:
            n = outputs.get(config.PAIR_OUTPUT_PREFIX + p["id"])
            if n:
                choices.append((n.name, f"{p['name']} (stereo pair)"))
        for n in sorted(g.local_sinks(), key=lambda n: n.description):
            choices.append((n.name, n.description))
        for spk in sorted(g.speakers().values(), key=lambda s: s.name):
            choices.append((spk.node.name, f"{spk.name} (speaker only)"))
        labels = Gtk.StringList.new([label for _, label in choices])

        for s in streams:
            target = g.targets.get(s.id) or s.props.get("target.object")
            if target and str(target).isdigit():  # some tools store object.serial
                tnode = next((n for n in g.nodes.values() if str(n.props.get("object.serial")) == str(target)), None)
                target = tnode.name if tnode else None
            if not target:
                linked = g.linked_sink(s)
                if linked and linked.name != g.default_sink_name:
                    target = linked.name
            idx = next((i for i, (name, _) in enumerate(choices) if name and name == target), 0)
            row = ComboRow(
                title=s.props.get("application.name") or s.description,
                subtitle=s.props.get("media.name") or "",
                model=labels,
                selected=idx,
            )
            icon = s.props.get("application.icon-name") or "audio-x-generic-symbolic"
            row.add_prefix(Gtk.Image(icon_name=icon, pixel_size=24))
            row.connect("notify::selected", lambda r, _p, sid=s.id: self.route(sid, choices[r.get_selected()][0]))
            group.add(row)

    # ----- actions -------------------------------------------------------

    def on_start_service(self):
        run_bg(start_service, done=lambda *_: self.poll_soon(1500))
        self.banner.set_revealed(False)

    def toggle_member(self, oid, kind, ref, active):
        out = config.find(self.cfg["outputs"], oid)
        if not out:
            return
        members = [m for m in out["members"] if not (m["type"] == kind and m["ref"] == ref)]
        if active:
            members.append({"type": kind, "ref": ref})
        out["members"] = members
        self.save()

    def set_default(self, node, active):
        if self.updating:
            return
        if active:
            run_bg(pw.set_default_sink, node.id, done=lambda *_: self.poll_soon())
            return
        local = self.graph.local_sinks()
        if local:
            run_bg(pw.set_default_sink, local[0].id, done=lambda *_: self.poll_soon())

    def route(self, stream_id, sink_name):
        if self.updating:
            return
        sink = self.graph.node_by_name(sink_name) if sink_name else None
        if sink_name and sink is None:
            self.toast("That output isn't available right now")
            return
        run_bg(pw.route_stream, stream_id, sink, done=lambda *_: self.poll_soon())

    def new_output_dialog(self):
        dialog = Adw.AlertDialog(heading="New Output", body="Give it a name, like “Downstairs” or “Office”.")
        entry = Gtk.Entry(text=self.suggest_output_name(), activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("create", "Create")
        dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("create")

        def resp(_d, r):
            name = entry.get_text().strip()
            if r == "create" and name:
                oid = config.new_id()
                self.cfg["outputs"].append({"id": oid, "name": name, "members": []})
                self.expanded.add(oid)
                self.save()

        dialog.connect("response", resp)
        dialog.present(self)

    def suggest_output_name(self):
        names = {o["name"] for o in self.cfg["outputs"]}
        base = "Network Speakers"
        if base not in names:
            return base
        i = 2
        while f"{base} {i}" in names:
            i += 1
        return f"{base} {i}"

    def rename_output(self, oid, name):
        out = config.find(self.cfg["outputs"], oid)
        name = name.strip()
        if out and name:
            out["name"] = name
            self.save()
            node = self.graph.outputs().get(oid) if self.graph else None
            if node and node.state == "running":
                self.toast("The new name shows in Sound Settings once playback stops")

    def delete_output(self, out):
        dialog = Adw.AlertDialog(
            heading=f"Delete “{out['name']}”?",
            body="Apps playing to it will move to the default output.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def resp(_d, r):
            if r == "delete":
                self.cfg["outputs"] = [o for o in self.cfg["outputs"] if o["id"] != out["id"]]
                self.save()

        dialog.connect("response", resp)
        dialog.present(self)

    def pair_name(self, left, right):
        a, b = self.speaker_name(left), self.speaker_name(right)
        for suffix in (" Left", " Right", " left", " right", " L", " R"):
            a, b = a.removesuffix(suffix), b.removesuffix(suffix)
        return a if a == b else f"{a} + {b}"

    def add_pair(self, name, left, right, pair=None):
        if pair is None:
            pair = {"id": config.new_id()}
            self.cfg["pairs"].append(pair)
        pair.update(name=name, left=left, right=right)
        self.save()
        self.toast(f"Pair “{name}” saved. It shows up as a sound device in a few seconds.")

    def swap_pair(self, p):
        p["left"], p["right"] = p["right"], p["left"]
        self.save()

    def delete_pair(self, p):
        self.cfg["pairs"] = [x for x in self.cfg["pairs"] if x["id"] != p["id"]]
        for out in self.cfg["outputs"]:
            out["members"] = [m for m in out["members"] if not (m["type"] == "pair" and m["ref"] == p["id"])]
        self.save()

    def pair_dialog(self, pair=None):
        speakers = sorted(self.graph.speakers().values(), key=lambda s: s.name.lower())
        keys = [s.key for s in speakers]
        if pair:
            for k in (pair["left"], pair["right"]):
                if k not in keys:
                    keys.append(k)
        if len(keys) < 2:
            self.toast("Roomcast needs to find at least two speakers first")
            return
        labels = Gtk.StringList.new([self.speaker_name(k) for k in keys])

        dialog = Adw.Dialog(title="Edit Pair" if pair else "New Stereo Pair", content_width=440)
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: dialog.close())
        save = Gtk.Button(label="Save" if pair else "Create")
        save.add_css_class("suggested-action")
        hb.pack_start(cancel)
        hb.pack_end(save)
        tv.add_top_bar(hb)

        prefs = Adw.PreferencesPage()
        grp = Adw.PreferencesGroup(description="The left speaker plays the left channel and "
                                   "the right speaker plays the right channel.")
        name_row = Adw.EntryRow(title="Name")
        left_row = ComboRow(title="Left speaker", model=labels)
        right_row = ComboRow(title="Right speaker", model=labels)
        if pair:
            left_row.set_selected(keys.index(pair["left"]))
            right_row.set_selected(keys.index(pair["right"]))
            name_row.set_text(pair["name"])
        else:
            right_row.set_selected(1)
        auto_name = {"value": not pair}

        def update(*_):
            l, r = keys[left_row.get_selected()], keys[right_row.get_selected()]
            save.set_sensitive(l != r and bool(name_row.get_text().strip()))
            if auto_name["value"]:
                auto_name["guard"] = True
                name_row.set_text(self.pair_name(l, r) if l != r else "")
                auto_name["guard"] = False

        def name_edited(*_):
            if not auto_name.get("guard"):
                auto_name["value"] = False
            update()

        left_row.connect("notify::selected", update)
        right_row.connect("notify::selected", update)
        name_row.connect("changed", name_edited)
        for r in (name_row, left_row, right_row):
            grp.add(r)
        prefs.add(grp)
        tv.set_content(prefs)
        dialog.set_child(tv)
        update()

        def do_save(*_):
            self.add_pair(
                name_row.get_text().strip(),
                keys[left_row.get_selected()],
                keys[right_row.get_selected()],
                pair,
            )
            dialog.close()

        save.connect("clicked", do_save)
        dialog.present(self)

    def sonos_pair_dialog(self, singles):
        labels = Gtk.StringList.new([f"{z.name} ({z.ip})" for z in singles])
        dialog = Adw.AlertDialog(
            heading="Create Sonos Pair",
            body="The two speakers become one stereo speaker in Sonos and AirPlay. "
            "It takes the left speaker's name.",
        )
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class("boxed-list")
        left = ComboRow(title="Left", model=labels)
        right = ComboRow(title="Right", model=labels, selected=1)
        box.append(left)
        box.append(right)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("pair", "Pair Speakers")
        dialog.set_response_appearance("pair", Adw.ResponseAppearance.SUGGESTED)

        def resp(_d, r):
            if r != "pair":
                return
            l, rr = singles[left.get_selected()], singles[right.get_selected()]
            if l is rr:
                self.toast("Choose two different speakers")
                return
            self.toast("Pairing Sonos speakers…")
            run_bg(SonosManager.create_pair, l, rr, done=self.sonos_done)

        dialog.connect("response", resp)
        dialog.present(self)

    def sonos_separate(self, zone):
        dialog = Adw.AlertDialog(
            heading=f"Separate “{zone.name}”?",
            body="Both speakers become single speakers again in Sonos.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("separate", "Separate")
        dialog.set_response_appearance("separate", Adw.ResponseAppearance.DESTRUCTIVE)

        def resp(_d, r):
            if r == "separate":
                self.toast("Separating Sonos pair…")
                run_bg(SonosManager.separate, zone, done=self.sonos_done)

        dialog.connect("response", resp)
        dialog.present(self)

    def sonos_done(self, _result, error):
        if error:
            self.toast(f"Sonos didn't accept the change: {error}")
        else:
            self.toast("Sonos updated. AirPlay speakers refresh in a few seconds.")
        GLib.timeout_add_seconds(3, lambda: (self.refresh_sonos(), False)[1])

    def show_about(self):
        Adw.AboutDialog(
            application_name="Roomcast",
            application_icon=APP_ID,
            version=__version__,
            comments="Play this computer's sound on HomePods, Sonos and other AirPlay speakers.",
            license_type=Gtk.License.GPL_3_0,
        ).present(self)

    def on_close(self, *_):
        self.mdns.close()
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        GLib.set_application_name("Roomcast")

    def do_activate(self):
        win = self.props.active_window
        if not win:
            run_bg(start_service)
            win = Window(self)
        win.present()


def main():
    return App().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
