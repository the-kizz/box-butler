"""Static checks over the packaging artifacts (Task 30; spec §8, §12).

These do NOT build the image, touch the network, or invoke ffmpeg/yt-dlp —
they are plain file-content assertions, run in CI like any other test.

Note (deviation from the task-30 brief): the brief's original
`test_compose_fragment_matches_spec` asserted estate-specific paths
(`/srv/appdata/box-butler`, `/srv/compose/env/box-butler.env`,
`TZ=Australia/Melbourne`, a `frontend` network, a watchtower label) belong
in the *repo's* example compose file. Spec §8.1 says the opposite: nothing
estate-specific may appear in the image, the repo, or the example compose
file — this repository is public. The estate's real deployment fragment is
Phase 8 / Task 35's job, kept outside this repo. So `compose/box-butler.yml`
here is a neutral, portable example, and this test asserts both that the
portable forms are present AND that the estate-specific strings are absent.
"""
import re
from pathlib import Path


def _dockerfile_stages(text):
    """Split a Dockerfile into (stage_name_or_None, stage_text) tuples in
    order, one per `FROM` line, so tests can pin behaviour to a specific
    stage instead of the file as a whole."""
    stages = []
    current_name = None
    current_lines = []
    for line in text.splitlines(keepends=True):
        m = re.match(r"\s*FROM\s+\S+(?:\s+AS\s+(\S+))?", line, re.IGNORECASE)
        if m:
            if current_lines:
                stages.append((current_name, "".join(current_lines)))
            current_name = m.group(1)
            current_lines = [line]
        else:
            current_lines.append(line)
    if current_lines:
        stages.append((current_name, "".join(current_lines)))
    return stages


def test_dockerfile_pins_base_and_installs_ffmpeg_node():
    d = Path("Dockerfile").read_text()
    assert d.startswith("FROM python:3.12-slim") and "ffmpeg" in d and "nodejs" in d and "HEALTHCHECK" in d and "/healthz" in d
    assert "tailwindcss" in d  # CSS built at image-build time (§5)


def test_nodejs_is_installed_in_the_final_runtime_stage():
    """nodejs is load-bearing at runtime, not a build-time nicety: yt-dlp
    needs a JS runtime (--js-runtimes node, yt-dlp-ejs) to extract from
    YouTube, and without it extraction silently degrades rather than
    failing loudly. A test that only checks 'nodejs' appears somewhere in
    the Dockerfile would still pass if nodejs were moved into the `css`
    build stage alone and dropped from the runtime stage - this test
    parses the stages and pins it to the *last* one specifically."""
    d = Path("Dockerfile").read_text()
    stages = _dockerfile_stages(d)
    assert len(stages) >= 2, "expected at least a css build stage and a runtime stage"
    last_name, last_stage = stages[-1]
    assert last_name is None or "css" not in last_name.lower(), (
        "the final Dockerfile stage looks like the css build stage, not the runtime stage"
    )
    assert "nodejs" in last_stage, "nodejs must be installed in the final (runtime) stage, not only an earlier build stage"


def test_dockerfile_pins_tailwind_version_and_discloses_ffmpeg_gpl():
    d = Path("Dockerfile").read_text()
    # Pinned, not "latest" - unpinned would make the image unreproducible.
    assert "tailwindcss-linux-x64" in d
    assert "v3.4.13" in d
    # ffmpeg is GPL and shipped as a subprocess dependency, never linked.
    assert "GPL" in d and "subprocess" in d


def test_dockerfile_runs_as_non_root_and_uses_tini():
    d = Path("Dockerfile").read_text()
    assert "tini" in d
    assert "useradd" in d  # a dedicated unprivileged app user is created
    # The image must not hard-code a permanent `USER root` for the app
    # process; startup runs as root only briefly (see entrypoint) to fix
    # bind-mount ownership, then drops to the unprivileged user.
    assert "gosu" in d or "su-exec" in d


def test_entrypoint_fixes_bind_mount_ownership_and_drops_privileges():
    """The single most common first-run failure for a self-hosted
    non-root container is a bind-mounted host directory owned by the
    wrong uid. The entrypoint must chown /data and /cache to the app
    user before executing the real process, and must not remain root."""
    entry = Path("docker-entrypoint.sh").read_text()
    assert "chown" in entry
    assert "/data" in entry and "/cache" in entry
    assert "gosu" in entry or "su-exec" in entry
    assert "exec " in entry


def test_entrypoint_honours_puid_pgid_not_a_hardcoded_uid():
    """Task-30 review Major finding: PUID/PGID were declared in
    compose/box-butler.yml and documented as controlling ownership, but
    docker-entrypoint.sh hardcoded `chown -R boxbutler:boxbutler` against
    the fixed uid 1000 baked in at image build - the knob did nothing.
    A string-presence check for "PUID" being merely mentioned can't catch
    that class of bug (a declared variable that is never read is
    structurally invisible to it), so this test additionally asserts the
    chown target and the process the entrypoint execs are *derived from*
    PUID/PGID rather than the hardcoded boxbutler:boxbutler name/uid."""
    entry = Path("docker-entrypoint.sh").read_text()
    assert "PUID" in entry and "PGID" in entry
    assert "chown -R boxbutler:boxbutler" not in entry, (
        "chown target must be derived from PUID/PGID, not hardcoded to the fixed uid-1000 user"
    )
    assert re.search(r'chown\s+-R\s+"?\$\{?PUID\}?:\$\{?PGID\}?"?', entry), (
        "expected the chown target to reference $PUID:$PGID directly"
    )
    # gosu must drop to the effective PUID/PGID, not the fixed `boxbutler`
    # user name - that way a PUID with no /etc/passwd entry still works,
    # since gosu accepts a raw "uid:gid" pair.
    assert re.search(r'gosu\s+"?\$\{?PUID\}?:\$\{?PGID\}?"?', entry), (
        "expected gosu to exec as $PUID:$PGID, not a fixed user name"
    )
    # Defaults must still land on 1000:1000 to match the image's baked-in
    # `useradd -u 1000` user for operators who set neither variable.
    assert re.search(r'PUID:-1000', entry) and re.search(r'PGID:-1000', entry)


