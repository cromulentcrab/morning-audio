"""Antoinette's show: a spoken morning news briefing, as a private podcast.

Each morning this:
  1. reads the free news feeds in feeds.txt,
  2. asks Claude to pick the day's best stories and write a spoken script
     about them, in its own words, naming each outlet,
  3. turns the script into audio with a Google Cloud voice,
  4. publishes the episode to a podcast feed (GitHub Pages), keeping the
     last couple of weeks, and
  5. emails a large-print list of the same stories with links.

Settings (GitHub secrets, see README.md):
  ANTHROPIC_API_KEY   writes the script
  GOOGLE_TTS_KEY      Google Cloud API key with Text-to-Speech turned on
  EMAIL_FROM          Gmail address that sends the email
  APP_PASSWORD        Gmail app password for EMAIL_FROM
  BRIEFING_TO         who gets the email (comma-separated); test runs go to
                      EMAIL_FROM only
  SITE_URL            the podcast's web address (set by the workflow)
  VOICE               optional: a Google voice name (repository variable)

    python briefing.py                 normal morning run
    python briefing.py --test          email only to EMAIL_FROM, don't mark
                                       stories as used
    python briefing.py --no-publish    make everything but don't touch the feed
"""
import argparse
import base64
import email.utils
import html
import io
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import tempfile
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import feedparser
import requests

HERE = Path(__file__).resolve().parent
FEEDS_FILE = HERE / "feeds.txt"
STATE_FILE = HERE / "state" / "seen.json"
STATIC = HERE / "site-static"
SITE_DIR = HERE / "site"

# ---------------------------------------------------------------------------
# Things you might want to change
# ---------------------------------------------------------------------------
SHOW_NAME = "Antoinette's show"
LISTENER = "Antoinette"
SHOW_DESCRIPTION = ("A spoken morning briefing on Australian politics, the environment and "
                    "climate, nature and design, from free outlets she trusts.")
SECTIONS = ["Australian politics", "Environment & climate", "Nature", "Design"]
TARGET_MINUTES = 12          # roughly how long each episode runs
STORIES = "9 to 11"          # how many stories per episode
KEEP_EPISODES = 14           # how many past episodes stay in the feed
MODEL = "claude-sonnet-5-5"  # Anthropic model that picks and writes
DEFAULT_VOICE = "en-AU-Chirp3-HD-Aoede"
FALLBACK_VOICE = "en-AU-Neural2-C"   # used only if the first voice is refused
SPEAKING_RATE = 1.0          # 0.9 = a little slower, 1.1 = a little faster
GAP_SECONDS = 1.1            # pause between stories

MAX_HOURS_BACK = 30
MAX_PER_FEED = 15
SEEN_DAYS = 21
SYDNEY = ZoneInfo("Australia/Sydney")
DROP_WORDS = ["crossword", "Wordle", "quiz", "horoscope", "AFL", "NRL", "cricket",
              "A-League", "Matildas", "Socceroos", "Wallabies", "Premier League", "recipe"]
DROP_URL_BITS = ["/sport", "/football/", "/crosswords/", "/lifeandstyle/", "/food/",
                 "/travel/", "/video/", "/live/", "/gallery/"]

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
SESSION = requests.Session()
SESSION.headers["User-Agent"] = UA


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------------------
# 1. Stories
# ---------------------------------------------------------------------------

def load_feeds():
    feeds = []
    for line in FEEDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        section, source, url = parts[:3]
        opts = {o.strip() for o in (parts[3] if len(parts) > 3 else "").split(",") if o.strip()}
        if section != "any" and section not in SECTIONS:
            log(f"feeds.txt: unknown section '{section}', skipping {source}")
            continue
        feeds.append({"section": section, "source": source, "url": url, "opts": opts})
    return feeds


def fetch_feed(feed):
    try:
        r = SESSION.get(feed["url"], timeout=25)
        r.raise_for_status()
        return feed, feedparser.parse(r.content).entries, None
    except Exception as e:  # noqa: BLE001 - one bad feed shouldn't stop the show
        return feed, [], str(e)[:120]


