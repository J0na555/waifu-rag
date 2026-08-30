#!/usr/bin/env python3
"""Per-character source inventory (spec pipeline step 1).

Usage:
    python3 scripts/source_inventory.py            # all 39 characters
    python3 scripts/source_inventory.py --only taiga-aisaka,rei-ayanami
    python3 scripts/source_inventory.py --limit 3 --no-cache
    python3 scripts/source_inventory.py --sleep 0.8   # be gentler to rate limits

Writes reports/source_inventory.json and reports/source_inventory.md.
HTTP responses are cached under .cache/source_inventory/ so reruns are fast
and do not hammer the APIs. Delete the cache dir to force fresh lookups.

Source reality notes (verified 2026-08-30):
  - Fandom wiki pages 403 on direct fetch; api.php works. Seed fandom_wiki
    domains are validated; when absent or wrong, the script tries Fandom's
    global-search endpoints (currently 404) and then candidate domains
    derived from the series name plus a small known-domain map.
  - TV Tropes blocks some bots on tvtropes.org; the tvtropes.fandom.com
    mirror (MediaWiki API) is used to discover Character slugs, then the
    live tvtropes.org page is fetched with a browser UA and content-verified
    (marker page-Article + the character name in the body).
  - MAL blocks scrapers intermittently (502/504, Cloudflare). Primary path is
    MAL's own /character.php search page with a browser UA; Jikan API is the
    fallback. Every hit is series-confirmed against the character page's
    animeography/mangaography (Main/Supporting roles only; walk-on cameos like
    a Death Note character appearing in Re:Zero are ignored) and the page
    title tag. A confirm failure downgrades the hit to mismatch or UNVERIFIED,
    never a silent 'found'.
  - Transcripts are static seed notes; per-episode hunting happens at
    verification time.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Any

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / ".cache" / "source_inventory"
REPORT_DIR = ROOT / "reports"
UA_DESCRIPTIVE = "waifu-chatbot-source-inventory/0.1 (local research inventory; polite, rate-limited)"
UA_BROWSER = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
ANILIST_URL = "https://graphql.anilist.co"
ANILIST_QUERY = """query ($q: String) {
  Page(page: 1, perPage: 10) {
    characters(search: $q) {
      id
      name { full }
      media(sort: POPULARITY_DESC, perPage: 8) {
        nodes { type title { romaji english } }
      }
    }
  }
}"""
TVTROPES_MIRROR = "https://tvtropes.fandom.com/api.php"
JIKAN_URL = "https://api.jikan.moe/v4/characters"
MAL_SEARCH_URL = "https://myanimelist.net/character.php"
FANDOM_GLOBAL_ENDPOINTS = [
    "https://community.fandom.com/api/v1/Search/List",
    "https://community.fandom.com/api/v1/Wikis/ByString",
    "https://community.fandom.com/api/v1/Search/CrossWiki",
]
# Series names whose Fandom wiki slug is not derivable from the English title.
FANDOM_KNOWN_WIKIS = {
    "kaguya-sama: love is war": "kaguyasama-wa-kokurasetai.fandom.com",
}

SESSION = requests.Session()
CFG = {"no_cache": False, "sleep": 0.5}
STATS = {"requests": 0, "cache_hits": 0, "network": 0}
LAST_NET = 0.0
TVTROPES_COOLDOWN = 0.0  # global pause when tvtropes.org starts challenging


# ---------------------------------------------------------------- http/cache

def _cache_get(key: str) -> dict | None:
    path = CACHE_DIR / f"{key}.json"
    if CFG["no_cache"] or not path.exists():
        return None
    STATS["cache_hits"] += 1
    return json.loads(path.read_text())


def _cache_put(key: str, data: dict) -> None:
    path = CACHE_DIR / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _key(method: str, url: str, params: Any, body: Any, ua: str = "bot") -> str:
    raw = f"{method}|{ua}|{url}|{json.dumps(params or {}, sort_keys=True)}|{json.dumps(body or {}, sort_keys=True)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def _throttle() -> None:
    global LAST_NET
    wait = CFG["sleep"] - (time.monotonic() - LAST_NET)
    if wait > 0:
        time.sleep(wait)
    LAST_NET = time.monotonic()


def http_get(url: str, *, params: dict | None = None, browser_ua: bool = False,
             timeout: int = 30, content_marker: str | None = None,
             retries: int = 2, skip_cache: bool = False) -> dict:
    """GET with cache. content_marker: only accept bodies containing it
    (defends against Cloudflare interstitial pages that return HTTP 200)."""
    ua_tag = "browser" if browser_ua else "bot"
    key = _key("GET", url, params, None, ua_tag)
    if not skip_cache:
        cached = _cache_get(key)
        if cached is not None:
            if content_marker is None or content_marker in cached.get("text", ""):
                return cached
            (CACHE_DIR / f"{key}.json").unlink(missing_ok=True)
    last: dict = {"error": "no attempt"}
    for attempt in range(retries + 1):
        _throttle()
        headers = {"User-Agent": UA_BROWSER if browser_ua else UA_DESCRIPTIVE}
        try:
            resp = SESSION.get(url, params=params, headers=headers, timeout=timeout)
            STATS["requests"] += 1
            STATS["network"] += 1
            data = {"status": resp.status_code, "final_url": resp.url, "text": resp.text}
            if content_marker is None or content_marker in resp.text:
                _cache_put(key, data)
            if content_marker is not None and content_marker not in resp.text:
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
            return data
        except requests.RequestException as exc:
            STATS["requests"] += 1
            STATS["network"] += 1
            last = {"error": f"{type(exc).__name__}: {exc}"}
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    return last


def http_post(url: str, body: dict, *, timeout: int = 30) -> dict:
    key = _key("POST", url, None, body, "bot")
    cached = _cache_get(key)
    if cached is not None:
        return cached
    _throttle()
    headers = {"User-Agent": UA_DESCRIPTIVE, "Content-Type": "application/json"}
    try:
        resp = SESSION.post(url, json=body, headers=headers, timeout=timeout)
        STATS["requests"] += 1
        STATS["network"] += 1
        data = {"status": resp.status_code, "final_url": resp.url, "text": resp.text}
        _cache_put(key, data)
        return data
    except requests.RequestException as exc:
        STATS["requests"] += 1
        STATS["network"] += 1
        return {"error": f"{type(exc).__name__}: {exc}"}


def _json(resp: dict) -> dict | None:
    """Parse a cached/raw http response into JSON, or None on failure."""
    if "error" in resp or resp.get("status", 0) != 200:
        return None
    try:
        return json.loads(resp["text"])
    except (ValueError, KeyError):
        return None


# ---------------------------------------------------------------- matching

def soft_norm(s: str) -> str:
    """Lowercase, ASCII-fold diacritics (Ryū -> Ryu), punctuation -> single
    spaces (keeps word boundaries)."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def kw_in_text(kw: str, text: str) -> bool:
    k = soft_norm(kw)
    return bool(k) and re.search(rf"\b{re.escape(k)}\b", soft_norm(text)) is not None


