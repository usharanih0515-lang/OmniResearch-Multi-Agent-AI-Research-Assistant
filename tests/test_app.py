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
    client = create_app({
        "TESTING": True,
        "RESEARCH_PROVIDER": "",
        "TAVILY_API_KEY": "",
        "RESEARCH_API_KEY": "",
    }).test_client()
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
    assert [takeaway.text for takeaway in result.summary.key_takeaways] == ["Evidence."]
    assert result.summary.supporting_evidence[0].title == "Valid"


def test_summary_is_concise_and_sources_are_deduplicated():
    result = parse_response("Question", {"sources": [
        {"title": "First", "url": "https://example.com/a", "snippet": "A useful finding."},
        {"title": "First duplicate", "url": "https://example.com/a", "snippet": "A useful finding."},
        {"title": "Same evidence", "url": "https://example.com/b", "snippet": "A useful finding."},
        {"title": "Second", "url": "https://example.com/c", "snippet": "A different finding."},
    ]})
    assert [source.title for source in result.sources] == ["First", "Same evidence", "Second"]
    assert [takeaway.text for takeaway in result.summary.key_takeaways] == ["A useful finding.", "A different finding."]
    assert len(result.summary.key_takeaways) <= 3


def test_takeaways_are_attributed_and_study_stage_is_not_invented():
    result = parse_response("Question", {"sources": [
        {"title": "Trial source", "url": "https://example.com/trial", "snippet": "A clinical trial enrolled participants. Results were reported."},
        {"title": "Animal source", "url": "https://example.com/animal", "snippet": "A mouse study observed a response."},
    ]})
    assert any(
        takeaway.source.title == "Trial source"
        and takeaway.evidence_type == "Clinical trial evidence"
        for takeaway in result.summary.key_takeaways
    )
    animal_takeaway = next(
        takeaway for takeaway in result.summary.key_takeaways if takeaway.source.title == "Animal source"
    )
    assert animal_takeaway.evidence_type == "Animal or preclinical evidence"


def test_promotional_language_is_not_presented_as_a_factual_takeaway():
    result = parse_response("Question", {"sources": [
        {"title": "Promotional source", "url": "https://example.com/promo", "snippet": "This breakthrough will completely revolutionise care. The study reports an early-stage result."},
    ]})
    assert [takeaway.text for takeaway in result.summary.key_takeaways] == ["The study reports an early-stage result."]
    assert any("Promotional wording" in limitation for limitation in result.summary.limitations)


def test_takeaways_prefer_evidence_over_article_introductions():
    result = parse_response("Question", {"sources": [
        {"title": "Research article", "url": "https://example.com/article", "snippet": "This article explores a promising new approach. A clinical trial enrolled 80 participants and reported improved symptoms."},
    ]})
    assert [takeaway.text for takeaway in result.summary.key_takeaways] == [
        "A clinical trial enrolled 80 participants and reported improved symptoms."
    ]


def test_supporting_excerpt_is_shorter_than_source_content():
    long_snippet = "A study reported a measured result. " + ("Additional background text. " * 30)
    result = parse_response("Question", {"sources": [
        {"title": "Long source", "url": "https://example.com/long", "snippet": long_snippet},
    ]})
    assert len(result.sources[0].excerpt) <= 220
    assert result.sources[0].url == "https://example.com/long"


def test_provider_dates_are_displayed_but_missing_dates_are_not_invented():
    result = parse_response("Question", {"sources": [
        {"title": "Dated source", "url": "https://example.com/dated", "snippet": "A study reported results.", "published_date": "2020-01-02"},
        {"title": "Undated source", "url": "https://example.com/undated", "snippet": "A study reported results."},
    ]})
    assert result.sources[0].published_date == "2020-01-02"
    assert result.sources[0].date_label == "Older background"
    assert result.sources[1].published_date is None
    assert result.sources[1].date_label is None


def test_insufficient_evidence_is_explicit():
    result = parse_response("Question", {"sources": [
        {"title": "No snippet", "url": "https://example.com/no-snippet"},
    ]})
    assert result.summary.key_takeaways == []
    assert any("not provide enough neutral evidence" in limitation for limitation in result.summary.limitations)


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
    app = create_app({
        "TESTING": True,
        "RESEARCH_PROVIDER": "tavily",
        "TAVILY_API_KEY": "",
    })
    response = app.test_client().post("/research", data={"question": "question"})
    assert response.status_code == 503
    assert b"No research integration is configured" in response.data


def test_local_env_configures_tavily_without_overriding_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RESEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "RESEARCH_PROVIDER=tavily\nTAVILY_API_KEY=local-test-key\n",
        encoding="utf-8",
    )

    app = create_app({"TESTING": True})

    assert app.config["RESEARCH_PROVIDER"] == "tavily"
    assert app.config["TAVILY_API_KEY"] == "local-test-key"


def test_process_environment_wins_over_local_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RESEARCH_PROVIDER", "http")
    monkeypatch.setenv("TAVILY_API_KEY", "environment-key")
    (tmp_path / ".env").write_text(
        "RESEARCH_PROVIDER=tavily\nTAVILY_API_KEY=local-key\n",
        encoding="utf-8",
    )

    app = create_app({"TESTING": True})

    assert app.config["RESEARCH_PROVIDER"] == "http"
    assert app.config["TAVILY_API_KEY"] == "environment-key"
