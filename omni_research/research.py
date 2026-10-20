import json
import html
import re
from dataclasses import dataclass
from datetime import date, datetime
from difflib import SequenceMatcher
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

    @property
    def topic_label(self) -> str | None:
        context = _source_context(self)
        return None if context == "general" else context


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
    topic_context: str | None = None


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
    summary = summarize_sources(_relevant_sources(sources, question), news, question)
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
        snippet = _clean_snippet(item.get("snippet", item.get("content", "")))
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


def _source_context(source: Source) -> str:
    text = f"{source.title} {source.snippet}".casefold()
    if re.search(r"\b(cyber|cybersecurity|malware|firewall|packet|intrusion|network security)\b", text):
        return "cybersecurity/network traffic"
    if re.search(r"\b(road|roads|vehicle|traffic flow|highway|congestion|transportation)\b", text):
        return "road/transport traffic"
    if re.search(r"\b(telecom|telecommunication|communication network|data traffic|bandwidth|signal)\b", text):
        return "communication traffic"
    return "general"


def _requested_context(question: str) -> str | None:
    text = question.casefold()
    if re.search(r"\b(cybersecurity|cyber security|network security|malware|intrusion)\b", text):
        return "cybersecurity/network traffic"
    if re.search(r"\b(road traffic|traffic flow|highway|vehicle traffic|transportation)\b", text):
        return "road/transport traffic"
    if re.search(r"\b(telecom|telecommunication|communication traffic|bandwidth)\b", text):
        return "communication traffic"
    return None


def _relevant_sources(sources: list[Source], question: str) -> list[Source]:
    requested = _requested_context(question)
    if not requested:
        return sources
    return [source for source in sources if _source_context(source) in {requested, "general"}]


def summarize_sources(
    sources: list[Source], news: list[Source] | None = None, question: str = ""
) -> EvidenceSummary:
    news = news or []
    key_takeaways = []
    seen_takeaways = set()
    limitations = []
    filtered_non_finding = False
    selected_sources = set()
    contexts = {_source_context(source) for source in sources} - {"general"}
    query_terms = _meaningful_terms(question)
    if len(query_terms) < 2:
        query_terms = set()
    for source in sources:
        candidates = sorted(
            enumerate(_sentences(source.snippet)),
            key=lambda item: (-_evidence_score(item[1], item[0], query_terms), item[0]),
        )
        for _, sentence in candidates:
            sentence_key = _comparison_key(sentence)
            if not sentence or not sentence_key or sentence_key in seen_takeaways:
                continue
            if _is_non_finding(sentence):
                filtered_non_finding = True
                continue
            if _meaningful_terms(sentence).isdisjoint(query_terms) and query_terms:
                continue
            if any(_near_duplicate(sentence_key, existing) for existing in seen_takeaways):
                continue
            if source.url in selected_sources:
                continue
            takeaway_text = _complete_takeaway(sentence)
            if takeaway_text is None:
                continue
            if len(key_takeaways) < 3 and source.url not in selected_sources:
                key_takeaways.append(
                    Takeaway(
                        text=takeaway_text,
                        source=source,
                        evidence_type=_evidence_type(sentence),
                        topic_context=source.topic_label,
                    )
                )
                selected_sources.add(source.url)
            seen_takeaways.add(sentence_key)

    supporting_evidence = [source for source in sources if source.snippet][:5]
    if len(sources) > len(supporting_evidence):
        limitations.append("Some returned sources were not included in the evidence list to keep the summary concise.")
    if any(not source.snippet for source in sources):
        limitations.append("Some returned sources did not include a snippet, so they are shown as links only.")
    if filtered_non_finding:
        limitations.append("Some source text was excluded because it contained navigation or promotional wording.")
    if len(contexts) > 1:
        limitations.append(
            "The returned evidence spans multiple meanings of the topic: "
            + ", ".join(sorted(contexts))
            + "."
        )
    if not key_takeaways:
        limitations.append("The retrieved content does not provide enough neutral evidence for a factual takeaway.")
    if not news:
        limitations.append("No news results were returned by the provider.")
    return EvidenceSummary(
        key_takeaways=key_takeaways,
        supporting_evidence=supporting_evidence,
        limitations=_unique_limitations(limitations),
    )


def _clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value)).strip())


