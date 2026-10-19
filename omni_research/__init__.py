import os
from pathlib import Path

from flask import Flask, render_template, request

from .research import ResearchError, create_provider


_LOCAL_ENV_KEYS = {
    "RESEARCH_PROVIDER",
    "RESEARCH_API_URL",
    "RESEARCH_API_KEY",
    "TAVILY_API_KEY",
    "TAVILY_API_URL",
}


def _load_local_env(path: str = ".env") -> None:
    """Load supported local settings without overriding real environment variables."""
    env_path = Path(path)
    if not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = (part.strip() for part in line.split("=", 1))
        if name.startswith("export "):
            name = name[7:].strip()
        if name not in _LOCAL_ENV_KEYS or name in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[name] = value


def create_app(test_config=None):
    _load_local_env()
    app = Flask(__name__)
    app.config.from_mapping(
        MAX_QUESTION_LENGTH=500,
        RESEARCH_PROVIDER=os.getenv("RESEARCH_PROVIDER", ""),
        RESEARCH_API_URL=os.getenv("RESEARCH_API_URL", ""),
        RESEARCH_API_KEY=os.getenv("RESEARCH_API_KEY", ""),
        TAVILY_API_KEY=os.getenv("TAVILY_API_KEY", ""),
        TAVILY_API_URL=os.getenv("TAVILY_API_URL", ""),
    )
    if test_config:
        app.config.update(test_config)

    @app.get("/")
    def index():
        return render_template("index.html", question="", result=None, error=None)

    @app.post("/research")
    def research():
        question = request.form.get("question", "").strip()
        if not question:
            return render_template(
                "index.html", question=question, result=None,
                error="Enter a research question first.",
            ), 400
        if len(question) > app.config["MAX_QUESTION_LENGTH"]:
            return render_template(
                "index.html", question=question, result=None,
                error=f"Keep the question under {app.config['MAX_QUESTION_LENGTH']} characters.",
            ), 400

        provider_name = app.config.get("RESEARCH_PROVIDER", "")
        if provider_name == "tavily":
            provider_url = app.config.get("TAVILY_API_URL", "")
            provider_key = app.config.get("TAVILY_API_KEY", "")
        else:
            provider_url = app.config.get("RESEARCH_API_URL", "")
            provider_key = app.config.get("RESEARCH_API_KEY", "")
        provider = create_provider(provider_name, provider_url, provider_key)
        try:
            result = provider.research(question)
        except ResearchError as exc:
            return render_template(
                "index.html", question=question, result=None, error=str(exc)
            ), 503
        return render_template("index.html", question=question, result=result, error=None)

    return app