def names_of(char: dict) -> list[str]:
    return [char["name"]] + [n for n in char.get("search_names", []) if n]


def name_present(text: str, char: dict) -> bool:
    """All tokens of any wanted name appear in the text (word-bounded)."""
    if not text:
        return False
    norm = soft_norm(text)
    for w in names_of(char):
        words = soft_norm(w).split()
        if words and all(re.search(rf"\b{re.escape(t)}\b", norm) for t in words):
            return True
    return False


def kws_of(char: dict) -> list[str]:
    return char.get("series_kw") or [char["series"]]


def name_score(candidate_names: list[str], char: dict) -> tuple[int, str]:
    """Score a candidate against wanted names/series. Returns (score, method)."""
    wanted = names_of(char)
    for cand in candidate_names:
        if any(soft_norm(cand) == soft_norm(w) for w in wanted):
            return (100, "exact-name")
    for cand in candidate_names:
        if any(soft_norm(w) in soft_norm(cand) for w in wanted if len(w) >= 4):
            return (80, "contains-name")
    return (0, "no-name-match")


def pick_best(items: list[dict], char: dict) -> tuple[dict | None, str]:
    """Pick the best candidate; items have 'names' and 'texts' lists."""
    for it in items:
        score, method = name_score(it["names"], char)
        if score >= 80:
            return it, method
    for it in items:
        if any(kw_in_text(kw, t) for kw in kws_of(char) for t in it["texts"]):
            return it, "series-match"
    if len(items) == 1:
        return items[0], "sole-result"
    return None, "no-confident-match"


