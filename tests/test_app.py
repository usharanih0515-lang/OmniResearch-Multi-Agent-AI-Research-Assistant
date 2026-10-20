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


def test_grouped_brief_markup_separates_topic_evidence_and_source_link():
    app = create_app({
        "TESTING": True,
        "RESEARCH_PROVIDER": "tavily",
        "TAVILY_API_KEY": "test-key",
    })
    payload = {"results": [
        {"title": "Cyber source", "url": "https://example.com/cyber", "content": "A cybersecurity study analyzed network traffic."},
        {"title": "Road source", "url": "https://example.com/road", "content": "A traffic flow study measured vehicle congestion."},
        {"title": "General source", "url": "https://example.com/general", "content": "A study reported results."},
    ]}
    with patch("omni_research.research.urlopen", return_value=FakeResponse(payload)):
        response = app.test_client().post("/research", data={"question": "What does traffic analysis show?"})
    assert response.status_code == 200
    body = response.data
    assert b'class="topic-label">cybersecurity/network traffic</span>' in body
    assert b'class="evidence-type">Provider-reported evidence</span>' in body
    assert b'class="source-link" href="https://example.com/cyber"' in body
    assert b'<li class="evidence-item">' in body
    assert b'<div class="evidence-meta">' in body
    assert b'class="source-link" href="https://example.com/general"' in body


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
        {"title": "Valid", "url": "https://example.com", "snippet": "A study reported evidence."},
        {"title": "Bad scheme", "url": "javascript:alert(1)", "snippet": "Ignore."},
        {"title": "No host", "url": "https:///missing-host", "snippet": "Ignore."},
    ]})
    assert [(source.title, source.url) for source in result.sources] == [("Valid", "https://example.com")]
    assert [takeaway.text for takeaway in result.summary.key_takeaways] == ["A study reported evidence."]
    assert result.summary.supporting_evidence[0].title == "Valid"
    assert "Some source text was excluded because it contained navigation or promotional wording." not in result.summary.limitations


def test_summary_is_concise_and_sources_are_deduplicated():
    result = parse_response("Question", {"sources": [
        {"title": "First", "url": "https://example.com/a", "snippet": "A study reported a useful finding."},
        {"title": "First duplicate", "url": "https://example.com/a", "snippet": "A study reported a useful finding."},
        {"title": "Same evidence", "url": "https://example.com/b", "snippet": "A study reported a useful finding."},
        {"title": "Second", "url": "https://example.com/c", "snippet": "A study reported a different finding."},
    ]})
    assert [source.title for source in result.sources] == ["First", "Same evidence", "Second"]
    assert [takeaway.text for takeaway in result.summary.key_takeaways] == ["A study reported a useful finding.", "A study reported a different finding."]
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
    assert result.summary.limitations.count(
        "Some source text was excluded because it contained navigation or promotional wording."
    ) == 1


def test_navigation_and_call_to_action_snippets_are_filtered():
    result = parse_response("Question", {"sources": [
        {"title": "Navigation", "url": "https://example.com/nav", "snippet": "Learn more"},
        {"title": "Slogan", "url": "https://example.com/slogan", "snippet": "This breakthrough will transform care."},
    ]})
    assert result.summary.key_takeaways == []
    assert "Some source text was excluded because it contained navigation or promotional wording." in result.summary.limitations
    assert "The retrieved content does not provide enough neutral evidence for a factual takeaway." in result.summary.limitations
    assert "No news results were returned by the provider." in result.summary.limitations


def test_rejected_sentences_create_only_one_aggregate_limitation():
    result = parse_response("Question", {"sources": [
        {"title": "First", "url": "https://example.com/first", "snippet": "Learn more. This breakthrough will transform care."},
        {"title": "Second", "url": "https://example.com/second", "snippet": "Click here. In conclusion, this is unprecedented."},
    ]})
    aggregate = "Some source text was excluded because it contained navigation or promotional wording."
    assert result.summary.limitations.count(aggregate) == 1
    assert not any("First" in item or "Second" in item for item in result.summary.limitations)


def test_html_entities_and_navigation_boilerplate_are_cleaned():
    result = parse_response("Question", {"sources": [
        {"title": "Clean source", "url": "https://example.com/clean", "snippet": "&lt;p&gt;A study reported results.&lt;/p&gt; Learn more"},
    ]})
    assert result.sources[0].snippet == "A study reported results."
    assert "Learn more" not in result.sources[0].excerpt


def test_mid_sentence_fragments_are_not_used_as_takeaways():
    result = parse_response("Question", {"sources": [
        {"title": "Fragment source", "url": "https://example.com/fragment", "snippet": "address real-world challenges. A study reported measurable results."},
    ]})
    assert [takeaway.text for takeaway in result.summary.key_takeaways] == ["A study reported measurable results."]


def test_truncated_long_sentences_are_not_presented_as_complete_takeaways():
    long_sentence = "A study reported " + ("important measured evidence " * 30)
    result = parse_response("Question", {"sources": [
        {"title": "Long source", "url": "https://example.com/long-takeaway", "snippet": long_sentence},
    ]})
    assert result.summary.key_takeaways == []
    assert any("not provide enough neutral evidence" in item for item in result.summary.limitations)


def test_adjacent_sentences_are_separated_without_changing_words():
    result = parse_response("Question", {"sources": [
        {"title": "Joined source", "url": "https://example.com/joined", "snippet": "A study reported threats.AI-based analysis found results."},
    ]})
    assert result.sources[0].snippet == "A study reported threats. AI-based analysis found results."


def test_navigation_heavy_excerpt_keeps_only_research_content():
    result = parse_response("Question", {"sources": [
        {"title": "Website source", "url": "https://example.com/site", "snippet": "Home | About us | Contact us. A study reported results. Privacy policy."},
    ]})
    assert result.sources[0].snippet == "A study reported results."


