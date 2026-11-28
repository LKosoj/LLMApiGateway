from __future__ import annotations

from llm_gateway_core.api.v1.web_relevance import (
    TRACKING_PARAMS,
    looks_relevant,
    query_overlap_tokens,
    relevance_score,
    strip_tracking_params,
)


def _source(title: str = "", snippet: str = "", url: str = "") -> dict:
    return {"title": title, "snippet": snippet, "url": url}


def _garbage(index: int) -> dict:
    return _source(title=f"news {index}", snippet="weather forecast", url=f"https://x.example/{index}")


class TestStripTrackingParams:
    def test_removes_utm_and_click_ids(self) -> None:
        url = "https://example.com/page?id=42&utm_source=twitter&utm_medium=social&gclid=abc"
        assert strip_tracking_params(url) == "https://example.com/page?id=42"

    def test_keeps_resource_params(self) -> None:
        url = "https://example.com/watch?v=xyz&from=search&sessionid=7&page=2"
        assert strip_tracking_params(url) == url

    def test_case_insensitive_keys(self) -> None:
        url = "https://example.com/?UTM_Source=x&GCLID=y&fbclid=z&id=1"
        assert strip_tracking_params(url) == "https://example.com/?id=1"

    def test_no_tracking_returns_verbatim(self) -> None:
        url = "https://example.com/path?q=a%20b&id="
        assert strip_tracking_params(url) == url

    def test_all_params_tracking_drops_query(self) -> None:
        url = "https://example.com/path?utm_campaign=x&msclkid=y"
        assert strip_tracking_params(url) == "https://example.com/path"

    def test_full_tracking_list(self) -> None:
        assert len(TRACKING_PARAMS) == 17
        query = "&".join(f"{name}=v" for name in sorted(TRACKING_PARAMS))
        assert strip_tracking_params(f"https://example.com/p?{query}&id=1") == "https://example.com/p?id=1"

    def test_invalid_urls_returned_as_is(self) -> None:
        for raw in ("", "not a url", "example.com/path?utm_source=x", "/relative?utm_source=x"):
            assert strip_tracking_params(raw) == raw

    def test_never_raises_on_broken_urls(self) -> None:
        for raw in ("http://example.com:99999/?utm_source=x", "http://[invalid/?utm_source=x", "https://"):
            assert strip_tracking_params(raw) == raw

    def test_preserves_fragment_path_and_port(self) -> None:
        url = "https://example.com:8443/p?utm_source=x&id=2#frag"
        assert strip_tracking_params(url) == "https://example.com:8443/p?id=2#frag"

    def test_removes_all_duplicate_occurrences(self) -> None:
        url = "https://example.com/?utm_source=a&utm_source=b&id=1&id=2"
        assert strip_tracking_params(url) == "https://example.com/?id=1&id=2"

    def test_keeps_blank_resource_values(self) -> None:
        url = "https://example.com/?gclid=x&id="
        assert strip_tracking_params(url) == "https://example.com/?id="


class TestQueryOverlapTokens:
    def test_ascii_lowercase_split(self) -> None:
        assert query_overlap_tokens("Python 3.14 Release!") == ["python", "14", "release"]

    def test_single_characters_dropped(self) -> None:
        assert query_overlap_tokens("a b c") == []

    def test_empty_and_symbols(self) -> None:
        assert query_overlap_tokens("") == []
        assert query_overlap_tokens("!!! ???") == []

    def test_cjk_run_of_two_kept_whole(self) -> None:
        assert query_overlap_tokens("天气") == ["天气"]

    def test_cjk_run_of_one_dropped(self) -> None:
        assert query_overlap_tokens("年") == []

    def test_cjk_run_of_three_yields_bigrams_only(self) -> None:
        assert query_overlap_tokens("北京天") == ["北京", "京天"]

    def test_cjk_run_of_four(self) -> None:
        assert query_overlap_tokens("北京天气") == ["北京", "京天", "天气"]

    def test_mixed_cjk_and_ascii_dedup(self) -> None:
        assert query_overlap_tokens("北京 weather 北京") == ["北京", "weather"]

    def test_ascii_dedup_preserves_order(self) -> None:
        assert query_overlap_tokens("test TEST test") == ["test"]

    def test_numeric_tokens_kept(self) -> None:
        assert query_overlap_tokens("2026") == ["2026"]

    def test_short_ascii_tokens(self) -> None:
        assert query_overlap_tokens("gt max") == ["gt", "max"]


