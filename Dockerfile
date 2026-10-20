FROM python:3.11.17-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /uvx /usr/local/bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    TERM=xterm-256color \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

RUN groupadd --system --gid 10001 shipwright \
    && useradd --system \
        --uid 10001 \
        --gid 10001 \
        --create-home \
        --home-dir /home/shipwright \
        shipwright

COPY pyproject.toml uv.lock README.md LICENSE ./

RUN uv sync --locked --no-dev --no-install-project

COPY shipwright ./shipwright

RUN uv sync --locked --no-dev \
    && chown -R shipwright:shipwright /app /home/shipwright

USER shipwright

CMD ["shipwright"]
