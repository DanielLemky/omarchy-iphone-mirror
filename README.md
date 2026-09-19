# Omarchy iPhone Mirror

An on-demand USB or Wi-Fi mirror application with an optional Omarchy bar plugin.

**Early alpha — limited device compatibility.** Tested on ARM64 Omarchy with an iPhone 13 running iOS 27. Intel support is not verified.

## Install the alpha

Run as your normal user in a terminal, not with `sudo`:

```sh
curl -fsSL https://github.com/daniellemky/omarchy-iphone-mirror/releases/download/v0.1.0/install-online.sh | bash -s -- --release v0.1.0
```

This installs **v0.1.0 (early alpha)**, not development HEAD or a later release. The guide explains phone trust, Developer Mode, and Wi-Fi pairing. The viewer does not start automatically.

Read the [requirements](#requirements) and [installation details](#installation-details), including how to inspect the script before running it. Close any running mirror before installation.

The application owns video, input, and connection management. The plugin calls the application's public commands. It does not handle USB. The separate Bluetooth controller plugin is unchanged.

## Features

- Direct HEVC playback in MPV with low-latency settings and a local cursor.
- Taps, drags, vertical wheel scrolling, and focused-window keyboard input over USB or Wi-Fi.
- Centered Home and Spotlight buttons. Home uses a hardware-button event; Spotlight uses Command+Space. Neither uses a swipe.
- Input release on focus loss and shutdown. No toggle shortcut is required.
- One application instance, private runtime files, and structured status.
- Manual start only: no startup at login or when a USB cable is connected.
- No saved video or keystrokes. Diagnostic errors exclude input contents.

## Requirements

- Omarchy/Linux with a working systemd user session and Hyprland.
- Python 3.14 (tested baseline), MPV, usbmuxd, iproute2, and wl-clipboard.
- A trusted iPhone with Developer Mode enabled.
- A mounted developer image that provides the CoreDevice display and input services.
- Tested phone: iPhone 13, iOS 27.0 build 24A437, developer image 27A5228h.
- ARM64 Omarchy is the reference test host. Intel/x86-64 USB worked in a manual test, but Intel testing remains incomplete after repeated Wi-Fi playback failures. Intel support is not verified.

Compatibility with other models and versions is not yet established. AssistiveTouch and Bluetooth input are not required. If the Bluetooth controller is running, leave it in computer mode while using this application.

## Compatibility observations

These are results from specific phones and software combinations, not a complete support matrix or a proven minimum iOS version.

| Host | Phone / iOS | Result |
| --- | --- | --- |
| ARM64 Omarchy | iPhone 13, iOS 27.0 build 24A437, developer image 27A5228h | USB and Wi-Fi mirroring and input worked in manual tests. |
| Intel/x86-64 Omarchy | Known-working iPhone 13, iOS 27.0, developer image 27A5228h | The user confirmed USB was working. Wi-Fi opened but stopped with `player-backlog`; the failure repeated. Individual input checks have not been recorded separately. Intel support remains unverified. |
| Intel/x86-64 Omarchy | Second test phone, iOS 17.1.1 | The current USB tunnel requires iOS 17.4 or later. Wi-Fi connected and the display service was present, but it reported zero supported media features; video startup failed. |
| Intel/x86-64 Omarchy | Same second phone after updating to iOS 18.7.10, developer image 27A5228h | The image mounted and the USB tunnel connected. The display service still reported zero supported media features; usable mirroring was not established. |

The second phone's model has not been recorded. These results do not prove that all iOS 17 or 18 devices fail, that iOS 27 is required, or that Intel is the cause. The known-working iOS 27 phone also worked over USB on Intel. A mounted image and an advertised display service do not by themselves establish mirroring support.

The Intel machine also has other video and suspend problems. These do not establish the cause of the mirror failure. A diagnostic test showed software HEVC decoding at about one CPU core's capacity with the app's one-thread setting. A temporary hardware-decoding test was inconclusive. Decoder settings remain unchanged; ARM64 remains the release-test reference.

## Installation details

Repository: https://github.com/daniellemky/omarchy-iphone-mirror

### Inspect the alpha installer

The alpha command above selects an explicit published version. The script verifies the source archive's SHA-256 checksum before running the existing installer. Phone prompts use the terminal, not the pipe. System packages are not installed automatically, and the viewer is not started.

The checksum detects corrupt or mismatched downloads. It is not an independent signature: the script, archive, and checksum are trusted GitHub release assets. To inspect the script before running it:

```sh
curl -fL -o install-online.sh https://github.com/daniellemky/omarchy-iphone-mirror/releases/download/v0.1.0/install-online.sh
less install-online.sh
bash install-online.sh --release v0.1.0
```

For installation without phone setup, append `--skip-phone-setup` to the alpha command, keeping `--release v0.1.0`. Without a terminal, this option is required.

There is no stable release yet. Without `--release`, the bootstrap selects only the latest stable release; it will not silently install an alpha. Version v0.1.0 is marked as a pre-release. Do not use the `/releases/latest/download/` install route until a stable release is published.

The downloaded source stays in a unique directory under `${XDG_DATA_HOME:-$HOME/.local/share}/iphone-mirror-releases/`. The script prints the paths for repeating setup and uninstalling. It does not overwrite an existing checkout. A failed application install leaves this source available for a manual retry.

### Install from a source checkout

Clone or copy this repository, open a terminal in its directory, and close any running mirror. Run as your normal user, not with `sudo`:

```sh
./install.sh
```

If a dependency is missing, the installer prints an `omarchy pkg add` command. Run that command yourself, then repeat `./install.sh`. Installation needs access to Python packages unless you have configured an offline pip source.

The installer creates:

| Location | Purpose |
| --- | --- |
| `~/.local/share/iphone-mirror/` | Application and private Python environment |
| `~/.local/bin/iphone-mirror` | Command client |
| `~/.local/share/applications/iphone-mirror.desktop` | Application launcher with automatic connection selection |
| `~/.config/systemd/user/iphone-mirror.service` | On-demand user service |

The installer refuses to replace a running application and does not enable or start the service. In an interactive terminal, it continues directly into guided phone setup. Each trust, image-mount, or Wi-Fi-pairing operation requires separate confirmation. Use `./install.sh --skip-phone-setup` to skip the guide, for example during an update. Noninteractive installs also skip it.

XDG data and config overrides are supported. Spaces and single quotes in paths are tested. Paths containing backslashes, double quotes, dollar signs, backticks, percent signs, or line breaks are rejected before installation.

Only the direct pymobiledevice3 requirement is pinned. Transitive dependencies are not yet fully locked.

## Prepare the phone

Follow the guide that starts after installation, or run it again later:

```sh
./setup-phone.sh
```

You can instead follow the [manual phone setup instructions](docs/phone-setup.md). Installing the application alone does not complete phone preparation. Setup does not start the viewer, and successful setup commands do not prove display-service compatibility.

## Commands

```sh
iphone-mirror start       # Start, or focus the existing window
iphone-mirror stop        # Release input and stop the session
iphone-mirror restart     # Stop fully, then start
iphone-mirror status      # JSON status; no USB connection is opened
iphone-mirror reload-ui   # Redraw toolbar without restarting capture
```

Closing the viewer also stops the session. Existing window focus uses Hyprland. Logs are available with:

```sh
journalctl --user -u iphone-mirror -n 30 --no-pager
```

The old prototype uses `iphone-usb-mirror.service`. The new application refuses to run while that prototype service is active, so it cannot create a second stream by accident.

## Automatic connection

There is one **iPhone Mirror** launcher. Each new session uses USB if a matching phone is connected; otherwise it uses Wi-Fi. No connection selector or settings dialog is needed.

Connecting or removing a cable does not change an active session. Close and reopen the viewer to select the available connection again. An attached USB phone with a connection error does not silently fall back to Wi-Fi. `iphone-mirror start` focuses an existing session without changing it.

Explicit CLI options remain available for diagnostics: `--connection usb`, `--connection wifi`, and `--serial DEVICE_UDID`.

Wi-Fi requires an existing CoreDevice pairing record and a local network connection between the computer and phone. Discovery replaces fixed IP addresses. The application does not create new pairing records or change firewall settings. It rejects the iPhone USB-tethering interface in Wi-Fi mode. If several saved phone pairing records exist, use `--serial` to select one.

Wi-Fi video and input were tested with the USB cable disconnected. The user confirmed good operation. Locked-phone startup and network-loss recovery still need controlled testing.

## Update and remove

Keep the checkout for these commands. Obtain the intended source version before updating, then run:

```sh
iphone-mirror stop
./install.sh --skip-phone-setup
```

To remove the application:

```sh
./uninstall.sh
```

Removal stops the mirror and removes only its application, command, service, and launcher files. It keeps UI configuration, phone pairing records, developer images, and unrelated integrations. The optional Omarchy plugin must be removed separately if you installed it.

## Optional Omarchy plugin

Install the application first. Plugin source is in `omarchy-plugin/`. See its [installation instructions](omarchy-plugin/README.md).

The plugin provides running/error state and start/focus/stop actions. It adds no key bindings and sends notifications only for errors. Installing the application does not change the bar layout or enable the plugin.

## Paste text

With the viewer focused, press **Ctrl + V** to copy plain text from the computer clipboard to the iPhone clipboard and send the iPhone paste shortcut. Select a text field on the phone first. The phone or target app may require paste permission.

This needs `wl-paste` from `wl-clipboard` and the phone's CoreDevice pasteboard service. Text is read only for an explicit paste request, is limited to 1 MiB, and is not logged or saved to files. It is intentionally placed on the phone clipboard. Unicode text and line breaks are preserved. Images and files are not supported. Pending paste work is cancelled when the viewer loses focus; clipboard data already sent to the phone cannot be recalled.

## Toolbar appearance without restarting

Optional file: `~/.config/iphone-mirror/ui.json`:

```json
{"button_spacing":64,"icon_size":28}
```

Run `iphone-mirror reload-ui` after an edit. Values are bounded and invalid settings use defaults. `button_spacing` is the distance between the icon centers. This changes the toolbar without restarting the phone's media service.

## Development

```sh
./setup.sh
./run.sh                 # Foreground development; close window or use Ctrl+C to stop
.venv/bin/python -m unittest discover -s tests -v
python3 -m unittest discover -s omarchy-plugin/tests -v
```

The command client and plugin manage the installed systemd unit, not the foreground development process. The instance lock prevents the two from capturing at the same time. Changes to source files are not deployed until the application is reinstalled.

Tests use mocked device and connection services. The MPV integration test injects synthetic keys into headless MPV. No phone is needed. These tests do not establish real-device compatibility or connection reliability.

## Connection and recovery

The application releases input, requests that the phone stop its media stream, and then closes the player, media transport, display connection, and tunnel. It cancels only its own tasks. The user service sends the initial stop signal only to the application, so the player is not killed before this cleanup.

The prototype sometimes left the developer display service unresponsive after a restart. Remounting the same developer image restored it in testing. The new shutdown path addresses problems found in the old orchestration, but repeated real-phone restart testing is still required. A failure is reported instead of silently remounting or reconnecting forever.

**Developer-image recovery is explicit.** This application never mounts, unmounts, replaces, or downloads developer images automatically. Confirm the phone and image before a manual recovery operation.

## Known limits

- Lower video latency is confirmed by user testing of the prototype, but it has not been measured end to end.
- International keyboard layouts, IME, multitouch, and horizontal wheel scrolling are not complete.
- An input-service failure disables input but leaves video running. A fresh click attempts reconnection without replaying the click or failed keys.
- The pinned pymobiledevice3 RTP receiver is reused internally. The application does not start a VNC TCP server. It opens the media transport needed to receive the phone stream; this is not a claim that the application opens no network sockets.
- The complete locked-phone USB unlock flow still needs controlled validation. Passcodes must remain user-entered and must never be logged.

See [development instructions](docs/development.md).

## License

Original project code is licensed under MIT, copyright 2026 Daniel Lemky. See [LICENSE](LICENSE).

The application uses **pymobiledevice3, licensed GPL-3.0-or-later**. MIT does not remove applicable GPL obligations for a combined distribution. Dependencies keep their own licenses. See [third-party notices](THIRD_PARTY_NOTICES.md).

The application uses the library rather than copied upstream source. This is not a claim that the combined application is MIT-only.
