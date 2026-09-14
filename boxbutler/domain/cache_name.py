"""One sanitiser for filenames AND chapter titles (spec §3.5).

`<sanitised title> [<source_key>].<ext>` — the bracketed key is the idempotency key.
Never match on title.
"""
import re
import unicodedata

MAX_TITLE = 110
_UNSAFE = re.compile(r'[<>:"|?*\x00-\x1f]')
_NAME_RE = re.compile(r"^(?P<title>.*?)\s*\[(?P<key>[^\[\]/\\]+)\]\.(?P<ext>[A-Za-z0-9]+)$")


def sanitise_title(title: str, max_len: int = MAX_TITLE) -> str:
    t = unicodedata.normalize("NFKD", title)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = "".join(c if ord(c) < 128 else " " for c in t)
    t = t.replace("/", "-").replace("\\", "-")
    t = _UNSAFE.sub("", t)
    t = re.sub(r"\s+", " ", t).strip(" .-")
    t = t[:max_len].strip(" .-")
    return t or "untitled"


def cache_name(title: str, source_key: str, ext: str = "m4a") -> str:
    return f"{sanitise_title(title)} [{source_key}].{ext}"


def parse_cache_name(name: str) -> tuple[str, str, str]:
    m = _NAME_RE.match(name)
    if not m:
        raise ValueError(f"not a cache name: {name!r}")
    return m["title"], m["key"], m["ext"]
