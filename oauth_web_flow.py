#!/usr/bin/env python3
"""
Simule l'authentification d'un utilisateur sur OpenShift **comme un navigateur**,
et rend la page `/oauth/token/display` obtenue en sortie.

Le parcours est exactement celui d'un humain qui ouvre l'URL de demande de
jeton : sélection du fournisseur d'identité, formulaire de login servi par
l'IdP, cookies de session, jeton CSRF, éventuel écran de consentement, puis la
page HTML finale qui affiche le jeton.

    GET  /oauth/authorize?client_id=openshift-browser-client
                         &response_type=code&redirect_uri=…/oauth/token/display
      → écran de choix de l'IdP, puis 302 vers sa page de login
    POST <page de login>                 identifiant + mot de passe + csrf + cookie
      → 302 /oauth/authorize?…           retour au flux
      → 302 /oauth/token/display?code=…  le serveur échange le code
    GET  /oauth/token/display            ← la page rendue ci-dessous

**Aucun appel à `oc`** : uniquement des requêtes HTTP (l'équivalent d'une série
de `curl -c cookies -b cookies -L`), stdlib pure. Le KUBECONFIG courant n'est
jamais touché.

Aucun fournisseur d'identité n'est présupposé. `--idp` prend le nom déclaré
dans `oauth/cluster` (htpasswd, LDAP, OIDC/Keycloak, GitHub…), `--list-idp`
affiche ceux que le cluster propose, les noms des champs du formulaire sont
détectés (`username`, `email`, `UserName`, `uid`…), les IdP en deux temps
(identifiant puis mot de passe) sont gérés, et les redirections sont suivies
même vers un autre domaine — cas d'un IdP externe qui héberge sa page de login.
`--user-field`, `--password-field` et `--field` couvrent les formulaires que la
détection ne reconnaîtrait pas.

**Seul le serveur OAuth est contacté** (plus, le cas échéant, l'IdP externe vers
lequel il redirige). L'API server n'est appelé que dans deux cas, tous deux
explicites : `--api-server` sans `--oauth-url`, pour découvrir l'endpoint via
`/.well-known/oauth-authorization-server` ; et `--check`, qui valide le jeton
obtenu par un `GET /apis/user.openshift.io/v1/users/~`. Fournir `--oauth-url`
suffit à garantir qu'aucune requête ne part ailleurs que vers l'OAuth. Les
adresses IP réellement contactées sont affichées à chaque exécution.

À ne pas confondre avec `--user/--password` des tests t0x : ceux-là utilisent le
flux *challenging client* (un GET avec en-tête Basic), qui ne passe ni par la
page de login ni par les cookies. Ce script-ci teste la chaîne complète —
Route `oauth-openshift`, certificat, template de login, session, consentement.

Usage :
    python3 oauth_web_flow.py --list-idp            # ce que le cluster propose
    python3 oauth_web_flow.py                       # demande login et mot de passe
    python3 oauth_web_flow.py -u user --idp htpasswd_provider
    echo 'motdepasse' | python3 oauth_web_flow.py -u user --password-stdin \
        --idp ldap_provider
    python3 oauth_web_flow.py -u jdoe --idp mon-oidc \
        --user-field email --field realm=corp        # IdP exotique
    python3 oauth_web_flow.py -u user --verbose --html out/token-display.html
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from llmdbench import auth, config, oauthweb, report  # noqa: E402


def erreur(msg) -> None:
    """Message d'échec sur stderr, sans désordonner la sortie déjà écrite."""
    sys.stdout.flush()
    print(f"  ❌ {msg}", file=sys.stderr)
    sys.stderr.flush()

ETAPES = {
    "start": "ouverture de /oauth/authorize",
    "login_user": "écran identifiant (IdP en deux temps)",
    "idp": "sélection du fournisseur d'identité",
    "login": "page de login servie",
    "grant": "écran de consentement",
    "token": "page /oauth/token/display",
}


def show_peers(br, quiet=False) -> list:
    """
    Affiche les adresses réellement contactées : hôte → IP:port.

    C'est le pair au bout de la socket, pas une résolution DNS refaite après
    coup — derrière une Route OpenShift plusieurs routeurs peuvent répondre, et
    c'est celui qui a servi la requête qui intéresse.
    """
    peers = getattr(br, "peers", []) if br else []
    if not peers:
        return []
    print("\n-- adresses contactées --")
    for p in peers:
        tls = "TLS" if p["tls"] else "clair"
        print(f"  {p['host']}  →  {p['ip']}:{p['port']}  ({tls})")
        if not quiet:
            tous = br.dns_records(p["host"], p["port"])
            autres = [a for a in tous if a != p["ip"]]
            if autres:
                print(f"      autres adresses publiées par le DNS : "
                      f"{', '.join(autres)}")
    return peers


