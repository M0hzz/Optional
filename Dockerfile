# syntax=docker/dockerfile:1
# optlab: one image for the API/dashboard and the Streamlit Research Lab.
#   docker compose up -d --build          (see docker-compose.yml and docs/DEPLOY.md)
#
# Base images can come from a mirror if Docker Hub rate-limits you:
#   OPTLAB_PYTHON_IMAGE=mirror.gcr.io/library/python:3.13-slim-bookworm
#   OPTLAB_NODE_IMAGE=mirror.gcr.io/library/node:22-bookworm-slim
# Behind a TLS-intercepting proxy, pass its CA certificate as a build secret (it is never stored in the image):
#   docker build --secret id=ca_bundle,src=/path/to/ca.pem --build-arg HTTPS_PROXY=... .
ARG OPTLAB_NODE_IMAGE=node:22-bookworm-slim
ARG OPTLAB_PYTHON_IMAGE=python:3.13-slim-bookworm

# ---- frontend build -----------------------------------------------------------------
FROM ${OPTLAB_NODE_IMAGE} AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/ca_bundle; fi; \
    npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---- python runtime -----------------------------------------------------------------
FROM ${OPTLAB_PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src HOME=/tmp/home MPLCONFIGDIR=/tmp/home/.mpl
WORKDIR /app
# nautilus_trader (live execution integration) is not used by either service and is left out.
COPY requirements.txt ./
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export PIP_CERT=/run/secrets/ca_bundle; fi; \
    grep -v -E '^(nautilus_trader)' requirements.txt > /tmp/req.txt \
 && pip install -r /tmp/req.txt \
 && mkdir -p /tmp/home && chmod 1777 /tmp/home
COPY . .
COPY --from=web /web/dist ./web/dist
# The container runs as the host user (docker-compose `user:`) so files in the bind-mounted data/
# folder keep the host's ownership.
EXPOSE 8600 8501
CMD ["python", "-m", "optlab.api"]
