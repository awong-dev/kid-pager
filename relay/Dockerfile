# `detools` has no manylinux wheel, so the builder stage carries the C/C++ toolchain.
#
# `--build-arg WEB=home --build-context web=web` bakes the web static export into /srv/web;
# the default WEB=none builds the cloud image without it.
ARG WEB=none

FROM python:3.12-slim AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ libc6-dev \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY pyproject.toml .
COPY app/ app/
RUN pip install --no-cache-dir .

# WEB=none: an empty /out, so the final COPY below adds nothing.
FROM python:3.12-slim AS web-none
RUN mkdir /out

# WEB=home: the export lands in /out/web, copied to /srv/web.
FROM node:20-slim AS web-home
WORKDIR /web
COPY --from=web package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY --from=web . .
RUN NEXT_PUBLIC_BACKEND=home npm run build && mkdir /out && cp -r out /out/web

FROM web-${WEB} AS web-out

FROM python:3.12-slim

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml .
# The SQLite store builds its indexes from this file.
COPY firestore.indexes.json .
COPY app/ app/
COPY --from=web-out /out/ /srv/

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
