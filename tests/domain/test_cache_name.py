import pytest
from boxbutler.domain.cache_name import sanitise_title, cache_name, parse_cache_name, MAX_TITLE

def test_emoji_stripped():
    assert sanitise_title("The Wobbling Badger! 🌙😴 Bedtime Story") == "The Wobbling Badger! Bedtime Story"

def test_path_unsafe_characters_removed_or_replaced():
    assert sanitise_title('A/B\\C:D*E?F"G<H>I|J') == "A-B-CDEFGHIJ"

def test_overlong_title_capped():
    long = "word " * 100
    out = sanitise_title(long)
    assert len(out) <= MAX_TITLE and not out.endswith(" ")

def test_whitespace_collapsed_and_trailing_dots_dashes_stripped():
    assert sanitise_title("  Hello   world .- ") == "Hello world"

def test_empty_becomes_untitled():
    assert sanitise_title("🍝🍝🍝") == "untitled"

def test_accents_folded():
    assert sanitise_title("Café Über") == "Cafe Uber"

def test_duplicate_titles_different_ids_are_distinct_names():
    a = cache_name("Same Title", "QzuMO0t5JUI")
    b = cache_name("Same Title", "dQw4w9WgXcQ")
    assert a != b and a.startswith("Same Title [")

def test_bracketed_id_round_trips():
    name = cache_name("Story 🍝 [not an id]", "QzuMO0t5JUI", "m4a")
    title, key, ext = parse_cache_name(name)
    assert key == "QzuMO0t5JUI" and ext == "m4a" and "🍝" not in title

def test_parse_rejects_name_without_id():
    with pytest.raises(ValueError):
        parse_cache_name("no id here.m4a")

def test_chapter_title_uses_same_rule_as_filename():
    # §3.5: one sanitiser, one behaviour — the tonie-card title is sanitise_title() too
    seo = "🌙 Relaxing Bedtime Story to Help Kids Sleep | The Wobbling Moon | " + "SEO tail " * 30
    out = sanitise_title(seo)
    assert "🌙" not in out and len(out) <= MAX_TITLE
