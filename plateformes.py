#!/usr/bin/env python3
"""
Replays des plateformes officielles — Radar Politique TV
--------------------------------------------------------
Lit les plans de site publics (« sitemaps », déclarés dans robots.txt pour les
moteurs de recherche) de bfmtv.com, tf1.fr (LCI), lcp.fr, publicsenat.fr,
france.tv, arte.tv, cnews.fr… et y repère les pages de replays et de vidéos
politiques récentes. Aucune connexion ni contournement : seules les pages
publiques déclarées par les sites sont lues.

Le rapport de diagnostic (replaysInfo.plateformes) indique, site par site, ce qui
a été trouvé, pour ajuster les réglages dans filtres.json.
"""
from __future__ import annotations

import gzip
import hashlib
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from collect import classer, est_documentaire, iso, norm, trouve

UA = "Mozilla/5.0 (Linux; Android 14; SM-S921B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
TAILLE_MAX = 90_000_000


def lire(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9",
                                               "Accept": "application/xml,text/xml,text/plain,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(TAILLE_MAX)
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def balise(el) -> str:
    return el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""


def enfant(el, nom):
    for c in el:
        if balise(c) == nom:
            return c
    return None


def texte(el, *chemin) -> str:
    for nom in chemin:
        if el is None:
            return ""
        el = enfant(el, nom)
    return (el.text or "").strip() if el is not None else ""


def date_iso(s: str) -> datetime | None:
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    for essai in (s, s[:19], s[:10]):
        try:
            d = datetime.fromisoformat(essai)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def titre_depuis_url(url: str) -> str:
    """« /programmes/primaire-socialiste-le-film-12345 » → « Primaire socialiste le film »."""
    chemin = urllib.parse.urlparse(url).path.rstrip("/")
    morceaux = [m for m in chemin.split("/") if m]
    if not morceaux:
        return ""
    dernier = re.sub(r"\.(html?|php)$", "", morceaux[-1])
    if re.fullmatch(r"[\d_-]+|video|videos|replay", dernier) and len(morceaux) > 1:
        dernier = morceaux[-2]
    dernier = re.sub(r"[_-]?(\d{4,}|[a-z]{1,3}\d+[a-z\d]*)$", "", dernier)  # identifiants en fin d'adresse
    mots = re.sub(r"[-_]+", " ", dernier).strip()
    return mots[:1].upper() + mots[1:] if mots else ""


def sitemaps_du_site(site: dict, info_site: dict) -> list[str]:
    urls = list(site.get("sitemaps", []))
    if site.get("robots", True):
        try:
            robots = lire(site["racine"].rstrip("/") + "/robots.txt").decode("utf-8", "replace")
            urls += re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots)
        except Exception as e:  # noqa: BLE001
            info_site.setdefault("erreurs", []).append(f"robots.txt : {e}")
    return list(dict.fromkeys(urls))


def score_sitemap(url: str, lastmod: datetime | None, mots: list[str], maintenant: datetime, numeros: str = "haut") -> float:
    u = url.lower()
    s = sum(3 for m in mots if m in u)
    if re.search(r"video|replay|programme|emission|news|actu", u):
        s += 2
    if re.search(r"image|tag|categor|author|auteur|page|static|archive", u):
        s -= 3
    if lastmod:
        jours = (maintenant - lastmod).total_seconds() / 86400
        s += 4 if jours < 2 else 2 if jours < 15 else -4 if jours > 60 else 0
    # Sans date : on privilégie les numéros les plus élevés (souvent les plus récents)
    if not lastmod:
        m = re.search(r"(\d+)(?:\.xml)?(?:\.gz)?$|page=(\d+)", u)
        if m:
            n = min(int(m.group(1) or m.group(2)), 999) / 1000
            s += (1 - n) if numeros == "bas" else n
    # Sitemaps datés dans l'adresse (…-2026-10…) : on privilégie le mois courant
    if maintenant.strftime("%Y-%m") in u or maintenant.strftime("%Y/%m") in u or maintenant.strftime("%Y%m") in u:
        s += 4
    return s


