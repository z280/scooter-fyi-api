# Photon geocoding sidecar — the upstream behind GET /api/v1/geocode/search.
#
# Built here rather than pulled from a community image on purpose: every
# third-party "photon" image on Docker Hub is an unaudited wrapper that
# downloads the jar itself, and this container is reachable from the API
# process. The only trust root added is the official komoot/photon release
# asset, pinned by version AND verified by sha256 — the same shape the worker
# Dockerfile uses for supercronic.
#
# The index is NOT baked in: it lives on the `photon_files` volume, seeded from
# R2 by `python -m src.cli fetch_photon_index` (src/r2_map.py) and rebuilt
# quarterly per scripts/build_photon_index.md. Photon opens the index at JVM
# startup and never re-reads it, so a new index needs a container restart.
FROM eclipse-temurin:21-jre

# Photon 1.x requires Java 21+ (hence the temurin 21 base) and embeds
# OpenSearch in-process, so the index format is VERSION-LOCKED: an index built
# by a different photon version may not open. Bump this only together with a
# rebuilt index (scripts/build_photon_index.md), and never in isolation.
#
# Checksum source (do not take it on faith when bumping — re-read it):
#   curl -s https://api.github.com/repos/komoot/photon/releases \
#     | jq -r '.[] | .assets[] | "\(.name) \(.digest)"'
#
# VERIFIED 2026-07-30 against the real release asset, not inferred:
#   * 1.2.1 is the newest release; the asset is `photon-1.2.1.jar` (97,524,141
#     bytes) and its sha256 is the digest below, downloaded and hashed.
#   * `photon-opensearch-1.2.1.jar` 404s. That name is the pre-1.0 artifact,
#     from when the OpenSearch build shipped alongside a deprecated
#     Elasticsearch one; 1.0.0 "fully switches to OpenSearch" and publishes a
#     single jar under the plain name. If you bump into a 0.7.x pin for any
#     reason, the filename changes back — check before assuming.
#   * The `serve` / `import` subcommands and every flag used here and in
#     scripts/build_photon_index.md were read out of this jar's own `-h`
#     output, including `-listen-ip` defaulting to 127.0.0.1.
ARG PHOTON_VERSION=1.2.1
ARG PHOTON_SHA256=6ea66d598a3f6fc1b59b0f81ad4f8abee72ccdb9d3db03bc12008974cdebd58d

# curl is REQUIRED, not a convenience: the eclipse-temurin JRE base ships
# neither curl nor wget, and the compose healthcheck (`curl -fsS
# http://127.0.0.1:2322/status`) execs inside THIS container. Without curl the
# healthcheck can only ever fail, `photon` never reports healthy, and anything
# gating on it stalls. It is also what fetches the jar below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /opt/photon.jar \
      "https://github.com/komoot/photon/releases/download/${PHOTON_VERSION}/photon-${PHOTON_VERSION}.jar" \
    && echo "${PHOTON_SHA256}  /opt/photon.jar" | sha256sum -c -

# `-data-dir` is the PARENT of photon_data/, which is exactly how the tarball
# unpacks (src/r2_map.PHOTON_DATA_DIRNAME).
ENV PHOTON_DATA_DIR=/photon
# Heap default; docker-compose.yml sets this explicitly next to mem_limit so
# the two are read together. Photon embeds OpenSearch, so the JVM heap is the
# real ceiling — a Colorado-scoped index is what keeps it under 2 GiB.
ENV JAVA_OPTS="-Xmx1536m"

# The volume must be writable: embedded OpenSearch takes a node lock and writes
# translog state inside photon_data/. Deliberately no USER directive — the
# one-shot fetch sidecar unpacks the index as root, and a non-root photon would
# be unable to lock what it was handed.
RUN mkdir -p /photon
WORKDIR /photon

# Internal only — no host port. Riders reach this through the API proxy.
EXPOSE 2322

# `serve` is photon 1.x's subcommand form. `-listen-ip 0.0.0.0` is load-bearing:
# photon defaults to 127.0.0.1, which inside a container means nothing on the
# compose network can reach it and every proxied lookup 503s.
# sh -c so $JAVA_OPTS expands; exec so java is PID 1 and gets SIGTERM directly.
CMD ["sh", "-c", "exec java $JAVA_OPTS -jar /opt/photon.jar serve -data-dir \"$PHOTON_DATA_DIR\" -listen-ip 0.0.0.0 -listen-port 2322"]
