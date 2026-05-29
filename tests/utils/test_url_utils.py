from utils.url_utils import (
    extract_markdown_links,
    extract_plain_links,
    normalize_url,
    extract_effective_search_term,
)


class TestExtractMarkdownLinks:
    def test_simple(self):
        text = "[link](https://admin.deadspace14.net/Connections?search=test)"
        result = extract_markdown_links(text)
        assert result == ["https://admin.deadspace14.net/Connections?search=test"]

    def test_multiple(self):
        text = "[a](http://a.com) and [b](http://b.com)"
        result = extract_markdown_links(text)
        assert result == ["http://a.com", "http://b.com"]

    def test_none(self):
        assert extract_markdown_links("no links here") == []

    def test_empty(self):
        assert extract_markdown_links("") == []

    def test_url_with_parentheses(self):
        text = "[link](https://example.com/path?q=(test))"
        result = extract_markdown_links(text)
        assert len(result) == 1
        assert "example.com" in result[0]


class TestExtractPlainLinks:
    def test_simple(self):
        text = "Visit https://admin.deadspace14.net/Connections"
        result = extract_plain_links(text)
        assert "https://admin.deadspace14.net/Connections" in result

    def test_multiple(self):
        text = "https://a.com and http://b.com/path"
        result = extract_plain_links(text)
        assert "https://a.com" in result
        assert "http://b.com/path" in result

    def test_none(self):
        assert extract_plain_links("no links") == []

    def test_empty(self):
        assert extract_plain_links("") == []


class TestNormalizeUrl:
    def test_preserves_essential_params(self):
        url = ("https://admin.deadspace14.net/Connections?"
               "search=TestPlayer&showSet=true&perPage=50&extra=removeme")
        result = normalize_url(url)
        assert "search=TestPlayer" in result
        assert "showSet=true" in result
        assert "perPage=50" in result
        assert "extra" not in result
        assert "removeme" not in result

    def test_sorts_params(self):
        url = "https://admin.deadspace14.net/Connections?perPage=20&search=test"
        result = normalize_url(url)
        assert result.index("perPage") < result.index("search")

    def test_invalid_url_returns_original(self):
        url = "not-a-url"
        assert normalize_url(url) == url

    def test_empty_query(self):
        url = "https://admin.deadspace14.net/Connections"
        result = normalize_url(url)
        assert "?" not in result or result.endswith("?")


class TestExtractEffectiveSearchTerm:
    def test_plain_term(self):
        assert extract_effective_search_term("TestPlayer") == "TestPlayer"

    def test_url_with_search_param(self):
        url = "https://admin.deadspace14.net/Connections?search=TestPlayer&showSet=true"
        assert extract_effective_search_term(url) == "TestPlayer"

    def test_url_with_encoded_search(self):
        url = "https://admin.deadspace14.net/Connections?search=Test%20Player"
        assert extract_effective_search_term(url) == "Test Player"

    def test_invalid_url_returns_original(self):
        url = "http://?"
        assert extract_effective_search_term(url) == url

    def test_empty_string(self):
        assert extract_effective_search_term("") == ""
