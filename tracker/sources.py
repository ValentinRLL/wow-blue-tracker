"""Lecture des sources Blizzard : blue tracker des forums US et articles de news."""

from __future__ import annotations

import html
import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from bs4 import BeautifulSoup

FORUM_BASE = "https://us.forums.blizzard.com/en/wow"
TRACKER_URL = f"{FORUM_BASE}/groups/blizzard-tracker/posts.json"
USER_AGENT = "wow-blue-tracker/1.0 (+https://github.com)"
TIMEOUT_S = 20
MAX_BODY_BYTES = 3_000_000

# Les liens « View Full Article » ne sont suivis que vers ces hôtes exacts.
NEWS_HOSTS = {"worldofwarcraft.com", "worldofwarcraft.blizzard.com"}
NEWS_PATH_RE = re.compile(r"^/[a-z]{2}-[a-z]{2}/news/\d+(/[\w-]*)?$")


@dataclass
class BluePost:
    id: int
    topic_id: int
    topic_title: str
    category_id: int
    category_name: str
    post_number: int
    author: str
    created_at: str
    url: str
    raw: str
    article_text: str = ""


class SourceError(Exception):
    pass


FORUM_HOSTS = {"us.forums.blizzard.com"}


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    """Suit une redirection seulement vers un hôte autorisé, en https."""

    def __init__(self, allowed_hosts: set[str]):
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urllib.parse.urlsplit(newurl)
        if u.scheme != "https" or u.hostname not in self.allowed_hosts or u.port is not None:
            raise SourceError(f"Redirection refusée vers {u.hostname}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _get(url: str, accept: str = "application/json") -> bytes:
    host = urllib.parse.urlsplit(url).hostname
    allowed = NEWS_HOSTS if host in NEWS_HOSTS else FORUM_HOSTS
    if host not in allowed:
        raise SourceError(f"Hôte non autorisé : {host}")
    opener = urllib.request.build_opener(_CheckedRedirects(allowed))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with opener.open(req, timeout=TIMEOUT_S) as resp:
            data = resp.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise SourceError(f"HTTP {e.code} sur {url}") from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        raise SourceError(f"Réseau indisponible pour {url} : {type(e).__name__}") from None
    if len(data) > MAX_BODY_BYTES:
        raise SourceError(f"Réponse trop grande pour {url}")
    return data


def _get_json(url: str) -> dict:
    try:
        return json.loads(_get(url))
    except json.JSONDecodeError:
        raise SourceError(f"JSON invalide sur {url}") from None


def list_tracker_posts(known_ids: set[int], pending_floor: int | None = None, max_pages: int = 10) -> list[dict]:
    """Renvoie les posts récents du tracker, du plus récent au plus ancien.

    S'arrête dès qu'une page contient un post déjà connu, et seulement après
    avoir atteint `pending_floor`, le plus ancien post encore en attente.
    """
    posts: list[dict] = []
    url = TRACKER_URL
    for _ in range(max_pages):
        page = _get_json(url).get("posts", [])
        if not page:
            break
        posts.extend(page)
        if not known_ids:
            break  # premier passage : une page suffit, tout sera marqué comme lu
        oldest = min(int(p["id"]) for p in page)
        reached_floor = pending_floor is None or oldest <= pending_floor
        if reached_floor and any(int(p["id"]) in known_ids for p in posts):
            break
        url = f"{TRACKER_URL}?before_post_id={oldest}"
    return posts


def load_categories() -> dict[int, str]:
    try:
        site = _get_json(f"{FORUM_BASE}/site.json")
        return {int(c["id"]): str(c["name"]) for c in site.get("categories", [])}
    except (SourceError, KeyError, ValueError, TypeError):
        return {}


def _safe_news_url(href: str) -> str | None:
    try:
        u = urllib.parse.urlsplit(href)
    except ValueError:
        return None
    if u.scheme != "https" or u.hostname not in NEWS_HOSTS or u.port is not None:
        return None
    if not NEWS_PATH_RE.match(u.path):
        return None
    return urllib.parse.urlunsplit(("https", u.hostname, u.path, "", ""))


_LINK_RE = re.compile(r"https://[^\s\"'<>()\[\]]+")


def _candidate_links(text: str) -> list[str]:
    """Liens trouvés dans le post, qu'il soit en HTML ou en Markdown."""
    links = [str(a["href"]) for a in BeautifulSoup(text, "html.parser").find_all("a", href=True)]
    links += _LINK_RE.findall(text)
    return links


def fetch_article_text(text: str, max_chars: int = 12_000) -> str:
    """Si le post renvoie vers un article officiel, récupère le texte de l'article."""
    for href in _candidate_links(text):
        url = _safe_news_url(href)
        if not url:
            continue
        try:
            page = BeautifulSoup(_get(url, accept="text/html"), "html.parser")
        except SourceError:
            return ""
        detail = page.select_one(".detail")
        if not detail:
            return ""
        for tag in detail.find_all(["script", "style", "iframe"]):
            tag.decompose()
        text = detail.get_text("\n", strip=True)
        return text[:max_chars]
    return ""


def fetch_post(summary: dict, categories: dict[int, str]) -> BluePost | None:
    """Récupère le contenu complet d'un post. Renvoie None si le post a disparu."""
    pid = int(summary["id"])
    try:
        full = _get_json(f"{FORUM_BASE}/posts/{pid}.json")
    except SourceError as e:
        if "HTTP 404" in str(e) or "HTTP 403" in str(e):
            return None
        raise
    raw = str(full.get("raw") or "")
    cooked = str(full.get("cooked") or "")
    if not raw:
        raw = BeautifulSoup(cooked, "html.parser").get_text("\n", strip=True)
    cat_id = int(summary.get("category_id") or 0)
    rel_url = str(summary.get("url") or f"/t/{summary.get('topic_id')}/{summary.get('post_number', 1)}")
    if not rel_url.startswith("/"):
        rel_url = "/" + rel_url
    rel_url = urllib.parse.quote(rel_url, safe="/%-._~")
    post = BluePost(
        id=pid,
        topic_id=int(summary.get("topic_id") or 0),
        topic_title=html.unescape(str(summary.get("topic_title") or "")),
        category_id=cat_id,
        category_name=categories.get(cat_id, ""),
        post_number=int(summary.get("post_number") or 1),
        author=str(summary.get("username") or ""),
        created_at=str(summary.get("created_at") or ""),
        url=FORUM_BASE + rel_url,
        raw=raw,
    )
    if "worldofwarcraft" in raw or "worldofwarcraft" in cooked:
        post.article_text = fetch_article_text(raw + "\n" + cooked)
    return post
