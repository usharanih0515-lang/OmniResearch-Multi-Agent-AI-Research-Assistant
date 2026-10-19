import json
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlparse


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
    summary: "EvidenceSummary"
    sources: list[Source]
    news: list[Source]


@dataclass(frozen=True)
class EvidenceSummary:
    key_takeaways: list[str]
    supporting_evidence: list[Source]
    limitations: list[str]


DEFAULT_TAVILY_URL = "https://api.tavily.com/search"
REQUEST_TIMEOUT_SECONDS = 20


class UnavailableProvider:
    def research(self, question: str) -> ResearchResult:
        raise ResearchError(
            "No research integration is configured. Set RESEARCH_PROVIDER=tavily "
            "and TAVILY_API_KEY, or configure an approved JSON endpoint."
        )


class HttpResearchProvider:
    """Calls an explicitly configured, approved endpoint; it has no default URL."""

    def __init__(self, url: str, api_key: str):
        self.url = url
        self.api_key = api_key

    def research(self, question: str) -> ResearchResult:
        body = json.dumps({"query": question}).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        request = Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise _http_error(exc) from exc
        except (URLError, TimeoutError) as exc:
            raise ResearchError("The configured research integration could not be reached.") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResearchError("The research integration returned malformed JSON.") from exc
        return parse_response(question, payload)


class TavilySearchProvider:
    """Adapter for Tavily's official search API."""

    def __init__(self, api_key: str, url: str = DEFAULT_TAVILY_URL):
        self.api_key = api_key
        self.url = url

    def research(self, question: str) -> ResearchResult:
        body = json.dumps(
            {"api_key": self.api_key, "query": question, "search_depth": "basic", "max_results": 5}
        ).encode("utf-8")
        request = Request(self.url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise _http_error(exc) from exc
        except (URLError, TimeoutError) as exc:
            raise ResearchError("Tavily could not be reached. Check your network connection and try again.") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResearchError("Tavily returned malformed JSON.") from exc

        if not isinstance(payload, dict):
            raise ResearchError("Tavily returned an unexpected response format.")
        results = payload.get("results")
        if not isinstance(results, list):
            raise ResearchError("Tavily returned malformed source data.")
        sources = [
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "snippet": item.get("content", item.get("snippet", "")),
            }
            for item in results
            if isinstance(item, dict)
        ]
        return parse_response(question, {"sources": sources, "news": payload.get("news", [])})


def _http_error(error: HTTPError) -> ResearchError:
    if error.code in (401, 403):
        return ResearchError("The research provider rejected the API key. Check your local configuration.")
    if error.code == 429:
        return ResearchError("The research provider rate limit was reached. Please wait and try again.")
    if 500 <= error.code <= 599:
        return ResearchError("The research provider is temporarily unavailable. Please try again later.")
    return ResearchError(f"The research provider returned HTTP {error.code}.")


def parse_response(question: str, payload: dict) -> ResearchResult:
    if not isinstance(payload, dict):
        raise ResearchError("The research integration returned an unexpected response format.")
    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list):
        raise ResearchError("The research integration returned malformed source data.")
    sources = _parse_sources(raw_sources)

    if not sources:
        raise ResearchError("The research integration returned no valid source links.")

    raw_news = payload.get("news", [])
    if not isinstance(raw_news, list):
        raise ResearchError("The research integration returned malformed news data.")
    news = _parse_sources(raw_news)
    news_urls = {source.url for source in news}
    sources = [source for source in sources if source.url not in news_urls]
    summary = summarize_sources(sources, news)
    return ResearchResult(question=question, summary=summary, sources=sources, news=news)


def _parse_sources(raw_sources: list[object]) -> list[Source]:
    sources = []
    seen_urls = set()
    seen_titles = set()
    for item in raw_sources:
        if not isinstance(item, dict):
            continue
        title = _clean_text(item.get("title", ""))
        url = str(item.get("url", "")).strip()
        snippet = _clean_text(item.get("snippet", item.get("content", "")))
        title_key = title.casefold()
        if title and _is_http_url(url) and url not in seen_urls and title_key not in seen_titles:
            sources.append(Source(title=title, url=url, snippet=snippet))
            seen_urls.add(url)
            seen_titles.add(title_key)
    return sources


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def summarize_sources(sources: list[Source], news: list[Source] | None = None) -> EvidenceSummary:
    news = news or []
    snippets = []
    for source in sources:
        if source.snippet and source.snippet.casefold() not in {item.casefold() for item in snippets}:
            snippets.append(source.snippet)

    key_takeaways = [_shorten(snippet) for snippet in snippets[:3]]
    supporting_evidence = [source for source in sources if source.snippet][:5]
    limitations = []
    if len(sources) > len(supporting_evidence):
        limitations.append("Some returned sources were not included in the evidence list to keep the summary concise.")
    if any(not source.snippet for source in sources):
        limitations.append("Some returned sources did not include a snippet, so they are shown as links only.")
    if not key_takeaways:
        limitations.append("The provider returned no source snippets, so no evidence-based takeaway is available.")
    if not news:
        limitations.append("No news results were returned by the provider.")
    return EvidenceSummary(
        key_takeaways=key_takeaways,
        supporting_evidence=supporting_evidence,
        limitations=limitations,
    )


def _clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value).strip())


def _shorten(value: str, limit: int = 240) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rsplit(" ", 1)[0] + "…"


def create_provider(provider: str, url: str, api_key: str):
    if provider == "tavily" and api_key:
        return TavilySearchProvider(api_key, url or DEFAULT_TAVILY_URL)
    if provider == "http" and url and api_key:
        return HttpResearchProvider(url, api_key)
    return UnavailableProvider()
