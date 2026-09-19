# Agent-assisted installation and phone setup

The updated v0.1.0 alpha release includes these commands. Older downloads of v0.1.0 do not; download the current release again before testing. Use the README install command with `--skip-phone-setup` appended, then run these commands from the retained source directory printed by the installer. The existing interactive guide is unchanged. Real-phone validation of the new commands is still pending.

An agent can run computer commands and explain the next phone action. It cannot enter your passcode or approve Trust and Developer Mode prompts for you.

## Commands

Install a version containing this feature first. From its retained source directory:

```sh
./setup-phone.sh plan
./setup-phone.sh check
./setup-phone.sh pair-usb --approve
./setup-phone.sh reveal-developer-mode --approve
./setup-phone.sh prepare-image --approve
./setup-phone.sh pair-wifi --approve
./setup-phone.sh check-display
```

Without arguments, `./setup-phone.sh` still opens the normal interactive guide. Named commands return one JSON object and do not need a terminal. They use the installed private environment, not an arbitrary system Python environment.

`--approve` is an assertion that the agent has obtained permission for that one operation. It is not proof of human consent. Agents must never add it merely because installation was requested.

| Command | Effect |
| --- | --- |
| `plan` | No phone access. Returns the sequence, security explanation, and human-only tasks. |
| `check` | Read-only USB check: iOS version, Developer Mode, mounted-image count, and local Wi-Fi pairing presence/count. Does not pair or mount anything. |
| `pair-usb` | Requests USB trust and saves pairing credentials. The user handles the phone prompt. |
| `reveal-developer-mode` | Reveals the setting. Does not enable it, restart, or approve the post-restart prompt. |
| `prepare-image` | Checks prerequisites and mounts an image only if none is mounted. Never unmounts or replaces an image. |
| `pair-wifi` | Creates/confirms the separate Wi-Fi pairing over USB. Does not remove other records. |
| `check-display` | Opens a temporary USB tunnel and queries media capabilities. Does not start video, send input, or save screen content. |

Except for `plan`, commands require one USB iPhone and an inactive viewer. Setup holds the application lock. Connection creation disables automatic USB pairing. Each approved change runs once. Individual operations have time limits; an outer worker process has a six-minute limit. A timeout does not mean that the phone reversed the operation. Run `check` before requesting another change.

The phone must remain the same between approval and execution. If the user changes phones, stop and obtain approval for the new phone. Pairing records and device identifiers are not included in JSON. Counts and version/capability information are included.

## Results

Example without approval:

```json
{"schema_version":1,"action":"pair-usb","ok":false,"code":"approval_required","message":"Obtain explicit user approval for this operation, then repeat with --approve.","data":{"title":"Trust this computer","effect":"Saves USB trust credentials. Approve Trust and enter the passcode only on the phone."}}
```

- `schema_version`: result format version.
- `action`: requested operation, or `unknown` if parsing/worker startup failed.
- `ok`: whether that operation completed, not whether mirroring works.
- `code`: fixed result category for agent decisions.
- `message`: fixed explanation without arbitrary device exception text.
- `data`: selected non-content status fields. `mirroring_verified: false` is intentional.

Exit codes: `0` completed, `1` failed or blocked, `2` invalid arguments, `3` approval needed, `130` cancelled. Check both the exit code and JSON. A successful `check` can still report Developer Mode off or an unsupported USB version.

`phone_action_required` means the reveal request completed but the user must still enable Developer Mode. `display_features_available` means capabilities were reported, not that playback or input was tested. `display_features_unavailable` means the service reported zero media features; stop and explain the compatibility limit. Never invent results if the worker returns no valid JSON.

## Prompt to give an agent

> Help me install and set up Omarchy iPhone Mirror. Read the repository README and agent setup instructions first. Use the documented release, not development HEAD, unless I explicitly approve development testing. If that release lacks the structured setup commands, explain this and stop rather than inventing commands.
>
> Check computer requirements and existing installation. Ask before adding system packages. Do not use sudo for the app installer. Install with phone setup skipped so that you can request each approval separately. Preserve unrelated applications, settings, and pairing records.
>
> Run `./setup-phone.sh plan`, then guide me one step at a time. Ask me to connect one iPhone and unlock it. Keep other phones disconnected. Run `check`; a missing trust connection is not permission to create one.
>
> Before each phone-changing command, explain its effect and ask for my approval. Use `--approve` only for the operation I approved. USB trust saves credentials. Revealing Developer Mode only makes its setting visible. Mounting may download a developer image. Wi-Fi pairing saves a separate network credential. Silence or a general request to install is not approval for these operations.
>
> Explain that Developer Mode reduces device security and permits developer-service access from trusted computers. Protect the computer and its pairing records. Turning off Developer Mode does not erase trust. Ask whether I understand and want to continue before revealing or enabling it.
>
> I must handle phone actions myself. Tell me to open Settings, Privacy & Security, Developer Mode; enable it; approve the restart; unlock with my passcode; and confirm Turn On after restart. Wait for me. Never ask me to disclose my passcode. Then run `check` again. Do not enable or restart the phone automatically.
>
> Check every JSON result. Stop on incompatible capabilities or an unexplained failure. Do not repeat failed changes automatically, replace mounted images, delete pairing records, change firewall settings, or enable automatic startup. If an operation times out, run a read-only check before discussing another attempt. A completed command does not prove mirroring works.
>
> After successful capability checks, ask before opening the viewer. Test USB first. Ask me to confirm live video and input. For Wi-Fi, confirm the separate pairing step, close the viewer, ask me to disconnect USB, and start a new session. Multiple saved Wi-Fi records need explicit device selection; do not delete them to make selection easier.
>
> Do not capture or log the screen, keystrokes, pointer positions, clipboard contents, passcodes, or pairing records. Do not change the Bluetooth controller. Finish with the installed version, completed steps, user-confirmed test results, remaining limits, and the paths for repeating setup and uninstalling.
