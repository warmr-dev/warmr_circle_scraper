# One image, two services (web + worker) — they differ only by CMD.
FROM python:3.12-slim
WORKDIR /app

COPY . .
# `llm` pulls in openai + anthropic so the classifier's LLM layer works for
# genuinely ambiguous posts. Without it make_backend() falls back to rules only,
# even when OPENAI_API_KEY / ANTHROPIC_API_KEY are set.
# `browser` (Playwright) is needed for `discover-directory`: discover.circle.so
# challenges plain HTTP requests (Cloudflare), so the bulk directory crawl runs a
# real headless Chromium -- see circle_leads/discovery/circle_directory.py.
RUN pip install --no-cache-dir -e '.[web,llm,browser]' \
    && playwright install --with-deps chromium

ENV CIRCLE_LEADS_DB=""
EXPOSE 8000

# Default command runs the dashboard. The worker overrides CMD (see render.yaml
# / deploy/railway.md).
CMD ["sh", "-c", "circle-leads --db \"$CIRCLE_LEADS_DB\" dashboard --host 0.0.0.0 --port ${PORT:-8000}"]
