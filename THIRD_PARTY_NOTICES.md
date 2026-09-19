# Third-party software and license scope

Original code in this project is licensed under the MIT License, copyright 2026 Daniel Lemky. See `LICENSE`. This grant does not relicense third-party code or grant rights to code that the project author does not own. Any third-party code retains its applicable license and notices.

## pymobiledevice3

- Project: https://github.com/doronz88/pymobiledevice3
- Required version: 11.13.1
- Declared license: GPL-3.0-or-later (`GPL-3.0-or-later` in the installed package metadata).
- Upstream license: https://github.com/doronz88/pymobiledevice3/blob/v11.13.1/LICENSE
- Upstream source for the pinned version: https://github.com/doronz88/pymobiledevice3/tree/v11.13.1

The application imports pymobiledevice3 for device connections, pairing transport, display streaming, and input services. The installer downloads it into a private Python environment. The MIT license for our original code does not replace its GPL license or remove applicable GPL obligations for a combined distribution.

If you distribute a combined application containing GPL-covered code, comply with the applicable GPL terms, including corresponding-source and notice requirements. An upstream link alone is not a complete source-provision plan for a bundled distribution. This project does not claim that a combined application is MIT-only.

## Other dependencies

pymobiledevice3 installs additional Python dependencies. MPV and other host tools are installed separately. Each component keeps its own license. This notice is not a complete inventory of those components and does not replace their license texts or notices.

## Distribution scope

This source repository uses pymobiledevice3 as a library rather than copying its source. Only direct dependencies are pinned; transitive dependencies are not locked. This document is not a complete inventory of dependency or native-library licenses.

The source release does not include a Python environment, third-party binaries, Apple developer images, or pairing records. A bundled environment or binary release would require its own notices and corresponding-source plan.