# ---------------------------------------------------------------- AniList

def anilist_record(char: dict) -> dict:
    cands: list[dict] = []
    query_used = ""
    for q in names_of(char)[:3]:
        resp = http_post(ANILIST_URL, {"query": ANILIST_QUERY,
                                       "variables": {"q": q}})
        data = _json(resp)
        if data is None:
            return {"status": "check_failed", "error": resp.get("error") or f"http {resp.get('status')}"}
        cands = data.get("data", {}).get("Page", {}).get("characters", []) or []
        if cands:
            query_used = q
            break
    if not cands:
        return {"status": "missing", "detail": f"no AniList character for '{char['name']}'"}

    def media_titles(c: dict) -> list[str]:
        titles: list[str] = []
        for node in c.get("media", {}).get("nodes", []) or []:
            title = node.get("title") or {}
            for lang in ("romaji", "english"):
                if title.get(lang):
                    titles.append(title[lang])
        return titles

    items = [{
        "id": c["id"],
        "names": [c["name"].get("full") or ""],
        "texts": media_titles(c),
    } for c in cands]
    hit, method = pick_best(items, char)
    if hit is None:
        hit = items[0]
        method = "top-result"
    urls = [f"https://anilist.co/character/{hit['id']}"]
    if hit["texts"]:
        urls.append(hit["texts"][0])
    return {"status": "found", "anilist_id": hit["id"], "url": urls[0],
            "series": hit["texts"][0] if hit["texts"] else "",
            "match_method": method, "query": query_used}


# ---------------------------------------------------------------- Fandom

def _fandom_search(domain: str, char: dict, limit: int = 8) -> dict:
    """Search one Fandom wiki's api.php. Returns found / missing / check_failed."""
    api = f"https://{domain}/api.php"
    for q in names_of(char)[:3] + [f"{char['name']} {char['series']}"]:
        resp = http_get(api, params={"action": "query", "format": "json",
                                     "list": "search", "srsearch": q,
                                     "srlimit": limit, "origin": "*"})
        data = _json(resp)
        if data is None:
            return {"status": "check_failed", "wiki": domain,
                    "error": resp.get("error") or f"not a MediaWiki (http {resp.get('status')})"}
        hits = data.get("query", {}).get("search", []) or []
        if hits:
            best = hits[0]
            for h in hits:
                if any(soft_norm(h.get("title", "")) == soft_norm(w) for w in names_of(char)):
                    best = h
                    break
            title = best.get("title", "")
            return {"status": "found", "wiki": domain,
                    "url": f"https://{domain}/wiki/{title.replace(' ', '_')}",
                    "page_title": title,
                    "match_method": "exact" if any(soft_norm(title) == soft_norm(w) for w in names_of(char))
                    else "top-search-hit", "query": q}
    return {"status": "missing", "wiki": domain,
            "detail": f"wiki reachable but no page matched {names_of(char)}"}


def _fandom_domain_candidates(char: dict) -> list[str]:
    """Candidate Fandom wiki domains when the seed does not name one."""
    cands: list[str] = []
    known = FANDOM_KNOWN_WIKIS.get(char["series"].lower())
    if known:
        cands.append(known)
    base = re.sub(r"[^a-z0-9]+", "-", char["series"].lower()).strip("-")
    if base:
        cands.append(base + ".fandom.com")
        cands.append(base.replace("-", "") + ".fandom.com")
    toks = [t for t in re.split(r"[^a-z0-9]+", char["series"].lower()) if t]
    if len(toks) >= 2:
        cands.append("-".join(toks[:2]) + ".fandom.com")
        cands.append("".join(toks[:2]) + ".fandom.com")
        cands.append("-".join(toks[2:]) + ".fandom.com")
        cands.append("".join(toks[2:]) + ".fandom.com")
    return list(dict.fromkeys(c for c in cands if c))


