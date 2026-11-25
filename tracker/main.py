"""Point d'entrée : un passage du tracker (lancé toutes les 15 minutes par GitHub Actions)."""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
import sys
import time
from pathlib import Path

import anthropic

from . import auth, discord, rewriter, sources, state as state_mod

STATE_PATH = Path(os.environ.get("STATE_PATH", "state/seen.json"))


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        value = default
    return max(lo, min(hi, value))


def log(msg: str) -> None:
    print(msg, flush=True)


def _age_hours(iso: str) -> float:
    try:
        created = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds() / 3600


def prefilter(summary: dict, scope: str, categories: dict[int, str]) -> bool:
    """Tri gratuit avant Claude. False = post certainement hors périmètre."""
    if scope == "all":
        return True
    text = " ".join(
        str(x)
        for x in (
            categories.get(int(summary.get("category_id") or 0), ""),
            summary.get("topic_title", ""),
            summary.get("excerpt", ""),
        )
    ).lower()
    if "forever" in text:
        return True
    # Grandes annonces : publiées par le compte officiel des news.
    return scope == "forever_plus_news" and summary.get("username") == "BlizzardEntertainment"


def main() -> int:
    dry_run = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    scope = os.environ.get("TRACKER_SCOPE", "").strip() or "forever_only"
    if scope not in rewriter.SCOPES:
        log("TRACKER_SCOPE inconnu, utilisation de forever_only")
        scope = "forever_only"
    max_posts = _int_env("MAX_POSTS_PER_RUN", 8, 1, 30)
    bootstrap_count = _int_env("BOOTSTRAP_COUNT", 0, 0, 10)
    preview_count = _int_env("PREVIEW_COUNT", 3, 0, 10)
    # Au-delà, on s'arrête proprement avant la limite de temps du job GitHub (20 min).
    deadline = time.monotonic() + _int_env("RUN_BUDGET_SECONDS", 840, 60, 1080)

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not dry_run and not discord.valid_webhook_url(webhook):
        log("ERREUR : le secret DISCORD_WEBHOOK_URL est absent ou mal formé.")
        return 1
    try:
        client = auth.make_client()
    except auth.AuthConfigError as e:
        log(f"ERREUR : {e}.")
        return 1
    log("Connexion Claude : " + ("fédération GitHub (sans clé stockée)" if auth.wif_configured() else "clé API"))
    if dry_run:
        # Aperçu : on vérifie tout de suite l'accès à Claude, même sans post à traiter.
        try:
            client.models.list(limit=1)
        except (anthropic.AnthropicError, auth.AuthConfigError) as e:
            log(f"ERREUR connexion Claude : {type(e).__name__}. Vérifie la règle de fédération.")
            return 1
        log("Connexion Claude vérifiée.")

    st = state_mod.load(STATE_PATH)
    # En mode aperçu, rien n'est enregistré : on peut relancer autant de fois que voulu.
    save = (lambda *_: None) if dry_run else state_mod.save

    try:
        summaries = sources.list_tracker_posts(st.seen_set(), st.pending_floor)
    except sources.SourceError as e:
        log(f"Avertissement : forum injoignable ({e}). Nouvel essai au prochain passage.")
        return 0

    summaries.sort(key=lambda p: int(p["id"]))

    if not st.initialized:
        keep = summaries[-bootstrap_count:] if bootstrap_count else []
        keep_ids = {int(p["id"]) for p in keep}
        for p in summaries:
            if int(p["id"]) not in keep_ids:
                st.mark_seen(int(p["id"]))
        st.initialized = True
        save(STATE_PATH, st)
        log(f"Premier passage : {len(summaries) - len(keep)} posts existants marqués comme lus.")

    seen = st.seen_set()
    unseen = [p for p in summaries if int(p["id"]) not in seen]
    categories = sources.load_categories() if unseen else {}
    if unseen and not categories:
        # Sans les noms de forums, le tri gratuit risquerait d'écarter un vrai post Forever.
        log("Avertissement : liste des forums indisponible. Nouvel essai au prochain passage.")
        return 0
    new = []
    for p in unseen:
        if not prefilter(p, scope, categories):
            # Hors périmètre sans aucun doute : aucun appel payant à Claude.
            st.mark_seen(int(p["id"]))
            continue
        new.append(p)
    pending_all = [int(p["id"]) for p in new]
    save(STATE_PATH, st)
    new = new[:max_posts]
    if dry_run and not new and preview_count:
        # Aperçu : sans nouveau post, on montre les derniers posts pour vérifier le rendu.
        categories = categories or sources.load_categories()
        new = [p for p in summaries if prefilter(p, scope, categories)][-preview_count:]
        log(f"Aperçu des {len(new)} derniers posts (aucun nouveau post).")
    if not new:
        st.pending_floor = None
        save(STATE_PATH, st)
        log("Aucun nouveau post bleu.")
        return 0

    if not dry_run:
        try:
            discord.check_webhook(webhook)
        except discord.DiscordError as e:
            log(f"ERREUR Discord ({e}). Aucun appel à Claude dans ce passage.")
            return 0 if e.retryable else 1

    exit_code = 0

    for summary in new:
        if time.monotonic() > deadline:
            log("Temps du passage écoulé : les posts restants partiront au prochain passage.")
            break
        pid = int(summary["id"])
        try:
            post = sources.fetch_post(summary, categories)
        except sources.SourceError as e:
            log(f"[{pid}] lecture impossible ({e}), nouvel essai plus tard.")
            if st.record_failure(pid):
                log(f"[{pid}] abandonné après plusieurs échecs.")
            save(STATE_PATH, st)
            continue
        if post is None:
            log(f"[{pid}] post supprimé ou privé, ignoré.")
            st.mark_seen(pid)
            save(STATE_PATH, st)
            continue

        try:
            rw = rewriter.rewrite(client, post, scope)
        except rewriter.TransientAPIError as e:
            log(f"Avertissement : Claude indisponible ({e}). Arrêt, nouvel essai au prochain passage.")
            if _age_hours(post.created_at) > 2:
                # Panne qui dure : le workflow échoue pour que GitHub envoie une alerte.
                log(f"[{pid}] en attente depuis plus de 2 heures.")
                exit_code = 1
            break
        except rewriter.FatalAPIError as e:
            if e.post_at_fault:
                log(f"[{pid}] ERREUR Claude ({e}) : requête refusée. Arrêt.")
                if st.record_failure(pid):
                    log(f"[{pid}] abandonné après plusieurs échecs.")
            else:
                log(f"ERREUR Claude ({e}) : accès refusé (fédération ou clé) ou plafond de dépense atteint. Arrêt.")
            exit_code = 1
            break
        except rewriter.RewriteError as e:
            log(f"[{pid}] réécriture impossible ({e}), nouvel essai plus tard.")
            if st.record_failure(pid):
                log(f"[{pid}] abandonné après plusieurs échecs.")
            save(STATE_PATH, st)
            continue

        if not rw.relevant:
            log(f"[{pid}] ignoré : {post.topic_title} ({rw.reason})")
            st.mark_seen(pid)
            save(STATE_PATH, st)
            continue

        if dry_run:
            log(f"[{pid}] APERÇU — {post.topic_title}")
            for chunk in discord.split_message(discord.format_message(post, rw)):
                log("-" * 60)
                log(chunk)
            log("=" * 60)
        else:
            try:
                discord.post_to_discord(webhook, post, rw)
            except discord.DiscordError as e:
                exit_code = 1
                if e.partial:
                    # Une partie est déjà dans le salon : on ne renvoie pas le post.
                    log(f"[{pid}] ERREUR Discord ({e}) : message peut-être incomplet. Arrêt.")
                    st.mark_seen(pid)
                elif e.bad_message:
                    # Discord refuse ce message-là : les posts suivants peuvent passer.
                    log(f"[{pid}] ERREUR Discord ({e}) : message refusé, nouvel essai plus tard.")
                    if st.record_failure(pid):
                        log(f"[{pid}] abandonné après plusieurs échecs.")
                    save(STATE_PATH, st)
                    continue
                else:
                    log(f"ERREUR Discord ({e}). Arrêt, nouvel essai au prochain passage.")
                break
            log(f"[{pid}] publié : {post.topic_title}")
        st.mark_seen(pid)
        save(STATE_PATH, st)

    # Plus ancien post encore en attente : le prochain passage relira les pages jusqu'à lui.
    remaining = [pid for pid in pending_all if pid not in st.seen_set()]
    st.pending_floor = min(remaining) if remaining else None
    save(STATE_PATH, st)
    return exit_code


def run() -> int:
    """Lance un passage. Une erreur imprévue n'affiche que son type, jamais son contenu."""
    # Le journal contient du texte venu des forums : GitHub ne doit y lire aucune commande « :: ».
    token = secrets.token_hex(16)
    print(f"::stop-commands::{token}", flush=True)
    try:
        return main()
    except Exception as e:  # noqa: BLE001
        log(f"ERREUR imprévue : {type(e).__name__}")
        return 1
    finally:
        print(f"::{token}::", flush=True)


if __name__ == "__main__":
    sys.exit(run())
