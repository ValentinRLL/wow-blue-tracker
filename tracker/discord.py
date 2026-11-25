"""Mise en forme et envoi des messages vers le webhook Discord."""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from .rewriter import Rewrite
from .sources import USER_AGENT, BluePost

DISCORD_LIMIT = 2000
MAX_BODY_CHARS = 4000  # au plus 3 messages Discord par post
PARIS = ZoneInfo("Europe/Paris")
WEBHOOK_RE = re.compile(r"^https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+$")

# Seuls ces hôtes exacts peuvent apparaître comme lien dans un message.
# Chaque hôte n'accepte que des chemins de contenu : pas de page de redirection ni de suivi de clic.
_LOCALE_PATH = re.compile(r"^/[a-z]{2}-[a-z]{2}(/[\w.%-]+)*/?$")
_FORUM_PATH = re.compile(r"^/[a-z]{2}/wow/t/[\w.%-]+(/\d+){1,2}/?$")
ALLOWED_LINK_HOSTS = {
    "worldofwarcraft.blizzard.com": _LOCALE_PATH,
    "worldofwarcraft.com": _LOCALE_PATH,
    "news.blizzard.com": _LOCALE_PATH,
    "shop.battle.net": _LOCALE_PATH,
    "us.forums.blizzard.com": _FORUM_PATH,
    "eu.forums.blizzard.com": _FORUM_PATH,
}

MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]

_ZW = "\u200b"
_PH = "\x00"  # délimite les liens déjà validés pendant le nettoyage
_MASS_MENTION_RE = re.compile(r"@(everyone|here)", re.IGNORECASE)
_ID_MENTION_RE = re.compile(r"<(@[!&]?|#)(\d+)>")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(\s*<?([^)\s>]+)>?[^)]*\)")
_URL_RE = re.compile(r"<?(?:[a-z][a-z0-9+.-]*://|www\.)[^\s<>()\[\]]+>?", re.IGNORECASE)
_INVITE_RE = re.compile(r"discord(?:app)?\.(?:gg|com/invite)/[\w-]+", re.IGNORECASE)
_PH_RE = re.compile(_PH + r"(\d+)" + _PH)


class DiscordError(Exception):
    def __init__(self, msg: str, partial: bool = False, retryable: bool = False, bad_message: bool = False):
        super().__init__(msg)
        # True si Discord a refusé ce message précis (HTTP 400) : les autres posts peuvent passer.
        self.bad_message = bad_message
        # True si le problème est passager : on réessaiera au prochain passage.
        self.retryable = retryable
        # True si une partie du message est déjà dans le salon : ne pas le renvoyer.
        self.partial = partial


def valid_webhook_url(url: str) -> bool:
    return bool(WEBHOOK_RE.match(url or ""))


def _allowed_url(url: str) -> str | None:
    """Renvoie l'URL si elle pointe vers un hôte autorisé, sinon None."""
    if not url.isascii() or any(c in url for c in "\\<>\"'` "):
        return None
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if u.scheme != "https" or "@" in u.netloc or u.port is not None:
        return None
    host = (u.hostname or "").lower()
    path_re = ALLOWED_LINK_HOSTS.get(host)
    if not path_re or not path_re.match(u.path):
        return None
    # Paramètres et ancre retirés : ils pourraient porter une redirection.
    return urllib.parse.urlunsplit(("https", host, u.path, "", ""))


def sanitize(text: str) -> str:
    """Neutralise mentions, invitations et liens externes dans un texte généré.

    Chaque URL passe par la liste blanche, quelle que soit sa forme.
    """
    text = text.replace("\r", "").replace(_PH, "")
    text = _MASS_MENTION_RE.sub(lambda m: "@" + _ZW + m.group(1), text)
    text = _ID_MENTION_RE.sub("", text)
    kept: list[str] = []

    def _keep(fragment: str) -> str:
        kept.append(fragment)
        return f"{_PH}{len(kept) - 1}{_PH}"

    def _clean_label(label: str) -> str:
        label = _URL_RE.sub("[lien retiré]", label)
        return _INVITE_RE.sub("[lien retiré]", label).replace("[", "").replace("]", "")

    def _md_link(m: re.Match) -> str:
        label, url = _clean_label(m.group(1)), _allowed_url(m.group(2))
        return _keep(f"[{label}](<{url}>)") if url else label

    def _bare(m: re.Match) -> str:
        url = _allowed_url(m.group(0).strip("<>"))
        return _keep(f"<{url}>") if url else "[lien retiré]"

    text = _MD_LINK_RE.sub(_md_link, text)
    text = _URL_RE.sub(_bare, text)
    text = _INVITE_RE.sub("[lien retiré]", text)
    text = _PH_RE.sub(lambda m: kept[int(m.group(1))], text)
    return text.strip()