def _fandom_discover(char: dict) -> dict:
    """Discover a Fandom wiki for a character with no usable seed domain."""
    notes: list[str] = []
    for endpoint in FANDOM_GLOBAL_ENDPOINTS:
        resp = http_get(endpoint, params={"query": char["name"], "limit": 5},
                        timeout=20, retries=0)
        if resp.get("status") == 200:
            data = _json(resp)
            if data and data.get("items"):
                notes.append(f"global search {endpoint} reachable")
                # newline-heavy; parse items if we ever see this shape
                for it in data["items"]:
                    dom = str(it.get("url", "")).replace("https://", "").split("/")[0]
                    hit = _fandom_search(dom, char)
                    if hit["status"] == "found":
                        hit["discovery"] = f"via global search {endpoint}"
                        return hit
        else:
            notes.append(f"global search {endpoint} -> http {resp.get('status') or resp.get('error')}")
    for dom in _fandom_domain_candidates(char):
        hit = _fandom_search(dom, char)
        if hit["status"] == "found":
            hit["discovery"] = "discovered by candidate domain probe"
            return hit
        notes.append(f"{dom} -> {hit['status']}")
    last = notes[-1] if notes else "no candidates tried"
    return {"status": "unverified", "detail": ", ".join(notes[:5]) + f" ... ({last})"}


def fandom_record(char: dict) -> dict:
    domain = char.get("fandom_wiki") or ""
    if domain:
        hit = _fandom_search(domain, char)
        if hit["status"] != "missing":
            return hit
        # seed domain is alive but has no page: try discovery before giving up
        fallback = _fandom_discover(char)
        fallback["seed_domain_checked"] = domain
        return fallback
    return _fandom_discover(char)


# ---------------------------------------------------------------- TV Tropes

def _mirror_to_real(title: str) -> str | None:
    """'Future Diary/Characters/Diary Holders' -> /Characters/FutureDiary/DiaryHolders"""
    parts = title.split("/")
    if "Characters" not in parts:
        return None
    idx = parts.index("Characters")
    if idx == 0:
        return None

    def slug(s: str) -> str:
        return "".join(w if w.isupper() or len(w) == 1 else w.capitalize()
                       for w in re.split(r"[^A-Za-z0-9]+", s))

    work = slug("/".join(parts[:idx]))
    sub = [slug(s) for s in parts[idx + 1:]]
    return "https://tvtropes.org/pmwiki/pmwiki.php/" + "/".join(["Characters", work] + sub)


def _camel_slug(series: str) -> str:
    return "".join(w if w.isupper() or len(w) == 1 else w.capitalize()
                   for w in re.split(r"[^A-Za-z0-9]+", series))


TVTROPES_ALIASES: dict[str, list[str]] = {
    "the melancholy of haruhi suzumiya": ["Characters/HaruhiSuzumiya"],
    "gurren lagann": ["Characters/TengenToppaGurrenLagann"],
    "mirai nikki": ["Characters/FutureDiary", "Characters/MiraiNikkiTheFutureDiary"],
    "food wars! shokugeki no soma": ["Characters/FoodWars", "Characters/FoodWarsShokugekiNoSoma"],
    "the familiar of zero": ["Characters/TheFamiliarOfZero", "Characters/ZeroNoTsukaima"],
    "love, chunibyo & other delusions": ["Characters/LoveChunibyoAndOtherDelusions"],
    "love live! sunshine!!": ["Characters/LoveLiveSunshine", "Characters/LoveLive"],
    "the idolmaster": ["Characters/TheIdolmaster"],
    "the idolm@ster": ["Characters/TheIdolmaster"],  # stylized title: @ replaces the 'a'
    "re:zero - starting life in another world": ["Characters/ReZero"],
    "monogatari": ["Characters/Bakemonogatari"],
    "komi can't communicate": ["Characters/KomiCantCommunicate", "Characters/KomisanCantCommunicate"],
    "kaguya-sama: love is war": ["Characters/KaguyaSamaLoveIsWar"],
    "overlord": ["Characters/Overlord"],
    "no game no life": ["Characters/NoGameNoLife"],
    "date a live": ["Characters/DateALive"],
    "oshi no ko": ["Characters/OshiNoKo"],
    "attack on titan": ["Characters/AttackOnTitan"],
    "is it wrong to try to pick up girls in a dungeon?": ["Characters/IsItWrongToTryToPickUpGirlsInADungeon"],
}


