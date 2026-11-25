import json

import pytest

from tracker import auth, discord, main, rewriter, sources
from tracker.rewriter import Rewrite
from tracker.sources import BluePost

WEBHOOK = "https://discord.com/api/webhooks/123456/abcDEF_-x"


def make_post(pid=1, raw="Fixed a bug."):
    return BluePost(
        id=pid, topic_id=10, topic_title="WoW: Forever Hotfixes", category_id=352,
        category_name="WoW Forever Bug Report", post_number=2, author="Kaivax",
        created_at="2026-11-25T19:42:28.736Z",
        url="https://us.forums.blizzard.com/en/wow/t/x/10/2", raw=raw,
    )


# --- Nettoyage du texte généré ---------------------------------------------

def test_sanitize_neutralizes_mass_mentions():
    out = discord.sanitize("Salut @everyone et @here !")
    assert "@everyone" not in out and "@here" not in out


def test_sanitize_removes_id_mentions():
    out = discord.sanitize("Bonjour <@123> <@!456> <@&789> <#42>")
    assert "<@" not in out and "<#" not in out


def test_sanitize_drops_external_links_and_invites():
    out = discord.sanitize("[Gratuit](https://evil.example/phish) https://evil.example discord.gg/abc")
    assert "evil.example" not in out
    assert "discord.gg" not in out
    assert "Gratuit" in out


def test_sanitize_rejects_lookalike_domains():
    out = discord.sanitize("[x](https://blizzard.com.evil.example/a) https://notblizzard.com/b")
    assert "evil.example" not in out and "notblizzard" not in out


def test_sanitize_keeps_blizzard_links_without_preview():
    out = discord.sanitize(
        "[article](https://worldofwarcraft.blizzard.com/en-us/news/1?utm=x#top) https://us.forums.blizzard.com/en/wow/t/slug/123/4"
    )
    assert "[article](<https://worldofwarcraft.blizzard.com/en-us/news/1>)" in out
    assert "<https://us.forums.blizzard.com/en/wow/t/slug/123/4>" in out


@pytest.mark.parametrize("url", [
    "https://us.forums.blizzard.com/clicks/track?url=https%3A%2F%2Fevil.example",
    "https://us.forums.blizzard.com/en/wow/u/someone",
    "https://worldofwarcraft.blizzard.com/redirect?to=evil",
])
def test_sanitize_rejects_redirect_paths(url):
    assert "[lien retiré]" == discord.sanitize(url)


def test_format_message_has_title_meta_and_source():
    msg = discord.format_message(make_post(), Rewrite(True, "", "WoW Forever", "🛠️ Correctifs", "- Un bug corrigé"))
    assert msg.startswith("# 🛠️ Correctifs\n-# WoW Forever · Kaivax · 25 nov. 2026")
    assert msg.endswith("[post original](<https://us.forums.blizzard.com/en/wow/t/x/10/2>)")


def test_split_message_respects_limit():
    text = "\n".join(f"- ligne numéro {i} " + "x" * 80 for i in range(200)) + "\n" + "y" * 4500
    chunks = discord.split_message(text)
    assert len(chunks) > 1
    assert all(0 < len(c) <= discord.DISCORD_LIMIT for c in chunks)


def test_webhook_url_validation():
    assert discord.valid_webhook_url(WEBHOOK)
    assert not discord.valid_webhook_url("https://evil.example/api/webhooks/1/x")
    assert not discord.valid_webhook_url("http://discord.com/api/webhooks/1/x")
    assert not discord.valid_webhook_url("https://discord.com/api/webhooks/1/x?y=1")


def test_send_payload_disables_all_mentions(monkeypatch):
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        seen["body"] = json.loads(req.data)
        return Resp()

    monkeypatch.setattr(discord.urllib.request, "urlopen", fake_urlopen)
    discord.send(WEBHOOK, "@everyone test")
    assert seen["body"]["allowed_mentions"] == {"parse": []}


# --- Sources ------------------------------------------------------------------

@pytest.mark.parametrize("href,ok", [
    ("https://worldofwarcraft.com/en-us/news/24315647", True),
    ("https://worldofwarcraft.blizzard.com/fr-fr/news/24315647/titre-slug", True),
    ("http://worldofwarcraft.com/en-us/news/1", False),
    ("https://worldofwarcraft.com.evil.example/en-us/news/1", False),
    ("https://worldofwarcraft.com:8443/en-us/news/1", False),
    ("https://evil.example/en-us/news/1", False),
    ("https://worldofwarcraft.com/en-us/account/1", False),
])
def test_safe_news_url(href, ok):
    assert (sources._safe_news_url(href) is not None) == ok


