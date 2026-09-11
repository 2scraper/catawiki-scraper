#!/usr/bin/env python3
"""
CI checks that are too long to live inside the workflow YAML.

They started out as heredocs in `.github/workflows/tests.yml` and moved here
for one practical reason: a Python block nested inside YAML inside a shell
`run:` needs three levels of quoting to stay intact, and shell-quoted regexes
like '(ws|wss)://[^ "'"'"']+' do not survive being copied through a browser.
A separate .py file is copy-paste safe, runs locally, and can be read on its
own.

Run any of these from the repo root:

    python .github/ci_checks.py --help-check
    python .github/ci_checks.py --sample-check
    python .github/ci_checks.py --secret-check
    python .github/ci_checks.py --all

Each prints what it looked at and exits non-zero on failure.
"""

import argparse
import csv
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Engine libraries are deliberately absent in CI — the offline suite does not
# need them. An ImportError naming one of these is expected, not a failure.
ENGINE_LIBS = ("playwright", "pyppeteer", "selenium", "webdriver_manager")

CLIS = ["playwright_scraper.py", "puppeteer_scraper.py", "selenium_scraper.py",
        "scraper_api_client.py", "fingerprint_client.py", "env_config.py"]

SAMPLE_FILES = ("sample_output.json", "sample_output.csv")

# Phrases that show up in hand-written or template sample data. The point of
# committing a sample is that it came from a real run; a placeholder teaches
# readers field names and value shapes that do not exist.
FABRICATION_MARKERS = ("sample-product-", "example brand", "sample product",
                       "product description text", "lorem ipsum",
                       "your_api_key", "123456789")

# A URL carrying real credentials — the shape is scheme://something:something@host
# (deliberately not spelled out as an example here: this file scans itself, and
# an illustrative credential in a comment is a false positive that turns the
# build red for no reason. It happened on the first run.)
CREDENTIALLED_URL = re.compile(r"(?:ws|wss|https?)://[^\s\"'/]+:[^\s\"'/]+@")

# Documented placeholders and test values, which are SUPPOSED to look like the
# real thing — that is the point of them. Each entry earns its place by being
# in a line whose job is to show the shape of a credential or to prove the
# masker removes one; a real secret matches none of these.
#
# Kept as an explicit list rather than a loose pattern so that adding one is a
# decision. The alternative — a regex broad enough to cover them all — would
# also cover a real login.
CREDENTIAL_ALLOWED = (
    # documentation placeholders
    "USER:PASS", "user:pass", "ACCOUNT:PASSWORD", "LOGIN:PASSWORD",
    # This repo's April 2026 prototype README documented a proxy URL as
    # `http://username:password@…`. That commit is in the history and cannot
    # be removed from it, so --history-check would fail forever on a literal
    # placeholder — which would teach everyone to ignore the one check that
    # exists to be read exactly once, before publishing. Allowed by NAME, so
    # a real login still fails.
    "username:password",
    "{login}", "{user}", "password}@", "***", "u:p@h",
    "login:password@host:port",     # the shape a refusal message prints
    "user:secret@",                 # the proxy-pool masking fixtures
    "u:supersecret@", "login:supersecret@",   # the redaction fixtures
    "u:pass@h1", "u:pass@h2",       # the global-masking fixture
    "only:1",                       # a one-exit pool fixture
)

# A 2captcha API key is a 32-character hex string.
HEX32 = re.compile(r"\b[0-9a-f]{32}\b")
# Contexts in which a 32-hex string is plainly not a key.
#
# Deliberately WITHOUT this site's asset hosts, unlike a sibling repo that
# needed them: Catawiki serves its images under dashed UUIDs
# (`95be01d1-5c40-4126-94fa-...`), which never match a bare 32-hex run, so
# the committed fixtures carry none and an allowlist entry for them would be
# dead configuration that reads like enforcement. Verified by running this
# check against the fixtures: 0 matches. Kept as a CONTEXT allowlist rather
# than by loosening the pattern -- a bare 32-hex string anywhere else still
# fails, which is the point.
HEX32_ALLOWED = ("sha", "hash", "nonce", "example", "md5", "digest",
                 "checksum")

SCANNED_SUFFIXES = (".py", ".md", ".txt", ".yml", ".yaml", ".example")


def scanned_files():
    for path in sorted(REPO.rglob("*")):
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        if any(part in {".git", "__pycache__", ".venv", "venv"}
               for part in path.parts):
            continue
        yield path


def help_check():
    failed = []
    for name in CLIS:
        script = REPO / name
        if not script.is_file():
            print(f"missing  {name}")
            failed.append(name)
            continue
        result = subprocess.run([sys.executable, str(script), "--help"],
                                capture_output=True, text=True, cwd=REPO)
        if result.returncode == 0:
            print(f"ok       {name}")
            continue
        blob = result.stdout + result.stderr
        if "ModuleNotFoundError" in blob and any(lib in blob for lib in ENGINE_LIBS):
            print(f"skipped  {name} (engine library not installed here)")
            continue
        print(f"FAILED   {name}\n{blob}")
        failed.append(name)
    return failed