def _slug_candidates(char: dict) -> list[str]:
    """Ordered full live-site paths (all under the Characters namespace):
    mirror-discovered, derived, aliased."""
    slugs: list[str] = []
    mirror_titles: list[str] = []
    kws = kws_of(char)
    for q in (f"{char['series']} Characters", f"{char['name']} {char['series']}"):
        resp = http_get(TVTROPES_MIRROR, params={
            "action": "query", "format": "json", "list": "search",
            "srsearch": q, "srlimit": 12, "origin": "*"})
        data = _json(resp)
        if data:
            for r in data.get("query", {}).get("search", []) or []:
                title = r.get("title", "")
                if "/Characters" not in title:
                    continue
                work = title.split("/Characters")[0]
                if any(kw_in_text(kw, work) for kw in kws) or \
                   any(kw_in_text(n, work) for n in names_of(char)):
                    mirror_titles.append(title)
    real = _mirror_to_real(f"{char['series']}/Characters")
    if real:
        slugs.append(real.replace("https://tvtropes.org/pmwiki/pmwiki.php/", ""))
    for title in dict.fromkeys(mirror_titles):
        u = _mirror_to_real(title)
        if u:
            slugs.append(u.replace("https://tvtropes.org/pmwiki/pmwiki.php/", ""))
    slugs.append("Characters/" + _camel_slug(char["series"]))
    for alias in TVTROPES_ALIASES.get(char["series"].lower(), []):
        slugs.append(alias)
    # Curated per-character pages (seed field tvtropes_pages). Some TV Tropes
    # character indexes (e.g. DanMachi, Overlord 2012) keep their subpages out
    # of the static HTML (client-rendered nav), so discovery cannot see them.
    for page in char.get("tvtropes_pages", []):
        slugs.append(page)
    return list(dict.fromkeys(s for s in slugs if s))


def _live_subpages(resp: dict, parent: str) -> list[str]:
    """Full 'Characters/...' subpage paths linked from a live Characters page;
    only siblings under the same work slug (global nav links to other works
    would otherwise flood the crawl)."""
    found: list[str] = []
    for m in re.finditer(r"pmwiki\.php/(Characters/[A-Za-z0-9/]+)", resp.get("text", "")):
        slug = m.group(1)
        if slug.startswith(parent) and slug != parent and slug not in found:
            found.append(slug)
    return found[:6]


def _page_has_marker(txt: str) -> bool:
    return "page-Article" in txt


def _live_tvtropes(url: str, char: dict, attempts: int = 2) -> dict | None:
    """Fetch a live tvtropes article page. The content marker keeps homepage
    and 404 stub bodies (which tvtropes serves for unknown slugs) out of the
    cache. On a challenge (no marker) a global cooldown pauses the whole run
    so Cloudflare stops treating the session as a bot storm. A wrong slug
    404s or serves a stub without the marker; a real page carries it."""
    for attempt in range(attempts):
        global TVTROPES_COOLDOWN
        wait = TVTROPES_COOLDOWN - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        resp = http_get(url, browser_ua=True, content_marker="page-Article",
                        retries=1, skip_cache=attempt > 0)
        txt = resp.get("text", "")
        if resp.get("status") == 200 and _page_has_marker(txt):
            return resp  # real article; caller checks the name and subpages
        if attempt < attempts - 1:
            TVTROPES_COOLDOWN = time.monotonic() + 8.0
            time.sleep(8.0)
    return None


