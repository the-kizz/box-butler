# Third-party licences

Box Butler itself is MIT-licensed (see [LICENSE](LICENSE)). This file lists the third-party
software it depends on, invokes, or bundles into its published container image, and the licence
each is under.

## Invoked as a separate process (not linked)

### ffmpeg / ffprobe — GPL

[ffmpeg](https://ffmpeg.org/) is licensed under the GPL, and the exact version depends on how a
given build was configured. A Debian/Ubuntu `ffmpeg` — which is what the published container image
installs — is built with `--enable-gpl` but without `--enable-version3`, and its own copyright file
reports **GPL-2+**. A build configured with `--enable-version3` (for example to include the AMR
codecs) is GPLv3 instead. The authoritative answer for any particular image is that image's own
`/usr/share/doc/ffmpeg/copyright`, not this file — check it before redistributing. Box Butler calls the `ffmpeg` and `ffprobe` binaries as separate subprocesses via
`boxbutler/audio/ffmpeg.py` — it never links against ffmpeg's libraries. That means:

- **This project's own source code stays MIT.** Invoking a GPL program as a subprocess does not
  place the calling program under the GPL.
- **The GPL does apply to the binaries themselves.** A published container image that bundles
  `ffmpeg`/`ffprobe` is redistributing GPL-licensed software, and whoever builds and redistributes
  such an image takes on the same GPL obligations (source availability, licence notices) that apply
  to redistributing any GPL binary. If you build and redistribute your own image, that applies to
  you too.

## Python dependencies

| Package | Licence | Notes |
|---|---|---|
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Unlicense | YouTube/media extraction |
| [yt-dlp-ejs](https://pypi.org/project/yt-dlp-ejs/) | MIT | JS-challenge solver plugin for `yt-dlp`, installed from PyPI and pinned like any other dependency |
| [tonie-api](https://pypi.org/project/tonie-api/) | MIT | Client for the tonies cloud API. Attribution: `tonie-api`, © its respective authors — see the package's own licence text for the full notice |
| FastAPI, Starlette, Uvicorn, Jinja2, python-multipart, argon2-cffi, itsdangerous, PyYAML, prometheus-client, httpx, and their transitive dependencies | Various (MIT / BSD / Apache-2.0) | See `pyproject.toml` for the authoritative list and each package's own distribution for its exact licence |

## Frontend assets

| Asset | Licence | Notes |
|---|---|---|
| [Fira Sans / Fira Code](https://github.com/mozilla/Fira) | SIL Open Font License 1.1 | Fetched by `scripts/fetch-fonts.sh`, committed under `boxbutler/web/static/fonts/` |
| [HTMX](https://htmx.org/) | MIT (0-clause / zero-clause BSD-style, per the HTMX project) | Used by the web UI for partial-page updates |
| [SortableJS](https://github.com/SortableJS/Sortable) | MIT | Used by the web UI for drag-to-reorder |

No third-party audio, video, or other copyrighted media is bundled with or committed to this
repository. Anything Box Butler fetches at runtime (YouTube audio, podcast episodes, uploaded
files) is pulled by the operator running their own instance against their own accounts and
libraries, never distributed as part of this project.
