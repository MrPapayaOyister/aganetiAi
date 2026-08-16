"""
News reader — latest headlines from public RSS feeds, rendered as a chat card.

Ported from Hermes's tools/news_reader.py. The feed list, the master-detail card
and its `textContent` discipline are theirs; the adaptations are ours:

  * httpx rather than aiohttp — already a dependency, and the tool dispatcher
    already runs in a worker thread, so this is sync with a small thread pool
    for the multi-category case instead of an event loop;
  * returns ``(HTMLResponse, context, meta)`` so backend.tool_result splits it
    like every other widget-producing tool here;
  * NO "Read Full Article" anchor in the card. Hermes has one, and it cannot
    work for us: our embed sandbox is `allow-scripts` ONLY, so a target=_blank
    link inside the frame gets an opaque origin and the browser refuses it —
    verified when the same problem broke the YouTube card. The article URLs go
    to the model in the context string instead, and it cites them in its reply
    as ordinary markdown, which react-markdown renders as working links.

Feeds are constants here on purpose. The user-editable source library
(media_sources) is being built for live TV, which genuinely needs it; news ships
against a curated seed list until then.

No API key — these are public RSS endpoints.
"""

from __future__ import annotations

import html as _html
import json as _json
import logging
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

from fastapi.responses import HTMLResponse

log = logging.getLogger("aria.news")

# Seeded from Hermes's curated list. Public endpoints, no key, no quota.
FEEDS = {
    # 🌍 World News
    "world": "http://feeds.bbci.co.uk/news/world/rss.xml",
    "world_nyt": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    
    # 💻 General Technology
    "tech": "https://feeds.arstechnica.com/arstechnica/index",
    "tech_crunch": "https://techcrunch.com/feed/", #
    "tech_verge": "https://www.theverge.com/rss/index.xml", #
    "tech_wired": "https://www.wired.com/feed/rss", #
    "tech_engadget": "https://www.engadget.com/rss.xml", #
    "tech_gizmodo": "https://gizmodo.com/rss", #
    "tech_hacker_news": "https://news.ycombinator.com/rss",

    # 🤖 Artificial Intelligence & Machine Learning
    "ai_openai": "https://openai.com/news/rss.xml", #
    "ai_mit": "https://www.technologyreview.com/topic/artificial-intelligence/feed/", #
    "ai_huggingface": "https://huggingface.co/blog/feed.xml", #
    "ai_marktechpost": "https://www.marktechpost.com/feed/", #
    "ai_arxiv": "https://rss.arxiv.org/rss/cs.AI", #

    # 📈 Business & Finance
    "business": "https://feeds.bloomberg.com/markets/news.rss",
    "business_wsj": "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml",
    "business_forbes": "https://www.forbes.com/leadership/feed/",

    # 🔬 Science
    "science": "https://www.sciencedaily.com/rss/all.xml",
    "science_nature": "http://www.nature.com/nature/current_issue/rss",
}

TIMEOUT = 12
MAX_ITEMS = 5
_TAGS = re.compile(r"<[^>]+>")


def _clean(text: str, limit: int = 1500) -> str:
    """RSS summaries arrive as HTML. Strip tags, collapse space, then truncate."""
    plain = _TAGS.sub(" ", text or "")
    plain = _html.unescape(plain)
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain[:limit]


