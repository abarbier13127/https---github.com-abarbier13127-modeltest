#!/usr/bin/env python3
"""
Faux serveur OAuth OpenShift, pour tester `oauth_web_flow.py` sans cluster.

Reproduit l'écran de sélection d'IdP, trois fournisseurs aux formulaires
volontairement différents, l'échange du code et la page /oauth/token/display :

    htpasswd_provider   champs `username` / `password`, CSRF vérifié
    ldap_corp           champs `UserName` / `Password` + champ caché `Domain`
    oidc_keycloak       IdP en deux temps : `email`, puis `password`

Compte de test : jdoe / s3cret.

    python3 tools/mock_oauth.py 8081 &
    python3 oauth_web_flow.py --oauth-url http://127.0.0.1:8081 --list-idp
    echo s3cret | python3 oauth_web_flow.py --oauth-url http://127.0.0.1:8081 \
        -u jdoe --password-stdin --idp ldap_corp

Pour simuler un IdP hébergé sur **une autre origine** (Keycloak séparé), lancer
une seconde instance et la désigner par $MOCK_EXT :

    python3 tools/mock_oauth.py 8082 &
    MOCK_EXT=http://127.0.0.1:8082 python3 tools/mock_oauth.py 8081 &
"""
import http.server, urllib.parse, sys, os

EXT = os.environ.get("MOCK_EXT", "")           # base de l'IdP externe
USER, PW = "jdoe", "s3cret"
TOKEN = "sha256~aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789_abcd"

