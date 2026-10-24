#!/usr/bin/env python3
"""
Annonces d'invités — Radar Politique TV
---------------------------------------
Cherche les annonces « X sera l'invité de Y » dans des sources gratuites :
  • Google Actualités (flux RSS de recherche) ;
  • Bluesky (API publique, sans compte) : comptes de chaînes, d'émissions, de partis.
Rapproche chaque annonce d'une émission de la grille (même chaîne, même horaire
ou titre d'émission cité) et ajoute le nom à la fiche, avec un lien vers la source.

Appelé par collect.py ; ne bloque jamais la collecte de la grille en cas d'échec.
X (Twitter), Instagram et Facebook ne sont pas interrogés : pas d'accès gratuit en lecture.
"""
from __future__ import annotations

import email.utils
import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

from collect import PARIS, cle_chaine, motif, norm, trouve, iso

UA = "Mozilla/5.0 (compatible; RadarPolitiqueTV/1.0; usage personnel)"
BSKY = "https://public.api.bsky.app/xrpc/"

# Chaînes reconnues dans les textes → clé comparable au nom de la chaîne dans la grille
ALIAS_CHAINES = [
    (r"\bbfm ?tv\b|\bbfm\b(?! ?business| ?paris| ?lyon| ?marseille)", ["bfmtv"], "BFM TV"),
    (r"\blci\b", ["lci", "lachaineinfo"], "LCI"),
    (r"\bc ?news\b", ["cnews"], "CNews"),
    (r"\bfranceinfo\b|\bfrance info\b", ["franceinfo", "franceinfotv"], "franceinfo"),
    (r"\bfrance ?2\b", ["france2"], "France 2"),
    (r"\bfrance ?3\b", ["france3"], "France 3"),
    (r"\bfrance ?5\b", ["france5"], "France 5"),
    (r"\btf1\b", ["tf1"], "TF1"),
    (r"\bm6\b", ["m6"], "M6"),
    (r"\btmc\b", ["tmc"], "TMC"),
    (r"\barte\b", ["arte"], "Arte"),
    (r"\blcp\b", ["lcp", "lcpassembleenationale", "lachaineparlementaire"], "LCP"),
    (r"\bpublic senat\b", ["publicsenat"], "Public Sénat"),
    (r"\bi24 ?news\b", ["i24news"], "i24 News"),
    (r"\bfrance ?24\b", ["france24"], "France 24"),
    (r"\brmc story\b", ["rmcstory"], "RMC Story"),
    (r"\bfrance inter\b", [], "France Inter"),
    (r"\brtl\b", [], "RTL"),
    (r"\beurope ?1\b", [], "Europe 1"),
    (r"\bsud radio\b", [], "Sud Radio"),
]
ALIAS_RE = [(re.compile(r), cles, nom) for r, cles, nom in ALIAS_CHAINES]

RE_ANNONCE = re.compile(
    r"\binvite|\brecoit\b|\brecevra\b|\bface a\b|\bdebat|\bconfrontation\b|\bplateau\b|"
    r"\binterview|\bentretien\b|\brepondra\b|\bgrand oral\b|\bsera (sur|dans le|dans l')")
# Annonces d'annulation : listées mais jamais rattachées à une émission comme « invité annoncé »
RE_ANNULE = re.compile(r"deprogramm|\bannul|\breport|\brenonce|\bboycott|\bdesiste")
MAJ = "A-ZÉÈÊÀÂÎÔÛÇ"
RE_NOM_INVITE = re.compile(
    rf"([{MAJ}][a-zéèêëàâîïôöûüç'’\-]+(?:\s+(?:de |du |d'|le |la )?[{MAJ}][\w'’\-]+){{1,2}})"
    rf",?\s+(?:[^,.]{{0,40}},\s+)?(?:sera|est|était)?\s*(?:l['’]\s?)?invit[ée]e?\b")
RE_HEURE = re.compile(r"\b(\d{1,2}) ?h ?(\d{2})?\b")
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
RE_JOUR = re.compile(r"\b(aujourd'hui|ce soir|ce matin|cet apres-midi|demain|" + "|".join(JOURS) + r")\b")


