# OmniResearch

OmniResearch is a small Flask web app for asking a research question, displaying a grounded summary, and listing source links returned by a configured research integration.

The repository does **not** include fake sources or a hidden demo provider. In the current OmniRush environment, no web-search integration is exposed directly to this workspace, so the default behavior is a clear setup message. The app can call an approved JSON endpoint when you configure one.

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

## Configure an approved research endpoint

Copy `.env.example` to `.env` only for local use and set `RESEARCH_API_URL` and `RESEARCH_API_KEY` in your terminal environment. The endpoint must accept:

```json
{"query":"your question"}
```

and return:

```json
{"summary":"grounded text", "sources":[{"title":"Source title", "url":"https://...", "snippet":"optional evidence"}]}
```

The app only displays `http://` and `https://` source URLs returned by that endpoint. Never commit `.env` or API keys.

## Test

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests run offline and cover the page, validation, unavailable-provider behavior, and source URL filtering.

## Docker

```powershell
```

The image starts the same safe, unconfigured state unless you pass approved configuration with `-e`; do not put secrets in the image or repository.

## Project layout

- `app.py` — application entry point
- `omni_research/` — Flask routes, provider boundary, HTML, and CSS
- `tests/` — offline pytest tests
- `requirements.in` and `requirements.lock` — pinned direct and fully resolved dependencies
- `Dockerfile` — reproducible container build
