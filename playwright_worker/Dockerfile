# playwright-worker — Chromium in its own container (§6.1).
#
# The official Playwright image, not python:slim + `playwright install`. Chromium
# needs a long list of OS libraries whose names differ between Debian releases;
# pinning the image that ships them is the difference between a reproducible build
# and one that breaks when a base image moves. It is also why this is a separate
# service: §6.1's "different base image and OS package set".
#
# The tag must match the `playwright` version in requirements.txt. A mismatch fails
# at launch with "Executable doesn't exist", which is a confusing runtime error for
# what is really a build-time version skew.
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

WORKDIR /app

# Dependencies first, so a source edit does not re-resolve them.
COPY playwright_worker/requirements.txt /app/playwright_worker/requirements.txt
RUN pip install --no-cache-dir -r /app/playwright_worker/requirements.txt

COPY playwright_worker/ /app/playwright_worker/

# Chromium renders untrusted content and is the component most likely to be
# compromised (§6.1). The base image ships `pwuser`; run as it rather than root so a
# renderer escape lands somewhere with nothing worth having.
USER pwuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BROWSER_LAB_URL=http://browser-lab:8080

EXPOSE 8090

# One worker process. Chromium contexts are held in-process, so a second uvicorn
# worker would mean two disjoint session registries behind one port — a caller's
# browser_session_id would resolve only half the time.
CMD ["python", "-m", "uvicorn", "playwright_worker.service:app", \
     "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]