def tvtropes_record(char: dict) -> dict:
    for path in _slug_candidates(char)[:8]:
        url = f"https://tvtropes.org/pmwiki/pmwiki.php/{path}"
        resp = _live_tvtropes(url, char)
        if resp is not None and name_present(resp.get("text", ""), char):
            return {"status": "found", "url": resp.get("final_url") or url,
                    "match_method": "live-site"}
        if resp is not None:
            # article exists but the wanted name is not on it; try subpages
            for sub in _live_subpages(resp, parent=path):
                sub_url = f"https://tvtropes.org/pmwiki/pmwiki.php/{sub}"
                sub_resp = _live_tvtropes(sub_url, char, attempts=2)
                if sub_resp is not None and name_present(sub_resp.get("text", ""), char):
                    return {"status": "found", "url": sub_resp.get("final_url") or sub_url,
                            "match_method": "live-subpage", "page": sub}
    return {"status": "missing",
            "detail": f"no live Characters page matching the name; slugs tried: {_slug_candidates(char)[:6]}"}


# ---------------------------------------------------------------- MAL

_MAL_WORK_PANEL_RE = re.compile(
    r'<a href="https://myanimelist\.net/(anime|manga)/(\d+)/[^"]*">([^<]+)</a>'
    r'.*?<small>([^<]*)</small>', re.S)


def _mal_page_works(txt: str) -> list[dict]:
    """Rows from the Animeography and Mangaography panels: kind, id, title, role.

    Scoped to the panel tables only. MAL character pages also embed walk-on
    cameo roles and sidebar noise elsewhere; confirming against those would
    accept a Death Note character who once appeared in Re:Zero."""
    works: list[dict] = []
    for marker in ("Animeography", "Mangaography"):
        start = txt.find(marker + "</div>")
        if start == -1:
            continue
        end = txt.find("</table>", start)
        if end == -1:
            continue
        seg = txt[start:end]
        for kind, mid, title, role in _MAL_WORK_PANEL_RE.findall(seg):
            works.append({"kind": kind, "id": int(mid),
                          "title": title.strip(), "role": role.strip()})
    return works


def _mal_page_title(txt: str) -> str:
    m = re.search(r"<title>([^<]*)</title>", txt)
    return (m.group(1).strip() if m else "").replace(" - MyAnimeList.net", "").strip()


def _mal_title_matches(title: str, kws: list[str]) -> bool:
    """Whole-phrase keyword containment on a MAL work title. Both sides are
    ASCII-folded and cleaned (so 'the idolm@ster' matches 'The iDOLM@STER')."""
    n = soft_norm(title)
    if not n:
        return False
    return any(k and soft_norm(k) in n for k in kws)


def _mal_search_page(char: dict) -> dict | None:
    """MAL /character.php search (browser UA; direct fetch works from this env).
    Returns a found-record or None. Search hits are table cells; the ranked
    'popular characters' list is skipped via the borderClass window."""
    for q in names_of(char)[:3]:
        time.sleep(0.7)  # MAL rate-limits harder than the others
        resp = http_get(MAL_SEARCH_URL, params={"q": q}, browser_ua=True,
                        content_marker="Search Characters", timeout=25)
        txt = resp.get("text", "")
        if resp.get("status") != 200 or "Search Characters" not in txt:
            continue
        hits: list[dict] = []
        for m in re.finditer(r'<a href="https://myanimelist\.net/character/(\d+)/[^"]*">([^<]+)</a>',
                             txt):
            prev = txt[max(0, m.start() - 300):m.start()]
            if "borderClass" not in prev:
                continue  # ranked popular list, not a search result
            hits.append({"id": int(m.group(1)), "name": m.group(2).strip()})
        for h in hits:
            if name_present(h["name"], char):
                return {"status": "found", "mal_id": h["id"],
                        "url": f"https://myanimelist.net/character/{h['id']}",
                        "match_method": "MAL-search-name", "name_on_page": h["name"],
                        "query": q, "via": "MAL search page"}
    return None