def _fetch_feed(url: str, category: str) -> list[dict]:
    """One feed → up to MAX_ITEMS articles. Raises so the caller can say why."""
    import httpx

    r = httpx.get(url, timeout=TIMEOUT, follow_redirects=True,
                  headers={"User-Agent": "aganeti-news/1.0"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    # RSS uses <item>; Atom uses <entry>. Try both so a feed swap doesn't
    # silently return nothing.
    nodes = root.findall(".//item") or root.findall(
        ".//{http://www.w3.org/2005/Atom}entry")
    for item in nodes[:MAX_ITEMS]:
        link = (item.findtext("link") or "").strip()
        if not link:
            href = item.find("{http://www.w3.org/2005/Atom}link")
            link = (href.get("href") if href is not None else "") or ""
        out.append({
            "title": (item.findtext("title")
                      or item.findtext("{http://www.w3.org/2005/Atom}title")
                      or "").strip(),
            "link": link.strip(),
            "summary": _clean(item.findtext("description")
                              or item.findtext("{http://www.w3.org/2005/Atom}summary")
                              or ""),
            "category": category,
        })
    return out


def _build_card(articles: list[dict]) -> str:
    """Master-detail reading card: headlines left, the selected article right.

    Article data crosses into the page as JSON and is rendered with
    `textContent`, never `innerHTML`. That is the whole XSS defence: feed
    content is third-party and arbitrary. (Hermes needed this because their
    frame runs with `allow-same-origin`; ours does not, which is strictly safer
    — but a hostile headline could still rewrite this card's own DOM, so the
    discipline stays.)
    """
    payload = _json.dumps(articles).replace("<", "\\u003c").replace(">", "\\u003e")
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: transparent; color: #E6EBF5; margin: 0; padding: 10px; max-width: 760px; }}
  @media (prefers-color-scheme: light) {{ body {{ color: #1f2328; }} }}
  h3 {{ margin: 0 0 12px; font-size: 16px; }}
  .wrap {{ display: flex; gap: 14px; align-items: stretch; }}
  .master {{ flex: 0 0 230px; max-height: 420px; overflow-y: auto;
             border-right: 1px solid rgba(128,128,128,.25); padding-right: 10px; }}
  .hl {{ display: block; width: 100%; text-align: left; background: transparent;
         border: 0; color: inherit; font: inherit; cursor: pointer;
         padding: 9px; border-radius: 8px; margin-bottom: 5px; line-height: 1.35; }}
  .hl:hover {{ background: rgba(128,128,128,.12); }}
  .hl.active {{ background: rgba(56,219,255,.16); }}
  .hl .k {{ display:block; font-size: 9px; text-transform: uppercase;
            opacity: .65; margin-bottom: 3px; font-weight: 700; }}
  .detail {{ flex: 1; min-width: 0; padding: 4px 2px; }}
  .detail h4 {{ margin: 0 0 8px; font-size: 15px; line-height: 1.35; }}
  .detail p {{ font-size: 13px; line-height: 1.55; opacity: .85; white-space: pre-wrap; }}
  .src {{ display:block; margin-top: 12px; font: 11px/1.4 ui-monospace, Menlo, monospace;
          opacity: .5; user-select: all; word-break: break-all; }}
</style></head>
<body>
  <h3>Latest news</h3>
  <div class="wrap">
    <div class="master" id="master"></div>
    <div class="detail" id="detail"></div>
  </div>
  <script>
    var articles = {payload};
    var master = document.getElementById('master');
    var detail = document.getElementById('detail');

    function show(i) {{
      var a = articles[i];
      if (!a) return;
      var all = document.querySelectorAll('.hl');
      for (var n = 0; n < all.length; n++) all[n].classList.toggle('active', n === i);

      detail.replaceChildren();
      var h = document.createElement('h4');
      h.textContent = a.title || '(untitled)';       // textContent, never innerHTML
      var p = document.createElement('p');
      p.textContent = a.summary || 'No summary provided by the feed.';
      detail.append(h, p);

      // The URL is SHOWN, not linked. An <a target="_blank"> here would be
      // blocked: this frame is sandboxed to allow-scripts only, so a popup gets
      // an opaque origin and the browser refuses it. The assistant cites the
      // link in its reply instead, where it is a real, working link.
      var url = String(a.link || '');
      if (/^https?:\\/\\//i.test(url)) {{
        var s = document.createElement('span');
        s.className = 'src';
        s.textContent = url;
        detail.appendChild(s);
      }}
      reportHeight();
    }}

    for (var i = 0; i < articles.length; i++) {{
      (function (idx) {{
        var a = articles[idx];
        var b = document.createElement('button');
        b.className = 'hl' + (idx === 0 ? ' active' : '');
        b.type = 'button';
        var k = document.createElement('span');
        k.className = 'k';
        k.textContent = a.category || '';
        var t = document.createElement('span');
        t.textContent = a.title || '(untitled)';
        b.append(k, t);
        b.addEventListener('click', function () {{ show(idx); }});
        master.appendChild(b);
      }})(i);
    }}
    if (articles.length) show(0);

    function reportHeight() {{
      try {{
        parent.postMessage({{ type: 'iframe:height',
          height: document.documentElement.scrollHeight }}, '*');
      }} catch (e) {{}}
    }}
    window.addEventListener('load', reportHeight);
    window.addEventListener('resize', reportHeight);
    if (window.ResizeObserver) {{ new ResizeObserver(reportHeight).observe(document.body); }}
    [50, 300, 900].forEach(function (d) {{ setTimeout(reportHeight, d); }});
  </script>
</body></html>"""


def get_news(category: str = "world"):
    """Headlines for a category, or "all" for every feed.

    Returns ``(HTMLResponse, context, meta)`` on success, or a plain string when
    there is nothing to show. Sync: the dispatcher already runs off the loop.
    """
    cat = (category or "world").strip().lower()
    wanted = list(FEEDS) if cat == "all" else [cat if cat in FEEDS else "world"]

    articles: list[dict] = []
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=min(4, len(wanted))) as pool:
        futures = {pool.submit(_fetch_feed, FEEDS[c], c): c for c in wanted}
        for fut, c in futures.items():
            try:
                articles.extend(fut.result())
            except Exception as exc:
                # Reported, not swallowed: a silently missing category looks
                # identical to "there is no news", which is never true.
                log.warning("news feed %s failed: %s", c, exc)
                failed.append(c)

    if not articles:
        detail = f" ({', '.join(failed)} unreachable)" if failed else ""
        return f"⚠️ Couldn't fetch any news right now{detail}."

    headlines = "; ".join(f"{a['title']} <{a['link']}>" for a in articles if a["title"])
    context = (
        f"Latest {'headlines' if cat == 'all' else cat + ' headlines'}: {headlines}. "
        f"The card is already visible to the user — do not describe it. Answer their "
        f"question from these headlines, and when you mention a story, include its URL "
        f"as a markdown link so they can open it (links inside the card cannot be clicked)."
    )
    if failed:
        context += f" NOTE: these feeds were unreachable and are missing: {', '.join(failed)}."

    # Structured links for React to render OUTSIDE the frame. The card cannot
    # carry a working anchor (sandboxed to allow-scripts, so a popup gets an
    # opaque origin and is refused), and the model-cited markdown fallback is
    # measurably unreliable: it produces links when it lists headlines and NONE
    # when it summarises — "what's the top story?" yielded zero links in 2/2
    # trials. A link the user can always click has to come from the payload.
    links = [{"title": a["title"], "url": a["link"], "category": a["category"]}
             for a in articles
             if a.get("title") and (a.get("link") or "").lower().startswith(
                 ("http://", "https://"))]

    return (
        HTMLResponse(content=_build_card(articles), media_type="text/html",
                     headers={"content-disposition": "inline"}),
        context,
        {"category": cat, "count": len(articles), "failed": failed,
         "articles": links},
    )