# ---------------------------------------------------------------- réseau

def lire(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def lire_json(url: str):
    return json.loads(lire(url).decode("utf-8"))


# ---------------------------------------------------------------- sources

def google_actualites(requetes: list[str], test: Path | None, info: dict) -> list[dict]:
    items = []
    fichiers = sorted(test.glob("*.xml")) if test else []
    flux = [(f.name, f.read_bytes()) for f in fichiers] if test else []
    if not test:
        for q in requetes:
            url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
                {"q": q, "hl": "fr", "gl": "FR", "ceid": "FR:fr"})
            try:
                flux.append((q, lire(url)))
            except Exception as e:  # noqa: BLE001
                info.setdefault("erreurs", []).append(f"Google Actualités ({q[:40]}…) : {e}")
    for nom, data in flux:
        try:
            racine = ET.fromstring(data)
        except ET.ParseError as e:
            info.setdefault("erreurs", []).append(f"Flux illisible {nom[:40]} : {e}")
            continue
        n = 0
        for it in racine.iter("item"):
            titre = unescape(it.findtext("title") or "").strip()
            lien = (it.findtext("link") or "").strip()
            src = (it.findtext("source") or "").strip()
            desc = re.sub(r"<[^>]+>", " ", unescape(it.findtext("description") or ""))
            try:
                date = email.utils.parsedate_to_datetime(it.findtext("pubDate") or "")
            except Exception:  # noqa: BLE001
                continue
            if src and titre.endswith(" - " + src):
                titre = titre[: -len(src) - 3]
            if not titre or not lien.startswith("https://"):
                continue
            items.append({"texte": titre, "complement": desc, "lien": lien, "date": date,
                          "source": "Presse · " + (src or "Google Actualités")})
            n += 1
        info.setdefault("google", {})[nom[:60]] = n
    return items


