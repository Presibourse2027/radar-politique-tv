#!/usr/bin/env python3
"""
Replays à la demande — Radar Politique TV
-----------------------------------------
Recense les vidéos politiques (débats, interviews, documentaires, émissions)
publiées par les chaînes officielles sur YouTube, via leurs flux RSS publics
(gratuits, sans clé). Les vidéos sont cumulées d'un passage à l'autre
(14 jours par défaut), car chaque flux ne donne que les 15 dernières vidéos.

Les plateformes france.tv, TF1+, M6+ et arte.tv n'ont pas de catalogue ouvert :
elles restent accessibles par le bouton « Chercher le replay » des émissions diffusées.
"""
from __future__ import annotations

import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

from collect import classer, est_documentaire, iso, norm, trouve

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
      "media": "http://search.yahoo.com/mrss/"}
RE_ID = [re.compile(p) for p in (
    r'feeds/videos\.xml\?channel_id=(UC[\w-]{22})',
    r'"externalId":"(UC[\w-]{22})"',
    r'itemprop="identifier" content="(UC[\w-]{22})"',
    r'"channelId":"(UC[\w-]{22})"')]
RE_INTEGRALE = re.compile(r"\bintegrale?\b|\ben integralite\b|\breplay\b|\bl'emission (du|de ce)\b|\bemission complete\b")
RE_BRUIT = re.compile(r"abonne|http|www\.|#|retrouvez|suivez|telecharge|application|facebook|instagram|twitter|tiktok|newsletter")


