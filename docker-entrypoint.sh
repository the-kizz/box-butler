#!/bin/sh
# Box Butler container entrypoint (Task 30).
#
# Runs briefly as root (the image's default USER, unset -> root) so it can
# fix ownership of bind-mounted /data and /cache, then permanently drops
# to an unprivileged uid/gid via gosu before exec'ing the real command.
# The application process itself never runs as root.
#
# Why this exists: a bind-mounted host directory (e.g. an operator's
# existing appdata folder) is very often owned by root, by a different
# uid, or doesn't exist yet. Without this fixup, the app fails on first
# write with a permission error - the single most common first-run
# failure for self-hosted non-root containers.
#
# PUID/PGID: the image bakes in a `boxbutler` user at uid/gid 1000 (see
# Dockerfile's `useradd -u 1000`), but an operator's host user is often a
# different uid. PUID/PGID (default 1000:1000, matching the baked-in
# user) let them choose which numeric uid/gid owns the bind-mounted data
# and runs the process, without us having to know in advance whether
# that uid has (or ever will have) an /etc/passwd entry. Rather than
# `usermod -u "$PUID" boxbutler` (which breaks if PUID collides with an
# existing account, and still leaves other files owned by the old uid),
# we chown straight to the numeric PUID:PGID and hand that same numeric
# pair to `gosu`, which accepts "uid:gid" directly and needs no passwd
# entry at all. This works identically whether or not PUID happens to
# match an existing user.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

for dir in /data /cache; do
    if [ -d "$dir" ]; then
        # Skip the recursive chown when ownership already matches - /cache
        # can hold tens of gigabytes of audio, and walking it on every
        # restart is a real, measurable startup delay, not a theoretical
        # one. Only the top-level directory is checked as a fast proxy for
        # "already correctly owned"; a fresh or freshly re-owned host
        # directory will differ at the top level and get the full chown.
        current_uid="$(stat -c '%u' "$dir")"
        current_gid="$(stat -c '%g' "$dir")"
        if [ "$current_uid" != "$PUID" ] || [ "$current_gid" != "$PGID" ]; then
            chown -R "${PUID}:${PGID}" "$dir" || true
        fi
    fi
done

# /media is intentionally never chowned here, even though it is now
# mounted read-write and Box Butler does write to it.
#
# /data and /cache are the app's own volumes: it created everything in
# them, so taking ownership is repair. /media is the exact opposite — it
# is the operator's audio library, very likely shared with Plex, Jellyfin,
# an *arr stack and whatever else the household runs, and plausibly
# terabytes of it. A recursive chown there would rewrite the ownership of
# every file the operator owns to suit us, which is precisely the
# "modifies what it did not create" behaviour the rest of this change
# exists to forbid, and would also take minutes to hours on every single
# restart.
#
# A *shallow* chown of just the top-level directory was considered and
# rejected too: it silently changes something the operator set
# deliberately, and it does not even solve the problem (the app also has
# to write inside per-library subfolders, which it would not have
# touched).
#
# So ownership of /media stays the operator's business, and Box Butler
# names the problem instead of papering over it: `require_media_root`
# (boxbutler/sources/library_folder.py) checks at startup that /media is
# mounted and writable by this uid, and refuses to start with a message
# telling the operator to drop `:ro` and set PUID/PGID to a uid that can
# write to their media directory. That is the same bargain Plex, Sonarr
# and Jellyfin strike, and it fails loudly instead of quietly mangling a
# media library.

exec gosu "${PUID}:${PGID}" "$@"