def test_media_is_never_a_chown_target():
    """/media is mounted read-only (compose/box-butler.yml). Chowning a
    read-only bind mount fails and would break startup for every operator
    using /media, so it must never appear as a chown target."""
    entry = Path("docker-entrypoint.sh").read_text()
    for line in entry.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "chown" in stripped:
            assert "/media" not in stripped


def test_cache_chown_is_conditional_not_unconditional():
    """/cache can hold tens of gigabytes of audio; an unconditional
    recursive chown on every boot is a real startup delay even when
    ownership is already correct. The entrypoint must check ownership
    before recursing."""
    entry = Path("docker-entrypoint.sh").read_text()
    assert "stat" in entry, "expected an ownership check (e.g. stat) before the recursive chown"
    # The chown call itself should be reachable only through a conditional
    # (an `if` gating it), not run unconditionally for every directory.
    assert re.search(r'if\s*\[.*\]\s*;?\s*then', entry)


def test_every_compose_env_var_is_read_somewhere_in_the_image():
    """General guard for the whole class of bug PUID/PGID exemplified: an
    env var can be declared and documented in the compose example without
    anything in the image ever reading it. Every variable named in
    compose/box-butler.yml's `environment:` block must be referenced by
    name somewhere in the image's own scripts (Dockerfile,
    docker-entrypoint.sh) or in the application source that gets baked
    into the image (boxbutler/**/*.py) - this would have caught the
    PUID/PGID dead-letter bug and will catch the next one of its kind."""
    c = Path("compose/box-butler.yml").read_text()
    env_block = c.split("environment:", 1)[1].split("env_file:", 1)[0]
    names = sorted(set(re.findall(r"^\s*-\s*([A-Za-z_][A-Za-z0-9_]*)=", env_block, re.MULTILINE)))
    assert names, "expected at least one declared environment variable in compose/box-butler.yml"

    haystack = Path("Dockerfile").read_text() + Path("docker-entrypoint.sh").read_text()
    for py_file in Path("boxbutler").rglob("*.py"):
        haystack += py_file.read_text()

    for name in names:
        referenced = (
            f"${name}" in haystack
            or f"${{{name}" in haystack
            or f'"{name}"' in haystack
            or f"'{name}'" in haystack
        )
        assert referenced, f"{name} is declared in compose/box-butler.yml but is never read anywhere in the image"


def test_dockerfile_default_command_is_not_an_applying_run():
    """The container's default command must be `serve()` (python -m boxbutler),
    never a one-shot applying CLI invocation - the scheduler inside serve()
    owns the one legitimate unattended apply."""
    d = Path("Dockerfile").read_text()
    assert '"python", "-m", "boxbutler"' in d or "python -m boxbutler" in d
    assert "--apply" not in d


def test_compose_fragment_is_portable_and_neutral():
    c = Path("compose/box-butler.yml").read_text()
    for s in [
        "8410:8410",
        "./data:/data",
        ":/cache",
        ":/media:ro",
        "restart: unless-stopped",
        "env_file: .env",
    ]:
        assert s in c, s


def test_compose_fragment_has_no_estate_specific_paths():
    """This repo is public - nothing about one operator's private
    infrastructure layout may appear in the example compose file."""
    c = Path("compose/box-butler.yml").read_text()
    for forbidden in [
        "/srv/appdata",
        "/srv/compose",
        "/srv/downloads",
        "/mnt/media",
        "/etc/estate",
        "lan.kizz.space",
        "Australia/Melbourne",
        "frontend",
        "watchtower",
    ]:
        assert forbidden not in c, f"estate-specific string leaked into public compose example: {forbidden!r}"


def test_env_example_has_names_only():
    e = Path("compose/box-butler.env.example").read_text()
    assert "BOXBUTLER_SINK_PASSWORD=" in e
    assert all(
        line.split("=", 1)[1].strip() in ("", "changeme")
        for line in e.splitlines()
        if "=" in line and not line.startswith("#")
    )


def test_env_example_has_no_estate_paths_or_credentials():
    e = Path("compose/box-butler.env.example").read_text()
    for forbidden in ["/etc/estate", "lan.kizz.space", "Infisical", "infisical"]:
        assert forbidden not in e


def test_dockerignore_excludes_dev_cruft():
    di = Path(".dockerignore").read_text()
    for s in [".venv", ".git", "*.egg-info", "__pycache__", "tests", ".pytest_cache"]:
        assert s in di, s
