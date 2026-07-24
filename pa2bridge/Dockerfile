FROM ghcr.io/home-assistant/base-python:3.14-alpine3.23-2026.06.1@sha256:66f223ca04b4053a59d24ff210518112465c3c3166b1967473cb2642a4462cab

ARG BUILD_VERSION=dev
ARG BUILD_ARCH=amd64

RUN apk upgrade --no-cache \
    && rm /usr/bin/tempio

RUN python3 -m venv /opt/pa2bridge

COPY pa2bridge/requirements.txt /tmp/requirements.txt
RUN /opt/pa2bridge/bin/pip install --no-cache-dir \
    --only-binary=:all: \
    --require-hashes \
    --requirement /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY src /opt/pa2bridge/src
COPY LICENSE /usr/share/licenses/pa2bridge/LICENSE
ENV PYTHONPATH=/opt/pa2bridge/src

COPY pa2bridge/rootfs /
RUN chmod 0755 /etc/services.d/pa2bridge/run \
    /etc/services.d/pa2bridge/finish

LABEL \
    io.hass.type="app" \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.arch="${BUILD_ARCH}" \
    org.opencontainers.image.title="Home Assistant App: PA2Bridge" \
    org.opencontainers.image.description="Safe dbx DriveRack PA2 control through Home Assistant" \
    org.opencontainers.image.source="https://github.com/gcs8/pa2bridge" \
    org.opencontainers.image.licenses="MIT"
