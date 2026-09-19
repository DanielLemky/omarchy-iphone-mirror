# Development

## Application

- `mirror.py`: video session, MPV process, control socket, signals, and ordered shutdown.
- `orientation.py`: portrait/landscape view rotation, window aspect, and HID remapping.
- `usb_input.py`: focused-window input, Home/Spotlight toolbar, orientation follow, and explicit clipboard paste.
- `connection.py`: Auto selection, USB transport, and authenticated Wi-Fi discovery.
- `lifecycle.py`: instance lock, private status, and cleanup.
- `cli.py`: installed service control.

Each new session selects USB when available, otherwise Wi-Fi. There is no connection selector or settings dialog. Diagnostic CLI transport overrides remain available.

Landscape follow polls SpringBoard `getInterfaceOrientation` on the existing tunnel. It sets MPV `video-rotate` and resizes the window with MPV geometry plus Hyprland `resizewindowpixel` when the player pid is known. Taps are inverse-rotated into the encoded buffer. If iOS later re-encodes a landscape buffer, extra rotation is dropped so the picture is not turned twice. Orientation poll failures are logged by exception type only and do not stop video.

The application reuses pinned pymobiledevice3 RTP/HEVC receiver methods, but does not start its VNC server. MPV decodes the compressed video. Wi-Fi selection temporarily replaces the pinned library's provider selector while its process-wide tunnel lock is held, and restores it in `finally`. This private API dependency needs review when updating pymobiledevice3.

Shutdown releases input, requests stream stop, cancels owned receiver tasks, stops MPV, closes media/display transports, and leaves the tunnel last. Handshake retries are bounded. Image remounting is never automatic.

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
