// Roomcast top-panel menu.
//
// Reads the PipeWire graph with pw-dump each time the menu opens, and acts
// with wpctl / pw-metadata, the same tools the Roomcast app uses. Nothing
// runs while the menu is closed.

import GObject from 'gi://GObject';
import St from 'gi://St';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Slider} from 'resource:///org/gnome/shell/ui/slider.js';

const OUTPUT_PREFIX = 'roomcast.';
const PAIR_PREFIX = 'roomcast.pair-';
const DISCOVERY_APP = 'Roomcast speaker discovery';
const APP_ID = 'io.github.jridder.Roomcast.desktop';
const SPEAKER_RE = /^(raop_sink\.|sendspin_sink\.)/;

function run(argv) {
    return new Promise(resolve => {
        let proc;
        try {
            proc = Gio.Subprocess.new(argv,
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE);
        } catch (e) {
            resolve('');
            return;
        }
        proc.communicate_utf8_async(null, null, (p, res) => {
            try {
                const [, out] = p.communicate_utf8_finish(res);
                resolve(out ?? '');
            } catch (e) {
                resolve('');
            }
        });
    });
}

function parseGraph(text) {
    let objects = [];
    try {
        objects = JSON.parse(text || '[]');
    } catch (e) {
        objects = [];
    }
    const nodes = new Map();
    const links = [];
    const targets = new Map();
    let defaultSink = null;
    let discovery = false;
    for (const o of objects) {
        const info = o.info ?? {};
        const props = info.props ?? {};
        if (o.type.endsWith(':Node')) {
            let volume = null;
            for (const p of info.params?.Props ?? []) {
                if (p.channelVolumes?.length) {
                    const lin = p.channelVolumes.reduce((a, b) => a + b, 0) / p.channelVolumes.length;
                    volume = Math.cbrt(Math.max(lin, 0));
                }
            }
            nodes.set(o.id, {id: o.id, props, state: info.state, volume});
        } else if (o.type.endsWith(':Link')) {
            links.push({out: info['output-node-id'], in: info['input-node-id']});
        } else if (o.type.endsWith(':Client')) {
            if (props['application.name'] === DISCOVERY_APP)
                discovery = true;
        } else if (o.type.endsWith(':Metadata') && o.props?.['metadata.name'] === 'default') {
            for (const m of o.metadata ?? []) {
                if (m.key === 'default.audio.sink')
                    defaultSink = m.value?.name ?? null;
                else if (m.key === 'target.object' && m.subject !== undefined)
                    targets.set(m.subject, String(m.value?.name ?? m.value ?? ''));
            }
        }
    }
    return {nodes, links, targets, defaultSink, discovery};
}

const name = n => n.props['node.name'] ?? '';
const label = n => n.props['node.description'] ?? n.props['application.name'] ?? name(n);

