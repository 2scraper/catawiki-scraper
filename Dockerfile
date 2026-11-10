# Builds the Playwright engine (the one the README recommends) into a
# container with its own Chromium — for a CI canary run or a scheduled job,
# not required for local development (`pip install` directly is simpler there).
#
#   docker build -t catawiki-scraper .
#   docker run --rm -v "$PWD/out:/out" catawiki-scraper \
#     --url "https://www.catawiki.com/en/c/333-watches" \
#     --pages 3 --out /out/mugs
#
# The site refuses a HEADLESS browser and serves a headful one, so the scraper
# runs headful by default and a headful Chromium needs a display. A container
# has none, so the entrypoint starts a virtual one (xvfb) around the scraper.
# `tini` is PID 1: without an init process the Xvfb child is never reaped and
# a run that finishes can leave the container hanging forever. The image
# therefore needs no `docker run --init`.
#
# Pass --proxy/--twocaptcha-key the same way as running locally, or mount a
# .env at /app/.env — nothing here bakes in a credential.
FROM python:3.12-slim

WORKDIR /app

# System packages first, and before anything that runs Playwright:
#   libatomic1  the Playwright driver is a node binary that links it; the slim
#               image does not carry it, so `playwright install` died before
#               installing anything (seen with playwright 1.65 on 2026-11-10).
#   xvfb, xauth the virtual display a headful Chromium needs (see above).
#   tini        PID 1, so Xvfb is reaped and the container exits.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 xvfb xauth tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-playwright.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-playwright.txt \
    # Playwright's own apt-get for Chromium's shared-library dependencies —
    # not pip packages, so this has to run as a separate, explicit step.
    && playwright install --with-deps chromium

# Every module playwright_scraper.py imports, transitively, plus diff_runs.py
# as a useful companion in the same image. smoke_test.py checks this list
# against the entrypoint's real import graph: an earlier version omitted
# proxy_pool.py, which the engine imports at module level, so the image died
# with ModuleNotFoundError on every invocation INCLUDING `--help` — a broken
# container that nothing in the repo would have noticed.
COPY captcha_solver.py env_config.py fingerprint_client.py output_writer.py \
     page_flow.py playwright_scraper.py product_parser.py proxy_pool.py \
     diff_runs.py ./

ENTRYPOINT ["tini", "--", "xvfb-run", "-a", "--server-args=-screen 0 1920x1080x24", \
            "python3", "playwright_scraper.py"]
CMD ["--help"]