def norm_url(url):
    s = urlsplit(url)
    return urlunsplit((s.scheme, s.netloc.lower(), s.path.rstrip("/"), "", ""))


def norm_title(t):
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()


def clean(raw, limit=400):
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def entry_time(e):
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def gather(results, since, seen):
    items, by_url, by_title = [], set(), set()
    for feed, entries, _ in results:
        n = 0
        for e in entries:
            title = clean(e.get("title", ""), 300)
            link = e.get("link", "")
            if not title or not link:
                continue
            if "gnews" in feed["opts"]:
                title = re.sub(r"\s+-\s+[^-]+$", "", title)
            when = entry_time(e)
            if when and when < since:
                continue
            if any(re.search(rf"\b{re.escape(w)}\b", title, re.I) for w in DROP_WORDS):
                continue
            if any(b in link for b in DROP_URL_BITS):
                continue
            u, t = norm_url(link), norm_title(title)
            if u in seen or t in seen or u in by_url or t in by_title:
                continue
            by_url.add(u)
            by_title.add(t)
            items.append({
                "id": len(items) + 1, "section": feed["section"], "source": feed["source"],
                "title": title, "link": link, "blurb": clean(e.get("summary", ""), 300),
                "gnews": "gnews" in feed["opts"],
            })
            n += 1
            if n >= MAX_PER_FEED:
                break
    return items


def outlet(source):
    """'The Guardian (wildlife)' -> 'The Guardian'."""
    return re.sub(r"\s*\(.*\)$", "", source)


def article_text(item, limit=7000):
    """Full text of the article, or the feed's blurb if the site won't give it."""
    if item.get("gnews"):
        # Google News links are redirects; follow them to the real page.
        try:
            r = SESSION.get(item["link"], timeout=20, allow_redirects=True)
            if "news.google.com" not in r.url:
                item["link"] = r.url
        except Exception:  # noqa: BLE001
            pass
    try:
        import trafilatura
        r = SESSION.get(item["link"], timeout=25)
        r.raise_for_status()
        text = trafilatura.extract(r.text, include_comments=False, include_tables=False) or ""
    except Exception as e:  # noqa: BLE001
        log(f"  couldn't fetch {item['source']}: {str(e)[:80]}")
        text = ""
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 400:
        return "", True
    return text[:limit], False


# ---------------------------------------------------------------------------
# 2. Claude picks the stories and writes the script
# ---------------------------------------------------------------------------

def claude(system, prompt, max_tokens=8000):
    key = os.environ["ANTHROPIC_API_KEY"]
    last = None
    for attempt in range(4):
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": MODEL, "max_tokens": max_tokens, "system": system,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=300,
            )
            if r.status_code in (429, 500, 502, 503, 529):
                raise RuntimeError(f"Anthropic busy ({r.status_code})")
            if r.status_code != 200:
                sys.exit(f"Anthropic API error {r.status_code}: {r.text[:400]}")
            text = "".join(b.get("text", "") for b in r.json()["content"])
            return parse_json(text)
        except (RuntimeError, ValueError, requests.RequestException) as e:
            last = e
            log(f"  Claude attempt {attempt + 1} failed: {e}")
            time.sleep(15 * (attempt + 1))
    raise RuntimeError(f"Claude didn't respond properly: {last}")


def parse_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON in reply")
    return json.loads(text[start:end + 1])


PICK_SYSTEM = f"""You are the editor of a private morning news podcast for {LISTENER}, \
an Australian who is very politically engaged and well informed. She has lost most of her \
sight, so this show is how she keeps up with the news. Her interests, in order: \
{", ".join(SECTIONS)}. Reply with JSON only."""

PICK_PROMPT = """Today is {date}. Here are new stories from her chosen outlets, one per line:
id | section hint | outlet | headline | blurb

{lines}

Choose {stories} stories for today's episode.
- Weight it towards Australian politics (about half), then Environment & climate, then Nature. \
Include one Design story only if there's a genuinely interesting one.
- "any" means the story came from a broad feed: only pick it if it truly fits a section, \
and put it in that section. Al Jazeera stories are welcome for climate and environment, \
and for any story about Australia.
- Prefer substance: decisions, policy, votes, reports, investigations, analysis. Skip \
celebrity, crime with no policy angle, sport, live blogs, quizzes and minor updates.
- When several outlets cover the same story, pick the single best version and list the \
other ids as duplicates. Spread picks across outlets where quality allows.
- Order the picks as they should be played: by section in the order above, most \
important first within each section.

Reply with JSON:
{{"picks": [{{"id": 12, "section": "Australian politics", "duplicates": [40, 7]}}, ...]}}"""

