"""
Beleidsmonitor Natuur en Milieu: dagelijkse verzameling.

Dit programma doet GEEN inhoudelijke selectie. Het haalt bij elke bron alles op
wat sinds de vorige controle nieuw of gewijzigd is, past bij bronnen met grote
aantallen een ruime trefwoordenvoorselectie toe, en legt per bron vast wat er
gecontroleerd is en wat mislukte. De inhoudelijke beoordeling gebeurt daarna
door Claude.

Uitvoer:
  data/kandidaten.json   alles wat deze run is doorgelaten
  data/recent.json       doorgelaten stukken van de afgelopen 14 dagen
  data/status.json       bronstatus van deze run (gecontroleerd, fouten, aantallen)
  data/overzicht.json    compacte lijst van alles wat deze run is doorgelaten
  data/archief/<datum>.json  compacte lijst per dag
  state/state.json       geheugen tussen runs (wat al gezien is)
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import time
import traceback
import urllib.parse
import urllib.robotparser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

try:
    from pypdf import PdfReader
except Exception:  # pragma: no cover
    PdfReader = None

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config"
DATA = ROOT / "data"
STATE_FILE = ROOT / "state" / "state.json"

UA = ("BeleidsmonitorNatuurMilieu/1.0 (persoonlijke monitor beleidsadviseur "
      "Bouwend Nederland; https://github.com/anne-bnl)")
TIMEOUT = 40
NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.date().isoformat()
RECENT_DAYS = 14
FIRST_RUN_DAYS = 7          # hoe ver terug bij de allereerste run
OVERLAP_HOURS = 30          # overlap met vorige run, zodat niets tussen wal en schip valt
MAX_PDF_PER_RUN = 1200      # rest gaat naar de achterstandsrij
MAX_PDF_BYTES = 40_000_000

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Language": "nl,en;q=0.8"})


# ------------------------------------------------------------------ hulpmiddelen

def load_yaml(name):
    with open(CONFIG / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"bronnen": {}, "pdf_rij": []}


def save_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def h(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def clean(text, limit=None):
    if not text:
        return ""
    text = BeautifulSoup(str(text), "html.parser").get_text(" ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] if limit else text


def get(url, tries=3, **kw):
    """Haal een adres op; bij een tijdelijke storing (5xx, time-out) tot 3 keer opnieuw."""
    for poging in range(tries):
        try:
            r = session.get(url, timeout=kw.pop("timeout", TIMEOUT), **kw)
            if r.status_code >= 500 and poging < tries - 1:
                time.sleep(5 * (poging + 1))
                continue
            r.raise_for_status()
            return r
        except (requests.ConnectionError, requests.Timeout):
            if poging == tries - 1:
                raise
            time.sleep(5 * (poging + 1))


def strip_ns(tag):
    return tag.split("}", 1)[-1]


# ------------------------------------------------------------------ trefwoorden

class Matcher:
    def __init__(self, cfg):
        self.rules = []  # (thema, term, regex)
        for thema, terms in cfg["thema's"].items():
            for t in terms:
                self.rules.append((thema, t.lstrip("="), self._rx(t)))
        for t in cfg.get("bouwcontext", []):
            self.rules.append(("Bouwcontext", t, self._rx(t)))
        self.niet_bij_bekendmakingen = {t.lower() for t in cfg.get("niet_bij_bekendmakingen", [])}
        self.volledige_tekst = cfg.get("volledige_tekst", [])

    @staticmethod
    def _rx(term):
        if term.startswith("="):
            # precies zo, hoofdlettergevoelig
            return re.compile(r"(?<![A-Za-z0-9])" + re.escape(term[1:]))
        if term.isupper() or (any(c.isupper() for c in term[1:]) and len(term) <= 6):
            # afkorting: hoofdlettergevoelig, als los woord
            return re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])")
        return re.compile(r"(?<![a-zà-ÿ0-9])" + re.escape(term.lower()), re.IGNORECASE)

    def match(self, text, bekendmaking=False, bouw=True):
        """Thema's met gevonden termen. Bij bekendmakingen tellen algemene
        vergunningstermen en (tenzij bouw=True) de bouwcontext niet mee."""
        found = {}
        if not text:
            return found
        for thema, term, rx in self.rules:
            if thema == "Bouwcontext" and not bouw:
                continue
            if bekendmaking and term.lower() in self.niet_bij_bekendmakingen:
                continue
            if rx.search(text):
                found.setdefault(thema, []).append(term)
        return found

    def snippets(self, text, max_n=4, width=180):
        out = []
        for thema, term, rx in self.rules:
            if thema == "Bouwcontext":
                continue
            m = rx.search(text)
            if m:
                a, b = max(0, m.start() - width), min(len(text), m.end() + width)
                out.append({"term": term, "fragment": "…" + text[a:b].strip() + "…"})
            if len(out) >= max_n:
                break
        return out


# ------------------------------------------------------------------ item-opbouw

def make_item(bron, *, uid, titel, url, datum=None, samenvatting="", soort="",
              extra=None, versie=""):
    return {
        "id": h(bron["naam"], uid),
        "bron": bron["naam"],
        "categorie": bron["categorie"],
        "methode": bron["methode"],
        "soort": soort,
        "titel": clean(titel, 400),
        "samenvatting_bron": clean(samenvatting, 1500),
        "url": url,
        "datum": datum,
        "versie": h(titel, versie or datum or ""),
        "extra": extra or {},
    }


# ------------------------------------------------------------------ Tweede Kamer

TK = "https://gegevensmagazijn.tweedekamer.nl/OData/v4/2.0/"
TK_PDF_SOORTEN = ("brief", "rapport", "bijlage", "nota", "verslag", "motie",
                  "amendement", "advies", "memorie", "voorstel", "notitie", "antwoord",
                  "position paper")
TK_PDF_OVERSLAAN = ("stenogram", "convocatie", "sprekerslijst", "mededeling",
                    "besluitenlijst", "agenda")
TK_MAX_LEEFTIJD = 90
TK_BESLUIT_ZAAKSOORTEN = ("motie", "amendement", "wetgeving", "initiatiefwetgeving",
                          "verdrag", "brief regering", "begroting")


def odata_all(url, max_pages=40):
    rows, pages = [], 0
    while url and pages < max_pages:
        js = get(url, headers={"Accept": "application/json"}).json()
        rows.extend(js.get("value", []))
        url = js.get("@odata.nextLink")
        pages += 1
    return rows, bool(url)


def tk_documenten(bron, since):
    ts = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    q = (f"{TK}Document?$filter=GewijzigdOp ge {ts} and Verwijderd eq false"
         "&$select=Id,Titel,Onderwerp,Soort,Datum,DocumentNummer,GewijzigdOp,ContentType"
         "&$expand=Zaak($select=Id,Nummer,Soort,Titel,Onderwerp)&$orderby=GewijzigdOp")
    rows, truncated = odata_all(q)
    items, oud = [], 0
    grens_oud = (NOW - dt.timedelta(days=TK_MAX_LEEFTIJD)).date().isoformat()
    for d in rows:
        # Oude stukken die alleen administratief zijn bijgewerkt, slaan we over
        if (d.get("Datum") or "9999")[:10] < grens_oud:
            oud += 1
            continue
        zaak = (d.get("Zaak") or [{}])[0] if d.get("Zaak") else {}
        nummer = zaak.get("Nummer")
        if nummer and d.get("DocumentNummer"):
            url = f"https://www.tweedekamer.nl/kamerstukken/detail?id={nummer}&did={d['DocumentNummer']}"
        else:
            url = f"https://www.tweedekamer.nl/downloads/document?id={d['Id']}"
        titel = d.get("Onderwerp") or d.get("Titel") or ""
        it = make_item(bron, uid=d["Id"], titel=titel, url=url,
                       datum=(d.get("Datum") or "")[:10], soort=d.get("Soort", ""),
                       samenvatting=f"{d.get('Titel','')}. Zaak: {zaak.get('Titel','')} {zaak.get('Onderwerp','')}",
                       versie=d.get("GewijzigdOp", ""),
                       extra={"zaaknummer": nummer, "zaaksoort": zaak.get("Soort"),
                              "documentnummer": d.get("DocumentNummer")})
        soort = (d.get("Soort") or "").lower()
        if not any(s in soort for s in TK_PDF_OVERSLAAN) and (
                "pdf" in (d.get("ContentType") or "") or any(s in soort for s in TK_PDF_SOORTEN)):
            it["_pdf"] = f"{TK}Document({d['Id']})/resource"
        items.append(it)
    notes = []
    if oud:
        notes.append(f"{oud} oudere stukken (> {TK_MAX_LEEFTIJD} dagen) alleen administratief bijgewerkt, overgeslagen")
    if truncated:
        notes.append("Meer dan 40 pagina's; rest volgt volgende run")
    return items, "; ".join(notes) or None


def tk_besluiten(bron, since):
    ts = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    q = (f"{TK}Besluit?$filter=GewijzigdOp ge {ts} and Verwijderd eq false"
         "&$select=Id,BesluitSoort,BesluitTekst,StemmingsSoort,Status,GewijzigdOp"
         "&$expand=Zaak($select=Id,Nummer,Soort,Titel,Onderwerp),"
         "Stemming($select=Soort,ActorFractie,FractieGrootte)&$orderby=GewijzigdOp")
    rows, truncated = odata_all(q)
    items, zonder_inhoud = [], 0
    for b in rows:
        zaken = b.get("Zaak") or []
        if not zaken or not (b.get("BesluitTekst") or "").strip():
            zonder_inhoud += 1
            continue
        z = zaken[0]
        zs = (z.get("Soort") or "").lower()
        tekst = b.get("BesluitTekst", "")
        if not any(s in zs for s in TK_BESLUIT_ZAAKSOORTEN) and not b.get("Stemming"):
            # procedurele besluiten (agendering, rondzending) over overige zaken:
            # alleen houden als het onderwerp raakt aan de thema's (wordt later bepaald)
            b["_procedureel"] = True
        stemmen = b.get("Stemming") or []
        voor = sorted({s["ActorFractie"] for s in stemmen if s.get("Soort") == "Voor" and s.get("ActorFractie")})
        tegen = sorted({s["ActorFractie"] for s in stemmen if s.get("Soort") == "Tegen" and s.get("ActorFractie")})
        zetels_voor = sum(s.get("FractieGrootte") or 0 for s in stemmen if s.get("Soort") == "Voor")
        zetels_tegen = sum(s.get("FractieGrootte") or 0 for s in stemmen if s.get("Soort") == "Tegen")
        samenvatting = f"{tekst}"
        if stemmen:
            samenvatting += f" Stemming: {zetels_voor} voor, {zetels_tegen} tegen."
        it = make_item(
            bron, uid=b["Id"], titel=f"{z.get('Soort','')}: {z.get('Onderwerp') or z.get('Titel','')}",
            url=f"https://www.tweedekamer.nl/kamerstukken/detail?id={z.get('Nummer','')}",
            datum=(b.get("GewijzigdOp") or "")[:10], soort="Besluit/stemming",
            samenvatting=samenvatting, versie=b.get("GewijzigdOp", ""),
            extra={"zaaknummer": z.get("Nummer"), "zaaksoort": z.get("Soort"),
                   "besluit": tekst, "fracties_voor": voor, "fracties_tegen": tegen})
        if b.get("_procedureel"):
            it["_alleen_bij_treffer"] = True
        items.append(it)
    notes = []
    if zonder_inhoud:
        notes.append(f"{zonder_inhoud} besluiten zonder besluittekst overgeslagen")
    if truncated:
        notes.append("Meer dan 40 pagina's; rest volgt volgende run")
    return items, "; ".join(notes) or None


# ------------------------------------------------------------------ KOOP (officiële bekendmakingen)

KOOP = "https://repository.overheid.nl/sru"
PUBNAAM = {"stb": "Staatsblad", "stcrt": "Staatscourant", "prb": "Provinciaal blad",
           "gmb": "Gemeenteblad", "wsb": "Waterschapsblad",
           "bgr": "Blad gemeenschappelijke regeling"}


def koop_parse(xml_text):
    root = ET.fromstring(xml_text)
    total, nxt, recs = 0, None, []
    for el in root.iter():
        t = strip_ns(el.tag)
        if t == "numberOfRecords":
            total = int(el.text or 0)
        elif t == "nextRecordPosition":
            nxt = int(el.text or 0)
        elif t == "record":
            r = {}
            for sub in el.iter():
                st = strip_ns(sub.tag)
                if sub.text and sub.text.strip() and st not in r:
                    r[st] = sub.text.strip()
            recs.append(r)
    return total, nxt, recs


def koop_query(cql, max_records=20000, page=500):
    out, start, total = [], 1, None
    while True:
        r = get(KOOP, params={"query": cql, "maximumRecords": page, "startRecord": start,
                              "operation": "searchRetrieve", "version": "2.0"}, timeout=120)
        total, nxt, recs = koop_parse(r.text)
        out.extend(recs)
        if not nxt or len(out) >= max_records or not recs:
            break
        start = nxt
    return out, total


def koop(bron, since, matcher):
    code = bron["publicatie"]
    naam = PUBNAAM[code]
    recs, total, notes = [], 0, []
    # Per dag opvragen: bij grote bladen (gemeenteblad) voorkomt dit time-outs.
    dag = since.date()
    while dag <= NOW.date():
        volgende = dag + dt.timedelta(days=1)
        base = (f'c.product-area==officielepublicaties AND dt.modified>={dag.isoformat()}'
                f' AND dt.modified<{volgende.isoformat()} AND w.publicatienaam=="{naam}"')
        r, t = koop_query(base)
        recs.extend(r)
        total += t or 0
        dag = volgende
    # Aanvullend: zoeken in de volledige tekst op onderscheidende woorden
    # (vindt ook stukken waarbij het trefwoord niet in de titel staat).
    fulltext_ids = set()
    if bron.get("filter") == "trefwoord" and matcher.volledige_tekst:
        base = (f'c.product-area==officielepublicaties AND dt.modified>={since.date().isoformat()}'
                f' AND w.publicatienaam=="{naam}"')
        terms = matcher.volledige_tekst
        for i in range(0, len(terms), 10):
            chunk = " OR ".join(f'cql.textAndIndexes="{t}"' for t in terms[i:i + 10])
            try:
                hits, _ = koop_query(f"{base} AND ({chunk})", max_records=3000)
                fulltext_ids.update(r.get("identifier") for r in hits)
            except Exception as e:  # volledige-tekstzoeken is een extra, geen vereiste
                notes.append(f"Zoeken in volledige tekst deels mislukt ({type(e).__name__})")
                break
    items = []
    for r in recs:
        ident = r.get("identifier", "")
        url = r.get("preferredUrl") or r.get("url") or f"https://zoek.officielebekendmakingen.nl/{ident}"
        it = make_item(bron, uid=ident, titel=r.get("title", ident), url=url,
                       datum=(r.get("available") or r.get("date") or r.get("modified") or "")[:10],
                       soort=r.get("type", ""), samenvatting=r.get("abstract", ""),
                       versie=r.get("modified", ""),
                       extra={"organisatie": r.get("creator"), "identifier": ident,
                              "spatial": r.get("spatial")})
        if ident in fulltext_ids:
            it["_fulltext_hit"] = True
        items.append(it)
    if total and total > len(recs):
        notes.append(f"{total} records gemeld, {len(recs)} opgehaald")
    if fulltext_ids:
        notes.append(f"{len(fulltext_ids)} treffers in volledige tekst")
    return items, "; ".join(notes) or None


# ------------------------------------------------------------------ rechtspraak

def rechtspraak(bron, since):
    items, start = [], 0
    while True:
        r = get(bron["url"], params={"modified": since.strftime("%Y-%m-%dT%H:%M:%S"),
                                     "max": 1000, "from": start, "return": "DOC"})
        feed = feedparser.parse(r.content)
        for e in feed.entries:
            ecli = e.get("id", "")
            titel = e.get("title", ecli)
            items.append(make_item(bron, uid=ecli, titel=titel,
                                   url=f"https://uitspraken.rechtspraak.nl/details?id={ecli}",
                                   datum=(e.get("updated") or "")[:10], soort="Uitspraak",
                                   samenvatting=e.get("summary", ""), versie=e.get("updated", ""),
                                   extra={"ecli": ecli,
                                          "raad_van_state": ":RVS:" in ecli}))
        if len(feed.entries) < 1000 or start > 20000:
            break
        start += 1000
    return items, None


# ------------------------------------------------------------------ KOOP-koppelingen voor internetconsultatie en wetgevingskalender
# Beide websites staan in robots.txt alleen zoekmachines toe. KOOP, de beheerder,
# heeft voor hergebruik deze officiële koppelingen aangewezen (zie data.overheid.nl,
# dataverzoek 268). We vragen ze één keer per dag op.

def _iso(datum):
    """Zet 12-10-2026 om naar 2026-10-12; laat 2026-10-12 ongemoeid."""
    m = re.match(r"(\d{1,2})-(\d{1,2})-(\d{4})", str(datum or ""))
    return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}" if m else str(datum or "")[:10]


KERNDEPARTEMENTEN = ("landbouw", "natuur", "lvvn", "volkshuisvesting", "vro", "infrastructuur",
                     "waterstaat", "ienw", "klimaat", "groene groei", "kgg")


def _veld(d, *namen):
    for k, v in d.items():
        kl = k.lower()
        if any(n in kl for n in namen) and v:
            return v
    return ""


def internetconsultatie(bron, since):
    url = bron.get("api", "https://www.internetconsultatie.nl/rest/openconsultaties/")
    r = get(url, headers={"Accept": "application/xml, text/xml, application/json"})
    DIAGNOSE.setdefault(bron["naam"], {})["voorbeeld"] = r.text[:2500]
    items = []
    if r.text.lstrip().startswith(("{", "[")):
        data = r.json()
        rows = data if isinstance(data, list) else next((v for v in data.values() if isinstance(v, list)), [])
    else:
        root = ET.fromstring(r.content)
        # het herhaalde element met kinderen is één consultatie
        tellingen = {}
        for el in root.iter():
            if len(list(el)) >= 2:
                tellingen[strip_ns(el.tag)] = tellingen.get(strip_ns(el.tag), 0) + 1
        tag = max((t for t in tellingen if t != strip_ns(root.tag)), key=lambda t: tellingen[t], default=None)
        rows = []
        for el in root.iter():
            if strip_ns(el.tag) == tag:
                rows.append({strip_ns(c.tag): (c.text or "").strip() for c in el.iter() if c is not el and (c.text or "").strip()})
    for d in rows:
        if not isinstance(d, dict):
            continue
        titel = _veld(d, "titel", "title", "naam")
        link = _veld(d, "url", "link", "href")
        if link and link.startswith("/"):
            link = "https://www.internetconsultatie.nl" + link
        sluit_ruw = _veld(d, "eind", "sluit", "einde", "end")
        sluit = _iso(sluit_ruw)
        start = _iso(_veld(d, "start", "begin", "publicatie", "datum"))
        items.append(make_item(bron, uid=link or titel, titel=titel, url=link or "https://www.internetconsultatie.nl/",
                               datum=str(start)[:10], soort="Internetconsultatie",
                               samenvatting=f"{_veld(d, 'omschrijving', 'beschrijving', 'samenvatting', 'description')} "
                                            f"Organisatie: {_veld(d, 'departement', 'ministerie', 'organisatie', 'eigenaar', 'department')}. "
                                            f"Sluit: {str(sluit)[:10]}",
                               versie=str(sluit_ruw), extra={"sluitingsdatum": sluit, "velden": list(d)[:20]}))
    if not items:
        raise ValueError("Koppeling gaf geen consultaties terug (zie diagnose)")
    return items, f"{len(items)} open consultaties via KOOP-koppeling"


def wetgevingskalender(bron, since):
    url = "https://zoekservice.overheid.nl/sru/Search"
    q = f"dcterms.modified>={since.date().isoformat()}"
    r = get(url, params={"operation": "searchRetrieve", "version": "1.2", "x-connection": "wgk",
                         "query": q, "maximumRecords": 500, "startRecord": 1})
    DIAGNOSE.setdefault(bron["naam"], {})["voorbeeld"] = r.text[:2500]
    total, nxt, recs = koop_parse(r.text)
    items = []
    for d in recs:
        ident = d.get("identifier", "")
        link = _veld(d, "preferredurl", "url", "link") or (
            ident if ident.startswith("http") else f"https://wetgevingskalender.overheid.nl/regeling/{ident}")
        dep = _veld(d, "departement", "creator", "ministerie")
        it = make_item(bron, uid=ident or d.get("title", ""), titel=d.get("title", ident),
                       url=f"https://wetgevingskalender.overheid.nl/regeling/{ident}" if ident.startswith("WGK") else link,
                       datum=(d.get("modified") or "")[:10], soort=f"Wetgevingskalender: {_veld(d, 'type')}",
                       samenvatting=f"{_veld(d, 'abstract', 'description', 'omschrijving')} "
                                    f"Ministerie: {dep}. Actieve fase: {_veld(d, 'fase', 'status', 'stadium')}.",
                       versie=d.get("modified", ""), extra={"ministerie": dep, "fase": _veld(d, "fase")})
        # regelingen van de voor bouw en natuur kernministeries gaan altijd door
        if any(k in dep.lower() for k in KERNDEPARTEMENTEN):
            it["_fulltext_hit"] = True
        items.append(it)
    note = f"{len(items)} gewijzigde regelingen via KOOP-zoekdienst"
    if total and total > len(items):
        note += f" ({total} gemeld)"
    return items, note


def plooi_proef(bron, since):
    """Proef: staan Woo-publicaties (PLOOI) ook in de KOOP-zoekdienst?"""
    uitkomst = {}
    for area in ("plooi", "openoverheid", "woo"):
        try:
            r = get(KOOP, params={"query": f"c.product-area=={area} AND dt.available>={since.date().isoformat()}",
                                  "maximumRecords": 3, "startRecord": 1, "operation": "searchRetrieve", "version": "2.0"}, tries=1)
            total, _, recs = koop_parse(r.text)
            uitkomst[area] = {"aantal": total, "voorbeeld": recs[:2]}
        except Exception as e:
            uitkomst[area] = {"fout": f"{type(e).__name__}: {str(e)[:120]}"}
    DIAGNOSE.setdefault(bron["naam"], {})["plooi_proef"] = uitkomst


# ------------------------------------------------------------------ planning: Tweede Kamer-activiteiten
# Alle geplande debatten, overleggen, rondetafelgesprekken en inbrengtermijnen
# van de komende maanden. Wordt elke run volledig opnieuw opgebouwd.

TK_PLANNING_DAGEN = 120
TK_COMMISSIES = ("LVVN", "IenW", "I&W", "VRO", "KGG", "BZK", "EZ", "EU")
# Herkenning op naam van de commissie, voor het geval de afkorting in de API afwijkt.
TK_KERN_NAMEN = ("landbouw", "infrastructuur en waterstaat", "volkshuisvesting", "klimaat en groene groei")
TK_OVERIG_NAMEN = ("binnenlandse zaken", "economische zaken", "europese zaken")


def tk_activiteiten(planning, matcher):
    van = NOW.date().isoformat()
    tot = (NOW + dt.timedelta(days=TK_PLANNING_DAGEN)).date().isoformat()
    q = (f"{TK}Activiteit?$filter=Datum ge {van}T00:00:00Z and Datum le {tot}T23:59:59Z and Verwijderd eq false"
         "&$orderby=Datum")
    rows, _ = odata_all(q, max_pages=30)
    n, telling = 0, {}
    for a in rows:
        soort = a.get("Soort") or ""
        if soort.lower() in ("procedurevergadering", "petitie", "werkbezoek", "delegatiereis"):
            continue
        onderwerp = a.get("Onderwerp") or ""
        commissie = a.get("Voortouwafkorting") or ""
        naam = (a.get("Voortouwnaam") or "").lower()
        telling[f"{commissie} | {a.get('Voortouwnaam') or ''}"] = telling.get(f"{commissie} | {a.get('Voortouwnaam') or ''}", 0) + 1
        kern = commissie in ("LVVN", "IenW", "I&W", "VRO", "KGG") or any(k in naam for k in TK_KERN_NAMEN)
        hits = {k: v for k, v in matcher.match(onderwerp).items() if k != "Bouwcontext"}
        if not hits and not kern and commissie not in TK_COMMISSIES \
                and not any(k in naam for k in TK_OVERIG_NAMEN):
            continue
        nummer = a.get("Nummer") or ""
        plenair = "plenair" in soort.lower() or (a.get("Voortouwnaam") or "").lower().startswith("tweede kamer")
        url = (f"https://www.tweedekamer.nl/debat_en_vergadering/plenaire_vergaderingen/details/activiteit?id={nummer}"
               if plenair else f"https://www.tweedekamer.nl/debat_en_vergadering/commissievergaderingen/details?id={nummer}")
        planning.append({
            "id": h("TK-activiteit", a.get("Id")), "bron": "Tweede Kamer", "soort": soort,
            "wat": onderwerp, "datum": (a.get("Datum") or "")[:10],
            "tijd": (a.get("Aanvangstijd") or "")[11:16], "status": a.get("Status"),
            "commissie": a.get("Voortouwnaam"), "commissie_afk": commissie, "url": url,
            "themas": sorted(hits), "kerncommissie": kern,
        })
        n += 1
    DIAGNOSE.setdefault("Tweede Kamer agenda", {})["per_commissie"] = telling
    return n


def planning_uit_consultaties(planning, items, matcher):
    for it in items:
        sluit = it.get("extra", {}).get("sluitingsdatum")
        if not sluit:
            continue
        hits = {k: v for k, v in matcher.match(it["titel"]).items() if k != "Bouwcontext"}
        planning.append({
            "id": it["id"], "bron": "Internetconsultatie", "soort": "Consultatie", "wat": it["titel"],
            "datum": sluit, "start": it.get("datum"), "url": it["url"],
            "commissie": (it["samenvatting_bron"].split("Organisatie:")[-1].split(".")[0].strip()
                          if "Organisatie:" in it["samenvatting_bron"] else ""),
            "themas": sorted(hits), "bouw": "Bouwcontext" in matcher.match(it["titel"]),
        })


# ------------------------------------------------------------------ onderzoek: OpenAlex
# OpenAlex is een open, gratis index van wetenschappelijke publicaties.
# De zoekvragen staan in config/onderzoek.yml.

def openalex(bron, since):
    cfg = load_yaml("onderzoek.yml")
    items, per = [], {}
    van = (since - dt.timedelta(days=2)).date().isoformat()
    for z in cfg.get("zoekvragen", []):
        # Zoeken in titel en samenvatting (niet in de volledige tekst), met AND, OR en "exacte woordgroepen".
        filt = (f'title_and_abstract.search:{z["zoek"]},from_publication_date:{van},is_paratext:false,'
                'type:article|review|preprint|report|book-chapter')
        if z.get("alleen_nl"):
            filt += ",authorships.institutions.country_code:NL"
        params = {"filter": filt, "sort": "publication_date:desc", "per_page": 25}
        try:
            time.sleep(1.5)  # OpenAlex beperkt het aantal verzoeken per seconde
            for poging in range(4):
                try:
                    r = get("https://api.openalex.org/works", params=params)
                    break
                except requests.HTTPError as fout:
                    if fout.response is not None and fout.response.status_code == 429 and poging < 3:
                        time.sleep(10 * (poging + 1))
                        continue
                    raise
            res = r.json().get("results", [])
        except Exception as e:
            per[z["zoek"]] = f"fout: {type(e).__name__}: {str(e)[:150]}"
            continue
        per[z["zoek"]] = len(res)
        for w in res:
            abstract = ""
            inv = w.get("abstract_inverted_index") or {}
            if inv:
                pos = {}
                for woord, plekken in inv.items():
                    for p in plekken:
                        pos[p] = woord
                abstract = " ".join(pos[i] for i in sorted(pos))[:1500]
            src = ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
            inst = sorted({i.get("display_name") for a in (w.get("authorships") or [])[:8]
                           for i in (a.get("institutions") or []) if i.get("display_name")})[:5]
            url = w.get("doi") or (w.get("primary_location") or {}).get("landing_page_url") or w.get("id")
            it = make_item(bron, uid=w.get("id"), titel=w.get("title") or "", url=url,
                           datum=w.get("publication_date"), soort=f"Wetenschappelijke publicatie ({w.get('type','')})",
                           samenvatting=abstract, versie=w.get("publication_date"),
                           extra={"tijdschrift": src, "instellingen": inst, "zoekvraag": z["zoek"],
                                  "thema_hint": z.get("thema"), "open_access": (w.get("open_access") or {}).get("is_oa")})
            items.append(it)
    DIAGNOSE.setdefault(bron["naam"], {})["per_zoekvraag"] = per
    # zelfde publicatie via meerdere zoekvragen: één keer
    uniek = {}
    for it in items:
        uniek.setdefault(it["id"], it)
    return list(uniek.values()), f"{len(uniek)} publicaties uit {len(per)} zoekvragen"


ONDERZOEK_WOORDEN = ("rapport", "onderzoek", "evaluatie", "monitor", "verkenning", "advies", "analyse",
                     "studie", "effectmeting", "doorrekening", "inventarisatie", "nulmeting")


def rubriek(it):
    """Voorlopige indeling: onderzoek, planning of bericht (Claude beslist uiteindelijk)."""
    t = it["titel"].lower()
    if it["methode"] == "api_openalex" or it["categorie"] == "Onderzoek":
        return "onderzoek"
    if it["methode"] == "api_tk_documenten" and (it.get("soort") or "").lower().startswith("bijlage") \
            and any(w in t for w in ONDERZOEK_WOORDEN) and "beslisnota" not in t:
        return "onderzoek"
    if it["methode"] == "api_internetconsultatie":
        return "planning"
    return "bericht"


# ------------------------------------------------------------------ feeds en webpagina's

_robots = {}


def robots_ok(url):
    p = urllib.parse.urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = session.get(base + "/robots.txt", timeout=15)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except Exception:
            rp.parse([])
        _robots[base] = rp
    return _robots[base].can_fetch(UA, url)


def feed_items(bron, feed_url, tries=3):
    r = get(feed_url, tries=tries, timeout=25 if tries == 1 else TIMEOUT)
    f = feedparser.parse(r.content)
    if not f.entries:
        raise ValueError("Feed bevat geen berichten")
    items = []
    for e in f.entries:
        link = e.get("link") or ""
        datum = ""
        for k in ("published_parsed", "updated_parsed"):
            if e.get(k):
                datum = time.strftime("%Y-%m-%d", e[k])
                break
        items.append(make_item(bron, uid=e.get("id") or link, titel=e.get("title", ""),
                               url=link, datum=datum, samenvatting=e.get("summary", ""),
                               versie=e.get("updated", "")))
    return items


def discover_feed(page_url, html):
    soup = BeautifulSoup(html, "html.parser")
    for l in soup.find_all("link", attrs={"rel": re.compile("alternate", re.I)}):
        if re.search(r"rss|atom", l.get("type", ""), re.I) and l.get("href"):
            return urllib.parse.urljoin(page_url, l["href"])
    return None


SKIP_LINK = re.compile(r"(login|inloggen|cookie|privacy|contact|zoeken|search|facebook|twitter|"
                       r"linkedin|instagram|youtube|mailto:|javascript:|#|vacature|werken-bij|"
                       r"disclaimer|toegankelijkheid|sitemap|nieuwsbrief)", re.I)


def page_links(bron, page_url, html):
    soup = BeautifulSoup(html, "html.parser")
    # alleen de algemene sitekop, navigatie en voettekst weghalen; een <header>
    # binnen een nieuwsbericht (kop met link) moet blijven staan
    for tag in soup(["nav", "footer", "script", "style"]):
        tag.decompose()
    for tag in soup.find_all("header"):
        if tag.find_parent(["article", "li", "section", "main"]) is None or tag.get("role") == "banner":
            tag.decompose()
    host = urllib.parse.urlparse(page_url).netloc.replace("www.", "")
    items, seen = [], set()
    for a in soup.find_all("a", href=True):
        url = urllib.parse.urljoin(page_url, a["href"]).split("#")[0]
        text = clean(a.get_text(" "))
        if url in seen or SKIP_LINK.search(url) or len(text) < 25:
            continue
        if host not in urllib.parse.urlparse(url).netloc:
            continue
        if urllib.parse.urlparse(url).path.count("/") < 2:
            continue
        seen.add(url)
        items.append(make_item(bron, uid=url, titel=text, url=url))
    return items


FEED_PADEN = ("feed/", "rss", "rss.xml", "feed", "nieuws/rss", "nieuws/rss.xml",
              "nieuws/feed/", "actueel/rss", "news/rss", "en/rss.xml")


def try_feed(bron, url):
    if not robots_ok(url):
        return None
    try:
        return feed_items(bron, url, tries=1)
    except Exception:
        return None


def web(bron, bstate):
    """Webpagina: 1) eerder gevonden feed, 2) feed op de pagina zelf,
    3) gangbare feedadressen, 4) feed op de homepage, 5) links op de pagina vergelijken."""
    url = bron["url"]
    feed_url = bstate.get("feed")
    if feed_url:
        items = try_feed(bron, feed_url)
        if items:
            return items, f"via feed {feed_url}", "feed"
        bstate.pop("feed", None)

    html, fout = None, None
    if robots_ok(url):
        try:
            html = get(url).text
        except Exception as e:
            fout = e
    else:
        fout = PermissionError("Website staat automatisch uitlezen van deze pagina niet toe (robots.txt)")

    kandidaten = []
    if html:
        found = discover_feed(url, html)
        if found:
            kandidaten.append(found)
    p = urllib.parse.urlparse(url)
    home = f"{p.scheme}://{p.netloc}/"
    base = url if url.endswith("/") else url.rsplit("/", 1)[0] + "/"
    for pad in FEED_PADEN:
        for b in {base, home}:
            kandidaten.append(urllib.parse.urljoin(b, pad))
    if url != home and robots_ok(home):
        try:
            hf = discover_feed(home, get(home, tries=1).text)
            if hf:
                kandidaten.insert(1, hf)
        except Exception:
            pass

    links = page_links(bron, url, html) if html else []
    if len(links) >= 3:
        # pagina bruikbaar; kijk alleen naar een feed die direct op de pagina staat
        if kandidaten and html and discover_feed(url, html):
            items = try_feed(bron, kandidaten[0])
            if items:
                bstate["feed"] = kandidaten[0]
                return items, f"feed gevonden: {kandidaten[0]}", "feed"
        return links, "links op pagina vergeleken", "pagina"

    seen = set()
    for k in kandidaten:
        if k in seen:
            continue
        seen.add(k)
        items = try_feed(bron, k)
        if items:
            bstate["feed"] = k
            return items, f"feed gevonden: {k}", "feed"
    # 6) laatste stap: de pagina laten opbouwen in een echte (onzichtbare) browser
    if robots_ok(url):
        try:
            rhtml, eindpunten = render(url)
            DIAGNOSE.setdefault(bron["naam"], {})["json_eindpunten"] = eindpunten[:15]
            found = discover_feed(url, rhtml)
            if found:
                items = try_feed(bron, found)
                if items:
                    bstate["feed"] = found
                    return items, f"feed gevonden na opbouwen pagina: {found}", "feed"
            rlinks = page_links(bron, url, rhtml)
            if len(rlinks) >= 3:
                return rlinks, "pagina opgebouwd in browser, links vergeleken", "browser"
            fout = ValueError(f"Ook na opbouwen in de browser geen berichten herkend ({len(rlinks)} links)")
        except Exception as e:
            fout = fout or e
            DIAGNOSE.setdefault(bron["naam"], {})["browserfout"] = f"{type(e).__name__}: {str(e)[:200]}"
    if fout:
        raise fout
    raise ValueError("Geen berichten herkend op de pagina en geen feed gevonden")


# ------------------------------------------------------------------ onzichtbare browser

DIAGNOSE = {}
_pw = {"browser": None}


def render(url):
    """Opent de pagina in een onzichtbare Chromium-browser, wacht tot de inhoud
    is opgebouwd en geeft de HTML terug, plus de JSON-adressen die de pagina zelf opvroeg."""
    if _pw["browser"] is None:
        from playwright.sync_api import sync_playwright
        _pw["ctx"] = sync_playwright().start()
        _pw["browser"] = _pw["ctx"].chromium.launch()
    page = _pw["browser"].new_page(user_agent=UA, locale="nl-NL")
    eindpunten = []

    def op_antwoord(resp):
        try:
            if "json" in (resp.headers.get("content-type") or "") and len(eindpunten) < 40:
                eindpunten.append(resp.url[:300])
        except Exception:
            pass
    page.on("response", op_antwoord)
    try:
        page.goto(url, wait_until="networkidle", timeout=45000)
        # cookiemelding: altijd de privacyvriendelijke keuze (alleen noodzakelijke cookies)
        for tekst in ("Alleen noodzakelijke cookies", "Alleen noodzakelijk", "Weigeren", "Afwijzen",
                      "Niet akkoord", "Reject all", "Decline", "Only necessary"):
            try:
                knop = page.get_by_role("button", name=tekst)
                if knop.count():
                    knop.first.click(timeout=2000)
                    page.wait_for_timeout(1500)
                    break
            except Exception:
                pass
        page.wait_for_timeout(2000)
        return page.content(), eindpunten
    finally:
        page.close()


def robots_samenvatting(url):
    p = urllib.parse.urlparse(url)
    try:
        r = session.get(f"{p.scheme}://{p.netloc}/robots.txt", timeout=15)
        regels = [l.strip() for l in r.text.splitlines() if l.strip() and not l.startswith("#")]
        return {"status": r.status_code, "regels": regels[:25],
                "pagina_toegestaan": robots_ok(url)}
    except Exception as e:
        return {"fout": f"{type(e).__name__}"}


# ------------------------------------------------------------------ PDF-tekst

def pdf_text(url):
    r = session.get(url, timeout=90, stream=True)
    r.raise_for_status()
    ctype = r.headers.get("Content-Type", "")
    buf = io.BytesIO()
    for chunk in r.iter_content(1 << 16):
        buf.write(chunk)
        if buf.tell() > MAX_PDF_BYTES:
            raise ValueError("Document te groot")
    raw = buf.getvalue()
    if raw[:4] == b"%PDF" and PdfReader:
        reader = PdfReader(io.BytesIO(raw))
        pages = len(reader.pages)
        text = " ".join((p.extract_text() or "") for p in reader.pages)
        return re.sub(r"\s+", " ", text), pages
    if "html" in ctype or "xml" in ctype or raw[:1] == b"<":
        return clean(raw.decode("utf-8", "ignore")), 1
    raise ValueError(f"Onbekend formaat ({ctype})")


# ------------------------------------------------------------------ compact overzicht

def compact(it):
    """Korte regel per stuk, zodat de beoordeling eerst alle titels kan overzien."""
    doc = it.get("document") or {}
    th = sorted(t for t in it.get("thema_treffers", {}) if t != "Bouwcontext")
    th_doc = sorted(t for t in (doc.get("thema_treffers_tekst") or {}) if t != "Bouwcontext")
    return {
        "id": it["id"], "bron": it["bron"], "cat": it["categorie"], "soort": it.get("soort", "")[:40],
        "titel": it["titel"][:220], "datum": it.get("datum"), "status": it.get("status"),
        "themas": th, "themas_tekst": th_doc, "bouw": "Bouwcontext" in it.get("thema_treffers", {}),
        "doc_fout": "fout" in doc, "volledige_tekst": it.get("treffer_in_volledige_tekst", False),
        "url": it["url"], "rubriek": rubriek(it),
    }


# ------------------------------------------------------------------ hoofdprogramma

def main():
    bronnen = load_yaml("bronnen.yml")["bronnen"]
    matcher = Matcher(load_yaml("trefwoorden.yml"))
    state = load_state()
    status, doorgelaten = [], []

    planning = []
    for bron in bronnen:
        naam = bron["naam"]
        bstate = state["bronnen"].setdefault(naam, {"gezien": {}})
        last = bstate.get("laatst_gelukt")
        since = (dt.datetime.fromisoformat(last) - dt.timedelta(hours=OVERLAP_HOURS)) if last \
            else NOW - dt.timedelta(days=FIRST_RUN_DAYS)
        st = {"bron": naam, "categorie": bron["categorie"], "methode": bron["methode"],
              "url": bron["url"], "filter": bron.get("filter", "alles"),
              "toegevoegd": bron.get("toegevoegd"), "gecontroleerd_op": NOW.isoformat(timespec="seconds"),
              "gelukt": False, "gevonden": 0, "nieuw": 0, "gewijzigd": 0, "doorgelaten": 0,
              "toelichting": None, "fout": None}
        print(f"→ {naam}", flush=True)
        try:
            eerste_keer = not bstate.get("laatst_gelukt")
            m = bron["methode"]
            if bron.get("proef") == "plooi":
                try:
                    plooi_proef(bron, since)
                except Exception:
                    pass
            if m in ("web", "zoekcontrole"):
                DIAGNOSE.setdefault(naam, {})["robots"] = robots_samenvatting(bron["url"])
            if m == "gedekt":
                st.update(gelukt=True, toelichting=bron.get("reden", "Gedekt via een andere bron"))
                status.append(st)
                continue
            if m == "zoekcontrole":
                st.update(gelukt=None, toelichting=(bron.get("reden", "Automatisch uitlezen is niet toegestaan of technisch niet mogelijk")
                          + ". Claude controleert deze bron met een gerichte zoekopdracht."))
                status.append(st)
                continue
            if m == "api_internetconsultatie":
                items, note = internetconsultatie(bron, since)
                planning_uit_consultaties(planning, items, matcher)
            elif m == "api_wetgevingskalender":
                items, note = wetgevingskalender(bron, since)
            elif m == "api_openalex":
                items, note = openalex(bron, since)
            elif m == "api_tk_documenten":
                items, note = tk_documenten(bron, since)
            elif m == "api_tk_besluiten":
                items, note = tk_besluiten(bron, since)
            elif m == "api_koop":
                items, note = koop(bron, since, matcher)
            elif m == "api_rechtspraak":
                items, note = rechtspraak(bron, since)
            elif m == "feed":
                items, note = feed_items(bron, bron["url"]), None
            elif m == "web":
                items, note, sub = web(bron, bstate)
                st["methode_gebruikt"] = sub
                # Bij een webpagina zonder feed is de eerste keer een nulmeting:
                # alles wat er al staat is 'oud'.
                if sub in ("pagina", "browser") and eerste_keer:
                    for it in items:
                        bstate["gezien"][it["id"]] = it["versie"]
                    st.update(gelukt=True, gevonden=len(items),
                              toelichting="Nulmeting: huidige berichten vastgelegd, nieuwe verschijnen vanaf morgen")
                    bstate["laatst_gelukt"] = NOW.isoformat()
                    status.append(st)
                    continue
            else:
                raise ValueError(f"Onbekende methode {m}")

            st["gevonden"] = len(items)
            st["toelichting"] = note
            if m == "web":
                st["toelichting"] = note
            # Een webpagina waarvan ineens vrijwel alle links nieuw zijn (andere opmaak of
            # verbeterde herkenning), wordt opnieuw als nulmeting vastgelegd in plaats
            # van tientallen oude berichten als nieuw te tonen.
            if m == "web" and st.get("methode_gebruikt") in ("pagina", "browser"):
                onbekend = [it for it in items if it["id"] not in bstate["gezien"]]
                if len(onbekend) > 10 and len(onbekend) > 0.6 * len(items):
                    for it in items:
                        bstate["gezien"][it["id"]] = it["versie"]
                    st.update(gelukt=True, toelichting="Opmaak gewijzigd of herkenning verbeterd: opnieuw nulmeting vastgelegd")
                    bstate["laatst_gelukt"] = NOW.isoformat()
                    status.append(st)
                    continue
            bekendmaking = m in ("api_koop", "api_rechtspraak")
            grens_feed = (since - dt.timedelta(days=2)).date().isoformat()
            for it in items:
                vorige = bstate["gezien"].get(it["id"])
                if vorige == it["versie"]:
                    continue
                bstate["gezien"][it["id"]] = it["versie"]
                # oude berichten in een (pas gevonden) feed niet als nieuw tonen
                if m in ("feed", "web") and it.get("datum") and it["datum"] < grens_feed and not eerste_keer:
                    continue
                it["status"] = "gewijzigd" if vorige else "nieuw"
                st["gewijzigd" if vorige else "nieuw"] += 1
                tekst = f"{it['titel']} {it['samenvatting_bron']}"
                rvs = bool(it.get("extra", {}).get("raad_van_state"))
                hits = matcher.match(tekst, bekendmaking=bekendmaking, bouw=(not bekendmaking) or rvs)
                it["thema_treffers"] = hits
                ft = it.pop("_fulltext_hit", False)
                if ft:
                    it["treffer_in_volledige_tekst"] = True
                alleen_bij_treffer = it.pop("_alleen_bij_treffer", False)
                filt = "trefwoord" if alleen_bij_treffer else bron.get("filter", "alles")
                if filt == "alles" or hits or ft:
                    it["verzameld_op"] = NOW.isoformat(timespec="seconds")
                    doorgelaten.append(it)
                    st["doorgelaten"] += 1
                elif it.get("_pdf"):
                    # titel zegt niets, maar het document zelf kan relevant zijn
                    it["verzameld_op"] = NOW.isoformat(timespec="seconds")
                    it["_alleen_bij_tekstrefer"] = True
                    doorgelaten.append(it)
            st["gelukt"] = True
            bstate["laatst_gelukt"] = NOW.isoformat()
            bstate["laatste_fout"] = None
        except Exception as e:
            st["fout"] = f"{type(e).__name__}: {str(e)[:300]}"
            bstate["laatste_fout"] = st["fout"]
            bstate["fouten_op_rij"] = bstate.get("fouten_op_rij", 0) + 1
            traceback.print_exc()
        else:
            bstate["fouten_op_rij"] = 0
        st["fouten_op_rij"] = bstate.get("fouten_op_rij", 0)
        st["laatst_gelukt"] = bstate.get("laatst_gelukt")
        status.append(st)
        # geheugen begrenzen
        if len(bstate["gezien"]) > 60000:
            bstate["gezien"] = dict(list(bstate["gezien"].items())[-40000:])

    # ---- planning: Tweede Kamer-activiteiten ----
    try:
        n_act = tk_activiteiten(planning, matcher)
        status.append({"bron": "Tweede Kamer (agenda: debatten en overleggen)", "categorie": "Parlement",
                       "methode": "api_tk_activiteiten", "url": TK, "filter": "commissie of thema",
                       "gelukt": True, "gevonden": n_act, "nieuw": 0, "gewijzigd": 0, "doorgelaten": n_act,
                       "toelichting": f"{n_act} geplande activiteiten in de komende {TK_PLANNING_DAGEN} dagen",
                       "fout": None, "gecontroleerd_op": NOW.isoformat(timespec="seconds")})
    except Exception as e:
        status.append({"bron": "Tweede Kamer (agenda: debatten en overleggen)", "categorie": "Parlement",
                       "methode": "api_tk_activiteiten", "url": TK, "gelukt": False, "gevonden": 0, "nieuw": 0,
                       "gewijzigd": 0, "doorgelaten": 0, "toelichting": None,
                       "fout": f"{type(e).__name__}: {str(e)[:300]}", "gecontroleerd_op": NOW.isoformat(timespec="seconds")})
    planning.sort(key=lambda p: (p.get("datum") or "9999", p.get("tijd") or ""))

    # ---- documenten lezen (bijlagen, rapporten, Kamerbrieven) ----
    # Stukken die op hun tekst wachten. Een stuk dat alleen om zijn tekst is
    # doorgelaten, gaat pas naar de beoordeling als de tekst een themawoord bevat.
    oude_rij = [r for r in state.get("pdf_rij", []) if r.get("item")]
    vervallen = len(state.get("pdf_rij", [])) - len(oude_rij)
    rij = oude_rij + [{"item": it, "url": it["_pdf"]} for it in doorgelaten if it.get("_pdf")]
    seen_ids, uniq = set(), []
    for r in rij:
        if r["item"]["id"] not in seen_ids:
            seen_ids.add(r["item"]["id"]); uniq.append(r)
    nu, later = uniq[:MAX_PDF_PER_RUN], uniq[MAX_PDF_PER_RUN:]
    nu_ids = {r["item"]["id"] for r in nu}
    later_ids = {r["item"]["id"] for r in later}

    def lees(r):
        try:
            text, pages = pdf_text(r["url"])
            hits = matcher.match(text)
            return r, {"pagina's": pages, "thema_treffers_tekst": hits,
                       "fragmenten": matcher.snippets(text), "tekst_begin": text[:1200]}, None
        except Exception as e:
            return r, None, f"{type(e).__name__}: {str(e)[:150]}"

    gelezen, mislukt, uit_rij = 0, [], []
    doorgelaten_ids = {it["id"] for it in doorgelaten}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for r, info, fout in pool.map(lees, nu):
            it = r["item"]
            if info:
                gelezen += 1
                it["document"] = info
            else:
                mislukt.append({"id": it["id"], "titel": it["titel"][:120], "url": r["url"], "fout": fout})
                it["document"] = {"fout": "Document kon niet worden gelezen"}
            if it["id"] not in doorgelaten_ids:
                it["later_gelezen"] = True
                uit_rij.append(it)

    final = []
    for it in doorgelaten + uit_rij:
        if it["id"] in later_ids:
            continue  # komt in een volgende run, na het lezen
        it.pop("_pdf", None)
        if it.pop("_alleen_bij_tekstrefer", False) or it.get("_tekst_nodig"):
            it.pop("_tekst_nodig", None)
            doc = it.get("document", {})
            hits = {k: v for k, v in (doc.get("thema_treffers_tekst") or {}).items() if k != "Bouwcontext"}
            if not hits and "fout" not in doc:
                continue
            for st in status:
                if st["bron"] == it["bron"]:
                    st["doorgelaten"] += 1
        final.append(it)
    for r in later:
        if r["item"].pop("_alleen_bij_tekstrefer", False):
            r["item"]["_tekst_nodig"] = True
    state["pdf_rij"] = later

    # ---- wegschrijven ----
    run = {
        "run_op": NOW.isoformat(timespec="seconds"),
        "bronnen_totaal": len(status),
        "bronnen_gelukt": sum(1 for s in status if s["gelukt"]),
        "bronnen_mislukt": [s["bron"] for s in status if s["gelukt"] is False],
        "bronnen_zoekcontrole": [s["bron"] for s in status if s["gelukt"] is None],
        "documenten_gelezen": gelezen,
        "documenten_mislukt": mislukt,
        "achterstand_documenten": len(later),
        "vervallen_uit_oude_rij": vervallen,
        "doorgelaten": len(final),
    }
    save_json(DATA / "kandidaten.json", {"run": run, "items": final})
    save_json(DATA / "status.json", {"run": run, "bronnen": status})
    save_json(DATA / "diagnose.json", DIAGNOSE)
    save_json(DATA / "planning.json", {"bijgewerkt": NOW.isoformat(timespec="seconds"), "items": planning})
    if _pw.get("browser"):
        try:
            _pw["browser"].close(); _pw["ctx"].stop()
        except Exception:
            pass
    overzicht = {"run": run, "items": [compact(it) for it in final]}
    save_json(DATA / "overzicht.json", overzicht)
    save_json(DATA / "archief" / f"{TODAY}.json", overzicht)

    recent_path = DATA / "recent.json"
    recent = json.loads(recent_path.read_text(encoding="utf-8"))["items"] if recent_path.exists() else []
    grens = (NOW - dt.timedelta(days=RECENT_DAYS)).isoformat()
    ids = {it["id"] for it in final}
    recent = [it for it in recent if it.get("verzameld_op", "") >= grens and it["id"] not in ids] + final
    save_json(recent_path, {"bijgewerkt": NOW.isoformat(timespec="seconds"), "items": recent})
    save_json(STATE_FILE, state)

    print(json.dumps(run, ensure_ascii=False, indent=1))
    # Indicatoren voor de voorpagina: één keer per maand
    try:
        import indicatoren
        indicatoren.run()
    except Exception as e:
        print(f"Indicatoren mislukt: {type(e).__name__}: {e}")
    # Mislukt alleen als vrijwel alles faalt (dan is er iets mis met de omgeving)
    if run["bronnen_gelukt"] < max(1, len(status) // 4):
        sys.exit(1)


if __name__ == "__main__":
    main()
