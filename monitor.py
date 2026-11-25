#!/usr/bin/env python3
"""
Pipeline critical-notice monitor.

Checks the critical-notice listings for Transco, Texas Eastern and Mountain
Valley Pipeline (via the PipeRiv aggregator) and pushes any NEW notice to your
phone with ntfy (https://ntfy.sh).

Setup: set the NTFY_TOPIC environment variable to a long, random topic name,
then subscribe to that same topic in the ntfy phone app.

    python monitor.py            # normal run
    python monitor.py --test     # send a test push and exit
    python monitor.py --dry-run  # print what would be sent, send nothing
"""
import json
import os
import re
import sys
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

# name -> listing URL.  NOTE: the Transco slug is my best guess and unverified;
# open the URL in a browser once and fix it if the page doesn't exist.
SOURCES = {
    "Texas Eastern": "https://www.piperiv.com/ip/texas-eastern/critical-notices",
    "Mountain Valley": "https://www.piperiv.com/ip/mountain-valley/critical-notices",
    "Transco": "https://www.piperiv.com/ip/transco/critical-notices",
}

# Fallback link used when a notice has no page of its own.
OFFICIAL = {
    "Transco": "https://www.1line.williams.com/Transco/index.html",
}

STATE_FILE = Path(__file__).with_name("seen.json")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
UA = {"User-Agent": "pipeline-notice-monitor/1.0 (personal use)"}

# Notices containing these words are sent as high priority.
URGENT = re.compile(
    r"\b(OFO|operational flow order|force majeure|emergency|unplanned|"
    r"outage|curtail|restrict|shut[- ]?in|critical day)\b",
    re.I,
)


class NoticeLinks(HTMLParser):
    """Collects <a href=".../notice/<id>">subject</a> links."""

    def __init__(self):
        super().__init__()
        self.links, self._href, self._text = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "")
            if re.search(r"/notice/\d+", href):
                self._href, self._text = href, []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            text = " ".join("".join(self._text).split())
            m = re.search(r"/notice/(\d+)", self._href)
            self.links.append((m.group(1), text, self._href))
            self._href = None


def parse_notices(html):
    """Return {notice_id: (subject, href)}."""
    p = NoticeLinks()
    p.feed(html)
    notices = {}
    for nid, text, href in p.links:
        # keep the longest label seen for an id (some pages link it twice)
        if nid not in notices or len(text) > len(notices[nid][0]):
            notices[nid] = (text or f"Notice {nid}", href)
    if notices:
        return notices
    # Fallback: pipe-delimited text rows such as
    # "PLANNED SERVICE OUTAGE | 04/01/25 | 05/06/25 | 05/09/25 | 121459 | Subject"
    for m in re.finditer(r"\|\s*(\d{4,8})\s*\|\s*([^|\n]+?)\s*\|", html):
        notices[m.group(1)] = (m.group(2), "")
    return notices


def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def ascii_header(s):
    return s.encode("ascii", "replace").decode("ascii")


def push(title, body, url="", urgent=False, dry=False):
    if dry or not NTFY_TOPIC:
        print(f"[{'DRY' if dry else 'NO TOPIC'}] {title}\n    {body}\n    {url}")
        return
    headers = {
        "Title": ascii_header(title),
        "Priority": "high" if urgent else "default",
        "Tags": "warning" if urgent else "fuelpump",
    }
    if url:
        headers["Click"] = url
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}", data=body.encode("utf-8"), headers=headers
    )
    urllib.request.urlopen(req, timeout=30).read()


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return None


def main():
    dry = "--dry-run" in sys.argv
    if "--test" in sys.argv:
        push("Pipeline monitor test", "If you can read this, alerts are working.", dry=dry)
        return

    state = load_state()
    first_run = state is None
    state = state or {}
    errors = 0

    for name, url in SOURCES.items():
        try:
            notices = parse_notices(fetch(url))
        except Exception as exc:  # network error, 404, etc.
            print(f"WARNING: could not read {name}: {exc}", file=sys.stderr)
            errors += 1
            continue
        if not notices:
            print(f"WARNING: no notices parsed for {name}; page layout may "
                  f"have changed or the URL is wrong: {url}", file=sys.stderr)
            errors += 1
            continue

        seen = set(state.get(name, []))
        new_ids = [i for i in notices if i not in seen]
        # On the very first run just record what exists so you aren't spammed.
        if not first_run:
            for nid in sorted(new_ids, key=int):
                subject, href = notices[nid]
                if href.startswith("http"):
                    link = href
                elif href:
                    link = "https://www.piperiv.com" + href
                else:
                    link = OFFICIAL.get(name, url)
                # Link goes in the text too, so it is tappable in any app.
                push(f"{name}: new critical notice", f"{subject}\n{link}", link,
                     urgent=bool(URGENT.search(subject)), dry=dry)
        state[name] = sorted(seen | set(notices), key=int)[-500:]

    if not dry:
        STATE_FILE.write_text(json.dumps(state, indent=1))
    if first_run:
        print("First run: recorded existing notices, no alerts sent.")
    if errors == len(SOURCES):
        sys.exit(1)


if __name__ == "__main__":
    main()
