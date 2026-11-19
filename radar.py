#!/usr/bin/env python3
"""Trend radar for marketers.

Reads RSS/Atom feeds and Hacker News, keeps only items that match your keywords,
removes duplicates and items you have already seen, and sends a short digest to
Telegram. Designed to run once a day on GitHub Actions. Uses only the Python
standard library, so there is nothing to install.

  Demo (no internet, no Telegram):  python radar.py --demo
  Preview real sources:             python radar.py --dry-run
  Send to Telegram:                 python radar.py   (needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

HERE = Path(__file__).resolve().parent
USER_AGENT = "trend-radar/1.0 (+https://github.com)"
TELEGRAM_LIMIT = 4000  # Telegram allows 4096 characters per message; keep a margin
SEEN_DAYS = 30
TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "ref")


# ---------- config ----------

def read_lines(path):
    """Non-empty lines, without comments (lines starting with #)."""
    lines = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def load_keywords(path):
    """Lines starting with '-' are exclusions. Everything else is a keyword to look for."""
    include, exclude = [], []
    for line in read_lines(path):
        if line.startswith("-"):
            if line[1:].strip():
                exclude.append(line[1:].strip())
        else:
            include.append(line)
    return include, exclude


# ---------- parsing ----------

def local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def child_text(el, *names):
    for child in el:
        if local(child.tag) in names and (child.text or "").strip():
            return child.text.strip()
    return ""


def clean_text(text):
    text = re.sub(r"<[^>]+>", " ", unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def parse_date(value):
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def atom_link(entry):
    fallback = ""
    for child in entry:
        if local(child.tag) == "link":
            href = child.get("href", "")
            if child.get("rel", "alternate") == "alternate" and href:
                return href
            fallback = fallback or href
    return fallback


def parse_feed(xml_text, source_name=""):
    """Parse RSS 2.0 or Atom. Returns a list of item dicts."""
    root = ET.fromstring(xml_text)
    items = []
    feed_title = ""
    for el in root.iter():
        if local(el.tag) in ("channel", "feed"):
            feed_title = child_text(el, "title")
            break
    source = source_name or feed_title or "Feed"
    for el in root.iter():
        kind = local(el.tag)
        if kind == "item":
            link = child_text(el, "link") or child_text(el, "guid")
            items.append({
                "title": clean_text(child_text(el, "title")),
                "link": link,
                "summary": clean_text(child_text(el, "description", "summary")),
                "published": parse_date(child_text(el, "pubdate", "date", "published")),
                "source": source,
            })
        elif kind == "entry":
            items.append({
                "title": clean_text(child_text(el, "title")),
                "link": atom_link(el),
                "summary": clean_text(child_text(el, "summary", "content")),
                "published": parse_date(child_text(el, "published", "updated")),
                "source": source,
            })
    return [i for i in items if i["title"] and i["link"]]


def parse_hn(json_text, keyword):
    """Parse a Hacker News search response (hn.algolia.com)."""
    items = []
    for hit in json.loads(json_text).get("hits", []):
        title = hit.get("title") or ""
        if not title:
            continue
        story = f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
        published = None
        if hit.get("created_at_i"):
            published = datetime.fromtimestamp(int(hit["created_at_i"]), tz=timezone.utc)
        items.append({
            "title": clean_text(title),
            "link": hit.get("url") or story,
            "summary": "",
            "published": published,
            "source": f"Hacker News ({hit.get('points') or 0} points)",
            "matched": [keyword],
        })
    return items


# ---------- filtering ----------

def keyword_pattern(keyword):
    return re.compile(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)", re.IGNORECASE)


def match_keywords(item, include, exclude):
    """Return the list of matched keywords, or [] if the item should be skipped."""
    text = f"{item['title']} {item.get('summary', '')}"
    if any(keyword_pattern(k).search(text) for k in exclude):
        return []
    found = [k for k in include if keyword_pattern(k).search(text)]
    for k in item.get("matched", []):  # Hacker News items already matched by the search
        if k not in found and keyword_pattern(k).search(item["title"]):
            found.append(k)
    return found


def normalize_url(url):
    parts = urllib.parse.urlsplit(url.strip())
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query)
             if not k.lower().startswith(TRACKING_PARAMS)]
    path = parts.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower().removeprefix("www."),
                                    path, urllib.parse.urlencode(query), ""))