def test_route_alert_navigation_is_filtered_but_factual_traffic_is_kept():
    result = parse_response("What does road traffic analysis show?", {"sources": [
        {"title": "Route page", "url": "https://example.com/route", "snippet": "View route alerts. Get directions."},
        {"title": "Traffic study", "url": "https://example.com/traffic", "snippet": "A traffic flow study reported congestion."},
    ]})
    assert result.sources[0].snippet == ""
    assert [takeaway.source.title for takeaway in result.summary.key_takeaways] == ["Traffic study"]


def test_supporting_evidence_link_markup_is_consistent_without_topic_label():
    result = parse_response("Question", {"sources": [
        {"title": "General source", "url": "https://example.com/general", "snippet": "A study reported results."},
    ]})
    assert result.summary.supporting_evidence[0].topic_label is None


def test_markdown_heading_artifacts_are_removed_from_excerpts():
    result = parse_response("Question", {"sources": [
        {"title": "Markdown source", "url": "https://example.com/markdown", "snippet": "### Findings\n\\### A study reported results."},
    ]})
    assert result.sources[0].snippet == "Findings A study reported results."


def test_marketing_text_is_filtered_without_removing_nearby_evidence():
    result = parse_response("Question", {"sources": [
        {"title": "Marketing source", "url": "https://example.com/marketing", "snippet": "A study reported improved outcomes. Book a demo to learn more. Try for free today."},
    ]})
    assert [takeaway.text for takeaway in result.summary.key_takeaways] == ["A study reported improved outcomes."]
    assert "Book a demo" not in result.sources[0].snippet
    assert "Try for free" not in result.sources[0].snippet


def test_mixed_traffic_meanings_are_disclosed_for_broad_questions():
    result = parse_response("What does traffic analysis show?", {"sources": [
        {"title": "Network security", "url": "https://example.com/cyber", "snippet": "A cybersecurity study analyzed network traffic."},
        {"title": "Road traffic", "url": "https://example.com/road", "snippet": "A traffic flow study measured vehicle congestion."},
    ]})
    assert len(result.summary.key_takeaways) == 2
    assert any("multiple meanings of the topic" in item for item in result.summary.limitations)
    assert {takeaway.source.url for takeaway in result.summary.key_takeaways} == {
        "https://example.com/cyber", "https://example.com/road"
    }
    assert {takeaway.topic_context for takeaway in result.summary.key_takeaways} == {
        "cybersecurity/network traffic", "road/transport traffic"
    }


def test_explicit_road_question_excludes_unrelated_cybersecurity_takeaway():
    result = parse_response("What does road traffic analysis show?", {"sources": [
        {"title": "Network security", "url": "https://example.com/cyber", "snippet": "A cybersecurity study analyzed network traffic."},
        {"title": "Road traffic", "url": "https://example.com/road", "snippet": "A traffic flow study measured vehicle congestion."},
    ]})
    assert [takeaway.source.title for takeaway in result.summary.key_takeaways] == ["Road traffic"]


def test_explicit_topic_with_no_matching_evidence_is_insufficient():
    result = parse_response("What does road traffic analysis show?", {"sources": [
        {"title": "Network security", "url": "https://example.com/cyber", "snippet": "A cybersecurity study analyzed network traffic."},
    ]})
    assert result.summary.key_takeaways == []
    assert any("not provide enough neutral evidence" in item for item in result.summary.limitations)


def test_research_button_markup_has_no_entity_space_join():
    response = create_app({"TESTING": True}).test_client().get("/")
    assert b"Research<span class=\"button-icon\"" in response.data
    assert b"Research&#x20;" not in response.data


def test_takeaway_markup_separates_metadata_text_and_source_title():
    app = create_app({
        "TESTING": True,
        "RESEARCH_PROVIDER": "tavily",
        "TAVILY_API_KEY": "test-key",
    })
    payload = {"results": [{
        "title": "Evidence source",
        "url": "https://example.com/evidence",
        "content": "A study reported measurable results.",
    }]}
    with patch("omni_research.research.urlopen", return_value=FakeResponse(payload)):
        response = app.test_client().post("/research", data={"question": "What are the results?"})
    body = response.data
    assert b'<div class="takeaway-meta">' in body
    assert b'<p class="takeaway-text">A study reported measurable results. <a class="source-link citation-link"' in body
    assert b">[Evidence source]</a>" not in body


def test_placeholder_and_truncated_fragments_are_not_takeaways():
    result = parse_response("Question", {"sources": [
        {"title": "Placeholder", "url": "https://example.com/placeholder", "snippet": "[...] A study reported results..."},
    ]})
    assert result.summary.key_takeaways == []
    assert result.sources[0].snippet == ""


def test_near_duplicate_takeaways_and_sources_are_reduced():
    result = parse_response("Question", {"sources": [
        {"title": "One", "url": "https://example.com/one", "snippet": "A study reported improved outcomes."},
        {"title": "Two", "url": "https://example.com/two", "snippet": "A study reported improved outcomes for patients."},
        {"title": "Three", "url": "https://example.com/three", "snippet": "A clinical trial measured a different outcome."},
    ]})
    assert len(result.summary.key_takeaways) == 2
    assert {takeaway.source.title for takeaway in result.summary.key_takeaways} == {"One", "Three"}


def test_no_meaningful_takeaway_keeps_insufficient_evidence_state():
    result = parse_response("Question", {"sources": [
        {"title": "Fragment", "url": "https://example.com/fragment", "snippet": "Learn more. Click here."},
    ]})
    assert result.summary.key_takeaways == []
    assert any("not provide enough neutral evidence" in item for item in result.summary.limitations)


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
