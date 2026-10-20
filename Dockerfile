FROM ghcr.io/astral-sh/uv:0.12.18 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /uvx /bin/
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
COPY campuspay ./campuspay
RUN useradd --create-home --uid 10001 campuspay && mkdir /data && chown campuspay:campuspay /data
USER campuspay
EXPOSE 8080
CMD ["uv", "run", "--locked", "--no-dev", "--no-sync", "python", "-m", "campuspay.main"]