class TestLooksRelevant:
    def test_empty_tokens_pass(self) -> None:
        assert looks_relevant("", []) is True
        assert looks_relevant("!!!", [_garbage(0)]) is True
        assert looks_relevant("a", []) is True

    def test_empty_sources_fail(self) -> None:
        assert looks_relevant("python tutorial", []) is False

    def test_garbage_batch_fails(self) -> None:
        assert looks_relevant("python tutorial", [_garbage(i) for i in range(3)]) is False

    def test_normal_batch_passes(self) -> None:
        assert looks_relevant("python tutorial", [_source(title="Python Tutorial")]) is True

    def test_one_third_threshold_six_sources(self) -> None:
        good = _source(title="Python Tutorial")
        assert looks_relevant("python tutorial", [good, good, *[_garbage(i) for i in range(4)]]) is True
        assert looks_relevant("python tutorial", [good, *[_garbage(i) for i in range(5)]]) is False

    def test_one_third_threshold_four_sources(self) -> None:
        good = _source(title="Python Tutorial")
        assert looks_relevant("python tutorial", [good, *[_garbage(i) for i in range(3)]]) is False
        assert looks_relevant("python tutorial", [good, good, _garbage(0), _garbage(1)]) is True

    def test_one_third_threshold_three_sources(self) -> None:
        good = _source(title="Python Tutorial")
        assert looks_relevant("python tutorial", [good, _garbage(0), _garbage(1)]) is True

    def test_title_only_hit_passes(self) -> None:
        source = {"title": "Python Tutorial", "snippet": None, "url": None}
        assert looks_relevant("python tutorial", [source]) is True

    def test_snippet_only_hit_fails_without_title_match(self) -> None:
        assert looks_relevant("python tutorial", [_source(title="Welcome", snippet="python tutorial")]) is False

    def test_url_only_hit_fails_without_title_match(self) -> None:
        source = _source(title="Welcome", url="https://docs.example/python-tutorial")
        assert looks_relevant("python tutorial", [source]) is False

    def test_short_ascii_token_not_title_strong(self) -> None:
        assert looks_relevant("gt tutorial", [_source(title="GT", snippet="tutorial")]) is False
        assert looks_relevant("gt tutorial", [_source(title="GT Tutorial")]) is True

    def test_only_short_tokens_fall_back_to_usable(self) -> None:
        assert looks_relevant("gt max", [_source(title="gt max")]) is True

    def test_digits_only_query_uses_digits(self) -> None:
        assert looks_relevant("2026", [_source(title="2026 report")]) is True

    def test_digits_filtered_when_strong_tokens_exist(self) -> None:
        assert looks_relevant("iphone 15", [_source(title="15 review", snippet="15")]) is False

    def test_two_matched_of_four_enough(self) -> None:
        assert looks_relevant("alpha beta gamma delta", [_source(title="alpha beta")]) is True

    def test_one_matched_of_four_not_enough(self) -> None:
        assert looks_relevant("alpha beta gamma delta", [_source(title="alpha")]) is False

    def test_half_ratio_passes_for_two_tokens(self) -> None:
        assert looks_relevant("alpha beta", [_source(title="alpha")]) is True

    def test_none_fields_do_not_raise(self) -> None:
        source = {"title": None, "snippet": None, "url": None}
        assert looks_relevant("python", [source]) is False

    def test_non_string_fields_stringified(self) -> None:
        assert looks_relevant("2026", [{"title": 2026}]) is True

    def test_cjk_bigrams_in_title_pass(self) -> None:
        assert looks_relevant("北京天气", [_source(title="北京天气预报")]) is True

    def test_cjk_snippet_only_hit_fails_without_title_match(self) -> None:
        assert looks_relevant("北京天气", [_source(title="上海新闻", snippet="北京")]) is False


class TestRelevanceScore:
    def test_empty_tokens_score_zero(self) -> None:
        assert relevance_score("", _source(title="anything")) == 0.0
        assert relevance_score("!!!", _source(title="anything")) == 0.0

    def test_full_match_scores_one(self) -> None:
        source = _source(title="Python Tutorial", snippet="python tutorial")
        assert relevance_score("python tutorial", source) == 1.0

    def test_no_overlap_scores_zero(self) -> None:
        assert relevance_score("python tutorial", _garbage(0)) == 0.0

    def test_snippet_only_scores_half(self) -> None:
        assert relevance_score("python", _source(snippet="python")) == 0.5

    def test_title_only_scores_one(self) -> None:
        assert relevance_score("python", _source(title="Python")) == 1.0

    def test_digits_only_query(self) -> None:
        assert relevance_score("2026", _source(title="2026")) == 1.0

    def test_short_token_title_fallback(self) -> None:
        assert relevance_score("gt", _source(title="gt")) == 1.0

    def test_empty_source_scores_zero(self) -> None:
        assert relevance_score("python", {}) == 0.0

    def test_bounds(self) -> None:
        sources = [
            _source(title="Python Tutorial", snippet="python tutorial", url="https://python.example/tutorial"),
            _source(title="Python", snippet="tutorial"),
            _source(snippet="python"),
            _garbage(0),
            {},
        ]
        for query in ("python tutorial", "gt max", "2026", "北京天气", ""):
            for source in sources:
                score = relevance_score(query, source)
                assert 0.0 <= score <= 1.0
