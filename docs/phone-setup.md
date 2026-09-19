# Prepare the iPhone

Install the application first. Phone preparation is separate from installation. These steps can change trust, developer access, and the mounted developer image. The installer and viewer do not perform them automatically.

Only the combination in the README has been tested. This is not a promise that other iOS versions or developer images provide the required display service.

## 1. Connect and trust

Connect one iPhone by USB. Unlock it and accept the Trust prompt if shown. Enter any passcode on the phone yourself. Never put a passcode in a command or diagnostic report.

Use the installed private Python environment:

```sh
PY="${XDG_DATA_HOME:-$HOME/.local/share}/iphone-mirror/venv/bin/python"
"$PY" -m pymobiledevice3 usbmux list --usb
```

This checks USB discovery; it does not establish that the display service works. The output can contain device identifiers. Do not paste it into a public issue without removing them.

USB trust saves pairing credentials through usbmuxd. On Linux those files are usually `/var/lib/lockdown/*.plist`. They contain host keys. Do not copy them or attach them to a public issue. Uninstalling this application does not delete them.

## 2. Enable Developer Mode

The guided setup offers to reveal the Developer Mode setting before you open Settings. This only makes the setting visible; it does not enable Developer Mode or restart the phone. To request this manually with the phone connected, trusted, and unlocked:

```sh
"$PY" -m pymobiledevice3 amfi reveal-developer-mode
```

Close and reopen Settings after the command. On the iPhone, open **Settings → Privacy & Security → Developer Mode**. Follow the phone's instructions, including the restart and confirmation if required.

Developer Mode changes the phone's security posture. Enable it only if you intend to use developer services. If the setting is absent, consult the setup instructions for your iOS version rather than repeatedly changing host settings.

## 3. Check the developer image

Close any running mirror before changing an image. Check the mounted images:

```sh
"$PY" -m pymobiledevice3 mounter list
```

If an image is needed, the pinned tool provides this **explicit, state-changing operation**:

```sh
"$PY" -m pymobiledevice3 mounter auto-mount
```

It can download and mount a developer image. Read its help before use:

```sh
"$PY" -m pymobiledevice3 mounter auto-mount --help
```

Automatic image selection does not guarantee that the image includes the required display service. An already mounted older image can also lack that service. Do not unmount or replace an image just because a connection failed. First check the device, image version, and diagnostic error. The application never remounts an image as automatic recovery.

## 4. Optional Wi-Fi pairing

USB mirroring does not require this separate network pairing step. For Wi-Fi, keep the phone connected by trusted USB and unlocked. The following command creates a saved CoreDevice pairing record if needed:

```sh
"$PY" -m pymobiledevice3 lockdown remotepairing --pair
```

This is a **state-changing pairing operation**. Over an already trusted USB connection, it can complete without a new Trust prompt. It writes a separate CoreDevice record under `${XDG_DATA_HOME:-$HOME/.local/share}/pymobiledevice3/` (`remote_*.plist`). Treat that file as sensitive and never include it in a public issue or repository. Uninstalling this application does not delete it.

Connect the phone and computer to the same local network. Disconnect USB before verifying wireless discovery, so USB tethering cannot be mistaken for Wi-Fi:

```sh
"$PY" -m pymobiledevice3 remote browse
```

Discovery output can contain device identifiers and network addresses. Network isolation can prevent discovery. These instructions do not open firewall ports or disable network security controls.

## Pairing records on this computer

These paths are host locations, not phone contents. They are enough to authenticate this computer to a trusted iPhone.

| Kind | Typical Linux location | Created by |
| --- | --- | --- |
| USB trust | `/var/lib/lockdown/*.plist` | usbmuxd, during USB pairing |
| Wi-Fi / CoreDevice pairing | `${XDG_DATA_HOME:-$HOME/.local/share}/pymobiledevice3/remote_*.plist` | Wi-Fi pairing |
| Developer image cache | `${XDG_DATA_HOME:-$HOME/.local/share}/pymobiledevice3/` | Image download / mount |

usbmuxd often leaves USB pairing files world-readable, and `/run/usbmuxd` is often reachable by every local account. This application does not change those host permissions. On a shared computer, treat USB trust as available to other local accounts. Restricting `/var/lib/lockdown` requires root and may be reset the next time usbmuxd saves a record.

Downloaded images and Wi-Fi records under the pymobiledevice3 data directory are owned by your user. They are still credentials or large developer artifacts; do not share that directory.

Turning off Developer Mode does not remove these files. `./uninstall.sh` does not remove them either. To revoke this computer, unpair or turn off Developer Mode on the phone and delete the local records yourself.

## 5. Start the viewer

Installation adds **iPhone Mirror** to the Omarchy application launcher. After phone setup, open the launcher, search for **iPhone Mirror**, and select it.

Each launch uses USB if connected, otherwise Wi-Fi. Close the viewer with **Super + W**. It does not start automatically at login or when a cable is connected.

You can also start it from a terminal:

```sh
iphone-mirror start  # USB if connected, otherwise Wi-Fi
```

There is one launcher and no connection selector. The transport is selected only at startup; connecting or removing a cable does not switch an active session. Close and reopen the viewer to select again.

An active USB or Wi-Fi connection does not prove that the phone is unlocked. This version has no lock-state or screen-power indicator.
