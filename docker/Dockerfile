# Build context is the repository root so the container runs the same modules
# as a local checkout:
#   docker compose -f docker/docker-compose.yml up --build
#
# The previous image installed google-chrome-stable via the deprecated
# `apt-key add` AND downloaded Playwright's Chromium, shipping two browser
# engines when the scraper uses one. It also hand-maintained a list of apt
# packages that breaks on newer Debian (libasound2 was renamed).
# `playwright install --with-deps chromium` resolves those system libraries
# itself, for the exact browser build the pinned Playwright expects.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && pip install -r requirements.txt \
    && playwright install --with-deps chromium

# Application modules, shared with the local (non-Docker) workflow.
COPY config.py models.py playwright_helpers.py scraper.py utils.py uploader.py run.py ./
COPY Query.txt ./

# Run as an unprivileged user rather than root.
RUN useradd --create-home --uid 10001 scraper \
    && mkdir -p /app/output \
    && chown -R scraper:scraper /app \
    && chmod -R a+rX /ms-playwright
USER scraper

ENV GMAPS_HEADLESS=1 \
    GMAPS_OUTPUT_DIR=/app/output \
    GMAPS_QUERY_FILE=/app/Query.txt

CMD ["python", "run.py", "--non-interactive"]
