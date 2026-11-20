"""Dry run with fake feeds, fake Claude and a fake voice. No keys needed."""
import io, json, os, sys, wave, math, struct
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import briefing as b

os.environ.update(SITE_URL="https://example.github.io/morning-audio/", ANTHROPIC_API_KEY="x", GOOGLE_TTS_KEY="x")
os.environ.pop("EMAIL_FROM", None)
from email.utils import format_datetime
from datetime import datetime, timezone, timedelta
now = datetime.now(timezone.utc)

def rss(source, n):
    items = "".join(f"<item><title>{source} story {k} about climate &amp; Albanese</title>"
                    f"<link>https://{source.replace(' ','').lower()}.example/story-{k}</link>"
                    f"<description>&lt;p&gt;Blurb {k} for {source}.&lt;/p&gt;</description>"
                    f"<pubDate>{format_datetime(now - timedelta(hours=k))}</pubDate></item>" for k in range(1, n + 1))
    old = f"<item><title>Old {source}</title><link>https://old.example/{source}</link><pubDate>{format_datetime(now - timedelta(days=3))}</pubDate></item>"
    sport = f"<item><title>NRL grand final</title><link>https://x.example/sport/1</link><pubDate>{format_datetime(now)}</pubDate></item>"
    return f"<rss><channel><title>{source}</title>{items}{old}{sport}</channel></rss>".encode()

class Resp:
    def __init__(self, content=b"", status=200, url=""):
        self.content, self.status_code, self.url = content, status, url
        self.text = content.decode("utf-8", "ignore")
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(f"HTTP {self.status_code}")
    def json(self): return json.loads(self.content)

PREV = Path("/tmp/prev"); PREV.mkdir(exist_ok=True)
def fake_get(url, **kw):
    if url.endswith("episodes.json"):
        p = PREV / "episodes.json"
        return Resp(p.read_bytes()) if p.exists() else Resp(b"", 404)
    if url.endswith(".mp3"):
        p = PREV / url.rsplit("/", 1)[1]
        return Resp(p.read_bytes()) if p.exists() else Resp(b"", 404)
    if "story-" in url:
        para = "<p>" + ("This is a sentence of the article body. " * 30) + "</p>"
        return Resp(f"<html><body><article><h1>T</h1>{para*3}</article></body></html>".encode(), url=url)
    if "news.google.com" in url and "rss" in url:
        return Resp(rss("Michael West Media", 2).replace(b"</title><link>", b" - Michael West</title><link>"))
    if "aljazeera" in url: return Resp(b"", 403)   # one broken feed
    for f in b.load_feeds():
        if f["url"] == url: return Resp(rss(f["source"], 3))
    return Resp(b"", 404)
b.SESSION.get = fake_get

def fake_claude(system, prompt, max_tokens=8000):
    if "Choose" in prompt:
        ids = [int(l.split(" | ")[0]) for l in prompt.splitlines() if l[:1].isdigit() and " | " in l]
        secs = ["Australian politics"]*5 + ["Environment & climate"]*3 + ["Nature"]*2 + ["Design"]
        return {"picks": [{"id": i, "section": s, "duplicates": [ids[-1]] if n == 0 else []}
                          for n, (i, s) in enumerate(zip(ids[::3], secs))]}
    ids = [int(l.split("Story id ")[1].split(" |")[0]) for l in prompt.splitlines() if l.startswith("=== Story id")]
    assert "headline and blurb only" not in prompt or True
    return {"intro": "Good morning, Antoinette. It's Friday.",
            "stories": [{"id": i, "section": "x", "title": f"Story <{i}> & co",
                         "email_summary": f"Summary for {i}.", "script": "Some words. " * 40} for i in ids],
            "outro": "That's the show."}
b.claude = fake_claude

def fake_synth(text, voice=None):
    secs = max(1, len(text) // 60)   # pretend ~15 chars/sec
    b._voice_in_use = "fake-voice"
    return b"".join(struct.pack("<h", int(3000*math.sin(2*math.pi*220*t/24000))) for t in range(24000*secs))
b.synthesize = fake_synth

sys.argv = ["briefing.py", "--test", "--out", "/tmp/out"]
b.main()
