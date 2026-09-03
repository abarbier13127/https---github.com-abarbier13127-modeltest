#!/usr/bin/env python3
"""
Simulation du parcours **navigateur** d'authentification OpenShift, jusqu'à la
page `/oauth/token/display`.

Différence avec `auth.py` — les deux flux OAuth d'OpenShift ne se ressemblent
pas et ne testent pas la même chose :

  `auth.py`     flux *challenging client* : un seul GET avec un en-tête
                `Authorization: Basic`, le jeton revient dans le fragment d'une
                redirection. C'est ce que fait `oc login -u/-p`. Rapide, mais il
                court-circuite tout ce qui fait l'expérience d'un utilisateur :
                pas de cookie, pas de page de login, pas d'écran de consentement.

  ce module     flux *browser client* (`openshift-browser-client`), exactement
                celui d'un humain devant Firefox :

                    GET  /oauth/authorize?client_id=openshift-browser-client
                                         &response_type=code
                                         &redirect_uri=…/oauth/token/display
                      → 302 vers la page de login de l'IdP  (page HTML)
                    POST <page de login>    identifiant + mot de passe + csrf
                      → 302 /oauth/authorize?…             (retour au flux)
                      → 302 /oauth/authorize/approve?…     (si consentement)
                      → 302 /oauth/token/display?code=…
                    GET  /oauth/token/display              (la page finale)

                Le jeton n'est jamais dans une redirection : c'est le serveur
                OAuth qui échange le `code` côté serveur puis **rend une page
                HTML** contenant le jeton. C'est cette page que l'on récupère.

Ce que ça permet de vérifier, et que le flux challenging ne voit pas : la Route
`oauth-openshift`, son certificat, les cookies de session, le CSRF, la page de
login servie par le template de l'IdP, l'écran de sélection quand plusieurs IdP
sont déclarés, et l'écran de consentement.

Rien n'est présupposé du fournisseur d'identité : le nom passé en `idp` est
repris tel quel (htpasswd, LDAP, OIDC, GitHub…), les champs du formulaire sont
détectés par leur nom plutôt que codés en dur, les IdP en deux temps
(identifiant, puis mot de passe) sont gérés, et les redirections sont suivies
même vers un autre domaine — un IdP externe héberge sa propre page de login.

**Aucune dépendance à `oc`** : uniquement `urllib` (donc l'équivalent exact
d'une série de `curl -c/-b -L`). Stdlib pure, Python 3.9+.
"""

from __future__ import annotations

import html as _html
import http.cookiejar
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

BROWSER_CLIENT_ID = "openshift-browser-client"
DEFAULT_SCOPE = "user:full"
TOKEN_DISPLAY_PATH = "/oauth/token/display"

# Un jeton OpenShift moderne est préfixé `sha256~`. On reste tolérant sur la
# longueur : c'est un repère d'extraction, pas une validation.
TOKEN_RE = re.compile(r"sha256~[A-Za-z0-9_.\-]{16,}")

# Noms de champ « identifiant » rencontrés selon l'IdP : htpasswd et LDAP
# utilisent `username`, mais un OIDC/Keycloak peut exposer `email`, un ADFS
# `UserName`, un annuaire `uid`. On reconnaît le champ par son nom plutôt que
# de le supposer, et `--user-field` reste là pour les cas non prévus.
LOGIN_HINTS = ("user", "login", "email", "mail", "identifier", "ident",
               "account", "uid", "cn", "principal")
TEXTLIKE = ("text", "email", "tel", "", "search")

# Navigateur plausible : certains proxys d'entreprise refusent un UA vide, et
# le serveur OAuth ne sert la page de login qu'à un client qui accepte du HTML.
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class FlowError(RuntimeError):
    """
    Échec du parcours, avec un message directement actionnable.

    `page` porte la dernière page reçue quand il y en a une : c'est elle qu'on
    enregistre avec `--html` pour comprendre un IdP inconnu.
    """

    def __init__(self, message, page=None):
        super().__init__(message)
        self.page = page


