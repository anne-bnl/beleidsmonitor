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
    for tag in soup(["nav", "header", "footer", "script", "style"]):
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
    if fout:
        raise fout
    raise ValueError("Geen berichten herkend op de pagina en geen feed gevonden "
                     "(pagina wordt waarschijnlijk pas in de browser opgebouwd)")


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
        "url": it["url"],
    }


# ------------------------------------------------------------------ hoofdprogramma

def main():
    bronnen = load_yaml("bronnen.yml")["bronnen"]
    matcher = Matcher(load_yaml("trefwoorden.yml"))
    state = load_state()
    status, doorgelaten = [], []

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
            if m == "zoekcontrole":
                st.update(gelukt=None, toelichting="Automatisch uitlezen is niet toegestaan of technisch niet mogelijk; "
                          "Claude controleert deze bron met een gerichte zoekopdracht")
                status.append(st)
                continue
            if m == "api_tk_documenten":
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
                if sub == "pagina" and eerste_keer:
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
    # Mislukt alleen als vrijwel alles faalt (dan is er iets mis met de omgeving)
    if run["bronnen_gelukt"] < max(1, len(status) // 4):
        sys.exit(1)


if __name__ == "__main__":
    main()