def bluesky(conf: dict, test: Path | None, info: dict) -> list[dict]:
    items, comptes = [], []
    if test:
        f = test / "bluesky.json"
        posts = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
        for p in posts:
            items.append(p | {"date": datetime.fromisoformat(p["date"].replace("Z", "+00:00"))})
        return items

    # 1) Comptes donnés explicitement (identifiant exact, ex. « franceinfo.fr »)
    for h in conf.get("comptes_bluesky", []):
        comptes.append(h.strip().lstrip("@"))
    # 2) Comptes trouvés automatiquement par leur nom (vérifiés : nom affiché identique)
    for nom in conf.get("bluesky_chercher_comptes", []):
        try:
            r = lire_json(BSKY + "app.bsky.actor.searchActors?" + urllib.parse.urlencode({"q": nom, "limit": 8}))
        except Exception as e:  # noqa: BLE001
            info.setdefault("erreurs", []).append(f"Bluesky recherche « {nom} » : {e}")
            continue
        cible = cle_chaine(nom)
        for a in r.get("actors", []):
            if cle_chaine(a.get("displayName") or "") == cible or cle_chaine(a.get("handle", "").split(".")[0]) == cible:
                comptes.append(a["handle"])
                break
    comptes = list(dict.fromkeys(comptes))
    info["comptesBluesky"] = comptes

    limite = datetime.now(timezone.utc) - timedelta(days=3)
    for h in comptes:
        try:
            r = lire_json(BSKY + "app.bsky.feed.getAuthorFeed?" + urllib.parse.urlencode(
                {"actor": h, "limit": 40, "filter": "posts_no_replies"}))
        except Exception as e:  # noqa: BLE001
            info.setdefault("erreurs", []).append(f"Bluesky @{h} : {e}")
            continue
        for x in r.get("feed", []):
            post = x.get("post", {})
            rec = post.get("record", {})
            try:
                date = datetime.fromisoformat(rec.get("createdAt", "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if date < limite:
                continue
            uri = post.get("uri", "")
            rkey = uri.rsplit("/", 1)[-1]
            auteur = post.get("author", {}).get("handle", h)
            items.append({"texte": rec.get("text", ""), "complement": "", "date": date,
                          "lien": f"https://bsky.app/profile/{auteur}/post/{rkey}",
                          "source": "Bluesky · @" + auteur})

    # 3) Recherche de messages (peut exiger un compte selon Bluesky : échec toléré)
    for q in conf.get("bluesky_recherches", []):
        try:
            r = lire_json(BSKY + "app.bsky.feed.searchPosts?" + urllib.parse.urlencode(
                {"q": q, "lang": "fr", "limit": 40, "sort": "latest"}))
        except Exception as e:  # noqa: BLE001
            info.setdefault("erreurs", []).append(f"Bluesky recherche de messages : {e}")
            break
        for post in r.get("posts", []):
            rec = post.get("record", {})
            try:
                date = datetime.fromisoformat(rec.get("createdAt", "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if date < limite:
                continue
            auteur = post.get("author", {}).get("handle", "")
            items.append({"texte": rec.get("text", ""), "complement": "", "date": date,
                          "lien": f"https://bsky.app/profile/{auteur}/post/{post.get('uri', '').rsplit('/', 1)[-1]}",
                          "source": "Bluesky · @" + auteur})
    return items


# ---------------------------------------------------------------- analyse

class Analyseur:
    def __init__(self, filtres: dict, programmes: list[dict]):
        noms = filtres.get("personnalites_politiques", [])
        self.motif_noms = motif(noms, pluriel=False)
        self.affichage = {}
        for n in noms:  # nom complet prioritaire pour l'affichage
            k = norm(n)
            self.affichage.setdefault(k, n)
        self.complets = {norm(n) for n in noms if " " in n.strip()}
        self.programmes = programmes
        self.par_chaine: dict[str, list[dict]] = {}
        for p in programmes:
            self.par_chaine.setdefault(cle_chaine(p["chaine"]), []).append(p)

    def chaines(self, texte_n: str) -> list[tuple[list[str], str]]:
        return [(cles, nom) for rx, cles, nom in ALIAS_RE if rx.search(texte_n)]

    def personnes(self, texte: str, texte_n: str) -> list[str]:
        trouves = trouve(self.motif_noms, texte_n)
        # « Bardella » est ignoré si « Jordan Bardella » est déjà trouvé
        garde = [t for t in trouves if not any(t != c and t in c for c in trouves)]
        res = [self.affichage.get(t, t.title()) for t in garde]
        for m in RE_NOM_INVITE.finditer(texte):
            n = m.group(1).strip()
            if norm(n) not in {norm(r) for r in res} and not any(norm(n) in norm(r) for r in res):
                res.append(n)
        return res[:4]

    def jour_vise(self, texte_n: str, date: datetime) -> datetime | None:
        loc = date.astimezone(PARIS)
        m = RE_JOUR.search(texte_n)
        if not m:
            return None
        mot = m.group(1)
        if mot == "demain":
            return loc + timedelta(days=1)
        if mot in JOURS:
            delta = (JOURS.index(mot) - loc.weekday()) % 7
            return loc + timedelta(days=delta)
        return loc

    def emission(self, cles: list[str], texte_n: str, date: datetime) -> dict | None:
        candidats = [p for c in cles for p in self.par_chaine.get(c, [])]
        if not candidats:
            return None
        debut_f, fin_f = date - timedelta(hours=6), date + timedelta(days=4)
        candidats = [p for p in candidats
                     if debut_f <= datetime.fromisoformat(p["debut"].replace("Z", "+00:00")) <= fin_f]
        jour = self.jour_vise(texte_n, date)
        # 1) Titre de l'émission cité dans l'annonce
        cites = [p for p in candidats if len(norm(p["titre"])) >= 6 and norm(p["titre"]) in texte_n]
        if jour:
            cites_jour = [p for p in cites if datetime.fromisoformat(p["debut"].replace("Z", "+00:00"))
                          .astimezone(PARIS).date() == jour.date()]
            cites = cites_jour or cites
        if cites:
            futurs = [p for p in cites if p["debut"] >= iso(date - timedelta(hours=1))]
            return (futurs or cites)[0]
        # 2) Chaîne + jour + heure
        h = RE_HEURE.search(texte_n)
        if jour and h and 0 <= int(h.group(1)) <= 23:
            t = jour.replace(hour=int(h.group(1)), minute=int(h.group(2) or 0), second=0, microsecond=0)
            for p in candidats:
                d = datetime.fromisoformat(p["debut"].replace("Z", "+00:00"))
                f = datetime.fromisoformat(p["fin"].replace("Z", "+00:00")) if p.get("fin") else d + timedelta(hours=1)
                if d - timedelta(minutes=20) <= t < f:
                    return p
        # 3) « ce soir » / « ce matin » sur une chaîne n'ayant qu'une émission politique dans ce créneau
        if jour and re.search(r"\bce soir\b|\bce matin\b", texte_n):
            soir = "ce soir" in texte_n
            creneau = [p for p in candidats
                       if datetime.fromisoformat(p["debut"].replace("Z", "+00:00")).astimezone(PARIS).date() == jour.date()
                       and ((18 <= datetime.fromisoformat(p["debut"].replace("Z", "+00:00")).astimezone(PARIS).hour)
                            if soir else
                            (6 <= datetime.fromisoformat(p["debut"].replace("Z", "+00:00")).astimezone(PARIS).hour < 11))]
            if len(creneau) == 1:
                return creneau[0]
        return None


def collecter(filtres: dict, programmes: list[dict], dossier_test: str | None = None):
    conf = filtres.get("annonces", {})
    info: dict = {"genereLe": iso(datetime.now(timezone.utc))}
    if conf.get("actif") is False:
        return [], info | {"desactive": True}
    test = Path(dossier_test) if dossier_test else None

    bruts = google_actualites(conf.get("requetes_presse", []), test, info) + bluesky(conf, test, info)
    an = Analyseur(filtres, programmes)
    limite = datetime.now(timezone.utc) - timedelta(days=int(conf.get("jours_conserves", 3)))
    vus, annonces = set(), []

    for it in sorted(bruts, key=lambda x: x["date"], reverse=True):
        if it["date"] < limite:
            continue
        texte = re.sub(r"\s+", " ", it["texte"]).strip()
        tout = texte + " " + it.get("complement", "")
        tn = norm(tout)
        cle = norm(texte)[:90]
        if not texte or cle in vus or not RE_ANNONCE.search(tn):
            continue
        chaines = an.chaines(tn)
        personnes = an.personnes(tout, tn)
        annule = bool(RE_ANNULE.search(tn))
        p = None
        for cles, _nom in ([] if annule else chaines):
            p = an.emission(cles, tn, it["date"])
            if p:
                break
        # On garde : annonce rattachée à une émission, ou citant une chaîne ET une personnalité
        if not p and not (chaines and personnes):
            continue
        vus.add(cle)
        a = {
            "id": hashlib.sha1(it["lien"].encode()).hexdigest()[:12],
            "texte": texte[:300],
            "lien": it["lien"],
            "source": it["source"],
            "date": iso(it["date"]),
            "personnes": personnes,
            "chaine": p["chaine"] if p else chaines[0][1],
            "emissionId": p["id"] if p else "",
            "emissionTitre": p["titre"] if p else "",
            "emissionDebut": p["debut"] if p else "",
            "annulation": annule,
        }
        annonces.append(a)
        if p and personnes:
            p.setdefault("annonces", [])
            if not any(x["lien"] == a["lien"] for x in p["annonces"]):
                p["annonces"].append({"noms": personnes, "source": a["source"], "lien": a["lien"]})
            deja = {norm(n) for n in p.get("invites", [])} | {norm(n) for n in p.get("invitesAnnonces", [])}
            for n in personnes:
                if norm(n) not in deja:
                    p.setdefault("invitesAnnonces", []).append(n)
                    deja.add(norm(n))

    annonces = annonces[: int(conf.get("nombre_maximum", 120))]
    info["bruts"] = len(bruts)
    info["retenues"] = len(annonces)
    info["rattachees"] = sum(1 for a in annonces if a["emissionId"])
    print(f"Annonces : {len(annonces)} retenues ({info['rattachees']} rattachées à une émission) "
          f"sur {len(bruts)} messages/articles lus", file=sys.stderr)
    for e in info.get("erreurs", [])[:10]:
        print("  ·", e, file=sys.stderr)
    return annonces, info