# --- analyse HTML -------------------------------------------------------------
class _Parser(HTMLParser):
    """Extrait formulaires, liens et titre. Suffisant pour ces pages-là."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self.links = []
        self.title = ""
        self._form = None
        self._a = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        d = {k.lower(): (v or "") for k, v in attrs}
        if tag == "form":
            self._form = {"action": d.get("action", ""),
                          "method": (d.get("method") or "get").lower(),
                          "fields": []}
            self.forms.append(self._form)
        elif tag in ("input", "button", "select", "textarea") and self._form is not None:
            default_type = "submit" if tag == "button" else "text"
            self._form["fields"].append({
                "name": d.get("name", ""),
                "value": d.get("value", ""),
                "type": (d.get("type") or default_type).lower(),
                "checked": "checked" in d,
            })
        elif tag == "a":
            self._a = {"href": d.get("href", ""), "text": ""}
        elif tag == "title":
            self._in_title = True

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        elif tag == "a" and self._a is not None:
            self._a["text"] = " ".join(self._a["text"].split())
            self.links.append(self._a)
            self._a = None
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._a is not None:
            self._a["text"] += data
        if self._in_title:
            self.title += data


class Page:
    """Une réponse HTTP : ce qu'on en lit, et ce qu'on en déduit."""

    def __init__(self, url: str, status: int, headers: dict, body: str):
        self.url = url
        self.status = status
        self.headers = headers
        self.body = body
        p = _Parser()
        try:
            p.feed(body)
        except Exception:  # noqa: BLE001 — un HTML tordu ne doit pas tout casser
            pass
        self.forms = p.forms
        self.links = p.links
        self.title = " ".join(p.title.split())

    # -- reconnaissance des étapes --------------------------------------------
    @property
    def login_form(self):
        """Formulaire contenant un champ mot de passe."""
        for f in self.forms:
            if any(x["type"] == "password" for x in f["fields"]):
                return f
        return None

    @property
    def user_form(self):
        """
        Formulaire « identifiant seul » : premier écran des IdP en deux temps
        (Keycloak avec `Username` puis `Password`, Azure AD, Google…). On exige
        un nom de champ évocateur pour ne pas confondre avec un champ de
        recherche.
        """
        if self.login_form is not None:
            return None
        for f in self.forms:
            if f["method"] != "post":
                continue
            names = [x["name"].lower() for x in f["fields"]
                     if x["name"] and x["type"] in TEXTLIKE]
            if any(any(h in n for h in LOGIN_HINTS) for n in names):
                return f
        return None

    @property
    def grant_form(self):
        """Écran de consentement : un bouton `approve` (ou une action /approve)."""
        for f in self.forms:
            names = {x["name"] for x in f["fields"]}
            if "approve" in names or "/approve" in f["action"]:
                return f
        return None

    @property
    def is_token_display(self) -> bool:
        return (urllib.parse.urlparse(self.url).path.rstrip("/").endswith(
            TOKEN_DISPLAY_PATH) and self.status == 200)

    @property
    def token(self):
        m = TOKEN_RE.search(self.body)
        return m.group(0) if m else None

    @property
    def idp_links(self):
        """
        Liens de la page de sélection d'IdP.

        Deux formes existent selon la version : `/login/<nom>?then=…` et
        `/oauth/authorize?…&idp=<nom>`. On garde le nom exact du fournisseur,
        car c'est lui — et pas le libellé affiché — qu'attend `--idp`.
        """
        out = []
        for a in self.links:
            href = a["href"]
            name = ""
            q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            if q.get("idp"):
                name = q["idp"][0]
            elif "/login/" in href:
                name = urllib.parse.unquote(
                    href.split("/login/", 1)[1].split("?")[0])
            if name:
                out.append({"href": href, "text": a["text"], "name": name})
        return out

    def text(self, width: int = 88) -> str:
        return html_to_text(self.body, width=width)


# --- rendu texte --------------------------------------------------------------
class _Text(HTMLParser):
    _SKIP = {"script", "style", "head", "svg", "noscript"}
    _BLOCK = {"p", "div", "br", "hr", "tr", "li", "form", "section", "header",
              "footer", "table", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
              "label", "pre", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.out.append("\n")
        if tag == "input":
            d = {k.lower(): (v or "") for k, v in attrs}
            t = (d.get("type") or "text").lower()
            if t == "password":
                self.out.append("[ mot de passe ]")
            elif t in ("submit", "button"):
                self.out.append(f"[ {d.get('value') or 'Envoyer'} ]")
            elif t not in ("hidden",):
                self.out.append(f"[ {d.get('name') or 'champ'} ]")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag in self._BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def html_to_text(body: str, width: int = 88) -> str:
    """Rendu texte lisible d'une page : c'est ce que l'utilisateur aurait vu."""
    p = _Text()
    try:
        p.feed(body)
    except Exception:  # noqa: BLE001
        pass
    raw = _html.unescape("".join(p.out))
    lines = []
    for line in raw.splitlines():
        line = " ".join(line.split())
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        while len(line) > width:
            cut = line.rfind(" ", 0, width)
            cut = cut if cut > 0 else width
            lines.append(line[:cut])
            line = line[cut:].lstrip()
        lines.append(line)
    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


