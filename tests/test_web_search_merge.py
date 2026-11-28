from __future__ import annotations

from llm_gateway_core.api.v1.web_search_merge import (
    _normalized_url_key,
    _registrable_domain,
    dedupe_near_duplicates,
    merge_free_results,
)


def _source(url: str, title: str = "", snippet: str = "") -> dict:
    return {"url": url, "title": title, "snippet": snippet}


_QUAKE_TITLE = "Breaking: Major Earthquake Strikes Coastal Region"
_QUAKE_SNIPPET = "A magnitude 7.1 earthquake struck the coastal region early today, authorities reported minor damage."
_PREPARE = _source(
    "https://prepare.example/guide",
    "Earthquake preparedness guide",
    "How to prepare your home and family for a major seismic event.",
)
_REPORTS = _source(
    "https://reports.example/damage",
    "Recent earthquake damage reports",
    "Officials assessed destroyed buildings after last week's tremor.",
)


class TestDedupeNearDuplicates:
    def test_exact_url_keeps_first(self) -> None:
        sources = [
            _source("https://a.example/x", "First", "alpha"),
            _source("https://a.example/x", "Second", "beta"),
        ]
        assert dedupe_near_duplicates(sources) == [sources[0]]

    def test_syndicated_copies_removed(self) -> None:
        origin = _source("https://origin.example/news/quake", _QUAKE_TITLE, _QUAKE_SNIPPET)
        copy = _source("https://copy.example/news/quake", _QUAKE_TITLE + "s", _QUAKE_SNIPPET)
        assert dedupe_near_duplicates([origin, copy]) == [origin]

    def test_different_articles_on_same_topic_kept(self) -> None:
        assert dedupe_near_duplicates([_PREPARE, _REPORTS]) == [_PREPARE, _REPORTS]

    def test_order_preserved(self) -> None:
        sources = [_PREPARE, _REPORTS, _source("https://origin.example/q", _QUAKE_TITLE, _QUAKE_SNIPPET)]
        assert dedupe_near_duplicates(sources) == sources

    def test_empty_list(self) -> None:
        assert dedupe_near_duplicates([]) == []

    def test_threshold_one_keeps_near_copies(self) -> None:
        origin = _source("https://origin.example/news/quake", _QUAKE_TITLE, _QUAKE_SNIPPET)
        copy = _source("https://copy.example/news/quake", _QUAKE_TITLE + "s", _QUAKE_SNIPPET)
        assert dedupe_near_duplicates([origin, copy], threshold=1.0) == [origin, copy]

    def test_urls_inside_text_ignored(self) -> None:
        first = _source("https://a.example/x", "Breaking news", "Read more at https://a.example/x")
        second = _source("https://b.example/y", "Breaking news", "Read more at https://b.example/y")
        assert dedupe_near_duplicates([first, second]) == [first]

    def test_punctuation_and_case_ignored(self) -> None:
        first = _source("https://a.example/x", "Hello, World!", "")
        second = _source("https://b.example/y", "hello world", "")
        assert dedupe_near_duplicates([first, second]) == [first]

    def test_bom_treated_as_whitespace_like_js(self) -> None:
        # Short texts: a kept U+FEFF would shift shingles and break similarity.
        first = _source("https://a.example/x", "\ufeffHello")
        second = _source("https://b.example/y", "Hello")
        assert dedupe_near_duplicates([first, second]) == [first]
        middle = _source("https://c.example/z", "Hello\ufeff world")
        clean = _source("https://d.example/w", "Hello world")
        assert dedupe_near_duplicates([middle, clean]) == [middle]

    def test_empty_texts_collapse_like_js(self) -> None:
        first = _source("https://a.example/x")
        second = _source("https://b.example/y")
        assert dedupe_near_duplicates([first, second]) == [first]

    def test_none_fields_do_not_raise(self) -> None:
        first = {"url": "https://a.example/x", "title": None, "snippet": None}
        second = {"url": "https://b.example/y"}
        assert dedupe_near_duplicates([first, second]) == [first]

    def test_short_texts_use_smaller_shingles(self) -> None:
        ab = _source("https://a.example/x", "ab")
        abc = _source("https://b.example/y", "abc")
        assert dedupe_near_duplicates([ab, abc]) == [ab, abc]
        assert dedupe_near_duplicates([ab, _source("https://c.example/z", "ab")]) == [ab]