def _date_fr(iso: str) -> str:
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    d = d.astimezone(PARIS)
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def format_message(post: BluePost, rw: Rewrite) -> str:
    title = sanitize(rw.title).replace("\n", " ").lstrip("# ").strip()
    if not title:
        title = sanitize(post.topic_title).replace("\n", " ").lstrip("# ").strip() or "Post bleu"
    meta = " · ".join(
        x.replace("\n", " ") for x in (sanitize(rw.tag), sanitize(post.author), _date_fr(post.created_at)) if x
    )
    body = sanitize(rw.body)
    if len(body) > MAX_BODY_CHARS:
        cut = body.rfind("\n", 0, MAX_BODY_CHARS)
        body = body[: cut if cut > MAX_BODY_CHARS // 2 else MAX_BODY_CHARS].rstrip() + "\n\n*(…la suite est dans le post original)*"
    source = f"**Source :** [post original](<{post.url}>)"
    return f"# {title}\n-# {meta}\n\n{body}\n\n{source}"


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Découpe en morceaux de moins de `limit` caractères, sur les sauts de ligne."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            cut = line.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:cut])
            line = line[cut:].lstrip()
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        chunks.append(current)
    return [c.strip("\n") for c in chunks if c.strip()]


def check_webhook(webhook_url: str) -> None:
    """Vérifie gratuitement que le webhook existe, avant tout appel payant à Claude."""
    if not valid_webhook_url(webhook_url):
        raise DiscordError("URL de webhook invalide (format non reconnu)")
    req = urllib.request.Request(webhook_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20):
            return
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            raise DiscordError(f"webhook refusé par Discord (HTTP {e.code}) : supprimé ou invalide") from None
        raise DiscordError(f"Discord a répondu HTTP {e.code}", retryable=True) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        raise DiscordError("Discord injoignable", retryable=True) from None


def send(webhook_url: str, content: str, max_retries: int = 4) -> None:
    if not valid_webhook_url(webhook_url):
        raise DiscordError("URL de webhook invalide (format non reconnu)")
    payload = json.dumps(
        {
            "content": content,
            # Aucune mention ne peut notifier qui que ce soit, quoi qu'il y ait dans le texte.
            "allowed_mentions": {"parse": []},
            # SUPPRESS_EMBEDS : pas d'aperçu de lien.
            "flags": 4,
        }
    ).encode()
    for attempt in range(max_retries):
        req = urllib.request.Request(
            webhook_url + "?wait=true",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(req, timeout=20):
                return
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    body = json.loads(e.read() or b"{}")
                    wait = float(body.get("retry_after", 2)) if isinstance(body, dict) else 2.0
                except (ValueError, TypeError, OSError, http.client.HTTPException):
                    wait = 2.0
                time.sleep(min(max(wait, 0.5), 30))
                continue
            if e.code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            # Le message d'erreur ne contient jamais l'URL du webhook.
            raise DiscordError(f"Discord a répondu HTTP {e.code}", bad_message=e.code == 400) from None
        except urllib.error.URLError:
            # Connexion impossible : le message n'est pas parti, on peut réessayer.
            time.sleep(2 * (attempt + 1))
        except (OSError, http.client.HTTPException):
            # Coupure après l'envoi : Discord a peut-être reçu le message. Pas de nouvel essai.
            raise DiscordError("réponse de Discord incertaine") from None
    raise DiscordError("Discord injoignable après plusieurs essais")


def post_to_discord(webhook_url: str, post: BluePost, rw: Rewrite) -> None:
    for i, chunk in enumerate(split_message(format_message(post, rw))):
        # Nouveau nettoyage après découpage : une coupure ne doit jamais créer un lien vers un autre domaine.
        chunk = sanitize(chunk)
        if not chunk:
            continue
        try:
            send(webhook_url, chunk)
        except DiscordError as e:
            raise DiscordError(
                str(e), partial=e.partial or i > 0 or "incertaine" in str(e), bad_message=e.bad_message
            ) from None
        time.sleep(1)