# --- Passage complet ----------------------------------------------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "STATE_PATH", tmp_path / "state" / "seen.json")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    for v in auth.WIF_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("BOOTSTRAP_COUNT", raising=False)
    monkeypatch.setattr(sources, "load_categories", lambda: {352: "WoW Forever Bug Report", 171: "General Discussion"})
    monkeypatch.setattr(sources, "fetch_post", lambda s, c: make_post(int(s["id"])))
    published = []
    monkeypatch.setattr(discord, "post_to_discord", lambda w, p, r: published.append(p.id))
    feed = {"posts": [forever(1), forever(2)]}
    monkeypatch.setattr(discord, "check_webhook", lambda w: None)
    monkeypatch.setattr(sources, "list_tracker_posts", lambda known, floor=None: list(feed["posts"]))
    import anthropic

    # Aperçu : la vérification d'accès à Claude ne doit pas partir sur le réseau pendant les tests.
    monkeypatch.setattr(type(anthropic.Anthropic(api_key="x").models), "list", lambda self, **kw: [])
    return {"published": published, "feed": feed, "state": tmp_path / "state" / "seen.json"}


def forever(pid, title="WoW: Forever Hotfixes"):
    return {"id": pid, "topic_title": title, "category_id": 352}


def relevant(client, post, scope):
    return Rewrite(True, "ok", "WoW Forever", "🛠️ Titre", "Corps")


def test_first_run_posts_nothing_then_new_post_once(env, monkeypatch):
    monkeypatch.setattr(rewriter, "rewrite", relevant)
    assert main.main() == 0
    assert env["published"] == []
    env["feed"]["posts"] = [forever(3), forever(1), forever(2)]
    assert main.main() == 0
    assert main.main() == 0
    assert env["published"] == [3]


def test_irrelevant_post_is_skipped(env, monkeypatch):
    monkeypatch.setattr(rewriter, "rewrite", lambda c, p, s: Rewrite(False, "Midnight", "", "", ""))
    main.main()
    env["feed"]["posts"].append(forever(5))
    main.main()
    assert env["published"] == []
    assert 5 in json.loads(env["state"].read_text())["seen"]


def test_temporary_failure_retries_then_gives_up(env, monkeypatch):
    calls = []

    def failing(c, p, s):
        calls.append(p.id)
        raise rewriter.RewriteError("x")

    monkeypatch.setattr(rewriter, "rewrite", failing)
    main.main()
    env["feed"]["posts"].append(forever(7))
    for _ in range(5):
        main.main()
    assert calls == [7, 7, 7]
    assert env["published"] == []


def test_fatal_api_error_stops_run_and_keeps_post(env, monkeypatch):
    def fatal(c, p, s):
        raise rewriter.FatalAPIError("AuthenticationError")

    monkeypatch.setattr(rewriter, "rewrite", fatal)
    main.main()
    env["feed"]["posts"].append(forever(8))
    assert main.main() == 1
    assert 8 not in json.loads(env["state"].read_text())["seen"]


def test_dry_run_writes_no_state(env, monkeypatch, capsys):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("BOOTSTRAP_COUNT", "1")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL")
    monkeypatch.setattr(rewriter, "rewrite", relevant)
    assert main.main() == 0
    assert not env["state"].exists()
    assert env["published"] == []
    assert "APERÇU" in capsys.readouterr().out


def test_missing_secrets_fail_without_echo(env, monkeypatch, capsys):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://evil.example/hook-secret-value")
    assert main.main() == 1
    assert "hook-secret-value" not in capsys.readouterr().out


# --- Cas ajoutés après la revue de sécurité ------------------------------------

@pytest.mark.parametrize("text", [
    "Clique <https://evil.example/free-mount>",
    "<www.evil.com>",
    "<http://evil.example>",
    "[x](https://evil.com\\@news.blizzard.com/a)",
    "[x](https://user@worldofwarcraft.com/a)",
    "https://worldofwarcraft.com:8443/a",
    "http://worldofwarcraft.com/a",
    "[x](javascript:alert(1))",
    "<https://discord.gg/abc>",
    "ftp://evil.example/x",
    "https://open-redirect.blizzard.com/?to=evil.example",
])
def test_sanitize_blocks_every_url_form(text):
    out = discord.sanitize(text)
    assert "evil" not in out and "discord.gg" not in out and "javascript" not in out
    assert ":8443" not in out and "http://" not in out and "user@" not in out
    assert "open-redirect" not in out


