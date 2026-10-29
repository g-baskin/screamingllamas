from screamingllamas.robots import RobotsRules, agent_token

URL = "https://example.com"


def test_longest_match_wins_and_allow_wins_ties() -> None:
    rules = RobotsRules.parse(
        "User-agent: *\nDisallow: /shop\nAllow: /shop/public\nDisallow: /tie\nAllow: /tie\n"
    )
    assert not rules.is_allowed(f"{URL}/shop/cart", "bot")
    assert rules.is_allowed(f"{URL}/shop/public/item", "bot")
    assert rules.is_allowed(f"{URL}/tie", "bot")
    assert rules.is_allowed(f"{URL}/other", "bot")


def test_wildcards_end_anchor_and_query() -> None:
    rules = RobotsRules.parse("User-agent: *\nDisallow: /*.pdf$\nDisallow: /*?sort=\n")
    assert not rules.is_allowed(f"{URL}/files/a.pdf", "bot")
    assert rules.is_allowed(f"{URL}/files/a.pdf?download=1", "bot")
    assert not rules.is_allowed(f"{URL}/list?sort=price", "bot")
    assert rules.is_allowed(f"{URL}/list?page=2", "bot")


def test_specific_group_beats_star_and_groups_merge() -> None:
    text = (
        "# comment\n"
        "User-agent: *\nDisallow: /\n\n"
        "User-agent: ScreamingLlamas\nUser-agent: other\nDisallow: /private\n\n"
        "user-agent: screamingllamas\nDisallow: /tmp  # trailing comment\n"
    )
    rules = RobotsRules.parse(text)
    assert rules.is_allowed(f"{URL}/page", "screamingllamas")
    assert not rules.is_allowed(f"{URL}/private/x", "screamingllamas")
    assert not rules.is_allowed(f"{URL}/tmp/x", "screamingllamas")
    assert not rules.is_allowed(f"{URL}/page", "somebot")


def test_empty_disallow_allows_everything_and_robots_txt_is_always_allowed() -> None:
    assert RobotsRules.parse("User-agent: *\nDisallow:\n").is_allowed(f"{URL}/x", "bot")
    rules = RobotsRules.parse("User-agent: *\nDisallow: /\n")
    assert not rules.is_allowed(f"{URL}/", "bot")
    assert rules.is_allowed(f"{URL}/robots.txt", "bot")


def test_non_ascii_patterns_match_encoded_urls() -> None:
    rules = RobotsRules.parse("\ufeffUser-agent: *\nDisallow: /café\n")
    assert not rules.is_allowed(f"{URL}/caf%C3%A9/menu", "bot")


def test_agent_token_and_blanket_rules() -> None:
    assert agent_token("ScreamingLlamas/0.1 (SEO crawler)") == "screamingllamas"
    assert RobotsRules.allow_all().is_allowed(f"{URL}/x", "bot")
    assert not RobotsRules.disallow_all().is_allowed(f"{URL}/x", "bot")


def test_hostile_wildcard_pattern_does_not_hang() -> None:
    rules = RobotsRules.parse("User-agent: *\nDisallow: /" + "*a" * 40 + "$\n")
    assert rules.is_allowed(f"{URL}/" + "a" * 5000 + "b", "bot")
    assert not rules.is_allowed(f"{URL}/" + "a" * 5000, "bot")
