# OmniResearch

OmniResearch is a small Flask web app for asking a research question, displaying a grounded summary, and listing source links returned by a configured research integration.

The repository does **not** include fake sources or a hidden demo provider. The app includes a real adapter for [Tavily Search](https://docs.tavily.com/), but this workspace does not currently have a Tavily key or a direct OmniRush search extension connected. Until you configure Tavily locally, the app shows a clear setup message.

The OmniRush extension inventory was checked before implementation: it exposes Google Workspace and image-generation actions, but no search/research action. Therefore this app uses the official Tavily API only when you explicitly configure it, and does not claim that an OmniRush-native search integration is available.

## Requirements

- Python 3.13 (pinned in `.python-version`)
- Windows through the supported OmniRush terminal, or Docker Desktop

## Run on Windows in OmniRush

From the project folder:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe app.py
```

Open http://127.0.0.1:5000 in a browser.

Without configuration, submitting a question intentionally shows that no research integration is configured. This is safer than inventing an answer.

## Configure Tavily Search (real provider)

Create a Tavily account and obtain an API key from its official dashboard. Then set these variables in the PowerShell session before starting the app. The app intentionally does not load `.env` files automatically:

```powershell
$env:RESEARCH_PROVIDER = "tavily"
$env:TAVILY_API_KEY = "your-local-tavily-key"
$env:TAVILY_API_URL = "https://api.tavily.com/search"
```

The key is sent only to Tavily over HTTPS and is never logged by the app. Do not commit it.

## Configure an approved JSON endpoint instead

If your organization provides an approved research service, set:

```powershell
$env:RESEARCH_PROVIDER = "http"
$env:RESEARCH_API_URL = "https://approved.example/research"
$env:RESEARCH_API_KEY = "your-local-key"
```

The endpoint must accept:

```json
{"query":"your question"}
```

and return:

```json
{"summary":"grounded text", "sources":[{"title":"Source title", "url":"https://...", "snippet":"optional evidence"}]}
```

The app only displays `http://` and `https://` source URLs returned by the provider. It creates an evidence summary only from returned source snippets/content; it does not invent unsupported claims.

## Test

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests run offline with mocked provider responses. They cover successful search, missing configuration, authentication failures, rate limits, timeouts, network failures, malformed responses, invalid URLs, and evidence-only summaries.

## Docker

```powershell
docker build -t omniresearch .
docker run --rm -p 5000:5000 -e RESEARCH_PROVIDER=tavily -e TAVILY_API_KEY=your-local-key omniresearch
```

The image starts the same safe, unconfigured state unless you pass approved configuration with `-e`; do not put secrets in the image or repository.

## Project layout

- `app.py` — application entry point
- `omni_research/` — Flask routes, provider boundary, HTML, and CSS
- `tests/` — offline pytest tests
- `requirements.in` and `requirements.lock` — pinned direct and fully resolved dependencies
- `Dockerfile` — reproducible container build
