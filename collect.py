#!/usr/bin/env python3
"""
Robot de collecte — Radar Politique TV
--------------------------------------
Télécharge une grille TV au format XMLTV (France, toutes chaînes disponibles),
repère les émissions politiques (débats, interviews, magazines, Parlement)
et écrit le résultat dans data/programmes.json, lu par l'application.

Aucune dépendance externe : Python 3.9+ standard.
La lecture est faite en flux (le fichier complet n'est jamais chargé en mémoire).

Usage :
    python3 scripts/collect.py                 # collecte normale
    python3 scripts/collect.py --fichier g.xml # tester sur un fichier local
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import sys
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

RACINE = Path(__file__).resolve().parent
FICHIER_FILTRES = RACINE / "filtres.json"
FICHIER_SORTIE = RACINE / "data" / "programmes.json"
USER_AGENT = "RadarPolitiqueTV/1.0 (usage personnel; collecte quotidienne)"

# ---------------------------------------------------------------- outils texte

def norm(s: str | None) -> str:
    """Minuscules, sans accents, apostrophes et espaces uniformisés."""
    if not s:
        return ""
    s = s.replace("’", "'").replace("‘", "'").replace(" ", " ")
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip()


def motif(mots: list[str], pluriel: bool = True) -> re.Pattern | None:
    """Expression régulière « mot entier » (pluriel/féminin tolérés)."""
    mots = [norm(m) for m in mots if m and norm(m)]
    if not mots:
        return None
    suffixe = r"(?:e|s|es|x)?" if pluriel else ""
    corps = "|".join(re.escape(m) for m in sorted(mots, key=len, reverse=True))
    return re.compile(rf"(?<![\w])(?:{corps}){suffixe}(?![\w])")


def trouve(p: re.Pattern | None, texte: str) -> list[str]:
    return sorted(set(p.findall(texte))) if p else []


def titre_dans_liste(titre_n: str, liste: list[str]) -> str | None:
    """Titre retenu si égal, commençant par le motif, ou le contenant (motif long)."""
    for m in liste:
        m = norm(m)
        if not m:
            continue
        if titre_n == m or titre_n.startswith(m + " ") or titre_n.startswith(m + ":") \
                or titre_n.startswith(m + ",") or titre_n.startswith(m + " -"):
            return m
        if len(m) >= 12 and re.search(rf"(?<![\w]){re.escape(m)}(?![\w])", titre_n):
            return m
    return None


def cle_chaine(nom: str) -> str:
    """Nom de chaîne comparable : « franceinfo: » = « Franceinfo », « BFM TV » = « BFMTV »."""
    return re.sub(r"[^a-z0-9+]", "", norm(nom))


# ---------------------------------------------------------------- dates XMLTV

def date_xmltv(s: str | None) -> datetime | None:
    if not s:
        return None
    m = re.match(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})?\s*([+-]\d{4}|Z)?", s.strip())
    if not m:
        return None
    y, mo, d, h, mi, se, tz = m.groups()
    dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(se or 0))
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        off = timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5]))
        dt = dt.replace(tzinfo=timezone(sign * off))
    else:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- classement

def classer(titre_n: str, desc_n: str, chaine_n: str) -> str:
    tout = titre_n + " " + desc_n
    if re.search(r"questions (d'actualite )?au gouvernement|seance publique|hemicycle|"
                 r"commission d'enquete|audition", tout) or \
            (re.search(r"\blcp\b|public senat", chaine_n) and
             re.search(r"assemblee|senat|seance|vote", titre_n)):
        return "Parlement"
    if re.search(r"soiree electorale|resultats? (du|des|de l')|premier tour|second tour", titre_n):
        return "Soirée électorale"
    if re.search(r"debat|face.a.face|confrontation|duel", titre_n) or \
            re.search(r"\bdebat (entre|avec|presidentiel|televise)|face.a.face", desc_n):
        return "Débat"
    if re.search(r"interview|entretien|grand jury|4 verites|quatre verites|l'evenement|"
                 r"invite politique|grand rendez-vous|en toute franchise|questions politiques",
                 titre_n) or re.search(r"\b(interview|entretien|repond aux questions|recoit)\b", desc_n):
        return "Interview"
    return "Magazine politique"


RE_INVITES = re.compile(
    r"invit[ée]e?s?\s*(?:du jour|de la semaine|politique)?\s*:\s*([^.;\n\(\)]{3,160})",
    re.IGNORECASE)
MAJ = "A-ZÉÈÊÀÂÎÔÛÇ"
RE_RECOIT = re.compile(
    rf"(?:re[çc]oit|accueille|interroge|face [àa])\s+"
    rf"([{MAJ}][\w'’\-]+(?:\s+(?:de |du |d'|le |la )?[{MAJ}][\w'’\-]+){{1,3}})")
RE_NOM = re.compile(rf"^[{MAJ}][\w'’\-.]+(?:\s+\S+){{0,4}}$")


def extraire_invites(prog: ET.Element, desc: str) -> list[str]:
    noms: list[str] = []
    credits = prog.find("credits")
    if credits is not None:
        for tag in ("guest",):
            for el in credits.findall(tag):
                if el.text and el.text.strip():
                    noms.append(el.text.strip())
    for m in RE_INVITES.finditer(desc or ""):
        for n in re.split(r",| et |/|;", m.group(1)):
            n = n.strip(" -–")
            # On ne garde que ce qui ressemble à un nom propre (exclut « ministre de… », « répond… »)
            if 3 <= len(n) <= 60 and RE_NOM.match(n):
                noms.append(n)
    if not noms:
        for m in RE_RECOIT.finditer(desc or ""):
            n = m.group(1).strip()
            if 3 <= len(n) <= 60:
                noms.append(n)
    vus, uniques = set(), []
    for n in noms:
        k = norm(n)
        if k and k not in vus:
            vus.add(k)
            uniques.append(n)
    return uniques[:6]


def presentateurs(prog: ET.Element) -> list[str]:
    credits = prog.find("credits")
    if credits is None:
        return []
    return [el.text.strip() for el in credits.findall("presenter") if el.text and el.text.strip()][:3]


# ---------------------------------------------------------------- détection

class Detecteur:
    def __init__(self, f: dict):
        self.seuil = int(f.get("seuil_detection", 4))
        self.blanc = f.get("titres_toujours_retenus", [])
        self.titres_exclus = motif(f.get("titres_exclus", []), pluriel=False)
        self.forts = motif(f.get("mots_forts", []))
        self.faibles = motif(f.get("mots_faibles", []))
        self.cat_pol = [norm(c) for c in f.get("categories_politiques", [])]
        self.cat_exc = [norm(c) for c in f.get("categories_exclues", [])]
        self.chaines_exc = {norm(c) for c in f.get("chaines_exclues", [])}
        self.nationales = {cle_chaine(c) for c in f.get("chaines_nationales", [])}

    def est_nationale(self, chaine: str) -> bool:
        return not self.nationales or cle_chaine(chaine) in self.nationales

    def evaluer(self, titre: str, sous_titre: str, desc: str, categories: list[str], chaine: str):
        t, st, d = norm(titre), norm(sous_titre), norm(desc)
        cats = [norm(c) for c in categories]
        if norm(chaine) in self.chaines_exc:
            return None
        cat_exclue = any(any(x == c or c.startswith(x + " ") for x in self.cat_exc) for c in cats)
        cat_pol = any(any(x in c for x in self.cat_pol) for c in cats)
        if cat_exclue and not cat_pol:
            return None

        raisons, score = [], 0
        liste = titre_dans_liste(t, self.blanc)
        if liste:
            score += 10
            raisons.append("titre connu")
        if cat_pol:
            score += 3
            raisons.append("catégorie")
        forts_titre = trouve(self.forts, t + " " + st)
        forts_desc = [m for m in trouve(self.forts, d) if m not in forts_titre]
        if forts_titre:
            score += 4
            raisons.append("titre : " + ", ".join(forts_titre[:3]))
        if forts_desc:
            score += 2 * min(len(forts_desc), 3)
            raisons.append("résumé : " + ", ".join(forts_desc[:3]))
        if forts_titre or forts_desc or cat_pol:
            faibles = trouve(self.faibles, t + " " + st + " " + d)
            if faibles:
                score += min(len(faibles), 2)
        # Journaux, météo, etc. : exclus sauf titre connu ou signal politique très fort
        if not liste and trouve(self.titres_exclus, t) and score < self.seuil + 4:
            return None
        if score < self.seuil:
            return None
        return score, raisons


# ---------------------------------------------------------------- lecture XMLTV

def ouvrir_source(urls: list[str], fichier: str | None):
    if fichier:
        brut = open(fichier, "rb")
        tete = brut.read(2)
        brut.seek(0)
        return (gzip.GzipFile(fileobj=brut) if tete == b"\x1f\x8b" else brut), fichier
    erreurs = []
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            rep = urllib.request.urlopen(req, timeout=180)
            donnees = rep.read()
            if len(donnees) < 1000:
                raise ValueError(f"fichier trop petit ({len(donnees)} octets)")
            flux = io.BytesIO(donnees)
            if donnees[:2] == b"\x1f\x8b":
                flux = gzip.GzipFile(fileobj=flux)
            print(f"Source utilisée : {url} ({len(donnees)/1e6:.1f} Mo téléchargés)")
            return flux, url
        except Exception as e:  # noqa: BLE001
            erreurs.append(f"{url} → {e}")
            print(f"Source indisponible : {url} ({e})", file=sys.stderr)
    raise RuntimeError("Aucune source XMLTV joignable :\n" + "\n".join(erreurs))


def collecter(flux, det: Detecteur, jours_passes: int, jours_a_venir: int):
    maintenant = datetime.now(timezone.utc)
    debut_fenetre = maintenant - timedelta(days=jours_passes)
    fin_fenetre = maintenant + timedelta(days=jours_a_venir)
    noms_chaines: dict[str, str] = {}
    icones_chaines: dict[str, str] = {}
    retenus: dict[str, dict] = {}
    total = 0

    for _evt, el in ET.iterparse(flux, events=("end",)):
        if el.tag == "channel":
            cid = el.get("id") or ""
            nom = el.findtext("display-name") or cid
            noms_chaines[cid] = nom.strip()
            ic = el.find("icon")
            if ic is not None and ic.get("src"):
                icones_chaines[cid] = ic.get("src")
            el.clear()
        elif el.tag == "programme":
            total += 1
            try:
                debut = date_xmltv(el.get("start"))
                fin = date_xmltv(el.get("stop"))
                if not debut or debut < debut_fenetre or debut > fin_fenetre:
                    continue
                titre = (el.findtext("title") or "").strip()
                if not titre:
                    continue
                sous_titre = (el.findtext("sub-title") or "").strip()
                desc = (el.findtext("desc") or "").strip()
                cats = [c.text.strip() for c in el.findall("category") if c.text]
                cid = el.get("channel") or ""
                chaine = noms_chaines.get(cid, cid)
                res = det.evaluer(titre, sous_titre, desc, cats, chaine)
                if not res:
                    continue
                score, raisons = res
                rediff = el.find("previously-shown") is not None or \
                    bool(re.search(r"\brediffusion\b|\(r\)$", norm(titre + " " + sous_titre)))
                cle = hashlib.sha1(f"{cid}|{iso(debut)}|{norm(titre)}".encode()).hexdigest()[:16]
                retenus[cle] = {
                    "id": cle,
                    "titre": titre,
                    "sousTitre": sous_titre,
                    "chaine": chaine,
                    "logo": icones_chaines.get(cid, ""),
                    "debut": iso(debut),
                    "fin": iso(fin) if fin else "",
                    "format": classer(norm(titre + " " + sous_titre), norm(desc), norm(chaine)),
                    "invites": extraire_invites(el, desc),
                    "presentateurs": presentateurs(el),
                    "resume": (desc[:420] + "…") if len(desc) > 420 else desc,
                    "categories": cats[:3],
                    "rediffusion": rediff,
                    "nationale": det.est_nationale(chaine),
                    "score": score,
                    "raisons": raisons,
                }
            finally:
                el.clear()
    return list(retenus.values()), total, len(noms_chaines)


# ---------------------------------------------------------------- principal

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fichier", help="fichier XMLTV local (.xml ou .xml.gz) pour tester")
    ap.add_argument("--sortie", default=str(FICHIER_SORTIE))
    args = ap.parse_args()

    filtres = json.loads(FICHIER_FILTRES.read_text(encoding="utf-8"))
    det = Detecteur(filtres)
    flux, source = ouvrir_source(filtres.get("sources_xmltv", []), args.fichier)
    programmes, total, nb_chaines = collecter(
        flux, det, int(filtres.get("jours_passes", 2)), int(filtres.get("jours_a_venir", 8)))

    if total == 0:
        print("La grille reçue ne contient aucun programme : fichier précédent conservé.", file=sys.stderr)
        return 1

    plafond = int(filtres.get("nombre_maximum", 1500))
    if len(programmes) > plafond:  # garde-fou : on conserve les détections les plus sûres
        programmes.sort(key=lambda p: (not p["nationale"], -p["score"]))
        programmes = programmes[:plafond]
        print(f"Plafond atteint : {plafond} émissions conservées (filtres trop larges ?)", file=sys.stderr)
    programmes.sort(key=lambda p: (p["debut"], p["chaine"]))
    sortie = {
        "version": 1,
        "genereLe": iso(datetime.now(timezone.utc)),
        "source": source if not args.fichier else "fichier local",
        "programmesAnalyses": total,
        "chainesAnalysees": nb_chaines,
        "nombre": len(programmes),
        "programmes": programmes,
    }
    chemin = Path(args.sortie)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(json.dumps(sortie, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{len(programmes)} émission(s) politique(s) retenue(s) sur {total} programmes "
          f"et {nb_chaines} chaînes → {chemin}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
