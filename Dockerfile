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
# nodejs is load-bearing, not incidental: yt-dlp needs a JS runtime
# (--js-runtimes node, paired with the yt-dlp-ejs package) to extract from
# YouTube. Without it, extraction silently degrades. It stays in this
# runtime stage, not only a build stage.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg nodejs ca-certificates tini gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY boxbutler ./boxbutler
COPY --from=css /src/boxbutler/web/static/app.css ./boxbutler/web/static/app.css
RUN pip install --no-cache-dir . && useradd -u 1000 -m boxbutler

ENV BOXBUTLER_DATA_DIR=/data BOXBUTLER_CACHE_DIR=/cache BOXBUTLER_MEDIA_ROOT=/media PYTHONUNBUFFERED=1
VOLUME ["/data", "/cache"]
EXPOSE 8410

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8410/healthz').status==200 else 1)"

# Entrypoint fixes up ownership of bind-mounted /data and /cache before
# dropping privileges: an operator bind-mounting a fresh or re-owned host
# directory onto /data or /cache is the single most common first-run
# failure for self-hosted containers (permission denied writing SQLite/
# audio as uid 1000). We start as root just long enough to chown, then
# re-exec as the unprivileged user via gosu - the app process itself never
# runs as root.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "boxbutler"]
