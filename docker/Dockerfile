# gptapi: headed Debian Chromium on Xvfb, visible over noVNC, CDP on 127.0.0.1:9222.
# The API server (python -m gptapi) is started from /app if present.
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    TZ=UTC \
    DISPLAY=:99 \
    SCREEN_GEOMETRY=1280x900x24 \
    CDP_PORT=9222 \
    CHROME_PROFILE=/data/chrome-profile \
    VNC_PORT=5900 \
    NOVNC_PORT=6080 \
    API_PORT=8000 \
    GPTAPI_DATA=/data/gptapi \
    CDP_URL=http://127.0.0.1:9222 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        xvfb xauth openbox \
        x11vnc novnc python3-websockify \
        fonts-liberation fonts-noto-color-emoji fonts-noto-core \
        tini supervisor procps curl ca-certificates \
        python3 python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir websocket-client \
    && useradd -m -u 1000 -s /bin/bash app \
    && mkdir -p /data /app /var/log/gptapi \
    && chown -R app:app /data /app /var/log/gptapi /opt/venv \
    && ln -sf vnc.html /usr/share/novnc/index.html

COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt
COPY --chmod=755 docker/bin/ /usr/local/bin/
COPY docker/supervisord.conf /etc/gptapi/supervisord.conf
COPY --chown=app:app gptapi/ /app/gptapi/

USER app
WORKDIR /app
VOLUME ["/data"]
EXPOSE 8000 6080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD curl -fsS http://127.0.0.1:${CDP_PORT}/json/version >/dev/null && curl -fsS http://127.0.0.1:${API_PORT}/health >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/gptapi/supervisord.conf"]