def test_sanitize_strips_placeholder_smuggling():
    out = discord.sanitize("\x000\x00 [a](https://worldofwarcraft.com/x)")
    assert "\x00" not in out


def test_title_fallback_is_sanitized():
    post = make_post()
    post.topic_title = "Free mount https://evil.example @everyone"
    msg = discord.format_message(post, Rewrite(True, "", "t", "", "b"))
    assert "evil" not in msg and "@everyone" not in msg


def test_redirect_to_other_host_is_refused():
    handler = sources._CheckedRedirects(sources.NEWS_HOSTS)
    with pytest.raises(sources.SourceError):
        handler.redirect_request(None, None, 302, "", {}, "https://evil.example/x")


def test_get_refuses_unknown_host():
    with pytest.raises(sources.SourceError):
        sources._get("https://evil.example/x")


def test_unexpected_error_prints_type_only(monkeypatch, capsys):
    def boom():
        raise RuntimeError("https://discord.com/api/webhooks/1/SECRET")

    monkeypatch.setattr(main, "main", boom)
    assert main.run() == 1
    out = capsys.readouterr().out
    assert "SECRET" not in out and "RuntimeError" in out


# --- Cas ajoutés après la revue du code ---------------------------------------

def test_label_of_allowed_link_is_cleaned():
    out = discord.sanitize("[discord.gg/evil https://evil.example](https://worldofwarcraft.com/en-us/news/1)")
    assert "evil" not in out and "discord.gg" not in out
    assert "(<https://worldofwarcraft.com/en-us/news/1>)" in out


def test_article_link_found_in_markdown(monkeypatch):
    calls = []

    def fake_get(url, accept="application/json"):
        calls.append(url)
        return b'<div class="detail"><p>Texte complet</p><script>x</script></div>'

    monkeypatch.setattr(sources, "_get", fake_get)
    text = sources.fetch_article_text("[View Full Article](https://worldofwarcraft.blizzard.com/en-us/news/24245678/slug)")
    assert text == "Texte complet"
    assert calls == ["https://worldofwarcraft.blizzard.com/en-us/news/24245678/slug"]


def test_partial_discord_failure_marks_post_seen(env, monkeypatch):
    monkeypatch.setattr(rewriter, "rewrite", relevant)

    def partial(w, p, r):
        raise discord.DiscordError("HTTP 400", partial=True)

    main.main()
    monkeypatch.setattr(discord, "post_to_discord", partial)
    env["feed"]["posts"].append(forever(9))
    assert main.main() == 1
    assert 9 in json.loads(env["state"].read_text())["seen"]


def test_dry_run_previews_latest_posts_when_nothing_new(env, monkeypatch, capsys):
    monkeypatch.setattr(rewriter, "rewrite", relevant)
    main.main()
    monkeypatch.setenv("DRY_RUN", "true")
    assert main.main() == 0
    assert capsys.readouterr().out.count("APERÇU") == 2
    assert env["published"] == []


class _Block:
    def __init__(self, type_, text=""):
        self.type, self.text = type_, text


class _FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


def _client_returning(msg=None, exc=None):
    class Messages:
        def stream(self, **kw):
            if exc:
                raise exc
            return _FakeStream(msg)

    class Beta:
        messages = Messages()

    class Client:
        beta = Beta()

    return Client()


def test_rewrite_reads_text_after_fallback_block():
    good = json.dumps({"relevant": True, "reason": "", "tag": "t", "title": "T", "body": "B"})
    msg = type("M", (), {"stop_reason": "end_turn", "content": [_Block("text", '{"rel'), _Block("fallback"), _Block("text", good)]})
    rw = rewriter.rewrite(_client_returning(msg), make_post(), "forever_only")
    assert rw.title == "T"


def test_bad_request_is_fatal():
    import anthropic
    import httpx2 as httpx

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.NotFoundError("not found", response=httpx.Response(404, request=req), body=None)
    with pytest.raises(rewriter.FatalAPIError):
        rewriter.rewrite(_client_returning(exc=err), make_post(), "forever_only")


# --- Cas ajoutés après la revue indépendante ---------------------------------

def test_prefilter_skips_other_games_without_claude(env, monkeypatch):
    calls = []
    monkeypatch.setattr(rewriter, "rewrite", lambda c, p, s: calls.append(p.id) or relevant(c, p, s))
    main.main()
    env["feed"]["posts"].append({"id": 20, "topic_title": "Midnight Hotfixes", "category_id": 171})
    env["feed"]["posts"].append(forever(21))
    main.main()
    assert calls == [21]
    assert 20 in json.loads(env["state"].read_text())["seen"]