def collecter_site(site: dict, det, limite: datetime, affichage: dict, budget_s: int = 75):
    budget_s = int(site.get("budget_s", budget_s))
    filtre_sm = re.compile(site["filtre_sitemap"], re.I) if site.get("filtre_sitemap") else None
    exclu_sm = re.compile(site["exclure_sitemap"], re.I) if site.get("exclure_sitemap") else None
    debut = time.time()
    maintenant = datetime.now(timezone.utc)
    info = {"sitemapsLus": [], "pagesVues": 0, "pagesRecentes": 0, "retenues": 0, "exemples": []}
    motif = re.compile(site.get("motif_url", "."), re.I)
    exclu = re.compile(site["exclure_url"], re.I) if site.get("exclure_url") else None
    mots = [m.lower() for m in site.get("mots_sitemap", [])]
    a_lire = [(u, None) for u in sitemaps_du_site(site, info)]
    vus, resultats = set(), []
    max_sitemaps = int(site.get("max_sitemaps", 8))

    while a_lire and len(info["sitemapsLus"]) < max_sitemaps and time.time() - debut < budget_s:
        if time.time() > ECHEANCE:
            info.setdefault("erreurs", []).append("temps global écoulé")
            break
        a_lire.sort(key=lambda x: score_sitemap(x[0], x[1], mots, maintenant, site.get("preferer_numeros", "haut")), reverse=True)
        url, _lm = a_lire.pop(0)
        if url in vus:
            continue
        vus.add(url)
        try:
            racine = ET.fromstring(lire(url))
        except Exception as e:  # noqa: BLE001
            info.setdefault("erreurs", []).append(f"{url[-70:]} : {e}")
            continue
        if balise(racine) == "sitemapindex":
            enfants = [(texte(s, "loc"), date_iso(texte(s, "lastmod"))) for s in racine if balise(s) == "sitemap"]
            info["sitemapsLus"].append(f"index {url[-70:]} ({len(enfants)})")
            a_lire += [(u, lm) for u, lm in enfants if u and u not in vus
                       and (not filtre_sm or filtre_sm.search(u)) and not (exclu_sm and exclu_sm.search(u))]
            continue
        n_ok, apercu = 0, None
        for u in racine:
            if balise(u) != "url":
                continue
            loc = texte(u, "loc")
            info["pagesVues"] += 1
            if apercu is None:  # diagnostic : première adresse et sa date
                v0, n0 = enfant(u, "video"), enfant(u, "news")
                apercu = f"{loc[:110]} | {texte(v0, 'publication_date') or texte(n0, 'publication_date') or texte(u, 'lastmod') or 'sans date'}"
            if not loc or not motif.search(loc) or (exclu and exclu.search(loc)):
                continue
            vid, news = enfant(u, "video"), enfant(u, "news")
            titre = texte(vid, "title") or texte(news, "title") or titre_depuis_url(loc)
            desc = texte(vid, "description")
            image = texte(vid, "thumbnail_loc")
            if not image:
                img = enfant(u, "image")
                image = texte(img, "loc")
            date = date_iso(texte(vid, "publication_date")) or date_iso(texte(news, "publication_date")) \
                or date_iso(texte(u, "lastmod"))
            if not date and site.get("sans_date"):  # pages de programmes non datées (catalogue)
                date = maintenant
            if not titre or not date or date < limite or date > maintenant + timedelta(days=2):
                continue
            info["pagesRecentes"] += 1
            chaine = site["nom"]
            for rx, nom in site.get("chaines_par_url", []):
                if re.search(rx, loc, re.I):
                    chaine = nom
                    break
            res = det.evaluer(titre, "", desc[:500], [], chaine)
            theme = ""
            if not res:
                theme = det.theme_documentaire(titre, "", desc[:500], [], chaine, 0)
                if not theme:
                    continue
            score, raisons = res if res else (0, ["documentaire"])
            tn, dn = norm(titre), norm(desc)
            fmt = classer(tn, dn, norm(chaine), est_documentaire(tn + " " + norm(loc), dn, [])) if res else "Documentaire"
            if fmt == "Magazine politique" and site.get("format_defaut"):
                fmt = site["format_defaut"]
            noms = trouve(det.personnes, tn + " " + dn)
            noms = [n for n in noms if not any(n != m and n in m for m in noms)]
            resultats.append({
                "id": "w-" + hashlib.sha1(loc.encode()).hexdigest()[:14],
                "titre": titre,
                "chaine": chaine,
                "compte": site["plateforme"],
                "lien": loc,
                "image": image if image.startswith("https://") else "",
                "date": iso(date),
                "format": fmt,
                "personnes": [affichage.get(n, n.title()) for n in noms][:4],
                "resume": desc[:300],
                "integrale": bool(site.get("integrales", False)),
                "score": score,
                "raisons": raisons,
                "plateforme": site["plateforme"],
                "section": "politique" if res else "documentaire",
                "theme": theme,
            })
            n_ok += 1
            if len(info["exemples"]) < 4:
                info["exemples"].append(loc)
        info["sitemapsLus"].append(f"{url[-70:]} ({n_ok} retenue(s))")
        if apercu and len(info.setdefault("apercu", [])) < 6:
            info["apercu"].append(apercu)
    plafond = int(site.get("max_resultats", 150))
    if len(resultats) > plafond:  # on garde les plus pertinentes puis les plus récentes
        resultats.sort(key=lambda r: (r["score"], r["date"]), reverse=True)
        resultats = resultats[:plafond]
    info["retenues"] = len(resultats)
    info["duree_s"] = round(time.time() - debut)
    return resultats, info


ECHEANCE = float("inf")


def collecter(filtres: dict, det, limite: datetime, affichage: dict):
    global ECHEANCE
    sites = filtres.get("replays", {}).get("plateformes", [])
    # Garde-fou : la collecte des plateformes ne dépasse pas ce temps au total (le robot a 20 min)
    ECHEANCE = time.time() + 60 * float(filtres.get("replays", {}).get("budget_total_minutes", 11))
    tout, infos = [], {}
    for site in sites:
        if site.get("actif") is False:
            continue
        try:
            r, i = collecter_site(site, det, limite, affichage)
        except Exception as e:  # noqa: BLE001
            r, i = [], {"erreur": str(e)}
        tout += r
        infos[site["plateforme"]] = i
    return tout, infos