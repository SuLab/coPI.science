# Stage 1 (`source`): the tracked tree plus .build_info.json, and nothing else.
# Bake the git identity of THIS build into .build_info.json. The runtime has
# no git binary, and the announced "commit/branch/dirty" must describe the
# image's code, which is what actually runs (src/ is baked, not mounted).
# `git clean -ffdx` runs first, so an untracked file .dockerignore does not
# name (a stray dump, a scratch token file) never reaches the image; it does
# not change `dirty_files`, which counts tracked changes only. write_build_info
# exits non-zero on any git failure, which fails the build. `.git` is then
# deleted, and this stage's git binary never reaches the final image. The
# safe.directory entry covers builders whose COPY'd files change apparent
# ownership.
FROM python:3.11-slim AS source

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY . .

RUN git config --global --add safe.directory /app \
    && git clean -ffdx \
    && python scripts/write_build_info.py \
    && rm -rf .git

# Stage 2: the runtime image. It never copies the build context directly:
# only pyproject.toml and src/ for the pip layer, then the cleaned tree from
# `source` (pinned by tests/unit/test_docker_build_context.py).
FROM python:3.11-slim

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir .

# Copy the cleaned source tree, .build_info.json included
COPY --from=source /app/ /app/

# Create directories for profiles and prompts
RUN mkdir -p profiles/public profiles/private prompts logs static

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