class TestNormalizedUrlKey:
    def test_scheme_host_and_trailing_slash(self) -> None:
        assert _normalized_url_key("http://EXAMPLE.com/Path/") == "https://example.com/Path"

    def test_default_ports_removed(self) -> None:
        assert _normalized_url_key("https://example.com:443/x") == "https://example.com/x"
        assert _normalized_url_key("http://example.com:80/x") == "https://example.com/x"

    def test_custom_port_kept(self) -> None:
        assert _normalized_url_key("https://example.com:8443/x") == "https://example.com:8443/x"

    def test_fragment_dropped_query_kept(self) -> None:
        assert _normalized_url_key("https://example.com/x?a=1#frag") == "https://example.com/x?a=1"

    def test_tracking_stripped_first(self) -> None:
        assert _normalized_url_key("https://example.com/x?id=1&utm_source=a") == "https://example.com/x?id=1"
        assert _normalized_url_key("https://example.com/x?id=&utm_source=a") == "https://example.com/x?id="

    def test_root_path(self) -> None:
        assert _normalized_url_key("https://example.com") == "https://example.com/"
        assert _normalized_url_key("https://example.com/") == "https://example.com/"

    def test_empty_and_hostless(self) -> None:
        assert _normalized_url_key("") == ""
        assert _normalized_url_key("not a url") == "not a url"
        assert _normalized_url_key("/relative/path/#frag") == "/relative/path"

    def test_malformed_port_never_raises(self) -> None:
        assert _normalized_url_key("https://example.com:99999/x") == "https://example.com:99999/x"


class TestRegistrableDomain:
    def test_subdomains_and_www(self) -> None:
        assert _registrable_domain("https://blog.example.com/x") == "example.com"
        assert _registrable_domain("https://www.example.com") == "example.com"
        assert _registrable_domain("https://a.b.example.com") == "example.com"

    def test_multi_part_suffixes(self) -> None:
        assert _registrable_domain("https://news.bbc.co.uk") == "bbc.co.uk"
        assert _registrable_domain("https://www.bbc.co.uk") == "bbc.co.uk"
        assert _registrable_domain("https://sports.sina.com.cn") == "sina.com.cn"
        assert _registrable_domain("https://example.co.uk") == "example.co.uk"

    def test_two_label_host(self) -> None:
        assert _registrable_domain("https://example.com") == "example.com"

    def test_ip_localhost_single_label(self) -> None:
        assert _registrable_domain("https://localhost:8080") == "localhost"
        assert _registrable_domain("http://192.168.0.1/x") == "192.168.0.1"
        assert _registrable_domain("http://[::1]:8080/x") == "::1"
        assert _registrable_domain("https://internal") == "internal"

    def test_trailing_dot(self) -> None:
        assert _registrable_domain("https://example.com./x") == "example.com"

    def test_invalid_and_empty(self) -> None:
        assert _registrable_domain("not a url") == "not a url"
        assert _registrable_domain("") == ""


