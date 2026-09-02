FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -e '.[web]'
# Default: run the dashboard. Override CMD for the search worker.
ENV CIRCLE_LEADS_DB=""
EXPOSE 8000
CMD ["sh", "-c", "circle-leads --db \"$CIRCLE_LEADS_DB\" dashboard --host 0.0.0.0 --port ${PORT:-8000}"]