const RoomcastIndicator = GObject.registerClass(
class RoomcastIndicator extends PanelMenu.Button {
    _init(ext) {
        super._init(0.5, 'Roomcast');
        this._ext = ext;
        this._volumeTimers = new Map();
        this._refreshId = 0;

        this.add_child(new St.Icon({
            gicon: Gio.icon_new_for_string(`${ext.path}/roomcast-symbolic.svg`),
            style_class: 'system-status-icon',
        }));

        this._outputs = new PopupMenu.PopupMenuSection();
        this._local = new PopupMenu.PopupMenuSection();
        this._apps = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._outputs);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem('This Computer'));
        this.menu.addMenuItem(this._local);
        this._appsSeparator = new PopupMenu.PopupSeparatorMenuItem('Playing Now');
        this.menu.addMenuItem(this._appsSeparator);
        this.menu.addMenuItem(this._apps);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        const open = new PopupMenu.PopupMenuItem('Open Roomcast');
        open.connect('activate', () => this._openApp());
        this.menu.addMenuItem(open);

        this.menu.connect('open-state-changed', (_m, isOpen) => {
            if (isOpen)
                this._refresh();
        });
    }

    _openApp() {
        const app = Gio.DesktopAppInfo.new(APP_ID);
        if (app)
            app.launch([], null);
        else
            GLib.spawn_command_line_async('roomcast');
    }

    _refreshSoon(ms = 500) {
        if (this._refreshId)
            GLib.source_remove(this._refreshId);
        this._refreshId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
            this._refreshId = 0;
            if (this.menu.isOpen)
                this._refresh();
            return GLib.SOURCE_REMOVE;
        });
    }

    async _refresh() {
        const g = parseGraph(await run(['pw-dump', '--no-colors']));
        if (!this.menu.isOpen)
            return;
        this._build(g);
    }

    _setDefault(node) {
        run(['wpctl', 'set-default', String(node.id)]).then(() => this._refreshSoon());
    }

    _setVolume(node, value) {
        const prev = this._volumeTimers.get(node.id);
        if (prev)
            GLib.source_remove(prev);
        this._volumeTimers.set(node.id, GLib.timeout_add(GLib.PRIORITY_DEFAULT, 100, () => {
            this._volumeTimers.delete(node.id);
            run(['wpctl', 'set-volume', String(node.id), value.toFixed(3)]);
            return GLib.SOURCE_REMOVE;
        }));
    }

    _route(stream, sink) {
        const argv = sink
            ? ['pw-metadata', String(stream.id), 'target.object',
                String(sink.props['object.serial']), 'Spa:Id']
            : ['pw-metadata', '-d', String(stream.id), 'target.object'];
        run(argv).then(() => this._refreshSoon());
    }

    _playing(g, node) {
        return g.links.some(l => l.in === node.id && g.nodes.get(l.out)?.state === 'running');
    }

    _build(g) {
        this._outputs.removeAll();
        this._local.removeAll();
        this._apps.removeAll();

        const sinks = [...g.nodes.values()].filter(n => n.props['media.class'] === 'Audio/Sink');
        const outputs = sinks.filter(n => name(n).startsWith(OUTPUT_PREFIX))
            .sort((a, b) => {
                const pa = name(a).startsWith(PAIR_PREFIX), pb = name(b).startsWith(PAIR_PREFIX);
                return pa === pb ? label(a).localeCompare(label(b)) : pa - pb;
            });
        const local = sinks.filter(n => !name(n).startsWith(OUTPUT_PREFIX) && !SPEAKER_RE.test(name(n)))
            .sort((a, b) => label(a).localeCompare(label(b)));

        if (!g.discovery) {
            const item = new PopupMenu.PopupMenuItem('Background service isn’t running', {reactive: false});
            this._outputs.addMenuItem(item);
        }
        if (!outputs.length) {
            const item = new PopupMenu.PopupMenuItem('No outputs yet — open Roomcast to add some', {reactive: false});
            this._outputs.addMenuItem(item);
        }
        for (const node of outputs) {
            const isPair = name(node).startsWith(PAIR_PREFIX);
            let text = label(node) + (isPair ? '  (stereo pair)' : '');
            if (this._playing(g, node))
                text += '  ·  Playing';
            const item = new PopupMenu.PopupMenuItem(text);
            item.setOrnament(g.defaultSink === name(node)
                ? PopupMenu.Ornament.CHECK : PopupMenu.Ornament.NONE);
            item.connect('activate', () => this._setDefault(node));
            this._outputs.addMenuItem(item);

            const sliderItem = new PopupMenu.PopupBaseMenuItem({activate: false});
            sliderItem.add_child(new St.Icon({icon_name: 'audio-volume-medium-symbolic', style_class: 'popup-menu-icon'}));
            const slider = new Slider(node.volume ?? 1);
            slider.x_expand = true;
            slider.accessible_name = `${label(node)} volume`;
            slider.connect('notify::value', () => this._setVolume(node, slider.value));
            sliderItem.add_child(slider);
            this._outputs.addMenuItem(sliderItem);
        }

        for (const node of local) {
            const item = new PopupMenu.PopupMenuItem(label(node));
            item.setOrnament(g.defaultSink === name(node)
                ? PopupMenu.Ornament.CHECK : PopupMenu.Ornament.NONE);
            item.connect('activate', () => this._setDefault(node));
            this._local.addMenuItem(item);
        }

        const streams = [...g.nodes.values()].filter(n =>
            n.props['media.class'] === 'Stream/Output/Audio' &&
            !n.props['node.virtual'] && !n.props['stream.monitor'] &&
            !name(n).startsWith('output.') && !name(n).startsWith('roomcast'));
        this._appsSeparator.visible = streams.length > 0;
        for (const s of streams) {
            const linked = g.links.find(l => l.out === s.id);
            const sinkNode = linked ? g.nodes.get(linked.in) : null;
            const appName = s.props['application.name'] ?? label(s);
            const sub = new PopupMenu.PopupSubMenuMenuItem(
                `${appName} → ${sinkNode ? label(sinkNode) : 'nowhere'}`);
            const explicit = g.targets.has(s.id) || s.props['target.object'];
            const choices = [[null, 'Default output'], ...outputs.map(n => [n, label(n)]),
                ...local.map(n => [n, label(n)])];
            for (const [sink, text] of choices) {
                const item = new PopupMenu.PopupMenuItem(text);
                const current = sink ? (explicit && sinkNode?.id === sink.id) : !explicit;
                item.setOrnament(current ? PopupMenu.Ornament.CHECK : PopupMenu.Ornament.NONE);
                item.connect('activate', () => this._route(s, sink));
                sub.menu.addMenuItem(item);
            }
            this._apps.addMenuItem(sub);
        }
    }

    destroy() {
        if (this._refreshId)
            GLib.source_remove(this._refreshId);
        for (const id of this._volumeTimers.values())
            GLib.source_remove(id);
        this._volumeTimers.clear();
        super.destroy();
    }
});

export default class RoomcastExtension extends Extension {
    enable() {
        this._indicator = new RoomcastIndicator(this);
        Main.panel.addToStatusArea(this.uuid, this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
