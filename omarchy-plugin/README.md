# iPhone Mirror Omarchy plugin

This optional thin plugin adds iPhone Mirror status and start/stop controls to the Omarchy bar. It calls the public `iphone-mirror` command. It does not access USB devices and does not manage the application process directly.

The icon is green while the application reports `running: true`. It is amber while the application starts or stops. It is dim when the application is stopped. The menu can start or focus the mirror and stop it. The plugin does not add key bindings.

The plugin sends a desktop notification only for an error. Normal start, stop, and status changes do not send notifications.

## Requirements

- `iphone-mirror` must be on the `PATH` used by Omarchy Shell.
- `iphone-mirror status` must print this JSON contract:

```json
{"running":true,"state":"running","error":null}
```

`state` must be `starting`, `running`, `stopping`, `disconnected`, `error`, or `stopped`. While running, status may also include `audio_muted` (boolean). The plugin ignores unknown fields.

## Install

Run these commands from the repository root after the application CLI is installed:

```sh
install -d "$HOME/.config/omarchy/plugins/quantumfire.iphone-mirror"
cp omarchy-plugin/BarWidget.qml \
  omarchy-plugin/manifest.json \
  omarchy-plugin/plugin_control.py \
  "$HOME/.config/omarchy/plugins/quantumfire.iphone-mirror/"
omarchy plugin validate "$HOME/.config/omarchy/plugins/quantumfire.iphone-mirror"
omarchy plugin enable quantumfire.iphone-mirror --section right
```

Omarchy Shell reloads user plugin files automatically. If it does not reload, run:

```sh
omarchy-shell shell rescanPlugins
```

To remove the bar item:

```sh
omarchy plugin disable quantumfire.iphone-mirror
rm -rf "$HOME/.config/omarchy/plugins/quantumfire.iphone-mirror"
```

## Verify the source

```sh
python3 -m unittest discover -s omarchy-plugin/tests -v
python3 -m py_compile omarchy-plugin/plugin_control.py
omarchy plugin validate omarchy-plugin
```

These checks do not start the application, connect an iPhone, access USB, or test the bar in a live Omarchy Shell session.
