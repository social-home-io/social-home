# Social Home core image — the household instance (§4).
#
# Entry point: ``python -m socialhome``.
# Ports: 8099 (HTTP/WebSocket) + 8124 (aiolibdatachannel signalling).
# Runtime: Python 3.14-slim + ffmpeg (video transcoding in SpacePosts
# + BazaarListing thumbnails) + libjpeg / libwebp (Pillow).
#
# Installs from a pre-built wheel — the SPA bundle in
# ``socialhome/static/`` is already baked in by the publish workflow's
# ``build`` job (or by a local ``pnpm --dir client run build`` +
# ``python -m build``). The runtime image therefore needs no node /
# pnpm at all, which keeps the multi-arch build cheap on QEMU.
#
# Published as ``ghcr.io/social-home-io/socialhome:{tag}`` by the
# ``docker-core`` job in .github/workflows/publish.yml.

FROM python:3.14-slim AS base

# uv replaces pip for the wheel install — ~10× faster, deterministic
# resolution, and ships static via the official ``ghcr.io/astral-sh/uv``
# image so we don't depend on an extra apt / pip-bootstrap layer.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# System deps for Pillow + ffmpeg.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ffmpeg \
      libjpeg-turbo-progs \
      libwebp-dev \
    && rm -rf /var/lib/apt/lists/*

# Non-root user up front so a host-mounted ``/data`` volume ends up
# with the right owner.
RUN groupadd --system --gid 10001 appuser && \
    useradd  --system --uid 10001 --gid appuser --create-home appuser

WORKDIR /app

# Pre-built wheel from the publish workflow (or local
# ``python -m build``). The SPA assets are inside under
# ``socialhome/static/``; ``--system`` installs into the base image's
# site-packages directly (no venv); ``--no-cache`` keeps the layer
# size down. The wheel is removed after install so it doesn't bloat
# the final layer.
COPY dist/socialhome-*.whl /tmp/
RUN uv pip install --system --no-cache /tmp/socialhome-*.whl && \
    rm /tmp/socialhome-*.whl

RUN mkdir -p /data && chown -R appuser:appuser /data

VOLUME /data
ENV SH_DATA_DIR=/data
ENV SH_MODE=standalone

EXPOSE 8099 8124

USER appuser

CMD ["python", "-m", "socialhome"]