WRITE_SYSTEM = f"""You write and present "{SHOW_NAME}", a private spoken news briefing \
for {LISTENER}, an Australian who is politically engaged and well informed. She has lost \
most of her sight, so she will only ever hear this; nobody reads it. A text-to-speech \
voice will read your words exactly as written. Reply with JSON only."""

WRITE_PROMPT = """Today is {date}. Write today's episode from the stories below. The \
episode should run about {minutes} minutes when read aloud (about {words} words in total).

{stories}

How to write it:
- Spoken Australian English, like a calm, warm ABC radio newsreader: clear, natural \
sentences, not chirpy, not stiff. Talk to {listener} directly in the opening and close only.
- Tell each story in your own words. Do not copy sentences from the articles. Quote at \
most a few words, and only when exact wording matters (say who said it).
- Always say where each story comes from, naturally: "The Guardian reports...", \
"Writing in Pearls and Irritations, former diplomat Jane Smith argues...". Make it clear \
when a piece is opinion or analysis rather than news, and whose view it is.
- Stick to what the article says. Don't add facts, background you're unsure of, or your \
own opinions. If a story's text is marked "headline and blurb only", keep it short and \
only say what's known.
- Give her substance: what happened, who's involved, what it means, what happens next. \
She knows the players, so no need to explain who the Prime Minister is.
- Written for the ear: no URLs, no "click", no brackets, no bullet points, no headings, \
no symbols. Write numbers the way a newsreader says them ("about three point two \
billion dollars", "forty per cent"). Say acronyms the way they're spoken, and expand \
unfamiliar ones once.
- Each story is 100 to 230 words. Start each new section with a short spoken signpost \
("Now to the environment.").
- Opening (in "intro"): "Good morning, {listener}." then today's date as spoken \
("It's Saturday, the twenty-first of November."), then one sentence on what's coming up.
- Close (in "outro"): one or two sentences: that's the show, the links to these stories \
are in her email, and a kind sign-off. Vary it a little from day to day.

Reply with JSON:
{{"intro": "...",
  "stories": [{{"id": 12, "section": "Australian politics",
               "title": "a short headline in your own words, for the email and chapter list",
               "email_summary": "one or two plain sentences in your own words for the email",
               "script": "what the voice reads for this story, including any section signpost"}}],
  "outro": "..."}}
Keep the stories in the order given."""


def pick_stories(items, date_words):
    lines = "\n".join(f"{i['id']} | {i['section']} | {outlet(i['source'])} | {i['title']} | {i['blurb']}"
                      for i in items)
    reply = claude(PICK_SYSTEM, PICK_PROMPT.format(date=date_words, lines=lines, stories=STORIES),
                   max_tokens=2000)
    by_id = {i["id"]: i for i in items}
    picks, dupes = [], []
    for p in reply.get("picks", []):
        item = by_id.get(int(p.get("id", 0)))
        if not item or item in picks:
            continue
        item = dict(item, section=p.get("section") if p.get("section") in SECTIONS else item["section"])
        if item["section"] not in SECTIONS:
            continue
        picks.append(item)
        dupes += [by_id[d] for d in p.get("duplicates", []) if d in by_id]
    picks.sort(key=lambda i: SECTIONS.index(i["section"]))  # stable: keeps editor's order within sections
    return picks, dupes


