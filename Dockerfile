FROM python:3.11-slim

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies from the hash-pinned lockfile first — this layer
# only invalidates when requirements.lock changes, not on every src/ edit (#27 I4).
COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY src/ src/
RUN pip install --no-cache-dir --no-deps .

# Copy source
COPY . .

# Create directories for profiles and prompts
RUN mkdir -p profiles/public profiles/private prompts logs static

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