# --- formulaires --------------------------------------------------------------
def form_pairs(form, override=None, check_all=False):
    """
    Champs à poster, sous forme de paires (et non d'un dict) : la page de
    consentement envoie plusieurs fois `scope`, qu'un dict écraserait.

    `override` remplace la valeur d'un champ existant (ou l'ajoute s'il manque),
    ce qui évite de poster deux fois `username`.
    """
    override = dict(override or {})
    pairs = []
    for f in form["fields"]:
        name = f["name"]
        if not name:
            continue
        if f["type"] in ("submit", "button", "reset", "image"):
            continue
        if f["type"] in ("checkbox", "radio") and not (f["checked"] or check_all):
            continue
        if name in override:
            continue
        pairs.append((name, f["value"]))
    for k, v in override.items():
        pairs.append((k, v))
    return pairs


def login_fields(form) -> tuple:
    """
    Devine (champ identifiant, champ mot de passe) d'un formulaire de login.

    Le champ mot de passe est sans ambiguïté (`type=password`). L'identifiant
    est cherché parmi les champs textuels **qui le précèdent** : d'abord un nom
    évocateur (`username`, `email`, `uid`…), sinon le dernier champ textuel
    avant le mot de passe. Cela couvre htpasswd, LDAP, Keycloak et ADFS sans
    rien coder en dur ; `--user-field` / `--password-field` tranchent le reste.
    """
    fields = form["fields"]
    pw, idx = "", None
    for i, f in enumerate(fields):
        if f["type"] == "password" and f["name"]:
            pw, idx = f["name"], i
            break
    scope = fields[:idx] if idx is not None else fields
    text = [f for f in scope if f["type"] in TEXTLIKE and f["name"]]
    hinted = [f for f in text if any(h in f["name"].lower() for h in LOGIN_HINTS)]
    if hinted:
        user = hinted[0]["name"]
    elif text:
        # Le plus proche du mot de passe : dans un formulaire à plusieurs
        # champs, c'est celui qui lui est associé.
        user = text[-1]["name"] if idx is not None else text[0]["name"]
    else:
        user = ""
    return user, pw


def form_url(page: Page, form) -> str:
    """URL de soumission : `action` vide = la page courante (cas du login)."""
    return urllib.parse.urljoin(page.url, form["action"] or page.url)


