# Build from the repository root:
#   docker buildx build --platform linux/amd64 -f captains/honeypot/listener/Dockerfile -t <registry>/db-backup:latest .
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app
RUN pip install --no-cache-dir boto3 && useradd --system --no-create-home app
WORKDIR /app
RUN mkdir -p captains/honeypot
COPY captains/honeypot/listener captains/honeypot/listener
RUN touch captains/__init__.py captains/honeypot/__init__.py
USER app
EXPOSE 5432
CMD ["python", "-m", "captains.honeypot.listener"]
