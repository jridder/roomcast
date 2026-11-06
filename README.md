# Roomcast

A GNOME app for playing this computer's sound on network speakers: HomePods,
Sonos and anything else that accepts AirPlay, plus Sendspin speakers such as
the FutureProofHomes Satellite1.

- **Outputs**: named groups of speakers. Each output shows up as an ordinary
  sound device in GNOME, so you can choose it in Sound Settings, or use it
  from any app.
- **Multi-room**: tick as many speakers and pairs as you like in one output.
- **Stereo pairs**: play the left channel on one speaker and the right
  channel on another. Works with any two speakers, HomePods included.
  HomePods already paired in Apple Home are offered as one-click suggestions.
- **Sonos pairing**: pair two Sonos speakers into a real Sonos stereo pair,
  or separate one, without the Sonos app.
- **Per-app routing**: the Apps tab sends a single app (say, a music player)
  to your speakers while notifications and other system sounds stay on the
  computer. WirePlumber remembers the choice for next time.

- **Top-panel menu**: a GNOME Shell extension that lists your outputs and pairs,
  each with a volume slider. Pick one to make it the default output, switch
  back to the computer's speakers, or send a playing app somewhere else.

## Install

```sh
./install.sh
```

This needs PipeWire with its RAOP (AirPlay) module, WirePlumber, and the
GTK 4 and libadwaita Python bindings, which Ubuntu and Fedora GNOME already
include. The installer puts `zeroconf` and `soco` in a private virtualenv
under `~/.local/share/roomcast`. It also installs and starts the
`roomcast.service` user service. Remove everything with
`./install.sh --uninstall`.

To run from the source tree without installing, use `bin/roomcast`. If the
service isn't installed, the app starts the background process itself.

## How it works

```
 app ──► roomcast.<id>  (virtual sink, one per output)
             │ monitor FL ──► Office Left  (FL+FR)   ┐ Roomcast pair
             │ monitor FR ──► Office Right (FL+FR)   ┘
             └ FL/FR ───────► Kitchen      (FL, FR)    stereo speaker
```

- `roomcastd` (the user service) runs PipeWire's `raop-discover` module in a
  child `pipewire` process. Every AirPlay speaker becomes a sink named
  `raop_sink.<host>.<ip>.<port>`.
- For each output in `~/.config/roomcast/config.json`, the service keeps a
  null sink named `roomcast.<id>`. It links that sink's monitor ports
  straight to the speakers, and re-links them if a speaker drops off and
  comes back.
- The output sinks set `node.passive = out`, so an output with nothing
  playing suspends together with its speakers. Without that, the speakers
  would get a constant silent stream and stay "in use".
- Changing which speakers an output plays on only changes links. The output
  sink stays in place, so a playing app never falls back to the computer's
  speakers.
- Per-app routing writes `target.object` (the sink's `object.serial`, type
  `Spa:Id`) to the default metadata, the form WirePlumber stores and
  restores.

### Sendspin speakers (Satellite1)

`roomcast.sendspin_bridge`, which the service starts, finds speakers through
their `_sendspin._tcp` mDNS records and gives each one a sink named
`sendspin_sink.<name>`. From then on the service treats it like any other
speaker. A passive `pw-record` captures that sink's audio and pushes it to
the speaker through an [aiosendspin](https://pypi.org/project/aiosendspin/)
server. The capture only runs while something plays, and the stream to the
speaker stops about a second after playback does. Satellite1 firmware
(v0.2.1) still uses the unencrypted pre-1.0 protocol, which the bridge
accepts.

Sendspin plays about 1 second behind, AirPlay about 2, so a Satellite1 and a
HomePod in the same output won't be in sync.

## Notes

- **HomePods and the User-Agent**: HomePods answer `403 Forbidden` to any
  AirPlay sender whose `User-Agent` isn't an Apple one, and PipeWire's RAOP
  module always sends `PipeWire/<version>`. At startup the service copies
  that one module into `$XDG_RUNTIME_DIR/roomcast/modules`, swaps the string
  for an `iTunes/…` one of the same length, and runs discovery with
  `PIPEWIRE_MODULE_DIR` pointing there. Every other module is a symlink to
  the system copy, and the system files stay untouched.
- **Home access setting**: the Home app's Home Settings › Speakers & TV ›
  Allow Speaker & TV Access must be "Everyone" or "Anyone on the Same
  Network", with "Require Password" off. Roomcast checks each speaker with
  one RTSP `OPTIONS` request, which doesn't interrupt playback, and flags
  any that refuse.
- **HomePod hardware pairs** can only be made in Apple's Home app; Apple has
  no public API for it. Roomcast pairs do the same job from the computer and
  leave your Home setup unchanged.
- **Sync** between speakers relies on AirPlay's own timing, which is close
  but not sample-exact. Mixing different speaker brands in one output may
  give a small offset.
- **Startup delay**: AirPlay starts streaming about 1–2 seconds after
  playback begins, because the speakers buffer audio.
