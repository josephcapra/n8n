"""Unit tests for the website-health checker's pure logic (no network)."""
from datetime import datetime, timezone

from tools import website_health as wh


def test_robots_blocks_all_only_for_wildcard_agent():
    # The live site: '*' blocks only admin paths; a single bot blocks '/'. NOT a block.
    live = (
        "User-agent: *\n"
        "Disallow: /admin/\n"
        "Disallow: /api/\n"
        "User-agent: oodlebot\n"
        "Disallow: /\n"
    )
    assert wh._robots_blocks_all(live) is False
    # A real site-wide block on the wildcard agent.
    assert wh._robots_blocks_all("User-agent: *\nDisallow: /\n") is True
    # Blank/no rules.
    assert wh._robots_blocks_all("User-agent: *\nDisallow:\n") is False


def test_grade_maps_severity_to_letter_and_light_inputs():
    clean = [wh.Finding("a", "ok", "ok", "")]
    assert wh._grade(clean)[0] == "A"
    # any critical -> F
    crit = [wh.Finding("x", "down", "critical", "")]
    assert wh._grade(crit)[0] == "F"
    # a couple of mediums stays in the green/yellow band, not failing
    mids = [wh.Finding(str(i), "m", "medium", "") for i in range(2)]
    assert wh._grade(mids)[0] in ("A", "B")


def test_newest_date_parses_iso_variants():
    got = wh._newest_date(["2026-01-01", "2026-05-20T10:00:00Z", "garbage"])
    assert got == datetime(2026, 5, 20, 10, 0, 0, tzinfo=timezone.utc)
    assert wh._newest_date(["not-a-date"]) is None


def test_seo_basics_flags_missing_and_passes_complete():
    bare = "<html><head></head><body>hi</body></html>"
    ids = {f.id for f in wh._check_seo_basics(bare) if f.severity != "ok"}
    assert {"seo_title", "seo_desc", "seo_viewport", "seo_schema"} <= ids

    full = (
        '<html><head><title>Paradise Realty</title>'
        '<meta name="description" content="x">'
        '<meta name="viewport" content="width=device-width">'
        '<link rel="canonical" href="https://x/">'
        '<meta property="og:title" content="x"><meta property="og:image" content="y">'
        '<script type="application/ld+json">{}</script>'
        "</head><body>hi</body></html>"
    )
    assert all(f.severity == "ok" for f in wh._check_seo_basics(full))