def lire(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9",
                                               "Cookie": "CONSENT=YES+1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def resoudre(handle: str) -> str | None:
    """Identifiant de chaîne (UC…) à partir du nom @handle."""
    html = lire("https://www.youtube.com/@" + handle.lstrip("@")).decode("utf-8", "replace")
    for rx in RE_ID:
        m = rx.search(html)
        if m:
            return m.group(1)
    return None


def nettoyer_desc(desc: str) -> str:
    """Garde le début utile de la description (sans les appels à s'abonner, liens, mots-dièse)."""
    lignes = [l.strip() for l in (desc or "").splitlines() if l.strip()]
    utiles = [l for l in lignes if not RE_BRUIT.search(norm(l))]
    return " ".join(utiles)[:500]


def lire_flux(data: bytes, nom_chaine: str) -> tuple[str, list[dict]]:
    racine = ET.fromstring(data)
    titre_chaine = racine.findtext("a:title", default="", namespaces=NS)
    videos = []
    for e in racine.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", default="", namespaces=NS)
        titre = (e.findtext("a:title", default="", namespaces=NS) or "").strip()
        lien_el = e.find("a:link", NS)
        lien = lien_el.get("href") if lien_el is not None else f"https://www.youtube.com/watch?v={vid}"
        pub = e.findtext("a:published", default="", namespaces=NS)
        grp = e.find("media:group", NS)
        desc = grp.findtext("media:description", default="", namespaces=NS) if grp is not None else ""
        if not vid or not titre:
            continue
        try:
            date = datetime.fromisoformat(pub.replace("Z", "+00:00"))
        except ValueError:
            continue
        videos.append({"vid": vid, "titre": titre, "lien": lien, "date": date, "desc": desc,
                       "chaine": nom_chaine, "short": "/shorts/" in lien})
    return titre_chaine, videos


def collecter(filtres: dict, det, anciens: list[dict], ids_connus: dict, dossier_test: str | None = None):
    conf = filtres.get("replays", {})
    info: dict = {"genereLe": iso(datetime.now(timezone.utc))}
    if conf.get("actif") is False:
        return anciens, info | {"desactive": True}
    maintenant = datetime.now(timezone.utc)
    limite = maintenant - timedelta(days=int(conf.get("jours_conserves", 14)))
    sans_shorts = conf.get("exclure_shorts", True)

    # Personnalités (affichage avec la graphie de la liste)
    affichage = {}
    for n in filtres.get("personnalites_politiques", []):
        affichage.setdefault(norm(n), n)

    flux: list[tuple[str, bytes]] = []
    ids: dict = {}
    if dossier_test:
        for f in sorted(Path(dossier_test).glob("youtube_*.xml")):
            flux.append((f.stem.replace("youtube_", "").replace("_", " "), f.read_bytes()))
    else:
        for c in conf.get("chaines_youtube", []):
            nom = c.get("nom", "")
            attendu = norm(c.get("attendu", ""))
            # Candidats : identifiants techniques connus, puis noms @ à essayer dans l'ordre
            essais = [("id", i) for i in ([c["id"]] if c.get("id") else []) + c.get("ids", [])]
            for h in ([c["handle"]] if c.get("handle") else []) + c.get("handles", []):
                essais.append(("handle", h))
            echecs = []
            for genre, val in essais:
                try:
                    cid = val if genre == "id" else (ids_connus.get(val) or resoudre(val))
                    if not cid:
                        raise ValueError("identifiant introuvable")
                    data = lire("https://www.youtube.com/feeds/videos.xml?channel_id=" + cid)
                    titre = ET.fromstring(data).findtext("a:title", default="", namespaces=NS)
                    if attendu and attendu not in norm(titre):
                        raise ValueError(f"compte « {titre} » sans rapport")
                    if genre == "handle":
                        ids[val] = cid
                    flux.append((nom, data))
                    break
                except Exception as e:  # noqa: BLE001
                    echecs.append(f"{'@' if genre == 'handle' else ''}{val} : {e}")
            else:
                if essais:
                    info.setdefault("erreurs", []).append(f"YouTube {nom} : " + " ; ".join(echecs))
    info["youtubeIds"] = ids

    nouveaux, lues = [], 0
    info["chaines"] = {}
    tout_retenir = {c.get("nom") for c in conf.get("chaines_youtube", []) if c.get("tout_retenir")}
    for nom, data in flux:
        try:
            titre_chaine, videos = lire_flux(data, nom)
        except ET.ParseError as e:
            info.setdefault("erreurs", []).append(f"Flux YouTube illisible ({nom}) : {e}")
            continue
        gardees = 0
        for v in videos:
            lues += 1
            if v["date"] < limite or (sans_shorts and v["short"]):
                continue
            desc = nettoyer_desc(v["desc"])
            res = det.evaluer(v["titre"], "", desc, [], nom)
            if not res and nom in tout_retenir:  # émission politique dédiée : toutes ses vidéos
                res = (det.seuil, ["émission suivie"])
            theme = "" if res else det.theme_documentaire(v["titre"], "", desc, [], nom, 0)
            if not res and not theme:
                continue
            section = "politique" if res else "documentaire"
            score, raisons = res if res else (0, ["documentaire"])
            tn, dn = norm(v["titre"]), norm(desc)
            noms = trouve(det.personnes, tn + " " + dn)
            noms = [n for n in noms if not any(n != m and n in m for m in noms)]
            nouveaux.append({
                "id": "yt-" + v["vid"],
                "titre": v["titre"],
                "chaine": nom,
                "compte": titre_chaine,
                "lien": v["lien"],
                "image": f"https://i.ytimg.com/vi/{v['vid']}/mqdefault.jpg",
                "date": iso(v["date"]),
                "format": classer(tn, dn, norm(nom), est_documentaire(tn, dn, [])) if res else "Documentaire",
                "section": section,
                "theme": theme,
                "personnes": [affichage.get(n, n.title()) for n in noms][:4],
                "resume": desc[:300],
                "integrale": bool(RE_INTEGRALE.search(tn)),
                "score": score,
                "raisons": raisons,
                "plateforme": "YouTube",
            })
            gardees += 1
        info["chaines"][nom] = {"compte": titre_chaine, "lues": len(videos), "retenues": gardees,
                                "shorts": sum(1 for v in videos if v["short"]),
                                "plusRecente": iso(max(v["date"] for v in videos)) if videos else "",
                                "exemple": videos[0]["titre"][:90] if videos else ""}

    # Plateformes officielles (plans de site publics) : bfmtv.com, tf1.fr, lcp.fr, france.tv…
    if not dossier_test:
        try:
            import plateformes
            p_res, p_info = plateformes.collecter(filtres, det, limite, affichage)
            nouveaux += p_res
            info["plateformes"] = p_info
        except Exception as e:  # noqa: BLE001
            info.setdefault("erreurs", []).append(f"Plateformes : {e}")

    # Cumul avec les passages précédents
    par_id = {r["id"]: r for r in anciens if r.get("date", "") >= iso(limite)}
    for r in nouveaux:
        par_id[r["id"]] = r
    tout = sorted(par_id.values(), key=lambda r: r["date"], reverse=True)
    # Plafond par plateforme (évite qu'un site très prolifique évince les autres)
    plafonds = {x.get("plateforme"): int(x.get("max_resultats", 300)) for x in conf.get("plateformes", [])}
    plafonds.setdefault("YouTube", int(conf.get("max_youtube", 400)))
    compte, garde = {}, []
    for r in tout:
        pf = r.get("plateforme", "YouTube")
        compte[pf] = compte.get(pf, 0) + 1
        if compte[pf] <= plafonds.get(pf, 300):
            garde.append(r)
    tout = garde[: int(conf.get("nombre_maximum", 600))]
    info.update({"lues": lues, "nouvelles": len(nouveaux), "total": len(tout)})
    print(f"Replays : {len(nouveaux)} vidéo(s) retenue(s) sur {lues} lues ; {len(tout)} au catalogue",
          file=sys.stderr)
    for e in info.get("erreurs", [])[:15]:
        print("  ·", e, file=sys.stderr)
    return tout, info