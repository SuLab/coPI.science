FROM python:3.11-slim AS builder

WORKDIR /app

# Build-time only: gcc/libpq-dev compile any dependency that ships as an sdist
# for this platform/Python combination. Not present in the runtime image
# below — asyncpg itself needs none of this, it bundles its own wire protocol
# implementation rather than linking libpq.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY src/ src/
# --no-build-isolation: build isolation would otherwise fetch a fresh,
# unhashed setuptools/wheel from PyPI at build time just to satisfy
# pyproject.toml's [build-system] requires; the base image's preinstalled
# setuptools/wheel already satisfy it. This local `pip install .` therefore
# is not hash-verified the way the `-r requirements.lock` install above is
# — accepted, since it installs only this repo's own source, not a
# third-party artifact off the network.
RUN pip install --no-cache-dir --no-deps --no-build-isolation .

FROM python:3.11-slim AS runtime

WORKDIR /app

# libpq5 only: the runtime client library a compiled wheel may dlopen. Nothing
# currently links it — asyncpg is pure-protocol — this is insurance for a
# future psycopg dependency. No compiler, no -dev headers, no build toolchain
# of any kind in this stage. Deliberately avoids naming the builder-stage
# packages here — the structural test in tests/unit/test_dockerfile_build.py
# asserts their names are absent from this section.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY . .

# Bake the bytecode cache while root still owns src/ — UID 10001 (set below)
# cannot write __pycache__ into root-owned src/, so without this every
# process start pays a first-import compile cost (~0.9s, measured). Must run
# AFTER src/ lands (COPY . . above) and BEFORE USER drops root.
RUN python -m compileall -q src

# Fixed UID so it matches whatever the prod host chowns the bind-mounted
# profiles/data trees to — a plain chown target on the host, not a real host
# account. Ownership is scoped to the directories the runtime user actually
# writes to (profiles/data/logs); src/, templates/, alembic/, scripts/ and
# static/ stay root-owned and read-only to this user, so a compromised
# process cannot rewrite its own code. static/ is deliberately excluded:
# StaticFiles only ever reads it, nothing under src/ writes to it, so a write
# grant there would be a needless stored-XSS surface on assets served
# straight to the browser.
RUN groupadd --gid 10001 copi \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin copi \
    && mkdir -p profiles/public profiles/private profiles/memory data logs \
    && chown -R 10001:10001 profiles data logs
ENV HOME=/app

USER 10001

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