def test_dead_webhook_stops_before_claude(env, monkeypatch):
    calls = []
    monkeypatch.setattr(rewriter, "rewrite", lambda c, p, s: calls.append(p.id))

    def dead(w):
        raise discord.DiscordError("HTTP 404")

    main.main()
    monkeypatch.setattr(discord, "check_webhook", dead)
    env["feed"]["posts"].append(forever(30))
    assert main.main() == 1
    assert calls == []


def test_discord_400_skips_only_that_post(env, monkeypatch):
    monkeypatch.setattr(rewriter, "rewrite", relevant)
    main.main()

    def picky(w, p, r):
        if p.id == 40:
            raise discord.DiscordError("HTTP 400", bad_message=True)
        env["published"].append(p.id)

    monkeypatch.setattr(discord, "post_to_discord", picky)
    env["feed"]["posts"] += [forever(40), forever(41)]
    main.main()
    assert env["published"] == [41]


def test_run_blocks_workflow_commands(monkeypatch, capsys):
    monkeypatch.setattr(main, "main", lambda: print("::add-mask::x") or 0)
    main.run()
    lines = capsys.readouterr().out.splitlines()
    token = lines[0].removeprefix("::stop-commands::")
    assert len(token) == 32 and lines[-1] == f"::{token}::"


# --- Fédération d'identité ------------------------------------------------------

def test_wif_is_preferred_over_api_key(monkeypatch):
    for v in auth.WIF_VARS:
        monkeypatch.setenv(v, "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-be-used")
    client = auth.make_client()
    assert client.api_key is None


def test_no_credentials_fails_cleanly(env, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert main.main() == 1
    assert "aucune connexion Claude" in capsys.readouterr().out


def test_github_oidc_token_needs_permission(monkeypatch):
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_URL", raising=False)
    with pytest.raises(auth.AuthConfigError):
        auth._github_oidc_token()


def test_github_oidc_token_request(monkeypatch):
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"value": "eyJ.jwt"}'

    def fake_urlopen(req, timeout):
        seen["url"], seen["auth"] = req.full_url, req.headers["Authorization"]
        return Resp()

    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://gh.example/token?api-version=2.0")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "req-token")
    monkeypatch.setattr(auth.urllib.request, "urlopen", fake_urlopen)
    assert auth._github_oidc_token() == "eyJ.jwt"
    assert seen["url"].endswith("&audience=https%3A%2F%2Fapi.anthropic.com")
    assert seen["auth"] == "Bearer req-token"


def test_missing_categories_marks_nothing(env, monkeypatch):
    calls = []
    monkeypatch.setattr(rewriter, "rewrite", lambda c, p, s: calls.append(p.id) or relevant(c, p, s))
    main.main()
    monkeypatch.setattr(sources, "load_categories", lambda: {})
    env["feed"]["posts"].append({"id": 50, "topic_title": "Hotfixes", "category_id": 352})
    assert main.main() == 0
    assert calls == []
    assert 50 not in json.loads(env["state"].read_text())["seen"]


# --- Correctifs de la revue xhigh ------------------------------------------------

def test_transient_api_error_stops_without_counting(env, monkeypatch):
    calls = []

    def down(c, p, s):
        calls.append(p.id)
        raise rewriter.TransientAPIError("OverloadedError")

    monkeypatch.setattr(rewriter, "rewrite", down)
    monkeypatch.setattr(main, "_age_hours", lambda iso: 0.5)
    main.main()
    env["feed"]["posts"] += [forever(60), forever(61)]
    for _ in range(5):
        assert main.main() == 0
    assert calls == [60] * 5  # jamais abandonné, et le passage s'arrête au premier
    data = json.loads(env["state"].read_text())
    assert 60 not in data["seen"] and data["attempts"] == {}
    assert data["pending_floor"] == 60


def test_bad_request_counts_failure_then_gives_up(env, monkeypatch):
    def bad(c, p, s):
        raise rewriter.FatalAPIError("BadRequestError", post_at_fault=True)

    monkeypatch.setattr(rewriter, "rewrite", bad)
    main.main()
    env["feed"]["posts"].append(forever(70))
    for _ in range(3):
        assert main.main() == 1
    assert 70 in json.loads(env["state"].read_text())["seen"]