class TestMergeFreeResults:
    def test_empty_inputs(self) -> None:
        assert merge_free_results({}, query="python", max_results=5) == []
        assert merge_free_results({"e1": []}, query="python", max_results=5) == []

    def test_non_positive_max_results(self) -> None:
        per_engine = {"e1": [_source("https://a.example/x", "Python", "python")]}
        assert merge_free_results(per_engine, query="python", max_results=0) == []
        assert merge_free_results(per_engine, query="python", max_results=-1) == []

    def test_tracking_variants_merge_keep_original_url(self) -> None:
        original = "https://example.com/a?id=1&utm_source=x"
        per_engine = {
            "bing": [_source(original, "Python Tutorial", "python tutorial")],
            "ddg": [_source("http://EXAMPLE.com/a/?id=1&utm_medium=y#ref", "Python Tutorial", "python tutorial")],
        }
        merged = merge_free_results(per_engine, query="python tutorial", max_results=5)
        assert merged == [{"url": original, "title": "Python Tutorial", "snippet": "python tutorial"}]

    def test_syndicated_copies_removed_keep_first_engine(self) -> None:
        per_engine = {
            "e1": [_source("https://origin.example/news/quake", _QUAKE_TITLE, _QUAKE_SNIPPET)],
            "e2": [_source("https://copy.example/news/quake", _QUAKE_TITLE + "s", _QUAKE_SNIPPET)],
        }
        merged = merge_free_results(per_engine, query="earthquake coastal region", max_results=5)
        assert [item["url"] for item in merged] == ["https://origin.example/news/quake"]

    def test_different_articles_on_same_topic_kept(self) -> None:
        per_engine = {"e1": [_PREPARE], "e2": [_REPORTS]}
        merged = merge_free_results(per_engine, query="earthquake", max_results=5)
        assert {item["url"] for item in merged} == {_PREPARE["url"], _REPORTS["url"]}

    def test_cross_source_boost_three_engines(self) -> None:
        # Base scores: multi 0.75, single 1.0. seen_in=3 adds 0.3 -> multi wins.
        multi = _source("https://x.example/a", "Alpha Beta Gamma", "first article about things")
        single = _source("https://y.example/b", "Alpha Beta Gamma Delta", "wholly different body text here")
        per_engine = {"e1": [single, multi], "e2": [multi], "e3": [multi]}
        merged = merge_free_results(per_engine, query="alpha beta gamma delta", max_results=5)
        assert [item["url"] for item in merged] == [multi["url"], single["url"]]

    def test_boost_is_zero_point_one_five_per_extra_engine(self) -> None:
        # multi 0.75 + 0.15 = 0.9 beats single 0.875 only with the exact boost.
        multi = _source("https://x.example/a", "Alpha Beta Gamma", "first article about things")
        single = _source("https://y.example/b", "Alpha Beta Gamma", "delta completely other text")
        per_engine = {"e1": [single, multi], "e2": [multi]}
        merged = merge_free_results(per_engine, query="alpha beta gamma delta", max_results=5)
        assert [item["url"] for item in merged] == [multi["url"], single["url"]]

    def test_boost_capped_at_zero_point_four_five(self) -> None:
        # multi 0.5 in 5 engines: capped 0.95 still loses to single 1.0.
        multi = _source("https://x.example/a", "Welcome", "alpha beta gamma delta")
        single = _source("https://y.example/b", "Alpha Beta Gamma Delta", "wholly different body text here")
        per_engine = {"e1": [single, multi], "e2": [multi], "e3": [multi], "e4": [multi], "e5": [multi]}
        merged = merge_free_results(per_engine, query="alpha beta gamma delta", max_results=5)
        assert [item["url"] for item in merged] == [single["url"], multi["url"]]

    def test_same_engine_duplicates_do_not_inflate_seen_in(self) -> None:
        multi = _source("https://x.example/a?utm_source=1", "Alpha Beta Gamma", "first article about things")
        twin = _source("https://x.example/a?utm_source=2", "Alpha Beta Gamma", "first article about things")
        single = _source("https://y.example/b", "Alpha Beta Gamma", "delta completely other text")
        per_engine = {"e1": [single, multi, twin]}
        merged = merge_free_results(per_engine, query="alpha beta gamma delta", max_results=5)
        assert [item["url"] for item in merged] == [single["url"], multi["url"]]

    def test_domain_diversity_interleaving(self) -> None:
        a1 = _source("https://a.example/1", "Python Tutorial", "python tutorial apples fresh fruit")
        a2 = _source("https://a.example/2", "Python Tutorial", "python tutorial oranges citrus grove")
        a3 = _source("https://a.example/3", "Python Tutorial", "python tutorial bananas tropical farm")
        b1 = _source("https://b.example/1", "Python", "python")
        per_engine = {"e1": [a1, a2, a3, b1]}
        top2 = merge_free_results(per_engine, query="python tutorial", max_results=2)
        assert [item["url"] for item in top2] == [a1["url"], b1["url"]]
        top4 = merge_free_results(per_engine, query="python tutorial", max_results=4)
        assert [item["url"] for item in top4] == [a1["url"], b1["url"], a2["url"], a3["url"]]

    def test_subdomains_share_one_group(self) -> None:
        s1 = _source("https://blog.example.com/1", "Python Tutorial", "python tutorial apples fresh fruit")
        s2 = _source("https://news.example.com/2", "Python Tutorial", "python tutorial oranges citrus grove")
        t1 = _source("https://other.example/1", "Python", "python")
        per_engine = {"e1": [s1, s2, t1]}
        merged = merge_free_results(per_engine, query="python tutorial", max_results=2)
        assert [item["url"] for item in merged] == [s1["url"], t1["url"]]

    def test_max_results_truncation(self) -> None:
        snippets = ["apples fresh fruit", "oranges citrus grove", "bananas tropical farm", "grapes vineyard harvest"]
        items = [
            _source(f"https://d{i}.example/x", "Python", f"python {snippet}") for i, snippet in enumerate(snippets)
        ]
        per_engine = {"e1": items}
        merged = merge_free_results(per_engine, query="python", max_results=3)
        assert [item["url"] for item in merged] == [f"https://d{i}.example/x" for i in range(3)]
        assert len(merge_free_results(per_engine, query="python", max_results=10)) == 4

    def test_missing_or_empty_urls_skipped(self) -> None:
        valid = _source("https://a.example/x", "Python", "python")
        broken = [{"title": "Python", "snippet": "python"}, {"url": "", "title": "Python"}, {"url": None}]
        per_engine = {"e1": [*broken, valid]}
        merged = merge_free_results(per_engine, query="python", max_results=5)
        assert merged == [{"url": valid["url"], "title": "Python", "snippet": "python"}]

    def test_output_has_only_three_fields(self) -> None:
        item = {"url": "https://a.example/x", "title": None, "snippet": None, "published_at": "2026-11-28"}
        merged = merge_free_results({"e1": [item]}, query="python", max_results=5)
        assert merged == [{"url": "https://a.example/x", "title": "", "snippet": ""}]

    def test_engine_priority_on_score_ties(self) -> None:
        first = _source("https://x.example/a", "Python Tutorial", "python tutorial apples fresh fruit")
        second = _source("https://y.example/b", "Tutorial Python", "tutorial python oranges citrus grove")
        per_engine = {"e1": [first], "e2": [second]}
        merged = merge_free_results(per_engine, query="python tutorial", max_results=5)
        assert [item["url"] for item in merged] == [first["url"], second["url"]]

    def test_same_domain_tie_keeps_first_occurrence(self) -> None:
        first = _source("https://d.example/1", "Python Tutorial", "python tutorial apples fresh fruit")
        second = _source("https://d.example/2", "Python Tutorial", "python tutorial oranges citrus grove")
        per_engine = {"e1": [first], "e2": [second]}
        merged = merge_free_results(per_engine, query="python tutorial", max_results=5)
        assert [item["url"] for item in merged] == [first["url"], second["url"]]
