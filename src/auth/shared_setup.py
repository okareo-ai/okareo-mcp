"""Setup page for shared connections from clients with short fields.

``GET /oauth/shared/setup`` serves a page where an admin pastes an Okareo API
key and gets the values to type into their OAuth client (research R14). The
page splits the key in the browser and sends only ``header.payload`` — the
claims, which end up in the token URL anyway — to ``POST
/oauth/shared/setup/pack``. The signature, the only secret part, never leaves
the browser; the page shows it as the client secret.

Packing happens server-side so there is one implementation of the layout,
the same one the token route rebuilds from.
"""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from src.auth.compact_key import CompactKeyError, pack
from src.auth.shared_connection import CLIENT_ID, _Diag

# The longest token URL Slack would save in testing was 244 characters;
# other clients with fixed-size fields are likely similar.
LONG_URL_WARNING_AT = 244
_MAX_BODY = 16_384

_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # Inline script and style only; the page may talk to nothing but this
    # server, so a pasted key can't be sent anywhere else.
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
}


def make_pack_route(base_url: str):
    token_base = base_url.rstrip("/") + "/oauth/shared/token/"

    async def _route(request: Request) -> Response:
        diag = _Diag("setup_pack")
        response = await _pack(request, token_base, diag)
        diag.emit(response)
        return response

    return _route


async def _pack(request: Request, token_base: str, diag: _Diag) -> Response:
    body = await request.body()
    if len(body) > _MAX_BODY:
        return diag.fail("too_large", _error(413, "The request is too large."))
    try:
        claims = json.loads(body).get("claims")
    except (ValueError, AttributeError):
        claims = None
    if not isinstance(claims, str):
        return diag.fail("bad_request", _error(400, 'Send {"claims": "<header>.<payload>"}.'))
    try:
        path = pack(claims)
    except CompactKeyError as exc:
        return diag.fail("invalid_key", _error(400, str(exc)))
    token_url = token_base + path
    result = {"token_url": token_url, "token_url_length": len(token_url)}
    if len(token_url) > LONG_URL_WARNING_AT:
        result["warning"] = (
            f"This token URL is {len(token_url)} characters. Slack and some other "
            "clients refuse URLs longer than about 244 characters. Keys are long when "
            "their creator belongs to several organizations; create the key as a user "
            "who belongs only to this one."
        )
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