def write_script(picks, date_words):
    blocks = []
    for i in picks:
        body = i["text"] if not i["blurb_only"] else f"(headline and blurb only)\n{i['blurb']}"
        blocks.append(f"=== Story id {i['id']} | {i['section']} | {outlet(i['source'])} ===\n"
                      f"Headline: {i['title']}\n\n{body}")
    words = TARGET_MINUTES * 150
    reply = claude(WRITE_SYSTEM, WRITE_PROMPT.format(
        date=date_words, minutes=TARGET_MINUTES, words=words, listener=LISTENER,
        stories="\n\n".join(blocks)), max_tokens=12000)
    by_id = {i["id"]: i for i in picks}
    stories = []
    for s in reply.get("stories", []):
        item = by_id.get(int(s.get("id", 0)))
        if item and s.get("script", "").strip():
            stories.append({**item, "headline": s.get("title") or item["title"],
                            "email_summary": s.get("email_summary", ""),
                            "script": s["script"].strip()})
    if not stories:
        raise RuntimeError("Claude's script had no stories")
    return reply.get("intro", "").strip(), stories, reply.get("outro", "").strip()


# ---------------------------------------------------------------------------
# 3. The voice
# ---------------------------------------------------------------------------

def split_text(text, limit=1500):
    """Google takes a limited amount per request; split at sentence ends."""
    sentences = re.split(r"(?<=[.!?…])\s+", text.strip())
    chunks, cur = [], ""
    for s in sentences:
        while len(s) > limit:  # a monster sentence: split at a comma or space
            cut = max(s.rfind(", ", 0, limit), s.rfind(" ", 0, limit))
            chunks.append((cur + " " + s[:cut + 1]).strip())
            cur, s = "", s[cut + 1:].strip()
        if len(cur) + len(s) + 1 > limit:
            chunks.append(cur.strip())
            cur = s
        else:
            cur = f"{cur} {s}"
    if cur.strip():
        chunks.append(cur.strip())
    return [c for c in chunks if c]


_voice_in_use = None


def synthesize(text, voice=None):
    """Return 24 kHz mono 16-bit PCM bytes for text."""
    global _voice_in_use
    key = os.environ["GOOGLE_TTS_KEY"]
    voice = voice or _voice_in_use or os.environ.get("VOICE", "").strip() or DEFAULT_VOICE
    pcm = b""
    for chunk in split_text(text):
        body = {"input": {"text": chunk},
                "voice": {"languageCode": "-".join(voice.split("-")[:2]), "name": voice},
                "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": 24000}}
        if SPEAKING_RATE != 1.0:
            body["audioConfig"]["speakingRate"] = SPEAKING_RATE
        for attempt in range(4):
            r = requests.post("https://texttospeech.googleapis.com/v1/text:synthesize",
                              headers={"x-goog-api-key": key}, json=body, timeout=120)
            if r.status_code == 200:
                break
            if r.status_code == 400 and voice != FALLBACK_VOICE and "voice" in r.text.lower():
                log(f"Voice {voice} refused ({r.text[:200]}); using {FALLBACK_VOICE}")
                _voice_in_use = FALLBACK_VOICE
                return synthesize(text, FALLBACK_VOICE)
            if r.status_code in (429, 500, 503) and attempt < 3:
                time.sleep(10 * (attempt + 1))
                continue
            raise RuntimeError(f"Google voice error {r.status_code}: {r.text[:300]}")
        audio = base64.b64decode(r.json()["audioContent"])
        with wave.open(io.BytesIO(audio)) as w:
            pcm += w.readframes(w.getnframes())
    _voice_in_use = voice
    return pcm


def build_audio(intro, stories, outro, out_mp3, meta):
    """Read each part, join with pauses, level the volume, add chapters."""
    rate = 24000
    gap = b"\x00\x00" * int(rate * GAP_SECONDS)
    parts = [("Welcome", intro, None)] + [(s["headline"], s["script"], s["link"]) for s in stories] \
        + [("That's the show", outro, None)]
    pcm, chapters = b"", []
    for title, text, link in parts:
        if not text:
            continue
        start = len(pcm) // 2 * 1000 // rate
        pcm += synthesize(text)
        chapters.append((title, start, len(pcm) // 2 * 1000 // rate, link))
        pcm += gap
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "all.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm)
        # Speech-friendly MP3: mono, 64 kbps, levelled to podcast loudness.
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav),
                        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "44100", "-ac", "1",
                        "-b:a", "64k", str(out_mp3)], check=True)
    add_tags(out_mp3, chapters, meta)
    return len(pcm) // 2 // rate, chapters


