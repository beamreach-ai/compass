# The Compass updater: the Docker CLI, the compose plugin, curl and jq, and one
# shell script. Deliberately not the Compass image: no Python, no Compass code.
# It runs with the Docker socket (docker-compose.yml, the `updater` profile).
# Published as ghcr.io/beamreach-ai/compass-updater on a tag.
FROM docker:27-cli
COPY --from=docker/compose-bin:v2.29.7 /docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
RUN apk add --no-cache curl jq
COPY docker/updater/updater.sh /usr/local/bin/compass-updater
RUN chmod +x /usr/local/bin/compass-updater
ENV COMPASS_EXTENSIONS_DIR=/compass/extensions
ENTRYPOINT ["compass-updater"]
