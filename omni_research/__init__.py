import os

from flask import Flask, render_template, request

from .research import ResearchError, create_provider


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        MAX_QUESTION_LENGTH=500,
        RESEARCH_API_URL=os.getenv("RESEARCH_API_URL", ""),
        RESEARCH_API_KEY=os.getenv("RESEARCH_API_KEY", ""),
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

        provider = create_provider(
            app.config.get("RESEARCH_API_URL", ""),
            app.config.get("RESEARCH_API_KEY", ""),
        )
        try:
            result = provider.research(question)
        except ResearchError as exc:
            return render_template(
                "index.html", question=question, result=None, error=str(exc)
            ), 503
        return render_template("index.html", question=question, result=result, error=None)

    return app