# --- navigateur ---------------------------------------------------------------
class Browser:
    """Client HTTP à cookies, qui suit les redirections et garde la trace."""

    def __init__(self, insecure: bool = True, timeout: float = 30.0):
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.trace = []  # [(méthode, url, statut, [redirections])]
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self._hops = []
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.HTTPCookieProcessor(self.jar),
            _Redirects(self._hops),
        )

    def open(self, url: str, data=None) -> Page:
        """GET, ou POST si `data` (liste de paires) est fourni."""
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "fr,en;q=0.8",
        }
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers=headers)
        method = "POST" if body is not None else "GET"
        del self._hops[:]
        try:
            r = self.opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            r = e  # 4xx/5xx : c'est une page à lire, pas une exception à fuir
        except ssl.SSLError as e:
            raise FlowError(
                f"erreur TLS sur {url} : {e}. Le certificat de la Route "
                "`oauth-openshift` n'est pas approuvé — utiliser le défaut "
                "(vérification désactivée) ou installer la CA du cluster."
            ) from e
        except OSError as e:
            raise FlowError(f"{url} injoignable : {e}. Vérifier le DNS "
                            "(*.apps.<domaine>), la Route et l'état du pod "
                            "oauth-openshift.") from e
        raw = r.read()
        charset = "utf-8"
        try:
            charset = r.headers.get_content_charset() or "utf-8"
        except Exception:  # noqa: BLE001
            pass
        page = Page(r.geturl(), r.getcode(),
                    {k.lower(): v for k, v in r.headers.items()},
                    raw.decode(charset, errors="replace"))
        self.trace.append((method, url, page.status, list(self._hops)))
        return page

    @property
    def cookies(self):
        return [c.name for c in self.jar]


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Suit les redirections comme un navigateur, mais les consigne."""

    max_redirections = 20

    def __init__(self, sink):
        self.sink = sink

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.sink.append((code, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# --- résolution de l'endpoint -------------------------------------------------
def oauth_base(value: str) -> str:
    """
    Normalise en base du serveur OAuth (`https://oauth-openshift.apps.…`).

    Accepte indifféremment la base, l'endpoint `/oauth/authorize` publié par la
    découverte, ou l'URL de la page `/oauth/token/request`.
    """
    v = value.strip().rstrip("/")
    for suffix in ("/oauth/authorize", "/oauth/token/request",
                   "/oauth/token/display", "/oauth"):
        if v.endswith(suffix):
            return v[: -len(suffix)]
    return v


def guess_oauth_base(url: str):
    """
    Déduit `oauth-openshift.apps.<domaine>` d'une URL du cluster.

    La Route du serveur OAuth s'appelle toujours `oauth-openshift` et vit dans
    le domaine des applications : depuis n'importe quelle URL en `*.apps.…` on
    la reconstruit sans appeler l'API server. Depuis une URL `api.<domaine>`,
    on remplace `api` par `apps` — la convention d'installation par défaut.
    """
    host = urllib.parse.urlparse(url if "//" in url else "//" + url).hostname
    if not host:
        return None
    parts = host.split(".")
    if "apps" in parts:
        return "https://oauth-openshift." + ".".join(parts[parts.index("apps"):])
    if parts[0] == "api" and len(parts) > 1:
        return "https://oauth-openshift.apps." + ".".join(parts[1:])
    return None


def authorize_url(base: str, client_id: str = BROWSER_CLIENT_ID,
                  scope: str = DEFAULT_SCOPE, idp: str = "",
                  state: str = "llmdbench") -> str:
    """URL d'entrée du parcours : celle que le navigateur ouvrirait."""
    base = base.rstrip("/")
    q = [("client_id", client_id),
         ("response_type", "code"),
         ("redirect_uri", base + TOKEN_DISPLAY_PATH),
         ("scope", scope),
         ("state", state)]
    if idp:
        q.append(("idp", idp))
    return base + "/oauth/authorize?" + urllib.parse.urlencode(q)