def add_tags(mp3, chapters, meta):
    from mutagen.id3 import (APIC, CHAP, CTOC, ID3, TALB, TDRC, TIT2, TPE1, WXXX,
                             CTOCFlags)
    tags = ID3()
    tags.add(TIT2(encoding=3, text=meta["title"]))
    tags.add(TALB(encoding=3, text=SHOW_NAME))
    tags.add(TPE1(encoding=3, text=SHOW_NAME))
    tags.add(TDRC(encoding=3, text=meta["date"]))
    tags.add(APIC(encoding=3, mime="image/png", type=3, desc="Cover",
                  data=(STATIC / "cover.png").read_bytes()))
    ids = []
    for n, (title, start, end, link) in enumerate(chapters):
        cid = f"ch{n}"
        ids.append(cid)
        sub = [TIT2(encoding=3, text=title)]
        if link:
            sub.append(WXXX(encoding=3, desc="", url=link))
        tags.add(CHAP(element_id=cid, start_time=start, end_time=end, sub_frames=sub))
    tags.add(CTOC(element_id="toc", flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED,
                  child_element_ids=ids, sub_frames=[TIT2(encoding=3, text="Stories")]))
    tags.save(str(mp3), v2_version=3)


# ---------------------------------------------------------------------------
# 4. The podcast feed
# ---------------------------------------------------------------------------

def site_url():
    url = os.environ.get("SITE_URL", "").strip()
    if not url:
        sys.exit("SITE_URL isn't set (the workflow sets it)")
    return url.rstrip("/") + "/"


def previous_episodes(base):
    """Episodes already published, re-downloaded so the feed keeps them."""
    try:
        r = SESSION.get(base + "episodes.json", timeout=30)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001
        log(f"Couldn't read previous episodes ({e}); starting the list afresh")
        return []


def publish(episode, mp3_path, base):
    SITE_DIR.mkdir(exist_ok=True)
    old = [e for e in previous_episodes(base) if e["file"] != episode["file"]]
    keep = [episode]
    for e in old[:KEEP_EPISODES - 1]:
        try:
            r = SESSION.get(base + e["file"], timeout=120)
            r.raise_for_status()
            (SITE_DIR / e["file"]).write_bytes(r.content)
            keep.append(e)
        except Exception as err:  # noqa: BLE001
            log(f"Dropping old episode {e['file']}: {err}")
    (SITE_DIR / episode["file"]).write_bytes(Path(mp3_path).read_bytes())
    for f in STATIC.iterdir():
        (SITE_DIR / f.name).write_bytes(f.read_bytes())
    (SITE_DIR / "episodes.json").write_text(json.dumps(keep, indent=1), encoding="utf-8")
    (SITE_DIR / "feed.xml").write_text(render_feed(keep, base), encoding="utf-8")
    (SITE_DIR / "index.html").write_text(render_index(keep, base), encoding="utf-8")
    (SITE_DIR / ".nojekyll").write_text("")
    log(f"Site built with {len(keep)} episode(s) in {SITE_DIR}")


def x(s):
    return html.escape(s or "", quote=True)