PAGE = """<html><head><title>{t}</title></head><body>{b}</body></html>"""


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    def log_message(self, *a): pass

    def _send(self, code, body="", headers=None):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    # -- pages ---------------------------------------------------------------
    def _select(self, then):
        return PAGE.format(t="Se connecter", b=f"""<h1>Log in</h1>
<a href="/oauth/authorize?idp=htpasswd_provider&then={then}">htpasswd_provider</a>
<a href="/oauth/authorize?idp=ldap_corp&then={then}">Annuaire d'entreprise</a>
<a href="/oauth/authorize?idp=oidc_keycloak&then={then}">SSO Keycloak</a>""")

    def _ldap(self, then):
        # Noms de champ volontairement différents de username/password.
        return PAGE.format(t="Annuaire", b=f"""<h1>Annuaire d'entreprise</h1>
<form action="/login/ldap_corp" method="POST">
<input type="hidden" name="then" value="{then}">
<input type="hidden" name="csrf" value="t0k3n">
<input type="text" name="Domain" value="CORP">
<input type="text" name="UserName" value="">
<input type="password" name="Password" value="">
<input type="submit" value="Connexion"></form>""")

    def _htpasswd(self, then, err=""):
        return PAGE.format(t="Log in", b=f"""<h1>Log in</h1>{err}
<form action="" method="POST">
<input type="hidden" name="then" value="{then}">
<input type="hidden" name="csrf" value="t0k3n">
<input type="text" name="username" value="">
<input type="password" name="password" value="">
<input type="submit" value="Log in"></form>""")

    def _step1(self, then):
        return PAGE.format(t="Sign in", b=f"""<h1>Sign in</h1>
<form action="/login/oidc_keycloak" method="POST">
<input type="hidden" name="then" value="{then}">
<input type="text" name="email" value="">
<input type="submit" value="Next"></form>""")

    def _step2(self, then, who):
        return PAGE.format(t="Password", b=f"""<h1>Enter password for {who}</h1>
<form action="/login/oidc_keycloak" method="POST">
<input type="hidden" name="then" value="{then}">
<input type="hidden" name="email" value="{who}">
<input type="password" name="password" value="">
<input type="submit" value="Sign in"></form>""")

    # -- routage -------------------------------------------------------------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        ck = self.headers.get("Cookie", "")
        then = urllib.parse.quote(self.path, safe="")

        if u.path == "/oauth/authorize":
            if "ssn=ok" in ck:
                return self._send(302, "", {"Location": q.get(
                    "redirect_uri", ["/oauth/token/display"])[0] + "?code=abc"})
            idp = q.get("idp", [""])[0]
            if not idp:
                return self._send(200, self._select(then))
            if idp == "ldap_corp":
                return self._send(302, "", {"Location": f"/login/ldap_corp?then={then}",
                                            "Set-Cookie": "csrf=t0k3n; Path=/"})
            if idp == "oidc_keycloak":
                if EXT:  # IdP hébergé ailleurs : redirection cross-origine
                    back = urllib.parse.quote(f"{_self(self)}/oauth/authorize?"
                                              + u.query, safe="")
                    return self._send(302, "", {"Location": f"{EXT}/sso/login?back={back}"})
                return self._send(302, "", {"Location": f"/login/oidc_keycloak?then={then}"})
            if idp != "htpasswd_provider":   # inconnu : OpenShift réaffiche le choix
                return self._send(200, self._select(then))
            return self._send(302, "", {"Location": f"/login?then={then}",
                                        "Set-Cookie": "csrf=t0k3n; Path=/"})

        if u.path == "/login/ldap_corp":
            return self._send(200, self._ldap(q.get("then", ["/"])[0]))
        if u.path == "/login/oidc_keycloak":
            return self._send(200, self._step1(q.get("then", ["/"])[0]))
        if u.path.startswith("/login"):
            return self._send(200, self._htpasswd(q.get("then", ["/"])[0]))
        if u.path == "/oauth/token/display":
            if not q.get("code"): return self._send(400, "missing code")
            return self._send(200, PAGE.format(t="Your API token", b=(
                f"<h1>Your API token is</h1><code>{TOKEN}</code>"
                "<p>This token will expire: Sep 4, 2026, 10:12 AM</p>")))

        # --- rôle « IdP externe » (autre origine) ---
        if u.path == "/sso/login":
            back = q.get("back", ["/"])[0]
            return self._send(200, PAGE.format(t="Keycloak", b=f"""<h1>Keycloak</h1>
<form action="/sso/login" method="POST">
<input type="hidden" name="back" value="{back}">
<input type="text" name="username" value="">
<input type="password" name="password" value="">
<input type="submit" value="Sign In"></form>"""))
        return self._send(404, "nope")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        f = urllib.parse.parse_qs(self.rfile.read(n).decode())
        then = urllib.parse.unquote(f.get("then", ["/"])[0])
        one = lambda k: f.get(k, [""])[0]

        if u.path == "/sso/login":                      # IdP externe
            if one("username") != USER or one("password") != PW:
                return self._send(200, PAGE.format(t="Keycloak", b="<p>Invalid credentials</p>"))
            return self._send(302, "", {"Location": urllib.parse.unquote(one("back")),
                                        "Set-Cookie": "ssn=ok; Path=/"})
        if u.path == "/login/ldap_corp":                # champs non standards
            if one("Domain") != "CORP": return self._send(400, "domaine manquant")
            if one("UserName") != USER or one("Password") != PW:
                return self._send(200, self._ldap(f.get("then", ["/"])[0]) +
                                  "<p>Invalid login</p>")
            return self._send(302, "", {"Location": then, "Set-Cookie": "ssn=ok; Path=/"})
        if u.path == "/login/oidc_keycloak":            # deux temps
            if not one("password"):
                if one("email") != USER:
                    return self._send(200, self._step1(f.get("then", ["/"])[0]))
                return self._send(200, self._step2(f.get("then", ["/"])[0], one("email")))
            if one("password") != PW:
                return self._send(200, self._step2(f.get("then", ["/"])[0], one("email")))
            return self._send(302, "", {"Location": then, "Set-Cookie": "ssn=ok; Path=/"})
        if u.path.startswith("/login"):                 # htpasswd
            if one("csrf") != "t0k3n": return self._send(403, "csrf")
            if one("username") != USER or one("password") != PW:
                return self._send(200, self._htpasswd(f.get("then", ["/"])[0],
                                                      "<p>Login failed.</p>"))
            return self._send(302, "", {"Location": then, "Set-Cookie": "ssn=ok; Path=/"})
        return self._send(404, "nope")


def _self(h):
    return "http://" + h.headers.get("Host", "127.0.0.1")


http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