# --- le parcours --------------------------------------------------------------
def run_flow(base: str, user: str, password: str, *, insecure=True, timeout=30.0,
             client_id=BROWSER_CLIENT_ID, scope=DEFAULT_SCOPE, idp="",
             user_field="", password_field="", extra_fields=None,
             max_steps=16, on_step=None):
    """
    Déroule le parcours jusqu'à `/oauth/token/display` et retourne
    (Page finale, Browser).

    Rien n'est supposé du fournisseur d'identité : `idp` est repris tel quel
    (htpasswd, LDAP, OIDC, GitHub…), les noms des champs du formulaire sont
    détectés (cf. `login_fields`) et les redirections sont suivies même vers un
    autre domaine — cas d'un IdP externe qui héberge sa propre page de login.
    `user_field` / `password_field` / `extra_fields` couvrent les formulaires
    exotiques (champ « domaine », « realm », case à cocher obligatoire…).

    `on_step(label, page, url)` est appelé à chaque étape reconnue — c'est par
    là que la CLI raconte le parcours sans que ce module n'imprime quoi que ce
    soit.
    """
    extra_fields = dict(extra_fields or {})
    br = Browser(insecure=insecure, timeout=timeout)
    start = authorize_url(base, client_id=client_id, scope=scope, idp=idp)
    _notify(on_step, "start", None, url=start)
    page = br.open(start)  # la page atteinte est identifiée par la boucle

    sent_user = sent_password = 0
    for _ in range(max_steps):
        if page.is_token_display or page.token:
            _notify(on_step, "token", page)
            return page, br

        form = page.login_form
        if form is not None:
            if sent_password:
                # Le formulaire de mot de passe revient : c'est un refus.
                raise FlowError(_login_failure(page, user, idp), page)
            uf, pf = login_fields(form)
            uf = user_field or uf
            pf = password_field or pf
            if not pf:
                raise FlowError(
                    f"formulaire de login sans champ mot de passe exploitable "
                    f"sur {page.url} — préciser --password-field", page)
            over = {pf: password}
            # Si l'identifiant a déjà été donné à l'écran précédent (IdP en deux
            # temps), ne pas le réémettre : le champ caché de la page fait foi.
            if uf and not sent_user:
                over[uf] = user
            over.update(extra_fields)
            sent_password += 1
            _notify(on_step, "login", page)
            page = br.open(form_url(page, form), form_pairs(form, over))
            continue

        ident = page.user_form
        if ident is not None:
            if sent_user:
                raise FlowError(
                    f"le fournisseur redemande l'identifiant : « {user} » est "
                    f"probablement inconnu de "
                    + (f"l'IdP « {idp} »" if idp else "cet IdP")
                    + f" ({page.url})", page)
            uf = user_field or login_fields(ident)[0]
            if not uf:
                raise FlowError(f"champ identifiant introuvable sur {page.url} "
                                "— préciser --user-field", page)
            over = {uf: user}
            over.update(extra_fields)
            sent_user += 1
            _notify(on_step, "login_user", page)
            page = br.open(form_url(page, ident), form_pairs(ident, over))
            continue

        grant = page.grant_form
        if grant is not None:
            _notify(on_step, "grant", page)
            page = br.open(form_url(page, grant),
                           form_pairs(grant, {"approve": "Allow selected permissions"},
                                      check_all=True))
            continue

        links = page.idp_links
        if links:
            _notify(on_step, "idp", page)
            try:
                choix = _pick_idp(links, idp)
            except FlowError as e:
                raise FlowError(str(e), page) from None
            page = br.open(urllib.parse.urljoin(page.url, choix["href"]))
            continue

        raise FlowError(_stuck(page, user, idp, sent_password), page)

    raise FlowError(f"parcours non terminé après {max_steps} étapes "
                    f"(dernière page : {page.url})", page)


def _pick_idp(links, idp: str) -> dict:
    """
    Choisit le fournisseur demandé dans la page de sélection.

    Un seul proposé : on le prend. Plusieurs sans `--idp` : on refuse plutôt que
    d'en choisir un au hasard. `--idp` fourni : correspondance exacte sur le nom
    du fournisseur, sinon sur le libellé affiché, sinon on liste les noms
    valides — se tromper de fournisseur produirait un « mot de passe refusé »
    trompeur.
    """
    noms = ", ".join(a["name"] for a in links)
    if idp:
        for a in links:
            if a["name"] == idp:
                return a
        for a in links:
            if idp.lower() in (a["name"].lower(), a["text"].lower()):
                return a
        raise FlowError(
            f"fournisseur d'identité « {idp} » absent de la page de "
            f"sélection. Déclarés sur ce cluster : {noms}")
    if len(links) == 1:
        return links[0]
    raise FlowError(
        f"{len(links)} fournisseurs d'identité déclarés sur ce cluster : "
        f"{noms}. Préciser lequel utiliser, par exemple --idp {links[-1]['name']}")


