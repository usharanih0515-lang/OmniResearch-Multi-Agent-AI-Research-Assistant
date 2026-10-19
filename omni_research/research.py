import json
import re
from dataclasses import dataclass
from datetime import date, datetime
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
    published_date: str | None = None

    @property
    def excerpt(self) -> str:
        return _shorten(self.snippet, 220)

    @property
    def date_label(self) -> str | None:
        if not self.published_date:
            return None
        try:
            published = datetime.fromisoformat(self.published_date.replace("Z", "+00:00")).date()
        except ValueError:
            return None
        age_days = (date.today() - published).days
        return "Older background" if age_days > 365 else "Recent source"


@dataclass(frozen=True)
class ResearchResult:
    question: str
    summary: "EvidenceSummary"
    sources: list[Source]
    news: list[Source]


@dataclass(frozen=True)
class EvidenceSummary:
    key_takeaways: list["Takeaway"]
    supporting_evidence: list[Source]
    limitations: list[str]


@dataclass(frozen=True)
class Takeaway:
    text: str
    source: Source
    evidence_type: str


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
                "published_date": item.get("published_date"),
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
        published_date = item.get("published_date")
        published_date = str(published_date).strip() if published_date else None
        title_key = title.casefold()
        if title and _is_http_url(url) and url not in seen_urls and title_key not in seen_titles:
            sources.append(Source(title=title, url=url, snippet=snippet, published_date=published_date))
            seen_urls.add(url)
            seen_titles.add(title_key)
    return sources


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def summarize_sources(sources: list[Source], news: list[Source] | None = None) -> EvidenceSummary:
    news = news or []
    key_takeaways = []
    seen_takeaways = set()
    limitations = []
    for source in sources:
        candidates = sorted(
            enumerate(_sentences(source.snippet)),
            key=lambda item: (-_evidence_score(item[1], item[0]), item[0]),
        )
        for _, sentence in candidates:
            sentence_key = sentence.casefold()
            if not sentence or sentence_key in seen_takeaways:
                continue
            if _is_promotional(sentence):
                limitations.append(
                    f"Promotional wording from {source.title} was not presented as a factual takeaway."
                )
                continue
            key_takeaways.append(
                Takeaway(
                    text=_shorten(sentence),
                    source=source,
                    evidence_type=_evidence_type(sentence),
                )
            )
            seen_takeaways.add(sentence_key)
            if len(key_takeaways) == 3:
                break
        if len(key_takeaways) == 3:
            break

    supporting_evidence = [source for source in sources if source.snippet][:5]
    if len(sources) > len(supporting_evidence):
        limitations.append("Some returned sources were not included in the evidence list to keep the summary concise.")
    if any(not source.snippet for source in sources):
        limitations.append("Some returned sources did not include a snippet, so they are shown as links only.")
    if not key_takeaways:
        limitations.append("The retrieved content does not provide enough neutral evidence for a factual takeaway.")
    if not news:
        limitations.append("No news results were returned by the provider.")
    return EvidenceSummary(
        key_takeaways=key_takeaways,
        supporting_evidence=supporting_evidence,
        limitations=limitations,
    )


def _clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value).strip())


def _sentences(value: str) -> list[str]:
    return [sentence.strip(" \t\r\n") for sentence in re.split(r"(?<=[.!?])\s+", value) if sentence.strip()]


def _is_promotional(value: str) -> bool:
    promotional_terms = (
        "revolution", "game-changing", "game changing", "breakthrough", "unprecedented",
        "transform", "world-leading", "best-in-class", "miracle", "guaranteed",
        "in conclusion", "broader implications", "should excite", "we explore",
        "this article", "in this review",
    )
    lowered = value.casefold()
    return any(term in lowered for term in promotional_terms)


def _evidence_type(value: str) -> str:
    lowered = value.casefold()
    if any(term in lowered for term in ("mouse", "mice", "animal study", "preclinical", "in vivo")):
        return "Animal or preclinical evidence"
    if any(term in lowered for term in ("clinical trial", "randomized", "participants", "patients were enrolled")):
        return "Clinical trial evidence"
    if any(term in lowered for term in ("in vitro", "laboratory", "early-stage", "early stage")):
        return "Early-stage research"
    if any(term in lowered for term in ("approved treatment", "standard treatment", "standard of care", "clinical practice")):
        return "Established treatment information"
    return "Provider-reported evidence"


def _evidence_score(value: str, position: int) -> int:
    lowered = value.casefold()
    evidence_terms = (
        "study", "trial", "participants", "patients", "results", "found", "observed",
        "reported", "associated", "measured", "treatment", "evidence", "data",
    )
    score = sum(term in lowered for term in evidence_terms) * 3
    if _is_promotional(value):
        score -= 20
    if position == 0:
        score -= 2
    return score


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
