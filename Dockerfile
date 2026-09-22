# uv + Python 3.14 base (bundles uv and the interpreter).
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /app

# Faster, hermetic installs.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Install deps first (cached until the lockfile changes). PLAN.md is the project
# readme referenced by pyproject, so it must be present for the build.
COPY pyproject.toml uv.lock PLAN.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

COPY scripts ./scripts

# The app reads PORT from the environment (Render/Railway/etc. inject it).
ENV PORT=3000
EXPOSE 3000

CMD ["uv", "run", "--no-dev", "clinic-agent"]