def render_feed(episodes, base):
    items = []
    for e in episodes:
        notes = "".join(f"<p><b>{x(s['headline'])}</b> ({x(s['source'])})<br>"
                        f"{x(s['summary'])}<br><a href=\"{x(s['link'])}\">Read or listen to the article</a></p>"
                        for s in e["stories"])
        dur = e["seconds"]
        items.append(f"""    <item>
      <title>{x(e['title'])}</title>
      <description><![CDATA[{notes}]]></description>
      <content:encoded><![CDATA[{notes}]]></content:encoded>
      <enclosure url="{x(base + e['file'])}" length="{e['bytes']}" type="audio/mpeg"/>
      <guid isPermaLink="false">{x(e['file'])}</guid>
      <pubDate>{e['pubDate']}</pubDate>
      <itunes:duration>{dur // 3600:02d}:{dur % 3600 // 60:02d}:{dur % 60:02d}</itunes:duration>
      <itunes:episodeType>full</itunes:episodeType>
      <itunes:explicit>false</itunes:explicit>
    </item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{x(SHOW_NAME)}</title>
    <link>{x(base)}</link>
    <atom:link href="{x(base)}feed.xml" rel="self" type="application/rss+xml"/>
    <description>{x(SHOW_DESCRIPTION)}</description>
    <language>en-au</language>
    <itunes:author>{x(SHOW_NAME)}</itunes:author>
    <itunes:image href="{x(base)}cover.png"/>
    <itunes:category text="News"/>
    <itunes:explicit>false</itunes:explicit>
    <itunes:type>episodic</itunes:type>
    <itunes:block>Yes</itunes:block>
    <lastBuildDate>{email.utils.format_datetime(datetime.now(timezone.utc))}</lastBuildDate>
{chr(10).join(items)}
  </channel>
</rss>
"""


def render_index(episodes, base):
    latest = episodes[0]
    return f"""<!doctype html><html lang="en-AU"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>{x(SHOW_NAME)}</title>
<style>body{{font:22px/1.5 system-ui,sans-serif;max-width:40em;margin:2em auto;padding:0 1em;
background:#12263a;color:#faf4e4}}a{{color:#f2b705}}audio{{width:100%}}code{{word-break:break-all}}</style>
</head><body>
<h1>{x(SHOW_NAME)}</h1>
<h2>{x(latest['title'])}</h2>
<audio controls preload="none" src="{x(latest['file'])}"></audio>
<p>To get every episode automatically, open the Podcasts app, go to Library,
tap the &middot;&middot;&middot; button, choose <b>Follow a Show by URL</b> and enter:</p>
<p><code>{x(base)}feed.xml</code></p>
</body></html>
"""


# ---------------------------------------------------------------------------
# 5. The email
# ---------------------------------------------------------------------------

def render_email(title, stories):
    rows, last = [], None
    for s in stories:
        if s["section"] != last:
            rows.append(f'<h2 style="font-size:26px;margin:36px 0 8px;color:#12263a;'
                        f'border-bottom:3px solid #f2b705;padding-bottom:4px">{x(s["section"])}</h2>')
            last = s["section"]
        rows.append(
            f'<h3 style="font-size:28px;line-height:1.3;margin:22px 0 6px">'
            f'<a href="{x(s["link"])}" style="color:#0b3d91">{x(s["headline"])}</a></h3>'
            f'<p style="font-size:24px;line-height:1.5;margin:0 0 6px">{x(s["email_summary"])}</p>'
            f'<p style="font-size:22px;margin:0 0 10px"><a href="{x(s["link"])}" style="color:#0b3d91">'
            f'Open the article from {x(outlet(s["source"]))}</a></p>')
    body = f"""<!doctype html><html lang="en-AU"><body style="margin:0;background:#fffdf7">
<div style="max-width:700px;margin:0 auto;padding:20px;font-family:Arial,Helvetica,sans-serif;color:#111">
<h1 style="font-size:34px;margin:0 0 6px">{x(SHOW_NAME)}</h1>
<p style="font-size:24px;margin:0 0 4px">{x(title)}</p>
<p style="font-size:22px;margin:0 0 10px">Today's episode is ready in the Podcasts app.
You can also say: "Hey Siri, play {x(SHOW_NAME)}."</p>
{''.join(rows)}
<p style="font-size:20px;margin-top:36px;color:#444">To hear an article read aloud, open it,
then say "Hey Siri, read this", or tap the page menu next to the web address and choose Listen to Page.</p>
</div></body></html>"""
    plain = "\n\n".join(f"{s['headline']} ({outlet(s['source'])})\n{s['email_summary']}\n{s['link']}"
                        for s in stories)
    return body, f"{SHOW_NAME}: {title}\n\n{plain}"


def send_email(subject, html_body, plain, test):
    sender = os.environ["EMAIL_FROM"]
    to = sender if test else (os.environ.get("BRIEFING_TO", "").strip() or sender)
    recipients = [a.strip() for a in to.split(",") if a.strip()]
    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    msg["Subject"] = ("[TEST] " if test else "") + subject
    msg["From"] = f"{SHOW_NAME} <{sender}>"
    msg["To"] = ", ".join(recipients)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(sender, os.environ["APP_PASSWORD"].replace(" ", ""))
        s.sendmail(sender, recipients, msg.as_string())
    log(f"Emailed {len(recipients)} address(es)")


# ---------------------------------------------------------------------------
# State and the main run
# ---------------------------------------------------------------------------

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"seen": {}, "last_run": None}


