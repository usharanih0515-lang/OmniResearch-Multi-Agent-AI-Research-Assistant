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
    assert result.summary == "Evidence summary: Evidence."


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
