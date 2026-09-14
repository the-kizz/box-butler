# Notes on the awkward parts

Why some of Box Butler's less obvious choices are the way they are. All of this was
measured against the real service rather than assumed, and each item is here because
getting it wrong produced a real failure.

## Ingest notes

**YouTube** extraction is pinned to format `140` (AAC, 129 kbps, 44.1 kHz stereo) via `yt-dlp` with
the `yt-dlp-ejs` JS-challenge solver and a Node.js runtime (`--js-runtimes node`). Both the pin and
the runtime are load-bearing, not incidental:

- Plain `yt-dlp` against current YouTube needs a JS challenge solved to extract at all; `yt-dlp-ejs`
  (installed from PyPI, pinned like any other dependency) supplies that solver without reaching out
  to fetch and execute arbitrary code at runtime the way some workarounds do.
- `-f bestaudio` fails outright under one of YouTube's ongoing server-side experiments: the formats
  it returns come back with missing URLs and the download errors out even though `-F` lists them
  fine. Format `140` is the one measured to keep working through that; format `251` (Opus), which
  would otherwise be the natural pick, hits the same failure. The pin stays until upstream `yt-dlp`
  closes that gap for good.

**Podcast RSS is preferred over YouTube wherever the content is available as a feed.** It's cheaper
and far more robust: no JS challenge, no format pinning, no server-side experiment to be broken by —
just a feed and stable enclosure URLs.

## A note on bedtime audio

Loudness normalisation (`loudnorm_default`) **defaults to off**, and that's deliberate rather than
an oversight. A lot of sleep and bedtime audio is mixed with a loud narrated section up front and a
deliberately quiet ambient or white-noise tail so a child drifts off — a wide loudness range on
purpose. Normalising that flattens the exact thing that makes it work, by raising the quiet tail
back up. Loudnorm is available per item for genuinely mixed-source playlists (tracks pulled from
different origins with inconsistent levels); it just isn't the right default for a story that's
already mixed the way it's supposed to sound.