def _error(status: int, description: str) -> JSONResponse:
    return JSONResponse(
        {"error": "invalid_key", "error_description": description},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


def make_setup_page_route(base_url: str):
    base = base_url.rstrip("/")
    page = (
        _PAGE.replace("__MCP_URL__", base + "/mcp")
        .replace("__AUTHORIZE_URL__", base + "/oauth/shared/authorize")
        .replace("__CLIENT_ID__", CLIENT_ID)
    )

    async def _route(request: Request) -> Response:  # noqa: ARG001
        return HTMLResponse(page, headers=_PAGE_HEADERS)

    return _route


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Okareo shared connection setup</title>
<style>
  :root { --bg:#fff; --fg:#1a1b1e; --muted:#5c5f66; --line:#dee2e6; --box:#f8f9fa; --accent:#5b3fd9; --warn:#b35c00; --bad:#c92a2a; }
  @media (prefers-color-scheme: dark) { :root { --bg:#1a1b1e; --fg:#e9ecef; --muted:#a6a7ab; --line:#373a40; --box:#25262b; --accent:#9775fa; --warn:#ffa94d; --bad:#ff8787; } }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
  main { max-width:760px; margin:0 auto; padding:32px 16px 64px; }
  h1 { font-size:22px; margin:0 0 8px; }
  p { color:var(--muted); margin:0 0 16px; }
  textarea { width:100%; min-height:120px; padding:10px; border:1px solid var(--line); border-radius:6px; background:var(--box); color:var(--fg); font:13px/1.4 ui-monospace,Menlo,monospace; }
  button { background:var(--accent); color:#fff; border:0; border-radius:6px; padding:8px 16px; font-size:14px; cursor:pointer; }
  button.copy { background:transparent; color:var(--accent); border:1px solid var(--line); padding:2px 10px; font-size:12px; }
  .row { border:1px solid var(--line); border-radius:6px; padding:10px 12px; margin:10px 0; }
  .row .label { display:flex; justify-content:space-between; align-items:center; font-weight:600; font-size:13px; }
  .row code { display:block; margin-top:6px; word-break:break-all; font:12px/1.4 ui-monospace,Menlo,monospace; color:var(--fg); }
  .note { font-size:13px; color:var(--muted); }
  .warn { color:var(--warn); }
  .bad { color:var(--bad); }
  [hidden] { display:none; }
</style>
</head>
<body>
<main>
  <h1>Shared connection setup</h1>
  <p>Paste an Okareo API key to get the values for an OAuth client whose fields can't hold a whole key, such as a Slack app's Manual OAuth settings. Everyone the client connects acts as this key.</p>
  <p class="note">Only the first two parts of the key (its claims) are sent to this server. The third part, the secret, stays in this page and is shown below as the client secret.</p>
  <textarea id="key" placeholder="eyJ..." autocomplete="off" spellcheck="false"></textarea>
  <p style="margin-top:12px"><button id="go" type="button">Generate settings</button></p>
  <p id="error" class="bad" hidden></p>
  <section id="out" hidden>
    <div class="row"><div class="label">MCP server URL <button class="copy" data-for="mcp">Copy</button></div><code id="mcp">__MCP_URL__</code></div>
    <div class="row"><div class="label">Client ID <button class="copy" data-for="cid">Copy</button></div><code id="cid">__CLIENT_ID__</code></div>
    <div class="row"><div class="label">Client secret <button class="copy" data-for="secret">Copy</button></div><code id="secret"></code></div>
    <div class="row"><div class="label">Authorization URL <button class="copy" data-for="auth">Copy</button></div><code id="auth">__AUTHORIZE_URL__</code></div>
    <div class="row"><div class="label">Token URL <span id="len" class="note"></span> <button class="copy" data-for="token">Copy</button></div><code id="token"></code></div>
    <p class="note">Turn PKCE on if the client offers it. Leave scopes, identity URL and account identifier empty. HTTP Basic authentication can be on or off.</p>
    <p id="warning" class="warn" hidden></p>
    <p class="note">The token URL contains the key's claims: the organization id and the creating user's id, email and name. They aren't secret and can't be used without the client secret, but they will appear in the client's settings and in this server's request logs. Revoking the key in app.okareo.com disconnects everyone.</p>
  </section>
</main>
<script>
(function () {
  var $ = function (id) { return document.getElementById(id); };
  function fail(msg) { $("error").textContent = msg; $("error").hidden = false; $("out").hidden = true; }
  $("go").addEventListener("click", function () {
    $("error").hidden = true;
    var parts = $("key").value.trim().split(".");
    if (parts.length !== 3 || !parts[2]) { fail("That doesn't look like an Okareo API key (expected three parts separated by dots)."); return; }
    fetch("setup/pack", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ claims: parts[0] + "." + parts[1] })
    }).then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        if (!res.ok) { fail(res.body.error_description || "This key can't be used for a shared connection."); return; }
        $("secret").textContent = parts[2];
        $("token").textContent = res.body.token_url;
        $("len").textContent = "(" + res.body.token_url_length + " characters)";
        $("warning").textContent = res.body.warning || "";
        $("warning").hidden = !res.body.warning;
        $("out").hidden = false;
      })
      .catch(function () { fail("Couldn't reach the server. Try again."); });
  });
  document.addEventListener("click", function (e) {
    var id = e.target && e.target.getAttribute && e.target.getAttribute("data-for");
    if (!id) return;
    navigator.clipboard.writeText($(id).textContent).then(function () {
      var old = e.target.textContent; e.target.textContent = "Copied"; setTimeout(function () { e.target.textContent = old; }, 1200);
    });
  });
})();
</script>
</body>
</html>
"""
