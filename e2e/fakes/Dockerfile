# The end-to-end suite's fakes (e2e/fakes/app.py): GitHub (REST, GraphQL and git over HTTP), a webhook
# receiver, the release feed, the catalog.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "fastapi>=0.115,<1" "uvicorn>=0.30,<1"
COPY app.py /app/app.py
WORKDIR /app
EXPOSE 9000
# info: every request in the logs, what Compass called and the answer it got
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9000", "--log-level", "info"]
