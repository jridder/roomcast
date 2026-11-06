# Roomcast

A GNOME app for playing your computer's sound on network speakers: HomePods,
Sonos and other AirPlay speakers, plus Sendspin speakers such as the
FutureProofHomes Satellite1.

- **Outputs**: named groups of speakers. Each output is an ordinary sound
  device, so you can pick it in GNOME's Sound Settings or from any app.
- **Multi-room**: put as many speakers and pairs as you like in one output.
- **Stereo pairs**: left channel on one speaker, right on another. Works with
  any two speakers, HomePods included. HomePods already paired in Apple Home
  are offered as one-click suggestions. Every pair is also its own sound
  device.
- **Sonos pairing**: bond two Sonos speakers into a real Sonos stereo pair,
  or separate one, without the Sonos app.
- **One app to the speakers**: the Apps tab sends a single app (a music
  player, say) to your speakers while notifications and other system sounds
  stay on the computer. The app remembers where you sent it.
- **Volume** for each output, pair, speaker and app. A speaker's slider sets
  that speaker's own volume: PipeWire passes it on to AirPlay speakers, and
  Roomcast sends it to Sendspin speakers.
- **Top-panel menu**: switch outputs, set volume and move apps from GNOME's
  top bar.

## Status

This is an early release. It has been used on one setup: Ubuntu 26.10,
GNOME 51 and PipeWire 1.6.8, with HomePod mini, HomePod (2nd generation),
a Sonos Era 100 SL stereo pair and a FutureProofHomes Satellite1. Other
setups should work, but they haven't been tried. Bug reports are welcome.

## Requirements

- A GNOME desktop with PipeWire and WirePlumber. Most current Ubuntu and
  Fedora releases use these by default.
- PipeWire's RAOP (AirPlay) modules
- GTK 4 with libadwaita 1.6 or newer, and their Python bindings
- Python 3.10 or newer

On Ubuntu or Debian:

```sh
sudo apt install pipewire-bin libpipewire-0.3-modules wireplumber \
    python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-venv
```

On Fedora or Arch, install the equivalent packages (untested):

```sh
sudo dnf install pipewire pipewire-utils wireplumber python3-gobject gtk4 libadwaita   # Fedora
sudo pacman -S pipewire wireplumber python-gobject gtk4 libadwaita                     # Arch
```

The installer checks for all of these and tells you what's missing.

## Install

```sh
git clone https://github.com/jridder/roomcast.git
cd roomcast
./install.sh
```

Everything is installed for your user only. Nothing needs `sudo`, and the
installer doesn't touch any system files. It:

- creates a private Python environment in `~/.local/share/roomcast` and
  installs `zeroconf`, `soco` and `aiosendspin` into it;
- adds the app to your app grid, and puts the `roomcast` and `roomcastd`
  commands in `~/.local/bin`;
- installs and starts `roomcast.service`, a user service that runs at login;
- installs the top-panel menu, a GNOME Shell extension. GNOME only loads new
  extensions at login, so **log out and back in once** before the menu
  appears.

To update, `git pull` and run `./install.sh` again. To remove everything,
run `./install.sh --uninstall`. Your settings in `~/.config/roomcast` are
kept.

## Getting started

1. Open **Roomcast**. The **Speakers** tab lists every speaker it finds.
2. On the **Outputs** tab, create an output and tick the speakers and pairs
   it should play on.
3. Either choose that output in Sound Settings (or turn on **Default output**
   on its row) to play everything there, or use the **Apps** tab to send
   one app to it.

For left and right speakers, use **+** under **Stereo Pairs** on the
Speakers tab.

## Good to know

- **HomePods.** HomePods refuse AirPlay from senders that don't identify
  themselves as Apple software, and PipeWire always identifies itself as
  PipeWire. To get around this, the service copies PipeWire's AirPlay
  module into `$XDG_RUNTIME_DIR/roomcast/modules` and changes its
  User-Agent string to `iTunes/<version>`. It uses that copy only for its
  own speaker discovery. Your system's PipeWire files are never modified.
