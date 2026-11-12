# Third-party notices

## Lucide icons

The interface icons are taken, path for path, from the published `lucide-static` package, version 1.71.0, and built into
a local sprite in the page by `scripts/build_assets.py`. Nothing is loaded from a CDN at run time.

- Project: https://lucide.dev
- Package: `lucide-static` 1.71.0
- Licence: ISC

```
ISC License

Copyright (c) 2026 Lucide Icons and Contributors

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

---

The following Lucide icons are derived from the Feather project:

airplay, alert-circle, alert-octagon, alert-triangle, aperture, arrow-down-circle, arrow-down-left, arrow-down-right, arrow-down, arrow-left-circle, arrow-left, arrow-right-circle, arrow-right, arrow-up-circle, arrow-up-left, arrow-up-right, arrow-up, at-sign, calendar, cast, check, chevron-down, chevron-left, chevron-right, chevron-up, chevrons-down, chevrons-left, chevrons-right, chevrons-up, circle, clipboard, clock, code, columns, command, compass, corner-down-left, corner-down-right, corner-left-down, corner-left-up, corner-right-down, corner-right-up, corner-up-left, corner-up-right, crosshair, database, divide-circle, divide-square, dollar-sign, download, external-link, feather, frown, hash, headphones, help-circle, info, italic, key, layout, life-buoy, link-2, link, loader, lock, log-in, log-out, maximize, meh, minimize, minimize-2, minus-circle, minus-square, minus, monitor, moon, more-horizontal, more-vertical, move, music, navigation-2, navigation, octagon, pause-circle, percent, plus-circle, plus-square, plus, power, radio, rss, search, server, share, shopping-bag, sidebar, smartphone, smile, square, table-2, tablet, target, terminal, trash-2, trash, triangle, tv, type, upload, x-circle, x-octagon, x-square, x, zoom-in, zoom-out

The MIT License (MIT) (for the icons listed above)

Copyright (c) 2013-present Cole Bemis

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## MeloShelf Soundwave icon

The logo and favicon are the **MeloShelf Soundwave** icon: two flowing rose acoustic waves on a deep plum rounded-square
tile. It is the project's own brand asset and not a third-party work. `Assets/MeloShelf-logo.svg` is the editable vector with a
transparent background (the original square backdrop behind the rounded tile was removed; the artwork is unchanged). The
same file is kept at `src/meloshelf/presentation/web/brand/MeloShelf-Soundwave.svg` and the Windows icon is generated from it.
The vector is a hand-recreated version of the original generated image that preserves its design language and is not
pixel-identical to it.

---

## Components inside the Windows build (`packaging/`)

The standalone `MeloShelf.exe` bundles these components. Each keeps its own licence; the full licence texts are in the
`*.dist-info` folders next to the executable (`_internal`).

| Component | Version | Licence |
| --- | --- | --- |
| Python runtime (with Tcl/Tk for the small control window) | 3.13 | PSF License; Tcl/Tk BSD-style |
| mutagen (audio tags) | 1.48.1 | **GPL-2.0-or-later** |
| keyring | 25.7.0 | MIT |
| pywin32-ctypes | 0.2.3 | BSD-3-Clause |
| jaraco.classes / jaraco.functools / jaraco.context, more-itertools | see dist-info | MIT |
| PyInstaller bootloader (packaging tool) | 6.22.3 | GPL-2.0-or-later with the PyInstaller exception for distributed programs |

**Note on mutagen:** because the build bundles mutagen (GPL-2.0-or-later), giving the build to other people requires
complying with the GPL for the combined work, including offering the corresponding source. MeloShelf has not chosen a licence
of its own yet; the build is intended for personal use until that is decided.

**Chromaprint / fpcalc is not included.** Its licence is layered (the project's own code is MIT, it contains parts of FFmpeg
under LGPL-2.1, and binaries must also respect the licence of the FFT library they were built with), and no redistributable,
verified build was available to ship. MeloShelf detects an fpcalc installed by the user instead. See https://acoustid.org/chromaprint.