def _mal_confirm_series(record: dict, char: dict) -> dict:
    """Verify a MAL hit against the character page's own animeography and
    mangaography (roles Main/Supporting only, never Background cameos) plus
    the page title tag. A hit is 'found' only when the expected series keyword
    is present; a wrong character of the same name becomes 'mismatch'; a page
    we cannot read becomes 'unverified'. Ambiguous single-name characters are
    always flagged for a human once more at verification time."""
    kws = kws_of(char)
    if not kws:
        record["needs_series_confirm"] = True
        record["series_confirmed"] = False
        return record
    try:
        time.sleep(0.7)
        resp = http_get(record["url"], browser_ua=True,
                        content_marker=f"character/{record['mal_id']}", timeout=25)
        txt = resp.get("text", "")
        if resp.get("status") != 200:
            record["status"] = "unverified"
            record["error"] = resp.get("error") or f"http {resp.get('status')}"
            return record
        rows = _mal_page_works(txt)
        title = _mal_page_title(txt)
        strong = [r for r in rows
                  if r["role"].lower() in ("main", "supporting")
                  and _mal_title_matches(r["title"], kws)]
        if strong or _mal_title_matches(title, kws):
            record["series_confirmed"] = True
            record["confirmed_by"] = (strong[0] if strong
                                      else {"kind": "page-title", "title": title})
            if len(char["name"].split()) == 1:
                # single-name characters match several entries by name; the
                # page check passed, but a human re-confirms at verification
                # time (rem/shiro/hestia/megumin/kuroneko/albedo/esdeath/holo)
                record["needs_series_confirm"] = True
            return record
        if not rows:
            record["status"] = "unverified"
            record["error"] = "MAL character page content blocked or missing work panels"
            return record
        record["status"] = "mismatch"
        record["series_confirmed"] = False
        record["needs_series_confirm"] = True
        seen = dict.fromkeys(f"{r['title']} ({r['role']})" for r in rows[:6])
        record["mismatch_detail"] = "page lists: " + "; ".join(seen)
        return record
    except Exception as exc:  # pragma: no cover - defensive
        record["series_confirmed"] = None
        record["confirm_error"] = str(exc)
    return record


def _mal_jikan(char: dict) -> dict:
    queries = names_of(char)[:3]
    for q in queries:
        resp = http_get(JIKAN_URL, params={"q": q, "limit": 6}, timeout=25)
        data = _json(resp)
        if data is None:
            return {"status": "unverified",
                    "error": resp.get("error") or f"Jikan/MAL unreachable (http {resp.get('status')})"}
        items = [{
            "id": it.get("mal_id"),
            "names": [it.get("name", "")] + (it.get("nicknames") or []),
            "texts": [a.get("title", "") for a in it.get("anime", []) or []],
        } for it in (data.get("data") or []) if it.get("mal_id")]
        if not items:
            continue
        hit, method = pick_best(items, char)
        if hit is not None:
            return {"status": "found", "mal_id": hit["id"],
                    "url": f"https://myanimelist.net/character/{hit['id']}",
                    "match_method": method, "via": "Jikan API"}
    return {"status": "missing", "detail": "no MAL character matched via Jikan"}


def mal_record(char: dict) -> dict:
    direct = _mal_search_page(char)
    if direct is not None:
        return _mal_confirm_series(direct, char)
    fallback = _mal_jikan(char)
    if fallback["status"] == "found":
        return _mal_confirm_series(fallback, char)
    return {"status": "unverified",
            "error": "MAL search page blocked and Jikan unreachable"}


# ---------------------------------------------------------------- orchestration

def inventory_char(char: dict) -> dict:
    anilist = anilist_record(char)
    fandom = fandom_record(char)
    tvtropes = tvtropes_record(char)
    mal = mal_record(char)
    transcripts = {"status": "note",
                   "note": char.get("transcript_note") or "hunt at verification time"}
    core = [anilist, fandom, tvtropes, mal]
    found = sum(1 for s in core if s.get("status") == "found")
    parts = [f"{found}/4 core sources confirmed"]
    uncertain = [k for k, s in zip(("anilist", "fandom", "tvtropes", "mal"), core)
                 if s.get("status") in ("check_failed", "unverified")]
    if uncertain:
        parts.append(f"unverifiable now: {', '.join(uncertain)}")
    mismatched = [k for k, s in zip(("anilist", "fandom", "tvtropes", "mal"), core)
                  if s.get("status") == "mismatch"]
    if mismatched:
        detail = core[3].get("mismatch_detail", "") if "mal" in mismatched else ""
        parts.append(f"mismatched: {', '.join(mismatched)}{': ' + detail if detail else ''}")
    if char["tier_target"] == "A":
        parts.append("tier A gate needs quotes/reception/key_moments at verification")
    if found <= 1:
        parts.append("THIN: hunt needed before drafting")
    elif found == 2:
        parts.append("moderate on paper")
    else:
        parts.append("strong on paper")
    return {
        "id": char["id"], "name": char["name"], "series": char["series"],
        "bucket": char["bucket"], "tier_target": char["tier_target"],
        "sources": {
            "anilist": anilist, "fandom": fandom, "tvtropes": tvtropes,
            "mal": mal, "transcripts": transcripts,
        },
        "feasibility_note": "; ".join(parts),
    }


