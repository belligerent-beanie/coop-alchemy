# Co-op Alchemy in a container. The server uses only the Python standard library, so the image is just Python plus this folder.
#
# Built and run on Docker Desktop for Windows (2026-11-07): the image builds, runs as the non-root user below, reports healthy,
# keeps accounts and solo progress in /data across a restart, and serves element pictures. Not yet tried on the real server.
# test_deploy_files.py also checks that every option used below exists.
#
# Data (accounts, saved games, logs, and the private recipe book and pictures: /data/recipes.json and /data/elements) lives in
# the /data volume, never in the image. Extra server options can be added after the
# image name (docker run ... coop-alchemy --trusted-proxy 172.16.0.0/12) or as `command:` in docker-compose.yml.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 ALCHEMY_DATA_DIR=/data
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
            "--invites-file", "/data/invites.json", "--reports-file", "/data/reports.json", "--solo-file", "/data/solo.json", "--profile-file", "/data/profiles.json", "--saves-dir", "/data/saves", \
            "--recipes", "/data/recipes.json", "--elements-dir", "/data/elements", "--snapshot-file", "/data/reload_snapshot.json", "--pid-file", "/data/alchemy.pid", \
            "--log-file", "/data/live.log", "--log-days", "14"]
CMD []
