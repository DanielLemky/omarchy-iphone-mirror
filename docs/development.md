# Development

## Application

- `mirror.py`: video session, MPV process, control socket, signals, and ordered shutdown.
- `orientation.py`: portrait/landscape view rotation, window aspect, and HID remapping.
- `usb_input.py`: focused-window input, Home/Spotlight toolbar, orientation follow, and explicit clipboard paste.
- `connection.py`: Auto selection, USB transport, and authenticated Wi-Fi discovery.
- `lifecycle.py`: instance lock, private status, and cleanup.
- `cli.py`: installed service control.

Each new session selects USB when available, otherwise Wi-Fi. There is no connection selector or settings dialog. Diagnostic CLI transport overrides remain available.

Landscape follow polls SpringBoard `getInterfaceOrientation` on the existing tunnel. It sets MPV `video-rotate` and resizes the window with MPV geometry plus Hyprland `resizewindowpixel` when the player pid is known. Taps and vertical wheel scrolls are inverse-rotated into the encoded buffer. If iOS later re-encodes a landscape buffer, extra rotation is dropped so the picture is not turned twice. Orientation poll failures are logged by exception type only and do not stop video.

The application reuses pinned pymobiledevice3 RTP/HEVC receiver methods, but does not start its VNC server. Each receiver decoder generation uses a `ReceiverDecoder` adapter. Receiver recovery can retire that generation without closing the MPV window or its input pipe. The next generation sends fresh parameter sets and a keyframe to the same player. Only application shutdown closes the player. A failed player input pipe ends the application with `player-pipe-failed`; it does not offer Retry with a stopped writer thread. Non-user stop codes and pipe exception types are logged without input or device exception contents. MPV decodes the compressed video. Wi-Fi selection repeats discovery and permitted endpoint connection attempts within a finite provider budget. The outer tunnel-opening limit remains 30 seconds. Missing or multiple pairing records fail without discovery retries. Discovery failures use fixed local error types so the viewer can distinguish a missing Wi-Fi advertisement from a failed endpoint connection without displaying device exception text. Wi-Fi selection temporarily replaces the pinned library's provider selector while its process-wide tunnel lock is held, and restores it in `finally`. This private API dependency needs review when updating pymobiledevice3.

Shutdown releases input, closes the stream-start display connection, requests stream stop on a fresh display connection, cancels owned receiver tasks, stops MPV, closes media/display transports, and leaves the tunnel last. The stop connection sends only one reply-bearing request: `com.apple.coredevice.feature.stopmediastream` with `{"stopAll": true}`. It must not send a status request or a start request first. This follows the [upstream protocol correction](https://github.com/doronz88/pymobiledevice3/commit/480b8a3eccec31ac41c1bf80b479d1d0cdb9ba00) without changing the pinned dependency. The stop affects all CoreDevice media streams on the phone. EOF, reset, or a broken pipe is not treated as proof of device teardown. Any status query must use another fresh connection; empty sessions do not prove camera restoration. Handshake retries are bounded. At startup, USB sessions check for a mounted image. Paired Wi-Fi sessions use one tunnel for capture if the display service is already available; they do not query the image service in that case. If the display service is missing, Wi-Fi startup checks the mounted images. If none is mounted, USB or Wi-Fi startup can mount the verified, pinned image from the local cache within a 90-second preparation limit. It never replaces or unmounts an existing image. After Wi-Fi image preparation, it closes the preparation tunnel before capture opens a new tunnel to discover the added display service.

## Installation

`install.sh`, `uninstall.sh`, and `packaging/install_support.py` manage exact user-local destinations. File replacement is serialized and the application instance lock prevents capture during installation. Obsolete Wi-Fi/Auto launcher files are removed as part of the same rollback-aware operation. Unrecovered backups are retained if restoration fails.

The installer does not start or enable the viewer. Its `setup-phone.py` guide starts by default in an interactive terminal and requires separate approval for phone changes. `--skip-phone-setup` bypasses the guide. `setup-phone.sh` runs the installed guide. The guide does not reverse completed phone changes if cancelled.

## Tests

Run `./setup.sh` to create the development environment, then:

```sh
.venv/bin/python -m unittest discover -s tests -v
python3 -m unittest discover -s omarchy-plugin/tests -v
```

Installer tests use temporary HOME/XDG paths and mock systemctl, venv/pip, and host commands. Phone setup tests mock all phone operations. MPV tests use synthetic input. These tests do not establish real-phone or x86-64 compatibility.

Never log clipboard contents, typed keys, pointer positions, passcodes, video frames, or arbitrary device exception contents. Clipboard paste intentionally places text on the phone clipboard; it does not save text in local diagnostic files.

## Build a source release

Commit the release changes, then choose an empty output directory outside the repository:

```sh
./packaging/build-release.sh /tmp/iphone-mirror-release
```

The builder archives committed files only. It refuses a dirty checkout or a non-empty output directory. It does not publish anything.

Attach all three output files to a GitHub release at that source commit:

- `install-online.sh`
- `iphone-mirror.tar.gz`
- `iphone-mirror.tar.gz.sha256`

Mark alpha releases as pre-releases. Their install command must download the bootstrap from a version-specific URL and pass the same tag with `--release TAG`. The bootstrap checks the returned tag, verifies the archive checksum, and rejects unsafe archive entries. Without `--release`, it selects stable releases only. Keep existing published assets unchanged when releasing a new version.

The checksum detects corruption or mismatched files; it is not an independent signature. Transitive dependencies are not locked. A source release must not include local environments, Apple images, pairing records, or captured content.

The optional Omarchy plugin remains separate and is never enabled by the application installer.