def _write_html(dest: str, body: str) -> str:
    path = os.path.expanduser(dest)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    print(f"  → HTML écrit : {path}")
    return path


def _split_field(spec: str) -> tuple:
    if "=" not in spec:
        raise ValueError(f"--field attend NOM=VALEUR, reçu {spec!r}")
    name, value = spec.split("=", 1)
    if not name:
        raise ValueError(f"--field sans nom de champ : {spec!r}")
    return name, value


def list_idp(args) -> int:
    """`--list-idp` : ce que le cluster propose, sans fournir d'identifiants."""
    br = None
    try:
        base, origine = resolve_base(args)
        br = oauthweb.Browser(insecure=args.insecure, timeout=args.timeout)
        links = oauthweb.list_idps(base, insecure=args.insecure,
                                   timeout=args.timeout,
                                   client_id=args.client_id, scope=args.scope,
                                   browser=br)
    except (oauthweb.FlowError, auth.AuthError) as e:
        show_peers(br, quiet=True)
        erreur(f"{e}")
        return report.EXIT_INCONCLUSIVE
    print(f"\n  serveur OAuth : {base}   ({origine})")
    if not links:
        print("  → un seul fournisseur déclaré : pas d'écran de sélection, "
              "--idp est inutile")
        return report.EXIT_OK
    print(f"  → {len(links)} fournisseur(s) — la valeur à passer à --idp est "
          "celle de la colonne « nom » :\n")
    for a in links:
        libelle = a["text"] or "-"
        print(f"      --idp {a['name']:<24} (libellé affiché : {libelle})")
    show_peers(br, quiet=args.quiet)
    return report.EXIT_OK


def resolve_base(args) -> tuple:
    """Base du serveur OAuth + comment on l'a trouvée. Sans `oc`."""
    if args.oauth_url:
        return oauthweb.oauth_base(args.oauth_url), "--oauth-url"
    if args.api_server:
        # Découverte publiée par l'API server, non authentifiée : c'est du HTTP,
        # pas `oc whoami --show-server`.
        ep = auth.discover_authorize_endpoint(args.api_server,
                                              insecure=args.insecure,
                                              timeout=args.timeout)
        return oauthweb.oauth_base(ep), f"découverte OAuth sur {args.api_server}"
    guess = oauthweb.guess_oauth_base(args.url)
    if guess:
        return guess, f"déduit de {args.url}"
    raise oauthweb.FlowError(
        "impossible de déterminer le serveur OAuth : fournir --oauth-url "
        "(https://oauth-openshift.apps.<domaine>) ou --api-server")


