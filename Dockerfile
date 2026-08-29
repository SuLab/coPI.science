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

# Copy source
COPY . .

# Bake the git identity of THIS build into .build_info.json — the runtime has
# no git binary, and the announced "commit/branch/dirty" must describe the
# image's code, which is what actually runs (src/ is baked, not mounted).
# git is installed and purged inside one layer; the safe.directory entry
# covers builders whose COPY'd files change apparent ownership.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && git config --global --add safe.directory /app \
    && python scripts/write_build_info.py \
    && apt-get purge -y git \
    && rm -rf /var/lib/apt/lists/*

# Create directories for profiles and prompts
RUN mkdir -p profiles/public profiles/private prompts logs static

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
