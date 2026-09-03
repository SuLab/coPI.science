FROM python:3.11-slim AS builder

WORKDIR /app

# Build-time only: gcc/libpq-dev compile any dependency that ships as an sdist
# for this platform/Python combination. Not present in the runtime image
# below (#27 I3 — asyncpg itself needs none of this, it bundles its own wire
# protocol implementation rather than linking libpq).
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY src/ src/
RUN pip install --no-cache-dir --no-deps .

FROM python:3.11-slim AS runtime

WORKDIR /app

# libpq5 only: the runtime client library a compiled wheel may dlopen. No
# compiler, no -dev headers, no build toolchain of any kind in this stage
# (#27 I3). Deliberately avoids naming the builder-stage packages here — the
# structural test in tests/unit/test_dockerfile_build.py asserts their names
# are absent from this section.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY . .

# Fixed UID so it matches whatever the prod host chowns the bind-mounted
# profiles/data trees to (see this task's Deploy note) — a plain chown
# target on the host, not a real host account.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin copi \
    && mkdir -p profiles/public profiles/private prompts logs static \
    && chown -R 10001:10001 /app

USER 10001

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
