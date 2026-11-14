# syntax=docker/dockerfile:1
ARG GO_VERSION=1.23.1

# Cross-compile on the build host; the binary is pure Go (CGO off), so no emulation is needed.
FROM --platform=$BUILDPLATFORM golang:${GO_VERSION}-alpine AS builder
ARG TARGETOS
ARG TARGETARCH
ARG VERSION=dev
ARG COMMIT=none
ARG BUILD_DATE=unknown
ARG REPO_OWNER=zhaarey
ARG REPO_NAME=apple-music-downloader
WORKDIR /src
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=bind,source=go.mod,target=go.mod \
    --mount=type=bind,source=go.sum,target=go.sum \
    go mod download
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    --mount=type=bind,target=. \
    CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} \
    go build -trimpath -ldflags="-s -w \
      -X 'amdl/internal/version.Version=${VERSION}' \
      -X 'amdl/internal/version.Commit=${COMMIT}' \
      -X 'amdl/internal/version.BuildDate=${BUILD_DATE}' \
      -X 'amdl/internal/updater.RepoOwner=${REPO_OWNER}' \
      -X 'amdl/internal/updater.RepoName=${REPO_NAME}'" \
      -o /out/amdl .

# MCP server for Claude Code (mcp/). Built for the target platform, so the locked
# wheels match the Python of the runtime image (same Alpine release).
FROM alpine:3.22 AS mcp
RUN apk add --no-cache python3
COPY --from=ghcr.io/astral-sh/uv:0.11.18 /uv /usr/local/bin/uv
ENV UV_PYTHON=/usr/bin/python3 UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/mcp/.venv
WORKDIR /opt/mcp
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=mcp/pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=mcp/uv.lock,target=uv.lock \
    uv sync --frozen --no-dev --no-install-project
COPY mcp/server.py /opt/mcp/server.py
COPY config.yaml.example /opt/mcp/config.yaml.example

FROM alpine:3.22
# ffmpeg is used for animated artwork and post-download conversion.
# python3 runs the MCP server (`mcp` command, see docker-entrypoint.sh).
RUN apk add --no-cache ca-certificates ffmpeg tzdata python3
COPY --from=builder /out/amdl /usr/local/bin/amdl
COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY --from=mcp /opt/mcp /opt/mcp

# config.yaml is created in /data from the embedded template on first run, and the
# relative save folders (AM-Lossless, AM-Atmos, ...) resolve there as well.
# The update-check cache lives under /data too, so the image also works when run
# as an arbitrary non-root user (--user 1000:1000) that has no home directory.
ENV XDG_CACHE_HOME=/data/.cache
# MCP server paths inside the image; its job logs and quarantine live in /data/.amdl-mcp.
ENV AMDL_MCP_CONTAINER=1 AMDL_DIR=/data AMDL_EXE=/usr/local/bin/amdl AMDL_MCP_STATE=/data/.amdl-mcp \
    MUSIC_ROOT=/music FOOBAR_PROFILE=/foobar
WORKDIR /data
VOLUME ["/data"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
