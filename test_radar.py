import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import radar  # noqa: E402

NOW = datetime(2026, 11, 19, 8, 0, tzinfo=timezone.utc)
INCLUDE = ["B2B", "product marketing", "positioning", "go-to-market", "AI search", "LinkedIn"]
EXCLUDE = ["sponsored", "webinar"]


def sample_items():
    items = []
    for name in ("sample_rss.xml", "sample_atom.xml"):
        items += radar.parse_feed((ROOT / name).read_text(encoding="utf-8"))
    return items


class ParsingTests(unittest.TestCase):
    def test_rss(self):
        items = radar.parse_feed((ROOT / "sample_rss.xml").read_text(encoding="utf-8"))
        self.assertEqual(len(items), 5)
        self.assertEqual(items[0]["source"], "Synthetic Demo Blog (fictional)")
        self.assertEqual(items[0]["summary"], "A fictional post about slide design.")  # HTML removed
        self.assertEqual(items[0]["published"], datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc))

    def test_atom(self):
        items = radar.parse_feed((ROOT / "sample_atom.xml").read_text(encoding="utf-8"))
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["link"], "https://example.org/ai-search-b2b")
        self.assertEqual(items[1]["published"], datetime(2026, 11, 17, 11, 0, tzinfo=timezone.utc))

    def test_hacker_news(self):
        payload = json.dumps({"hits": [
            {"title": "B2B SaaS pricing pages", "url": "https://example.net/pricing",
             "objectID": "1", "points": 42, "created_at_i": 1763452800},
            {"title": "Ask HN: B2B onboarding?", "url": None, "objectID": "2", "points": 3},
            {"title": None, "objectID": "3"},
        ]})
        items = radar.parse_hn(payload, "B2B")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["source"], "Hacker News (42 points)")
        self.assertEqual(items[1]["link"], "https://news.ycombinator.com/item?id=2")

    def test_hacker_news_min_points(self):
        payload = json.dumps({"hits": [
            {"title": "Big story", "objectID": "1", "points": 50},
            {"title": "Tiny story", "objectID": "2", "points": 2},
        ]})
        self.assertEqual([i["title"] for i in radar.parse_hn(payload, "x", min_points=5)], ["Big story"])

    def test_bad_date_is_none(self):
        self.assertIsNone(radar.parse_date("not a date"))


class FilterTests(unittest.TestCase):
    def test_keywords_and_exclusions(self):
        item = {"title": "Sponsored: B2B tool", "summary": ""}
        self.assertEqual(radar.match_keywords(item, INCLUDE, EXCLUDE), [])
        item = {"title": "Positioning for B2B", "summary": ""}
        self.assertEqual(radar.match_keywords(item, INCLUDE, EXCLUDE), ["B2B", "positioning"])

    def test_whole_words_only(self):
        item = {"title": "LinkedInfluencer weekly", "summary": ""}
        self.assertEqual(radar.match_keywords(item, ["LinkedIn"], []), [])

    def test_normalize_url_drops_tracking(self):
        a = radar.normalize_url("https://www.example.com/post/?utm_source=x&id=5")
        b = radar.normalize_url("https://example.com/post?id=5")
        self.assertEqual(a, b)

    def test_select_on_samples(self):
        picked = radar.select(sample_items(), INCLUDE, EXCLUDE, {}, NOW, 0, 10)
        titles = [i["title"] for i in picked]
        self.assertEqual(len(picked), 4)
        self.assertEqual(titles.count("Why B2B buyers ignore your LinkedIn carousel"), 1)  # duplicate removed
        self.assertNotIn("Our office dog has a new favourite toy", titles)
        self.assertFalse(any("Sponsored" in t or "webinar" in t for t in titles))
        self.assertEqual(len(picked[0]["matched"]), 2)  # most matches first

    def test_recency_and_seen(self):
        items = sample_items()
        recent = radar.select(items, INCLUDE, EXCLUDE, {}, NOW, 24, 10)
        self.assertTrue(all(i["published"] >= NOW - timedelta(hours=24) for i in recent))
        seen = {radar.normalize_url("https://example.org/ai-search-b2b"): NOW.isoformat()}
        picked = radar.select(items, INCLUDE, EXCLUDE, seen, NOW, 0, 10)
        self.assertNotIn("https://example.org/ai-search-b2b", [i["link"] for i in picked])

    def test_max_items(self):
        self.assertEqual(len(radar.select(sample_items(), INCLUDE, EXCLUDE, {}, NOW, 0, 2)), 2)


class OutputTests(unittest.TestCase):
    def test_digest_fits_telegram(self):
        item = {"title": "B2B " + "x" * 300, "source": "Feed", "matched": ["B2B"], "link": "https://e.com"}
        text = radar.format_digest([item] * 40, NOW)
        self.assertLessEqual(len(text), 4096)
        self.assertIn("cut to fit", text)

    def test_seen_file_prunes_old_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen.json"
            radar.save_seen(path, {"old": (NOW - timedelta(days=40)).isoformat(), "new": NOW.isoformat()})
            self.assertEqual(list(radar.load_seen(path, NOW)), ["new"])
            self.assertEqual(radar.load_seen(Path(tmp) / "missing.json", NOW), {})

    def test_keyword_file(self):
        include, exclude = radar.load_keywords(ROOT / "keywords.txt")
        self.assertIn("B2B", include)
        self.assertIn("sponsored", exclude)