def select(items, include, exclude, seen, now, hours, max_items):
    """Filter by keywords, recency and history, remove duplicates, rank, cut to max_items."""
    since = now - timedelta(hours=hours) if hours else None
    picked, links, titles = [], set(), set()
    for item in items:
        if since and item["published"] and item["published"] < since:
            continue
        matched = match_keywords(item, include, exclude)
        if not matched:
            continue
        key = normalize_url(item["link"])
        title_key = re.sub(r"\W+", " ", item["title"].lower()).strip()
        if key in seen or key in links or title_key in titles:
            continue
        links.add(key)
        titles.add(title_key)
        picked.append({**item, "matched": matched, "key": key})
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    picked.sort(key=lambda i: (len(i["matched"]), i["published"] or oldest), reverse=True)
    return picked[:max_items]


# ---------- output ----------

def format_digest(items, today):
    header = f"Trend radar, {today:%d %b %Y}\n{len(items)} new items matching your keywords"
    blocks = []
    for n, item in enumerate(items, start=1):
        blocks.append(
            f"{n}. {item['title']}\n"
            f"   {item['source']} | keywords: {', '.join(item['matched'])}\n"
            f"   {item['link']}"
        )
    message = header
    for block in blocks:
        if len(message) + len(block) + 2 > TELEGRAM_LIMIT:
            message += "\n\n(more items were cut to fit one Telegram message)"
            break
        message += "\n\n" + block
    return message


def send_telegram(text, token, chat_id):
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not body.get("ok"):
        raise RuntimeError(f"Telegram error: {body.get('description')}")


# ---------- state ----------

def load_seen(path, now):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    cutoff = now - timedelta(days=SEEN_DAYS)
    return {k: v for k, v in data.items() if parse_date(v) and parse_date(v) >= cutoff}


def save_seen(path, seen):
    Path(path).write_text(json.dumps(seen, indent=1, sort_keys=True) + "\n", encoding="utf-8")


# ---------- fetching ----------

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def collect(feeds, include, use_hn, now, hours):
    items, failures, sources = [], 0, 0
    for url in feeds:
        sources += 1
        try:
            items += parse_feed(fetch(url))
        except Exception as exc:  # one broken feed should not stop the radar
            failures += 1
            print(f"Warning: could not read {url}: {exc}", file=sys.stderr)
    if use_hn:
        since = int((now - timedelta(hours=hours or 48)).timestamp())
        for keyword in include:
            sources += 1
            query = urllib.parse.urlencode({
                "query": keyword, "tags": "story", "hitsPerPage": 20,
                "numericFilters": f"created_at_i>{since}",
            })
            try:
                items += parse_hn(fetch(f"https://hn.algolia.com/api/v1/search_by_date?{query}"), keyword)
            except Exception as exc:
                failures += 1
                print(f"Warning: Hacker News search failed for '{keyword}': {exc}", file=sys.stderr)
    return items, failures, sources


def main():
    p = argparse.ArgumentParser(description="Daily trend radar: feeds + Hacker News to Telegram.")
    p.add_argument("--feeds", default=str(HERE / "feeds.txt"))
    p.add_argument("--keywords", default=str(HERE / "keywords.txt"))
    p.add_argument("--seen", default=str(HERE / "seen.json"))
    p.add_argument("--hours", type=int, default=48, help="Only items from the last N hours (0 = no limit)")
    p.add_argument("--max-items", type=int, default=10)
    p.add_argument("--no-hn", action="store_true", help="Skip Hacker News")
    p.add_argument("--dry-run", action="store_true", help="Print the digest instead of sending it")
    p.add_argument("--demo", action="store_true", help="Use the sample feeds next to this script, no internet")
    args = p.parse_args()

    now = datetime.now(timezone.utc)
    include, exclude = load_keywords(args.keywords)
    if not include:
        sys.exit("Add at least one keyword to keywords.txt")

    if args.demo:
        items = []
        for name in ("sample_rss.xml", "sample_atom.xml"):
            items += parse_feed((HERE / name).read_text(encoding="utf-8"))
        print(format_digest(select(items, include, exclude, {}, now, 0, args.max_items), now))
        return

    items, failures, sources = collect(read_lines(args.feeds), include, not args.no_hn, now, args.hours)
    if sources and failures == sources:
        sys.exit("Every source failed. Check your internet connection and the URLs in feeds.txt.")

    seen = load_seen(args.seen, now)
    picked = select(items, include, exclude, seen, now, args.hours, args.max_items)
    if not picked:
        print("No new items today.")
        return

    digest = format_digest(picked, now)
    if args.dry_run:
        print(digest)
        return

    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (GitHub: Settings > Secrets), or use --dry-run.")
    send_telegram(digest, token, chat_id)
    for item in picked:
        seen[item["key"]] = now.isoformat()
    Path(args.seen).parent.mkdir(parents=True, exist_ok=True)
    save_seen(args.seen, seen)
    print(f"Sent {len(picked)} items to Telegram.")


if __name__ == "__main__":
    main()
