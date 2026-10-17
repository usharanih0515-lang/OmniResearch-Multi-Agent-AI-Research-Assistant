import json
from dataclasses import dataclass
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen


class ResearchError(Exception):
    """A user-safe research integration error."""


@dataclass(frozen=True)
class Source:
    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class ResearchResult:
    question: str
    summary: str
    sources: list[Source]


class UnavailableProvider:
    def research(self, question: str) -> ResearchResult:
        raise ResearchError(
            "No research integration is configured. Set RESEARCH_API_URL and "
            "RESEARCH_API_KEY to an approved JSON research endpoint, then try again."
        )


class HttpResearchProvider:
    """Calls an explicitly configured, approved endpoint; it has no default URL."""

    def __init__(self, url: str, api_key: str):
        self.url = url
        self.api_key = api_key

    def research(self, question: str) -> ResearchResult:
        body = json.dumps({"query": question}).encode("utf-8")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        request = Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ResearchError("The configured research integration could not be reached.") from exc
        return parse_response(question, payload)


def parse_response(question: str, payload: dict) -> ResearchResult:
    raw_sources = payload.get("sources", [])
    sources = []
    for item in raw_sources:
        if not isinstance(item, dict):
            continue
        title, url = str(item.get("title", "")).strip(), str(item.get("url", "")).strip()
        snippet = str(item.get("snippet", "")).strip()
        if title and url.startswith(("https://", "http://")):
            sources.append(Source(title=title, url=url, snippet=snippet))

    if not sources:
        raise ResearchError("The research integration returned no valid source links.")

    summary = str(payload.get("summary", "")).strip()
    if not summary:
        summary = " ".join(source.snippet for source in sources if source.snippet)
    if not summary:
        summary = "Sources were found, but the integration did not provide summary text."
    return ResearchResult(question=question, summary=summary, sources=sources)


def create_provider(url: str, api_key: str):
    if url and api_key:
        return HttpResearchProvider(url, api_key)
    return UnavailableProvider()
