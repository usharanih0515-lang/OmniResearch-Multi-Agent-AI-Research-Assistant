import json
from urllib.error import HTTPError, URLError
from unittest.mock import patch

import pytest

from omni_research import create_app
from omni_research.research import ResearchError, TavilySearchProvider, parse_response


class FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


def test_home_page_is_available():
    client = create_app({"TESTING": True}).test_client()
    response = client.get("/")
    assert response.status_code == 200
    assert b"OmniResearch" in response.data


def test_empty_question_is_rejected():
    client = create_app({"TESTING": True}).test_client()
    response = client.post("/research", data={"question": "   "})
    assert response.status_code == 400
    assert b"Enter a research question first" in response.data


def test_unconfigured_provider_is_explicit():
    client = create_app({"TESTING": True}).test_client()
    response = client.post("/research", data={"question": "What is renewable energy?"})
    assert response.status_code == 503
    assert b"No research integration is configured" in response.data


def test_tavily_success_is_rendered_from_provider_response():
    app = create_app({
        "TESTING": True,
        "RESEARCH_PROVIDER": "tavily",
        "TAVILY_API_KEY": "test-key",
    })
    payload = {"results": [{
        "title": "Renewable energy overview",
        "url": "https://example.com/energy",
        "content": "Renewable energy comes from replenished natural processes.",
    }]}
    with patch("omni_research.research.urlopen", return_value=FakeResponse(payload)):
        response = app.test_client().post("/research", data={"question": "What is renewable energy?"})
    assert response.status_code == 200
    assert b"Renewable energy overview" in response.data
    assert b"https://example.com/energy" in response.data
    assert b"Renewable energy comes from replenished natural processes." in response.data
    assert b"No news results were returned" in response.data


def test_provider_timeout_is_user_safe():
    provider = TavilySearchProvider("test-key")
    with patch("omni_research.research.urlopen", side_effect=TimeoutError):
        with pytest.raises(ResearchError, match="could not be reached"):
            provider.research("question")


def test_provider_network_error_is_user_safe():
    provider = TavilySearchProvider("test-key")
    with patch("omni_research.research.urlopen", side_effect=URLError("offline")):
        with pytest.raises(ResearchError, match="could not be reached"):
            provider.research("question")


@pytest.mark.parametrize("status, message", [
    (401, "rejected the API key"),
    (429, "rate limit was reached"),
])
def test_provider_handles_auth_and_rate_limit(status, message):
    provider = TavilySearchProvider("test-key")
    error = HTTPError("https://api.tavily.com/search", status, "failure", {}, None)
    with patch("omni_research.research.urlopen", side_effect=error):
        with pytest.raises(ResearchError, match=message):
            provider.research("question")


def test_malformed_provider_json_fails_cleanly():
    class BadResponse(FakeResponse):
        def read(self):
            return b"not-json"

    with patch("omni_research.research.urlopen", return_value=BadResponse({})):
        with pytest.raises(ResearchError, match="malformed JSON"):
            TavilySearchProvider("test-key").research("question")


def test_invalid_urls_are_not_returned_as_sources():
    result = parse_response("Question", {"sources": [
        {"title": "Valid", "url": "https://example.com", "snippet": "Evidence."},
        {"title": "Bad scheme", "url": "javascript:alert(1)", "snippet": "Ignore."},
        {"title": "No host", "url": "https:///missing-host", "snippet": "Ignore."},
    ]})
    assert [(source.title, source.url) for source in result.sources] == [("Valid", "https://example.com")]
    assert result.summary.key_takeaways == ["Evidence."]
    assert result.summary.supporting_evidence[0].title == "Valid"


def test_summary_is_concise_and_sources_are_deduplicated():
    result = parse_response("Question", {"sources": [
        {"title": "First", "url": "https://example.com/a", "snippet": "A useful finding."},
        {"title": "First duplicate", "url": "https://example.com/a", "snippet": "A useful finding."},
        {"title": "Same evidence", "url": "https://example.com/b", "snippet": "A useful finding."},
        {"title": "Second", "url": "https://example.com/c", "snippet": "A different finding."},
    ]})
    assert [source.title for source in result.sources] == ["First", "Same evidence", "Second"]
    assert result.summary.key_takeaways == ["A useful finding.", "A different finding."]
    assert len(result.summary.key_takeaways) <= 3


def test_missing_snippets_are_linked_but_not_presented_as_evidence():
    result = parse_response("Question", {"sources": [
        {"title": "No snippet", "url": "https://example.com/no-snippet"},
        {"title": "With snippet", "url": "https://example.com/with-snippet", "snippet": "Observed evidence."},
    ]})
    assert [source.title for source in result.sources] == ["No snippet", "With snippet"]
    assert [source.title for source in result.summary.supporting_evidence] == ["With snippet"]
    assert any("did not include a snippet" in item for item in result.summary.limitations)


def test_empty_news_results_are_honest_and_do_not_create_sources():
    result = parse_response("Question", {
        "sources": [{"title": "Main source", "url": "https://example.com", "snippet": "Evidence."}],
        "news": [],
    })
    assert result.news == []
    assert any("No news results" in item for item in result.summary.limitations)


def test_news_results_are_kept_separate_from_main_sources():
    result = parse_response("Question", {
        "sources": [{"title": "Main source", "url": "https://example.com/main", "snippet": "Evidence."}],
        "news": [{"title": "News item", "url": "https://example.com/news", "snippet": "News evidence."}],
    })
    assert [source.title for source in result.sources] == ["Main source"]
    assert [source.title for source in result.news] == ["News item"]


def test_malformed_source_data_fails_loudly():
    with pytest.raises(ResearchError, match="malformed source data"):
        parse_response("Question", {"sources": {}})


def test_empty_results_fail_loudly():
    with pytest.raises(ResearchError, match="no valid source links"):
        parse_response("Question", {"sources": []})


def test_provider_configuration_requires_key():
    app = create_app({"TESTING": True, "RESEARCH_PROVIDER": "tavily"})
    response = app.test_client().post("/research", data={"question": "question"})
    assert response.status_code == 503
    assert b"No research integration is configured" in response.data