class IdeasTests(unittest.TestCase):
    def test_prompt_numbers_items_like_the_digest(self):
        items = radar.select(sample_items(), INCLUDE, EXCLUDE, {}, NOW, 0, 10)
        prompt = radar.build_prompt(items, "Ukrainian")
        self.assertIn("Ukrainian", prompt)
        self.assertIn("Do not invent facts", prompt)
        self.assertIn(f"1. {items[0]['title']} |", prompt)
        self.assertIn(f"{len(items)}. {items[-1]['title']} |", prompt)

    def test_prompt_uses_only_the_first_pool_items(self):
        item = {"title": "T", "source": "S", "summary": "x" * 1000, "link": "https://e.com", "matched": ["B2B"]}
        prompt = radar.build_prompt([item] * 20, "English")
        self.assertEqual(prompt.count("snippet:"), radar.IDEAS_POOL)
        self.assertNotIn("x" * (radar.SNIPPET_CHARS + 1), prompt)

    def test_ideas_message_fits_telegram(self):
        self.assertLessEqual(len(radar.format_ideas("a" * 9000, NOW)), 4096)


class FullRunTests(unittest.TestCase):
    """Runs main() end to end with the network replaced by the sample files."""

    def test_send_and_remember(self):
        from unittest import mock

        rss = (ROOT / "sample_rss.xml").read_text(encoding="utf-8")
        sent = []

        class FakeResponse:
            def __init__(self, body):
                self.body = body
            def read(self):
                return self.body
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=30):
            url = req.full_url
            if url.startswith("https://api.telegram.org/bot"):
                sent.append(req.data.decode())
                return FakeResponse(b'{"ok": true}')
            return FakeResponse(rss.encode())

        with tempfile.TemporaryDirectory() as tmp:
            feeds = Path(tmp) / "feeds.txt"
            feeds.write_text("https://example.com/feed\n", encoding="utf-8")
            seen = Path(tmp) / "seen.json"
            kw = Path(tmp) / "keywords.txt"
            kw.write_text("\n".join(INCLUDE + ["-sponsored"]), encoding="utf-8")
            argv = ["radar.py", "--feeds", str(feeds), "--keywords", str(kw), "--seen", str(seen),
                    "--no-hn", "--hours", "0"]
            env = {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "42"}
            with mock.patch.object(sys, "argv", argv), mock.patch.dict("os.environ", env), \
                    mock.patch("urllib.request.urlopen", fake_urlopen):
                radar.main()
                self.assertEqual(len(sent), 1)
                self.assertIn("chat_id=42", sent[0])
                self.assertEqual(len(json.loads(seen.read_text())), 2)
                radar.main()  # second run: everything already seen, nothing is sent
                self.assertEqual(len(sent), 1)

    def test_ideas_are_sent_first_and_failures_do_not_block_the_digest(self):
        from unittest import mock

        rss = (ROOT / "sample_rss.xml").read_text(encoding="utf-8")
        sent, api_calls = [], []

        class FakeResponse:
            def __init__(self, body):
                self.body = body
            def read(self):
                return self.body
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        def make_urlopen(api_works):
            def fake_urlopen(req, timeout=30):
                url = req.full_url
                if url.startswith("https://api.telegram.org/bot"):
                    sent.append(req.data.decode())
                    return FakeResponse(b'{"ok": true}')
                if url.startswith("https://api.anthropic.com"):
                    api_calls.append(json.loads(req.data))
                    if not api_works:
                        raise OSError("API down")
                    return FakeResponse(json.dumps(
                        {"content": [{"type": "text", "text": "Item 1: idea one"}]}).encode())
                return FakeResponse(rss.encode())
            return fake_urlopen

        with tempfile.TemporaryDirectory() as tmp:
            feeds = Path(tmp) / "feeds.txt"
            feeds.write_text("https://example.com/feed\n", encoding="utf-8")
            kw = Path(tmp) / "keywords.txt"
            kw.write_text("\n".join(INCLUDE + ["-sponsored"]), encoding="utf-8")
            env = {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "42", "ANTHROPIC_API_KEY": "sk-test"}
            for works, expected_messages in ((True, 2), (False, 1)):
                sent.clear()
                api_calls.clear()
                seen = Path(tmp) / f"seen_{works}.json"
                argv = ["radar.py", "--feeds", str(feeds), "--keywords", str(kw), "--seen", str(seen),
                        "--no-hn", "--hours", "0"]
                with mock.patch.object(sys, "argv", argv), mock.patch.dict("os.environ", env), \
                        mock.patch("urllib.request.urlopen", make_urlopen(works)):
                    radar.main()
                self.assertEqual(len(sent), expected_messages)
                self.assertEqual(len(api_calls), 1)
                self.assertIn("Trend+radar", sent[-1])  # the digest is always last (urlencoded)
                if works:
                    self.assertIn("idea+one", sent[0])  # urlencoded form data


if __name__ == "__main__":
    unittest.main()
