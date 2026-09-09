# One image, two services (web + worker) — they differ only by CMD.
FROM python:3.12-slim
WORKDIR /app

# System deps for Playwright/Chromium are only needed if you run the browser
# feed reader in the cloud (private communities). The public harvest doesn't
# need it, so it's left out to keep the image small. Add it if you need it.
COPY . .
# `llm` pulls in openai + anthropic so the classifier's LLM layer works for
# genuinely ambiguous posts. Without it make_backend() falls back to rules only,
# even when OPENAI_API_KEY / ANTHROPIC_API_KEY are set.
RUN pip install --no-cache-dir -e '.[web,llm]'

ENV CIRCLE_LEADS_DB=""
EXPOSE 8000

# Default command runs the dashboard. The worker overrides CMD (see render.yaml
# / deploy/railway.md).
CMD ["sh", "-c", "circle-leads --db \"$CIRCLE_LEADS_DB\" dashboard --host 0.0.0.0 --port ${PORT:-8000}"]
