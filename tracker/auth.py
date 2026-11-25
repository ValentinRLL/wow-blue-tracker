"""Connexion à l'API Claude : fédération d'identité GitHub (recommandé) ou clé API."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

import anthropic
from anthropic.lib.credentials import WorkloadIdentityCredentials

AUDIENCE = "https://api.anthropic.com"
WIF_VARS = ("ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID", "ANTHROPIC_SERVICE_ACCOUNT_ID")


class AuthConfigError(Exception):
    pass


def _github_oidc_token() -> str:
    """Demande à GitHub un jeton d'identité neuf (valable environ 5 minutes)."""
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    bearer = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not url or not bearer:
        raise AuthConfigError("jeton GitHub indisponible : le job doit avoir la permission id-token: write")
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(
        f"{url}{sep}audience={urllib.parse.quote(AUDIENCE, safe='')}",
        headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            value = json.loads(resp.read()).get("value")
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        raise AuthConfigError("échec de la demande du jeton d'identité GitHub") from None
    if not isinstance(value, str) or not value:
        raise AuthConfigError("réponse inattendue de GitHub pour le jeton d'identité")
    return value


def wif_configured() -> bool:
    return all(os.environ.get(v, "").strip() for v in WIF_VARS)


def make_client() -> anthropic.Anthropic:
    """Fédération si les 3 identifiants sont définis, sinon clé API."""
    common = {"max_retries": 1, "timeout": 120}
    if wif_configured():
        creds = WorkloadIdentityCredentials(
            # Fonction et non fichier : chaque renouvellement repart d'un jeton GitHub neuf.
            identity_token_provider=_github_oidc_token,
            federation_rule_id=os.environ["ANTHROPIC_FEDERATION_RULE_ID"].strip(),
            organization_id=os.environ["ANTHROPIC_ORGANIZATION_ID"].strip(),
            service_account_id=os.environ["ANTHROPIC_SERVICE_ACCOUNT_ID"].strip(),
            workspace_id=os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip() or None,
        )
        return anthropic.Anthropic(credentials=creds, **common)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise AuthConfigError("aucune connexion Claude configurée (fédération GitHub ou ANTHROPIC_API_KEY)")
    return anthropic.Anthropic(api_key=api_key, **common)