def sample_check():
    failed = []
    for name in SAMPLE_FILES:
        if not (REPO / name).is_file():
            failed.append(f"{name} is missing — regenerate it from a real run")
    if failed:
        return failed

    rows = json.loads((REPO / "sample_output.json").read_text(encoding="utf-8"))
    if not rows:
        return ["sample_output.json is empty — a run that found nothing is not a sample"]

    blob = json.dumps(rows).lower()
    hits = [m for m in FABRICATION_MARKERS if m in blob]
    if hits:
        failed.append(f"sample_output.json looks fabricated: {hits}")

    # The committed sample doubles as a schema test: rename a field in the code
    # and forget the sample, and this fails rather than the docs going stale.
    sys.path.insert(0, str(REPO))
    from dataclasses import asdict

    from output_writer import Product
    expected = list(asdict(Product()).keys())

    for i, row in enumerate(rows):
        if list(row.keys()) != expected:
            failed.append(f"sample_output.json row {i}: columns differ from "
                          f"output_writer.Product")
            break

    with (REPO / "sample_output.csv").open(newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle))
    if header != expected:
        failed.append("sample_output.csv header differs from output_writer.Product")

    if not failed:
        discounted = sum(1 for r in rows if r.get("original_price"))
        print(f"ok       {len(rows)} rows, {len(expected)} columns, "
              f"{discounted} discounted, schema matches")
    return failed


def secret_check():
    failed = []
    scanned = 0
    for path in scanned_files():
        scanned += 1
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            rel = path.relative_to(REPO)

            if CREDENTIALLED_URL.search(line) and not any(
                    token in line for token in CREDENTIAL_ALLOWED):
                failed.append(f"{rel}:{lineno} looks like a URL with real "
                              f"credentials in it")

            for match in HEX32.findall(line):
                if any(token in line.lower() for token in HEX32_ALLOWED):
                    continue
                failed.append(f"{rel}:{lineno} contains {match[:6]}… — a "
                              f"32-char hex string, the shape of a 2captcha key")

    if not failed:
        print(f"ok       {scanned} files scanned, nothing credential-shaped")
    return failed


def history_check():
    """The same rules, applied to every blob that has EVER existed.

    `secret_check` reads the working tree, which is the right scope for CI:
    it fails a pull request before the mistake lands. This one is for the
    step CI cannot do anything about — publishing.

    A commit on top cannot reach what a published tag and a merged PR's refs
    already hold; those stay attached to the PR and cannot be deleted from
    it. So the decision has to be made BEFORE the repository goes public,
    and afterwards only a fresh repository removes anything. Run this then:

        python .github/ci_checks.py --history-check

    Deliberately NOT part of `--all` and not run by CI. It shells out to git
    once per object, which is fine for a hundred and wasteful on every push,
    and a repo whose history is dirty needs a decision rather than a red
    check.
    """
    try:
        listing = subprocess.run(["git", "rev-list", "--objects", "--all"],
                                 cwd=REPO, capture_output=True, text=True,
                                 check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        return [f"could not read the git history ({e}) — run this inside a "
                f"clone, not an export"]

    objects = []
    for line in listing.splitlines():
        parts = line.split(None, 1)
        if parts:
            objects.append((parts[0], parts[1] if len(parts) > 1 else ""))

    failed, scanned = [], 0
    for sha, path in objects:
        if not (path.endswith(SCANNED_SUFFIXES) or path in ("Dockerfile",)):
            continue
        kind = subprocess.run(["git", "cat-file", "-t", sha], cwd=REPO,
                              capture_output=True, text=True).stdout.strip()
        if kind != "blob":
            continue
        scanned += 1
        body = subprocess.run(["git", "cat-file", "blob", sha], cwd=REPO,
                              capture_output=True, text=True,
                              errors="replace").stdout
        for lineno, line in enumerate(body.splitlines(), 1):
            if CREDENTIALLED_URL.search(line) and not any(
                    token in line for token in CREDENTIAL_ALLOWED):
                failed.append(f"{path}:{lineno} (in a past commit) looks like "
                              f"a URL with real credentials in it")
            for match in HEX32.findall(line):
                if any(token in line.lower() for token in HEX32_ALLOWED):
                    continue
                failed.append(f"{path}:{lineno} (in a past commit) contains "
                              f"{match[:6]}… — the shape of a 2captcha key")

    if not failed:
        print(f"ok       {scanned} blob(s) across {len(objects)} object(s) "
              f"that have ever existed — nothing credential-shaped")
    else:
        print("         NOTE: a later commit cannot remove any of these. A "
              "published tag and a merged PR's refs keep them, so this needs "
              "a decision BEFORE the repo goes public.")
    return failed


CHECKS = {"help": help_check, "sample": sample_check,
          "secret": secret_check, "history": history_check}


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--help-check", action="store_true",
                        help="Every shipped CLI answers --help")
    parser.add_argument("--sample-check", action="store_true",
                        help="sample_output.* exist, are real, match the schema")
    parser.add_argument("--secret-check", action="store_true",
                        help="No credentials committed anywhere")
    parser.add_argument("--history-check", action="store_true",
                        help="The same rules over every blob that has EVER "
                             "existed. For before publishing, not for CI — "
                             "see history_check(). Not included in --all.")
    parser.add_argument("--all", action="store_true",
                        help="help, sample and secret. NOT history: that one "
                             "is a pre-publication step, and it shells out to "
                             "git once per object.")
    args = parser.parse_args()

    selected = [name for name in CHECKS
                if getattr(args, f"{name}_check")
                or (args.all and name != "history")]
    if not selected:
        parser.error("pick at least one check, or --all")

    failures = []
    for name in selected:
        print(f"--- {name} check")
        failures += [f"[{name}] {line}" for line in CHECKS[name]()]

    if failures:
        print()
        for line in failures:
            print("FAILED:", line)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