def save_state(state):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=0, ensure_ascii=False), encoding="utf-8")


def spoken_date(d):
    n = d.day
    ords = {1: "first", 2: "second", 3: "third", 21: "twenty-first", 22: "twenty-second",
            23: "twenty-third", 31: "thirty-first"}
    return f"{d:%A}, the {ords.get(n, str(n) + 'th')} of {d:%B} {d.year}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="email only EMAIL_FROM; don't mark stories used")
    ap.add_argument("--no-publish", action="store_true", help="don't update the podcast feed")
    ap.add_argument("--out", default=str(HERE / "out"), help="where to keep the script and audio")
    args = ap.parse_args()

    now = datetime.now(SYDNEY)
    out = Path(args.out)
    out.mkdir(exist_ok=True)
    state = load_state()
    since = now - timedelta(hours=MAX_HOURS_BACK)
    if state.get("last_run"):
        since = max(since, datetime.fromisoformat(state["last_run"]) - timedelta(hours=3))
    cutoff = (now - timedelta(days=SEEN_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}

    # 1. Stories
    with ThreadPoolExecutor(10) as ex:
        results = list(ex.map(fetch_feed, load_feeds()))
    for feed, entries, err in results:
        log(f"{'FAILED ' + err if err else len(entries)}  {feed['source']} ({feed['section']})")
    items = gather(results, since, set(state["seen"]))
    log(f"{len(items)} new stories to choose from")
    if len(items) < 5:
        sys.exit("Too few new stories; is something wrong with the feeds?")

    # 2. Pick and write
    date_words = spoken_date(now)
    picks, dupes = pick_stories(items, date_words)
    log(f"Picked {len(picks)}: " + "; ".join(f"{p['source']}: {p['title']}" for p in picks))
    with ThreadPoolExecutor(6) as ex:
        texts = list(ex.map(article_text, picks))
    for p, (text, blurb_only) in zip(picks, texts):
        p["text"], p["blurb_only"] = text, blurb_only
    intro, stories, outro = write_script(picks, date_words)
    title = f"{now:%A} {now.day} {now:%B}"
    script = "\n\n".join([intro] + [s["script"] for s in stories] + [outro])
    (out / "script.txt").write_text(script, encoding="utf-8")
    log(f"Script: {len(script.split())} words, {len(stories)} stories")

    # Email first, so she still gets the news if the voice step fails.
    html_body, plain = render_email(title, stories)
    (out / "email.html").write_text(html_body, encoding="utf-8")
    if os.environ.get("EMAIL_FROM"):
        send_email(f"{SHOW_NAME}: {title}", html_body, plain, args.test)

    # 3. Voice
    file = f"{now:%Y-%m-%d}.mp3"
    mp3 = out / file
    seconds, chapters = build_audio(intro, stories, outro, mp3, {"title": title, "date": f"{now:%Y-%m-%d}"})
    log(f"Audio: {seconds // 60} min {seconds % 60} s, voice {_voice_in_use}")

    # 4. Feed
    if not args.no_publish:
        episode = {"title": title, "file": file, "bytes": mp3.stat().st_size, "seconds": seconds,
                   "pubDate": email.utils.format_datetime(now),
                   "stories": [{"headline": s["headline"], "source": outlet(s["source"]),
                                "summary": s["email_summary"], "link": s["link"]} for s in stories]}
        publish(episode, mp3, site_url())

    if not args.test:
        stamp = now.isoformat()
        for i in [*picks, *dupes]:
            state["seen"][norm_url(i["link"])] = stamp
            state["seen"][norm_title(i["title"])] = stamp
        state["last_run"] = stamp
        save_state(state)


if __name__ == "__main__":
    main()