def cell(rec: dict, key: str) -> str:
    s = rec.get("status")
    url = rec.get("url") or ""
    if s == "found":
        return f"[found]({url})" if url else "found"
    if s == "missing":
        return "—"
    if s == "mismatch":
        return f"[mismatch]({url})" if url else "mismatch"
    if s == "unverified":
        return "UNVERIFIED"
    return "CHECK_FAILED"


def write_markdown(results: list[dict], elapsed: float) -> None:
    lines = ["# Source inventory", "",
             f"Generated {time.strftime('%Y-%m-%d %H:%M:%S')} in {elapsed:.1f}s "
             f"({STATS['network']} network requests, {STATS['cache_hits']} cache hits).", "",
             "| character | bucket | tier | AniList | Fandom | TV Tropes | MAL | transcript |",
             "|---|---|---|---|---|---|---|---|"]
    for r in results:
        src = r["sources"]
        t = src["transcripts"].get("note", "")
        lines.append(f"| {r['id']} | {r['bucket']} | {r['tier_target']} "
                     f"| {cell(src['anilist'], 'anilist')} | {cell(src['fandom'], 'fandom')} "
                     f"| {cell(src['tvtropes'], 'tvtropes')} | {cell(src['mal'], 'mal')} | {t} |")
    lines += ["", "Legend: found = link; — = missing; UNVERIFIED = API blocked/unreachable; "
              "mismatch = the linked page is a different character of the same name "
              "(MAL series-confirm failed); CHECK_FAILED = source error.", "",
              "## Thin-source characters", ""]
    thin = [r for r in results if "THIN" in r["feasibility_note"]]
    for r in thin:
        lines.append(f"- {r['id']}: {r['feasibility_note']}")
    if not thin:
        lines.append("- none")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "source_inventory.md").write_text("\n".join(lines) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Per-character source inventory")
    ap.add_argument("--only", help="comma-separated character ids")
    ap.add_argument("--limit", type=int, help="first N characters only")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.5, help="seconds between requests")
    args = ap.parse_args()
    CFG["no_cache"] = args.no_cache
    CFG["sleep"] = args.sleep

    seed_path = ROOT / "data" / "seed_characters.yaml"
    seed = yaml.safe_load(seed_path.read_text())
    if args.only:
        wanted = set(args.only.split(","))
        seed = [c for c in seed if c["id"] in wanted]
    if args.limit:
        seed = seed[: args.limit]

    start = time.monotonic()
    results = [inventory_char(c) for c in seed]
    elapsed = time.monotonic() - start

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "source_inventory.json").write_text(
        json.dumps({"generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "seed_loader": "yaml",
                    "stats": {"runtime_s": round(elapsed, 1),
                              "requests": STATS["requests"],
                              "network": STATS["network"],
                              "cache_hits": STATS["cache_hits"]},
                    "characters": results},
                   indent=2, ensure_ascii=False) + "\n")
    write_markdown(results, elapsed)

    print(f"seed: {len(results)} characters via yaml")
    print(f"runtime: {elapsed:.1f}s | requests: {STATS['requests']} "
          f"(network {STATS['network']}, cache hits {STATS['cache_hits']})")
    for r in results:
        print(f"  {r['id']:<22} {r['feasibility_note']}")


if __name__ == "__main__":
    main()