from omni_research import create_app
from omni_research.research import ResearchError, parse_response


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


def test_only_valid_http_sources_are_rendered():
    result = parse_response(
        "Question",
        {
            "summary": "A grounded summary.",
            "sources": [
                {"title": "Valid", "url": "https://example.com", "snippet": "Evidence."},
                {"title": "Missing URL", "snippet": "Ignored."},
                {"title": "Bad URL", "url": "javascript:alert(1)"},
            ],
        },
    )
    assert result.summary == "A grounded summary."
    assert [(source.title, source.url) for source in result.sources] == [("Valid", "https://example.com")]


def test_empty_provider_sources_fail_loudly():
    try:
        parse_response("Question", {"sources": []})
    except ResearchError as exc:
        assert "no valid source links" in str(exc)
    else:
        raise AssertionError("Expected invalid source response to fail")