- **Home app setting.** In Apple's Home app, Home Settings › Speakers & TV ›
  Allow Speaker & TV Access must be "Everyone" or "Anyone on the Same
  Network", with "Require Password" off. Roomcast flags any speaker that
  refuses it.
- **Apple TV** isn't supported yet. It refuses the connection even with the
  HomePod workaround, so Roomcast hides it.
- **AirPlay 1 only.** Roomcast streams with AirPlay 1, through PipeWire. That
  works with HomePods and AirPlay 2 speakers, but it can't join AirPlay 2
  groups started from an iPhone.
- **HomePod stereo pairs** can only be created in Apple's Home app. Roomcast
  pairs do the same job from the computer, without changing your Home
  setup.
- **Delay.** AirPlay speakers play about 1.5 seconds behind live, Sendspin
  speakers about 1 second. On the **Speakers** tab, **Timing** has a delay
  for each Sendspin speaker. Raise it until a Sendspin speaker and an
  AirPlay speaker playing together stop echoing. About 0.5 s is typical.
- **Sync between brands** (HomePod and Sonos, say) relies on AirPlay's own
  timing, which is close but not sample-exact.

## Troubleshooting

- **No speakers found.** The computer and the speakers must be on the same
  network, and mDNS (port 5353 over UDP) must be allowed through any
  firewall. Check that the service is running:
  `systemctl --user status roomcast`.
- **A speaker is marked "Refused AirPlay".** Check the Home app setting
  above. Then choose **Restart Background Service** from the app's main
  menu.
- **The top-panel icon is missing.** Log out and back in after the first
  install. If it's still missing, run `gnome-extensions info
  roomcast@jridder.local`, and check `journalctl --user -b | grep -i
  roomcast` for errors.
- **Logs:** `journalctl --user -u roomcast -f`

## How it works

```
 app ──► roomcast.<id>  (virtual sink, one per output and one per pair)
             │ monitor FL ──► Office Left  (FL+FR)   ┐ pair
             │ monitor FR ──► Office Right (FL+FR)   ┘
             └ FL/FR ───────► Kitchen      (FL, FR)    stereo speaker
```

- `roomcastd`, the user service, runs PipeWire's `raop-discover` module in
  a child `pipewire` process. Every AirPlay speaker becomes a sink named
  `raop_sink.<host>.<ip>.<port>`.
- For each output in `~/.config/roomcast/config.json`, and for each pair,
  the service keeps a null sink named `roomcast.<id>`. It links that sink's
  monitor ports straight to the speakers' inputs, and re-links them if a
  speaker drops off and comes back.
- The output sinks set `node.passive = out`. An output with nothing playing
  suspends together with its speakers, so they aren't sent a constant
  silent stream.
- Changing which speakers an output plays on only changes links. The
  output's sink stays in place, so a playing app never falls back to the
  computer's speakers.
- Per-app routing writes `target.object` (the sink's `object.serial`, as
  `Spa:Id`) to PipeWire's default metadata. In that form WirePlumber
  remembers it.
- `roomcast.sendspin_bridge` finds Sendspin speakers through
  `_sendspin._tcp` mDNS and gives each one a sink named
  `sendspin_sink.<name>`. A passive `pw-record` captures that sink's audio,
  and an [aiosendspin](https://pypi.org/project/aiosendspin/) server streams
  it to the speaker. The capture only runs while something plays. Satellite1
  firmware v0.2.1 uses the older unencrypted Sendspin protocol, which the
  bridge accepts.
- Roomcast checks each AirPlay speaker with a single RTSP `OPTIONS` request,
  which doesn't interrupt playback.

## Development

```sh
bin/roomcast                               # run the app from the source tree
python3 -m roomcast.daemon                 # run the service in the foreground
python3 -m unittest discover -s tests      # unit tests
```

When you run from source without the service installed, the app starts the
background process itself. The app, the service and the top-panel menu
communicate only through `~/.config/roomcast/config.json` and the PipeWire
graph.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
