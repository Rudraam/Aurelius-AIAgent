# Multi-arch (linux/amd64, linux/arm64 for Oracle Ampere): every base image here is published for both.
# Supply-chain rule (CLAUDE.md): images are pinned by multi-arch INDEX digest, each at least 14 days old.
# The tag stays in the reference so Dependabot (docker ecosystem) can propose updates.
# No "# syntax=" directive: it would pull an extra, unpinned BuildKit frontend image.

# python:3.12-slim-bookworm, Python 3.12.15, index built 2026-10-21 (amd64 + arm64 both 3.12.15)
FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

# httpx2/httpcore2 on Linux trust ONLY the OS CA store (no certifi bundle). Without ca-certificates
# every TLS handshake to Questrade fails.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && test -s /etc/ssl/certs/ca-certificates.crt

# ghcr.io/astral-sh/uv:0.12.5, index built 2026-08-14
COPY --from=ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY app ./app
RUN uv sync --locked --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Fail the build if Python cannot see a CA file through the paths httpcore2 checks.
RUN python -c "import ssl, os; p = ssl.get_default_verify_paths(); \
assert any(os.path.isfile(f) for f in (p.openssl_cafile, '/etc/ssl/certs/ca-certificates.crt')), p"

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin aurelius
USER aurelius

# The API entrypoint (uvicorn app.main:app) arrives in M1. Until then the image runs the CLI.
ENTRYPOINT ["aurelius"]
CMD ["--help"]