def test_paging_continues_until_pending_floor(monkeypatch):
    pages = {
        None: [{"id": i} for i in range(120, 100, -1)],
        101: [{"id": i} for i in range(100, 80, -1)],
        81: [{"id": i} for i in range(80, 60, -1)],
    }

    def fake_json(url):
        before = int(url.split("=")[1]) if "before_post_id" in url else None
        return {"posts": pages.get(before, [])}

    monkeypatch.setattr(sources, "_get_json", fake_json)
    known = {119}
    assert len(sources.list_tracker_posts(known)) == 20
    assert len(sources.list_tracker_posts(known, pending_floor=85)) == 40
    assert len(sources.list_tracker_posts(set())) == 20


def test_split_link_is_sanitized_again(monkeypatch):
    sent = []
    monkeypatch.setattr(discord, "send", lambda w, c: sent.append(c))
    monkeypatch.setattr(discord.time, "sleep", lambda s: None)
    long_word = "x" * 1960
    rw = Rewrite(True, "", "t", "T", f"{long_word} https://worldofwarcraft.com/en-us/news/1")
    monkeypatch.setattr(discord, "split_message", lambda text: [text[:1990], text[1990:]])
    discord.post_to_discord(WEBHOOK, make_post(), rw)
    for chunk in sent:
        for url in discord._URL_RE.findall(chunk):
            assert discord._allowed_url(url.strip("<>"))


def test_body_is_capped():
    rw = Rewrite(True, "", "t", "T", "\n".join(["- ligne " + "x" * 90] * 200))
    msg = discord.format_message(make_post(), rw)
    assert len(msg) < discord.MAX_BODY_CHARS + 400
    assert len(discord.split_message(msg)) <= 3


def test_date_is_paris_time():
    post = make_post()
    post.created_at = "2026-11-25T23:30:00Z"
    msg = discord.format_message(post, Rewrite(True, "", "t", "T", "b"))
    assert "26 nov. 2026" in msg


def test_long_input_tells_model_it_was_cut():
    post = make_post(raw="a" * (rewriter.MAX_INPUT_CHARS + 10))
    assert "texte coupé" in rewriter._post_prompt(post)


def test_dry_run_checks_claude_access_first(env, monkeypatch, capsys):
    import anthropic

    def denied(self, **kw):
        raise anthropic.WorkloadIdentityError("x")

    monkeypatch.setattr(type(anthropic.Anthropic(api_key="x").models), "list", denied)
    monkeypatch.setenv("DRY_RUN", "true")
    assert main.main() == 1
    assert "ERREUR connexion Claude : WorkloadIdentityError" in capsys.readouterr().out


def _status_error(cls_name, status, body):
    import anthropic
    import httpx2 as httpx

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    cls = getattr(anthropic, cls_name)
    return cls("err", response=httpx.Response(status, request=req), body=body)


def test_mid_stream_overload_is_transient():
    err = _status_error("APIStatusError", 200, {"type": "error", "error": {"type": "overloaded_error"}})
    with pytest.raises(rewriter.TransientAPIError):
        rewriter.rewrite(_client_returning(exc=err), make_post(), "forever_only")


def test_mid_stream_other_error_is_a_post_failure():
    err = _status_error("APIStatusError", 200, {"type": "error", "error": {"type": "invalid_request_error"}})
    with pytest.raises(rewriter.RewriteError):
        rewriter.rewrite(_client_returning(exc=err), make_post(), "forever_only")


def test_network_cut_mid_stream_is_transient():
    import httpx2 as httpx

    with pytest.raises(rewriter.TransientAPIError):
        rewriter.rewrite(_client_returning(exc=httpx.RemoteProtocolError("cut")), make_post(), "forever_only")


def test_model_not_found_is_config_error_without_penalty():
    err = _status_error("NotFoundError", 404, None)
    with pytest.raises(rewriter.FatalAPIError) as info:
        rewriter.rewrite(_client_returning(exc=err), make_post(), "forever_only")
    assert info.value.post_at_fault is False


def test_long_outage_on_old_post_alerts(env, monkeypatch):
    def down(c, p, s):
        raise rewriter.TransientAPIError("OverloadedError")

    monkeypatch.setattr(rewriter, "rewrite", down)
    main.main()
    env["feed"]["posts"].append(forever(80))
    monkeypatch.setattr(main, "_age_hours", lambda iso: 3.0)
    assert main.main() == 1
    monkeypatch.setattr(main, "_age_hours", lambda iso: 0.5)
    assert main.main() == 0
    assert json.loads(env["state"].read_text())["attempts"] == {}
