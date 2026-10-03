# The exec-mcp image: one toolchain for provisioning (with a network) and for
# exec-mcp (without). It carries no credentials.
#
#   podman build -t exec-mcp .
FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.22@sha256:f513a91fc62fe7c17567eee97230dd198e43edb8a9fbecca843714a4358fe1bc /uv /uvx /bin/

# git: provisioning clones with it and exec-mcp's git tools commit with it
# cmake, ninja and a compiler: the build toolchain
# systemd: for `systemd-analyze` only
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates git \
        cmake ninja-build build-essential \
        systemd; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app/mcp

COPY pyproject.toml uv.lock ./
RUN UV_PROJECT_ENVIRONMENT=/app/.venv uv sync --frozen --no-install-project

# The server is copied in, never bind-mounted, so every change to it needs a
# rebuild.
COPY server.py proc.py git_tools.py manifest.py log.py prefixes.py wire_contract.toml ./
RUN /app/.venv/bin/python -c "import server; print('deps ok')"

ENV PYTHONUNBUFFERED=1
CMD ["/app/.venv/bin/python", "server.py"]
