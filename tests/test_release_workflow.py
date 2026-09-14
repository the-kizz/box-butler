"""Static checks over the GHCR release workflow and packaging artifacts
(Task 37; spec §8/§12 follow-on).

These do NOT build the image, touch the network, or invoke docker/act -
they are plain file-content / YAML-structure assertions, run in CI like
any other test.
"""
import re
from pathlib import Path

import yaml

WORKFLOW_PATH = Path(".github/workflows/release.yml")


def test_dockerfile_has_no_host_specific_paths():
    """No estate-specific host paths or hostnames anywhere in the
    Dockerfile, entrypoint or example compose file - this repo is
    public."""
    forbidden = ["/srv", "/mnt", "lan.kizz.space", "kizserv", "kizz.space"]
    for path in [Path("Dockerfile"), Path("docker-entrypoint.sh"), Path("compose/box-butler.yml")]:
        text = path.read_text()
        for bad in forbidden:
            assert bad not in text, f"{bad!r} leaked into {path}"


def test_example_compose_uses_ghcr_pinned_tag():
    """The example compose file must reference a pinned GHCR tag, not a
    local build and not the floating :latest tag - an unattended `docker
    compose pull` on :latest can change the running version with no
    warning, which is a real cost for a service a child's bedtime
    depends on."""
    c = Path("compose/box-butler.yml").read_text()
    assert "build:" not in c, "example compose must not use a local build"
    m = re.search(r"^\s*image:\s*(\S+)", c, re.MULTILINE)
    assert m, "expected an `image:` line in compose/box-butler.yml"
    image = m.group(1)
    assert image.startswith("ghcr.io/"), f"expected a ghcr.io image reference, got {image!r}"
    assert not image.endswith(":latest"), "example must pin a specific version, not float on :latest"
    assert re.search(r":\d+\.\d+\.\d+$", image), f"expected a semver tag pin, got {image!r}"


def _load_workflow():
    return yaml.safe_load(WORKFLOW_PATH.read_text())


def test_workflow_builds_both_architectures():
    """Parse the workflow as YAML and inspect structure - not a substring
    search over the raw text, which would pass even if the platform
    string sat inertly in a comment."""
    wf = _load_workflow()

    # Trigger is a v* tag push.
    on = wf.get("on") or wf.get(True)  # PyYAML may parse bare `on:` as True
    assert on is not None, "workflow must define a trigger"
    push = on.get("push", {})
    tags = push.get("tags", [])
    assert any(re.match(r"^v\*", t) for t in tags), f"expected a v* tag trigger, got {tags!r}"

    jobs = wf["jobs"]
    assert "test" in jobs, "expected a `test` job"
    test_job = jobs["test"]
    test_steps_text = " ".join(
        str(step.get("run", "")) for step in test_job.get("steps", [])
    )
    assert "pytest" in test_steps_text, "the test job must actually run pytest, not a placeholder"

    # Find the build/publish job and confirm it depends on the test job.
    build_job = None
    build_job_name = None
    for name, job in jobs.items():
        if name == "test":
            continue
        needs = job.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        if "test" in needs:
            build_job = job
            build_job_name = name
            break
    assert build_job is not None, "expected a build/publish job with `needs: test` (or including test)"

    steps = build_job.get("steps", [])
    build_step = None
    for step in steps:
        uses = step.get("uses", "")
        if isinstance(uses, str) and "build-push-action" in uses:
            build_step = step
            break
    assert build_step is not None, f"expected a docker/build-push-action step in job {build_job_name!r}"

    platforms = build_step.get("with", {}).get("platforms", "")
    platform_list = [p.strip() for p in platforms.split(",")]
    assert "linux/amd64" in platform_list, f"expected linux/amd64 in platforms, got {platforms!r}"
    assert "linux/arm64" in platform_list, f"expected linux/arm64 in platforms, got {platforms!r}"

    # The push must be gated on the test job - either the build job's
    # `needs: test` (checked above) is the sole gate, or the push step
    # itself is additionally conditioned. Either way `needs: test` on the
    # job containing the push step is the load-bearing gate; assert the
    # build-push step actually pushes (push: true) so the gating is
    # meaningful and not a build-only no-op job.
    assert build_step.get("with", {}).get("push") in (True, "true"), (
        "the build-push-action step must actually push - otherwise gating it on tests is moot"
    )


def test_no_secret_names_leak():
    """Neither the workflow nor the image may reference Infisical,
    /etc/estate or any estate key name - this repository is public."""
    forbidden = ["Infisical", "infisical", "/etc/estate", "estate-secrets", "ESTATE_SECRETS"]
    texts = {
        "workflow": WORKFLOW_PATH.read_text(),
        "Dockerfile": Path("Dockerfile").read_text(),
        "docker-entrypoint.sh": Path("docker-entrypoint.sh").read_text(),
    }
    for name, text in texts.items():
        for bad in forbidden:
            assert bad not in text, f"{bad!r} leaked into {name}"