def list_idps(base: str, *, insecure=True, timeout=30.0,
              client_id=BROWSER_CLIENT_ID, scope=DEFAULT_SCOPE) -> list:
    """
    Fournisseurs d'identité proposés par le cluster, sans aucun identifiant.

    Une liste vide signifie qu'un seul IdP est déclaré : OpenShift redirige
    alors directement vers sa page de login, sans écran de sélection.
    """
    br = Browser(insecure=insecure, timeout=timeout)
    page = br.open(authorize_url(base, client_id=client_id, scope=scope))
    return page.idp_links


def _notify(cb, label, page, url=None):
    """`page` vaut None pour l'étape « start » : la requête n'est pas partie."""
    if cb:
        cb(label, page, url)


def _login_failure(page: Page, user: str, idp: str = "") -> str:
    """
    Message de refus. Le détail vient du serveur lui-même : selon l'IdP c'est
    « Login failed », « Invalid credentials » ou un texte localisé, et le
    recopier vaut mieux que de le paraphraser.
    """
    detail = ""
    for line in page.text().splitlines():
        if re.search(r"invalid|failed|incorrect|error|échou|refus|erreur",
                     line, re.I):
            detail = line.strip()
            break
    ou = f"l'IdP « {idp} »" if idp else "le fournisseur d'identité"
    return (f"identifiants refusés pour « {user} »"
            + (f" — le serveur répond : « {detail} »" if detail else "")
            + f". Vérifier le mot de passe et que le compte existe bien dans "
              f"{ou} (pour un IdP htpasswd : le secret htpass-secret).")


def _stuck(page: Page, user="", idp="", sent_password=0) -> str:
    if page.status >= 400:
        extrait = " ".join(page.text().split())[:200]
        return (f"HTTP {page.status} sur {page.url}"
                + (f" — {extrait}" if extrait else ""))
    if sent_password and re.search(r"invalid|failed|incorrect|denied|échou|"
                                   r"refus|erreur", page.body, re.I):
        # Certains IdP externes ne réaffichent pas le formulaire après un
        # refus : sans ce cas, l'échec serait rapporté comme « page inattendue ».
        return _login_failure(page, user, idp)
    return (f"page inattendue à l'étape courante : {page.url} "
            f"(HTTP {page.status}, titre « {page.title or '?'} ») — ni "
            "formulaire de login, ni écran identifiant, ni consentement, ni "
            "page de jeton. Cet IdP a probablement un formulaire que la "
            "détection ne reconnaît pas : relancer avec --verbose --html "
            "page.html pour voir ce qui a été reçu, puis forcer les champs "
            "avec --user-field / --password-field / --field.")


def token_details(page: Page) -> dict:
    """Jeton et informations affichées par la page finale."""
    txt = page.text()
    out = {"token": page.token, "expires": None, "url": page.url,
           "title": page.title}
    for line in txt.splitlines():
        if re.search(r"expire|expiration|valid", line, re.I):
            out["expires"] = " ".join(line.split())
            break
    return out


def whoami(api_server: str, token: str, insecure=True, timeout=30.0) -> dict:
    """
    Confirme que le jeton obtenu est réellement utilisable, sans `oc` :
    GET /apis/user.openshift.io/v1/users/~ avec l'en-tête Bearer.
    """
    import json
    url = api_server.rstrip("/") + "/apis/user.openshift.io/v1/users/~"
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise FlowError(f"jeton refusé par l'API server (HTTP {e.code})") from e
    except OSError as e:
        raise FlowError(f"API server injoignable ({url}) : {e}") from e
    return {"name": data.get("metadata", {}).get("name"),
            "groups": data.get("groups", []),
            "uid": data.get("metadata", {}).get("uid")}
