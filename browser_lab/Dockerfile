# Browser Lab — hermetic dummy site (docs/automation-architecture.md §10).
#
# Build context is the repo root so the image can copy browser_lab/ as a
# package; see the `build:` block in docker-compose.yml.
#
# Deliberately NOT based on the app image: the lab shares no code with the
# system under test, and a fixture that has to be rebuilt whenever the backend
# changes is a fixture people stop rebuilding.
FROM python:3.12-slim

# Network is needed for this layer and this layer only. Once built, the
# container makes no outbound calls — that is what §10.1 means by hermetic, and
# it is why the image carries no CDN assets, no fonts and no telemetry.
COPY browser_lab/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

WORKDIR /app
COPY browser_lab/ /app/browser_lab/

# Unprivileged: the lab parses uploads from whatever is driving it, and there is
# no reason for that code path to be running as root.
RUN useradd --create-home --uid 10001 lab && chown -R lab:lab /app
USER lab

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BROWSER_LAB_PORT=8080

EXPOSE 8080

# Uses the lab's own /_test/health rather than a shell curl loop, and depends on
# nothing that is not already in the image.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/_test/health', timeout=2).status == 200 else 1)"

CMD ["python", "-m", "browser_lab"]
