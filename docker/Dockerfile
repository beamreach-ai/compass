# Compass as one image: the compass-open distribution, Terraform for code-as-cloud, git.
# Published as ghcr.io/beamreach-ai/compass on a tag (.github/workflows/release.yml);
# built from the wheel in dist/ (python -m build --wheel).
FROM hashicorp/terraform:1.5 AS terraform

FROM python:3.12-slim
ARG COMPASS_VERSION=
ARG WHEEL=dist/*.whl
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=terraform /bin/terraform /usr/local/bin/terraform
COPY ${WHEEL} /tmp/dist/
RUN pip install --no-cache-dir "$(ls /tmp/dist/*.whl)[anthropic]" && rm -rf /tmp/dist
COPY docker/entrypoint.sh /usr/local/bin/compass-entrypoint
RUN chmod +x /usr/local/bin/compass-entrypoint
WORKDIR /compass
ENV COMPASS_CONFIG=/compass/config/local.json \
    COMPASS_MAP_DIR=/compass/map \
    COMPASS_MAP_PORT=8787 \
    COMPASS_ALLOW_LOCAL_IAM=1 \
    COMPASS_EXTENSIONS_DIR=/compass/extensions \
    PYTHONUNBUFFERED=1
EXPOSE 8787
# Extensions (COMPASS_EXTENSIONS, a list of pip requirements) are installed into
# /compass/extensions on start; their captains are found by entry point.
ENTRYPOINT ["compass-entrypoint"]
CMD ["serve"]
