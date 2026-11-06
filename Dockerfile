# Co-op Alchemy in a container. The server uses only the Python standard library, so the image is just Python plus this folder.
#
# NOT YET BUILT OR RUN: this was written on a machine without Docker. Build it once (docker build -t coop-alchemy .), start it,
# open /healthz, and fix whatever it misses before relying on it. test_deploy_files.py checks that every option used below exists.
#
# Data (accounts, saved games, logs) lives in the /data volume, never in the image. Extra server options can be added after the
# image name (docker run ... coop-alchemy --trusted-proxy 172.16.0.0/12) or as `command:` in docker-compose.yml.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN useradd --create-home --uid 10001 alchemy && mkdir /data && chown alchemy /data
COPY --chown=alchemy . /app
USER alchemy

VOLUME /data
EXPOSE 8000

# Healthy when the server answers its own health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

ENTRYPOINT ["python", "server.py", "--host", "0.0.0.0", "--port", "8000", \
            "--accounts-file", "/data/accounts.json", "--daily-file", "/data/daily.json", "--drafts-file", "/data/drafts.json", \
            "--invites-file", "/data/invites.json", "--saves-dir", "/data/saves", \
            "--snapshot-file", "/data/reload_snapshot.json", "--pid-file", "/data/alchemy.pid", \
            "--log-file", "/data/live.log", "--log-days", "14"]
CMD []