def _clean_snippet(value: object) -> str:
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\[\s*\.\.\.\s*\]", " ", text)
    was_truncated = bool(re.search(r"(?:\.\.\.|…)\s*$", text))
    text = re.sub(r"(?m)^\s*\\?#{1,6}\s*", "", text)
    text = re.sub(r"(?<=[.!?])(?=[A-Z])", " ", text)
    text = re.sub(
        r"\b(?:learn more|read more|click here|sign up|subscribe|contact us|get in touch|book a demo|request a demo|schedule a demo|try for free|free trial|start your free|view route alerts?|route alerts?|check route|get directions|traffic alerts?|travel advisory|detour|menu|navigation|skip to content|home|about us|privacy policy|terms of use|cookie policy)\b[.!:—-]*",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s*(?:\.\.\.|…)\s*$", "", text)
    text = re.sub(r"\s+", " ", text).strip(" -–—|:")
    text = re.sub(r"^[.|:;—-]+\s*", "", text)
    return "" if was_truncated else text


def _sentences(value: str) -> list[str]:
    return [sentence.strip(" \t\r\n") for sentence in re.split(r"(?<=[.!?])\s+", value) if sentence.strip()]


def _is_promotional(value: str) -> bool:
    promotional_terms = (
        "revolution", "game-changing", "game changing", "breakthrough", "unprecedented",
        "transform", "world-leading", "best-in-class", "miracle", "guaranteed",
        "in conclusion", "broader implications", "should excite", "we explore",
        "this article", "in this review", "free trial", "try for free", "start your free",
        "book a demo", "request a demo", "schedule a demo", "get started free", "contact sales",
    )
    lowered = value.casefold()
    return any(term in lowered for term in promotional_terms)


def _is_non_finding(value: str) -> bool:
    lowered = value.casefold().strip(" .!?:;-")
    if _is_incomplete_fragment(value):
        return True
    if _is_promotional(value):
        return True
    if _is_navigation_only(value):
        return True
    if re.fullmatch(r"(?:learn|read|click|see|view|visit|sign|subscribe)\b.*", lowered):
        return True
    if len(_meaningful_terms(value)) < 3:
        return True
    if not re.search(r"\b(?:study|trial|result|found|observed|reported|associated|measured|treatment|evidence|data|patients?|participants?|research|analysis|risk|rate|effect|outcome)\b", lowered):
        return True
    return False


def _is_incomplete_fragment(value: str) -> bool:
    stripped = value.strip()
    return bool(stripped) and (
        stripped[0].islower()
        or stripped.startswith(("…", "...", "["))
        or stripped.endswith(("…", "..."))
        or "[...]" in stripped
    )


def _is_navigation_only(value: str) -> bool:
    navigation_terms = r"\b(route|routes|alert|alerts|directions|detour|menu|navigation|contact|subscribe|sign up)\b"
    factual_terms = r"\b(study|trial|result|found|observed|reported|measured|treatment|evidence|data|patients?|participants?|research|analysis|risk|rate|effect|outcome|traffic flow|congestion)\b"
    return bool(re.search(navigation_terms, value.casefold())) and not re.search(factual_terms, value.casefold())


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


def _evidence_score(value: str, position: int, query_terms: set[str] | None = None) -> int:
    lowered = value.casefold()
    evidence_terms = (
        "study", "trial", "participants", "patients", "results", "found", "observed",
        "reported", "associated", "measured", "treatment", "evidence", "data",
    )
    score = sum(term in lowered for term in evidence_terms) * 3
    score += len(_meaningful_terms(value) & (query_terms or set())) * 5
    if _is_promotional(value):
        score -= 20
    return score


def _meaningful_terms(value: str) -> set[str]:
    stop_words = {
        "about", "after", "also", "been", "being", "could", "from", "have", "into",
        "more", "that", "their", "there", "these", "they", "this", "those", "what",
        "when", "where", "which", "with", "would", "your", "the", "and", "for", "are",
        "how", "does", "does", "can", "use", "using", "new", "recent",
    }
    return {word for word in re.findall(r"[a-z0-9]+", value.casefold()) if len(word) > 2 and word not in stop_words}


def _comparison_key(value: str) -> str:
    return " ".join(sorted(_meaningful_terms(value)))


def _near_duplicate(left: str, right: str) -> bool:
    left_terms, right_terms = set(left.split()), set(right.split())
    overlap = len(left_terms & right_terms) / max(1, len(left_terms | right_terms))
    return overlap >= 0.7 or SequenceMatcher(None, left, right).ratio() >= 0.82


def _unique_limitations(values: list[str]) -> list[str]:
    """Keep displayed report limitations stable and non-repetitive."""
    seen = set()
    unique = []
    for value in values:
        key = re.sub(r"\s+", " ", value.casefold()).strip()
        if key not in seen:
            unique.append(value)
            seen.add(key)
    return unique


def _shorten(value: str, limit: int = 240) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rsplit(" ", 1)[0] + "…"


def _complete_takeaway(value: str, limit: int = 240) -> str | None:
    """Return only a complete source sentence; never present a truncated one."""
    if len(value) > limit or not value.rstrip().endswith((".", "!", "?")):
        return None
    return value


def create_provider(provider: str, url: str, api_key: str):
    if provider == "tavily" and api_key:
        return TavilySearchProvider(api_key, url or DEFAULT_TAVILY_URL)
    if provider == "http" and url and api_key:
        return HttpResearchProvider(url, api_key)
    return UnavailableProvider()