def frame(title: str, body: str, width: int = 92) -> str:
    """Encadre le rendu de la page pour qu'on voie où elle commence et finit."""
    bar = "─" * width
    out = [f"┌─ {title} " + "─" * max(0, width - len(title) - 3) , ""]
    out += ["  " + line for line in body.splitlines()]
    out += ["", "└" + bar]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Simule le parcours navigateur d'authentification OpenShift "
                    "et rend la page /oauth/token/display",
        formatter_class=argparse.RawDescriptionHelpFormatter)

    s = ap.add_argument_group("serveur OAuth (aucun `oc` requis)")
    s.add_argument("--oauth-url", default=os.environ.get("LLMD_OAUTH_URL") or None,
                   help="base du serveur OAuth, ex. "
                        "https://oauth-openshift.apps.ds.alf.corp")
    s.add_argument("--api-server", default=os.environ.get("LLMD_API_SERVER") or None,
                   help="API server, pour la découverte "
                        "/.well-known/oauth-authorization-server")
    s.add_argument("--url", default=config.DEFAULT_URL,
                   help=f"URL quelconque du cluster, dont la base OAuth est "
                        f"déduite (défaut: {config.DEFAULT_URL})")

    i = ap.add_argument_group("identité")
    i.add_argument("-u", "--user", help="utilisateur ; demandé si absent")
    i.add_argument("--password", help="mot de passe (déconseillé : visible dans "
                                      "`ps` ; préférer --password-stdin)")
    i.add_argument("--password-stdin", action="store_true",
                   help="lit le mot de passe sur l'entrée standard")
    i.add_argument("--idp", default=os.environ.get("LLMD_IDP", ""),
                   help="nom du fournisseur d'identité tel que déclaré dans "
                        "`oauth/cluster` (htpasswd_provider, ldap, mon-oidc…) ; "
                        "obligatoire si le cluster en déclare plusieurs ; "
                        "défaut $LLMD_IDP")
    i.add_argument("--list-idp", action="store_true",
                   help="liste les fournisseurs proposés puis s'arrête "
                        "(aucun identifiant requis)")
    i.add_argument("--user-field", default="",
                   help="nom du champ identifiant du formulaire, si la "
                        "détection automatique échoue (ex. UserName pour ADFS)")
    i.add_argument("--password-field", default="",
                   help="nom du champ mot de passe, si la détection échoue")
    i.add_argument("--field", action="append", default=[], metavar="NOM=VALEUR",
                   help="champ supplémentaire à poster (domaine, realm, case à "
                        "cocher…) ; répétable")

    f = ap.add_argument_group("flux")
    f.add_argument("--client-id", default=oauthweb.BROWSER_CLIENT_ID,
                   help=f"client OAuth (défaut: {oauthweb.BROWSER_CLIENT_ID})")
    f.add_argument("--scope", default=oauthweb.DEFAULT_SCOPE,
                   help=f"scope demandé (défaut: {oauthweb.DEFAULT_SCOPE})")
    f.add_argument("--secure", dest="insecure", action="store_false",
                   default=config.DEFAULT_INSECURE,
                   help="vérifier le certificat TLS (défaut: ignoré)")
    f.add_argument("--timeout", type=float, default=30.0,
                   help="timeout HTTP par requête, en secondes")
    f.add_argument("--check", action="store_true",
                   help="vérifier le jeton obtenu auprès de l'API server "
                        "(GET /apis/user.openshift.io/v1/users/~). C'est le "
                        "SEUL appel hors du serveur OAuth, et il faut le "
                        "demander explicitement ; il exige --api-server")

    o = ap.add_argument_group("sortie")
    o.add_argument("--quiet", action="store_true",
                   help="sortie condensée : n'interroge pas le DNS pour "
                        "signaler les autres adresses de l'hôte")
    o.add_argument("--verbose", action="store_true",
                   help="détaille chaque requête et chaque redirection")
    o.add_argument("--html", metavar="FICHIER",
                   help="enregistre le HTML brut de la page finale")
    o.add_argument("--open", dest="open_browser", action="store_true",
                   help="ouvre la page enregistrée dans le navigateur")
    o.add_argument("--json", metavar="FICHIER", help="écrit le résultat en JSON")
    o.add_argument("--mask", action="store_true",
                   help="masque le jeton dans la sortie texte")
    o.add_argument("--width", type=int, default=88,
                   help="largeur du rendu texte (défaut 88)")
    args = ap.parse_args()

    # -- identité ------------------------------------------------------------
    if args.list_idp:
        return list_idp(args)

    if not args.user:
        if not sys.stdin.isatty():
            erreur("aucun utilisateur fourni et pas de terminal pour le "
                   "demander : utiliser -u/--user")
            return report.EXIT_INCONCLUSIVE
        args.user = input("Utilisateur OpenShift : ").strip()
        if not args.user:
            erreur("utilisateur vide")
            return report.EXIT_INCONCLUSIVE

    try:
        extra_fields = dict(_split_field(f) for f in args.field)
    except ValueError as e:
        erreur(e)
        return report.EXIT_INCONCLUSIVE

    try:
        base, origine = resolve_base(args)
    except (oauthweb.FlowError, auth.AuthError) as e:
        erreur(f"{e}")
        return report.EXIT_INCONCLUSIVE

    print(f"\n=== Parcours navigateur OAuth — /oauth/token/display ===")
    print(f"  serveur OAuth : {base}   ({origine})")
    print(f"  utilisateur   : {args.user}")
    print(f"  client        : {args.client_id}   scope={args.scope}")
    if args.idp:
        print(f"  IdP           : {args.idp}")
    print(f"  TLS           : {'non vérifié' if args.insecure else 'vérifié'}")
    if args.check and args.api_server:
        appels = f"serveur OAuth, puis {args.api_server} pour --check"
    elif args.check:
        appels = "serveur OAuth uniquement (--check inopérant sans --api-server)"
    else:
        appels = "serveur OAuth uniquement"
    print(f"  appels        : {appels}")
    print()

    try:
        password = auth.read_password(args)
    except auth.AuthError as e:
        erreur(f"{e}")
        return report.EXIT_INCONCLUSIVE

    # -- parcours ------------------------------------------------------------
    etapes = []

    def on_step(label, page, url=None):
        if page is None:  # étape « start » : la requête n'est pas encore partie
            etapes.append({"etape": label, "url": url})
            print(f"  → {ETAPES.get(label, label):<38}           {url}")
            return
        etapes.append({"etape": label, "url": page.url, "status": page.status,
                       "titre": page.title})
        print(f"  → {ETAPES.get(label, label):<38} HTTP {page.status}  {page.url}")

    br = oauthweb.Browser(insecure=args.insecure, timeout=args.timeout)
    try:
        page, br = oauthweb.run_flow(
            base, args.user, password, browser=br,
            insecure=args.insecure, timeout=args.timeout,
            client_id=args.client_id, scope=args.scope, idp=args.idp,
            user_field=args.user_field, password_field=args.password_field,
            extra_fields=extra_fields, on_step=on_step)
    except oauthweb.FlowError as e:
        show_peers(br, quiet=args.quiet)
        print()
        # La page où le parcours s'est arrêté vaut le diagnostic : on la garde
        # si --html a été demandé, et on en montre le rendu.
        if getattr(e, "page", None) is not None:
            print(frame(f"dernière page reçue  ({e.page.url})",
                        e.page.text(width=args.width), width=args.width))
            if args.html:
                _write_html(args.html, e.page.body)
        erreur(e)
        return report.EXIT_FAIL

    show_peers(br, quiet=args.quiet)

    if args.verbose:
        print("\n-- requêtes --")
        for method, url, status, hops in br.trace:
            print(f"  {method:<4} {url}")
            for code, newurl in hops:
                print(f"       {code} → {newurl}")
            print(f"       {status}")
        hotes = []
        for _m, u, _s, hops in br.trace:
            for candidat in [u] + [n for _c, n in hops]:
                h = urllib.parse.urlparse(candidat).netloc
                if h and h not in hotes:
                    hotes.append(h)
        print(f"  cookies : {', '.join(br.cookies) or 'aucun'}")
        print(f"  hôtes contactés : {', '.join(hotes)}")

    # -- la page de sortie ----------------------------------------------------
    det = oauthweb.token_details(page)
    print()
    print(frame(f"{det['title'] or 'oauth/token/display'}  ({page.url})",
                page.text(width=args.width), width=args.width))

    tok = det["token"]
    print()
    if tok:
        montre = (tok[:14] + "…" + tok[-4:]) if args.mask else tok
        print(f"  jeton   : {montre}")
        if det["expires"]:
            print(f"  validité: {det['expires']}")
    else:
        print("  ⚠️  aucun jeton `sha256~…` trouvé dans la page finale.")

    # -- le jeton fonctionne-t-il vraiment ? ----------------------------------
    # Hors --check, rien ne sort du serveur OAuth : c'est la garantie que ce
    # script teste bien la chaîne d'authentification, et elle seule.
    identite = None
    if tok and args.check:
        if not args.api_server:
            print("  ⚠️  --check sans --api-server : la vérification du jeton "
                  "est impossible (aucune URL d'API server connue)")
        else:
            try:
                identite = oauthweb.whoami(args.api_server, tok,
                                           insecure=args.insecure,
                                           timeout=args.timeout)
                print(f"  identité: {identite['name']}  "
                      f"(groupes : {', '.join(identite['groups']) or '-'})")
            except oauthweb.FlowError as e:
                print(f"  ⚠️  vérification du jeton impossible : {e}")

    # -- artefacts ------------------------------------------------------------
    if args.html:
        path = _write_html(args.html, page.body)
        if args.open_browser:
            import webbrowser
            webbrowser.open("file://" + os.path.abspath(path))
    elif args.open_browser:
        print("  (--open sans --html : rien à ouvrir)")

    if args.json:
        report.write_json(args.json, {
            "oauth_base": base, "user": args.user, "client_id": args.client_id,
            "scope": args.scope, "steps": etapes,
            "final_url": page.url, "title": det["title"],
            "token": tok, "expires": det["expires"],
            "whoami": identite,
            "cookies": br.cookies,
            "trace": [{"method": m, "url": u, "status": s,
                       "redirects": [{"code": c, "to": n} for c, n in h]}
                      for m, u, s, h in br.trace],
        })

    return report.EXIT_OK if tok else report.EXIT_INCONCLUSIVE


if __name__ == "__main__":
    sys.exit(main())
