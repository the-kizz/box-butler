# The JS runtime yt-dlp needs, at a version it will actually accept.
# Debian bookworm's `nodejs` package is Node 20; yt-dlp's
# NodeJsRuntime.MIN_SUPPORTED_VERSION is (22, 0, 0), and anything below it
# is detected, labelled "unsupported" and then ignored -- leaving YouTube
# extraction to fail with a misleading "This video is not available".
# Pinned to a major, from the official image, rather than adding a
# third-party apt repo: same reasoning as installing yt-dlp-ejs from PyPI
# instead of fetching a solver from GitHub at runtime.
FROM node:22-bookworm-slim AS node

FROM python:3.12-slim AS css
# Pinned to an exact release, not "latest" - an unpinned build tool would
# make the image unreproducible. This is a build dependency, not a
# third-party service the operator would be exposed to at runtime.
ADD https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.13/tailwindcss-linux-x64 /usr/local/bin/tailwindcss
RUN chmod +x /usr/local/bin/tailwindcss
WORKDIR /src
COPY boxbutler/web ./boxbutler/web
COPY tailwind.config.js .
RUN tailwindcss -i boxbutler/web/static/src/input.css -o boxbutler/web/static/app.css --minify

FROM python:3.12-slim
# ffmpeg is GPL and is distributed in this image as a separate binary;
# Box Butler (MIT) invokes it as a subprocess via boxbutler/audio/ffmpeg.py
# and never links against it. See README.md for the disclosure.
#
# node is load-bearing, not incidental: yt-dlp needs a JS runtime
# (--js-runtimes node, paired with the yt-dlp-ejs package) to solve
# YouTube's challenge. Without one that yt-dlp *accepts*, extraction fails
# with "This video is not available" -- which reads like a bad URL rather
# than a broken image, and is how Node 20 shipped in 0.1.3.
#
# It is copied from the node stage rather than installed from apt, because
# Debian's package is too old (see the node stage above). Only the binary
# is needed: yt-dlp executes JS with it and never uses npm. It stays in
# this runtime stage, not only a build stage.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates tini gosu \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY boxbutler ./boxbutler
COPY --from=css /src/boxbutler/web/static/app.css ./boxbutler/web/static/app.css
RUN pip install --no-cache-dir . && useradd -u 1000 -m boxbutler

# Fail the BUILD, not the operator's evening.
#
# The text tests in tests/test_dockerfile.py check that this file pins a
# new enough node, but they cannot see what the image actually ended up
# with -- a base image retag, a cache hit or an edit to the copy above
# could still ship a runtime yt-dlp refuses. This asks the installed
# yt-dlp directly, in the finished image, and is the only check that knows
# the real answer. 0.1.3 shipped a node yt-dlp would not use and nothing
# noticed until every YouTube URL failed.
RUN python -c "\
from yt_dlp.utils._jsruntime import NodeJsRuntime; \
i = NodeJsRuntime().info; \
assert i, 'no node runtime found in the image'; \
assert i.supported, f'yt-dlp rejects the bundled {i.name} {i.version} (needs >= {NodeJsRuntime.MIN_SUPPORTED_VERSION[0]}): YouTube extraction would fail with \"This video is not available\"'; \
print(f'js runtime ok: {i.name} {i.version}')"


ENV BOXBUTLER_DATA_DIR=/data BOXBUTLER_CACHE_DIR=/cache BOXBUTLER_MEDIA_ROOT=/media PYTHONUNBUFFERED=1
# /media is deliberately not a VOLUME: it is a required bind mount of the
# operator's own audio library (see compose/box-butler.yml), and declaring
# it here would make Docker invent an anonymous volume for it when the
# operator forgets the mount — turning "you haven't mounted /media" into
# "downloads vanish into a volume nobody can find". Better to fail at
# startup naming the missing mount.
VOLUME ["/data", "/cache"]
EXPOSE 8410

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8410/healthz').status==200 else 1)"

# Entrypoint fixes up ownership of bind-mounted /data and /cache before
# dropping privileges (never /media — see docker-entrypoint.sh for why the
# operator's own media library is not ours to re-own): an operator bind-mounting a fresh or re-owned host
# directory onto /data or /cache is the single most common first-run
# failure for self-hosted containers (permission denied writing SQLite/
# audio as uid 1000). We start as root just long enough to chown, then
# re-exec as the unprivileged user via gosu - the app process itself never
# runs as root.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "boxbutler"]
