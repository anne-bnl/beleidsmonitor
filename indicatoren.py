"""Maandelijkse ophaalroutine voor de indicatoren op de voorpagina van het dashboard.

Haalt open data op bij CBS (StatLine) en KNMI en slaat die op in data/indicatoren_ruw.json.
Draait alleen als de vorige ophaalronde langer dan 'dagen_tussen_updates' geleden is,
of als de omgevingsvariabele INDICATOREN de waarde 'ja' heeft.
"""
import datetime as dt
import io
import json
import os
import time
import zipfile
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
DATA = ROOT / "data"
UIT = DATA / "indicatoren_ruw.json"
CBS = "https://opendata.cbs.nl/ODataApi/OData"
FEED = "https://opendata.cbs.nl/ODataFeed/OData"
CATALOGUS = "https://opendata.cbs.nl/ODataCatalog/Tables"
KNMI = "https://cdn.knmi.nl/knmi/map/page/klimatologie/gegevens/daggegevens/etmgeg_{st}.zip"

s = requests.Session()
s.headers.update({"User-Agent": "Beleidsmonitor Natuur en Milieu (GitHub Actions; open data)"})


def haal(url, params=None, pogingen=3):
    for p in range(pogingen):
        try:
            r = s.get(url, params=params, timeout=90)
            if r.status_code >= 500 and p < pogingen - 1:
                time.sleep(5 * (p + 1)); continue
            r.raise_for_status()
            return r
        except (requests.ConnectionError, requests.Timeout):
            if p == pogingen - 1:
                raise
            time.sleep(5 * (p + 1))


def cbs_tabel(cfg):
    tid = cfg["id"]
    uit = {"naam": cfg["naam"], "id": tid}
    info = haal(f"{CBS}/{tid}/TableInfos").json()["value"][0]
    uit["titel"], uit["gewijzigd"], uit["periode"] = info.get("Title"), info.get("Modified"), info.get("Period")
    props = haal(f"{CBS}/{tid}/DataProperties").json()["value"]
    uit["onderwerpen"] = [{"key": p["Key"], "titel": p.get("Title"), "eenheid": p.get("Unit")}
                         for p in props if p.get("Type") in ("Topic",)]
    dims = {}
    filt = None
    for p in props:
        if p.get("Type") in ("Dimension", "TimeDimension", "GeoDimension", "GeoDetail"):
            rijen = haal(f"{CBS}/{tid}/{p['Key']}").json()["value"]
            dims[p["Key"]] = {r["Key"]: r.get("Title") for r in rijen}
            if cfg.get("alleen_nederland") and p.get("Type") in ("GeoDimension", "GeoDetail", "Dimension") \
                    and p["Key"].lower().startswith("regio"):
                nl = [r["Key"] for r in rijen if (r.get("Title") or "").strip().lower() == "nederland"]
                if nl:
                    filt = f"{p['Key']} eq '{nl[0]}'"
    uit["dimensies"] = dims
    # De ODataApi geeft maximaal 10.000 rijen en kent geen $skip; bij meer rijen volgt een vervolglink.
    params = {"$format": "json"}
    if filt:
        params["$filter"] = filt
    try:
        r = haal(f"{CBS}/{tid}/TypedDataSet", params=params).json()
        rijen = r.get("value", [])
        volgende = r.get("odata.nextLink")
        while volgende and len(rijen) < 60000:
            r = haal(volgende).json()
            rijen += r.get("value", [])
            volgende = r.get("odata.nextLink")
    except requests.HTTPError:
        # Terugval: de ODataFeed ondersteunt wel bladeren met $skip
        rijen, skip = [], 0
        while len(rijen) < 60000:
            r = haal(f"{FEED}/{tid}/TypedDataSet", params={**params, "$top": 5000, "$skip": skip}).json()
            deel = r.get("value", [])
            rijen += deel
            if len(deel) < 5000:
                break
            skip += 5000
        uit["via"] = "ODataFeed"
    uit["rijen"] = rijen
    uit["aantal_rijen"] = len(rijen)
    return uit


def cbs_zoek(woorden):
    gevonden = []
    for w in woorden:
        try:
            r = haal(CATALOGUS, params={"$filter": f"substringof('{w}',Title)", "$format": "json",
                                        "$select": "Identifier,Title,Modified,Period"})
            for t in r.json().get("value", []):
                gevonden.append({"zoekwoord": w, **t})
        except Exception as e:
            gevonden.append({"zoekwoord": w, "fout": f"{type(e).__name__}: {str(e)[:150]}"})
    return gevonden


def knmi_jaarreeks(station):
    """Jaargemiddelde temperatuur, jaarneerslag en aantal zomerse en tropische dagen."""
    r = haal(KNMI.format(st=station))
    z = zipfile.ZipFile(io.BytesIO(r.content))
    tekst = z.read(z.namelist()[0]).decode("latin-1")
    kop, per_jaar = None, {}
    for regel in tekst.splitlines():
        if regel.startswith("# STN,YYYYMMDD"):
            kop = [k.strip() for k in regel.lstrip("# ").split(",")]
            continue
        if not kop or regel.startswith("#") or not regel.strip():
            continue
        v = dict(zip(kop, [x.strip() for x in regel.split(",")]))
        jaar = int(v["YYYYMMDD"][:4])
        j = per_jaar.setdefault(jaar, {"dagen": 0, "tg": [], "rh": [], "tx25": 0, "tx30": 0})
        j["dagen"] += 1
        if v.get("TG"):
            j["tg"].append(int(v["TG"]) / 10)
        if v.get("RH"):
            j["rh"].append(max(int(v["RH"]), 0) / 10)  # -1 betekent minder dan 0,05 mm
        if v.get("TX"):
            tx = int(v["TX"]) / 10
            j["tx25"] += tx >= 25
            j["tx30"] += tx >= 30
    reeks = []
    for jaar in sorted(per_jaar):
        j = per_jaar[jaar]
        if j["dagen"] < 360:
            continue  # onvolledig jaar (meestal het lopende jaar)
        reeks.append({
            "jaar": jaar,
            "temperatuur": round(sum(j["tg"]) / len(j["tg"]), 2) if len(j["tg"]) > 350 else None,
            "neerslag": round(sum(j["rh"])) if len(j["rh"]) > 350 else None,
            "zomerse_dagen": j["tx25"], "tropische_dagen": j["tx30"],
        })
    return reeks


def archief_per_maand():
    """Telt per maand de stukken in het archief van de monitor, per thema en per soort bron."""
    maanden = {}
    for f in sorted((DATA / "archief").glob("*.json")):
        try:
            items = json.loads(f.read_text(encoding="utf-8")).get("items", [])
        except Exception:
            continue
        m = maanden.setdefault(f.stem[:7], {"dagen": 0, "totaal": 0, "thema": {}, "categorie": {}, "_ids": set()})
        m["dagen"] += 1
        for it in items:
            if it["id"] in m["_ids"]:
                continue
            m["_ids"].add(it["id"])
            m["totaal"] += 1
            m["categorie"][it.get("cat", "")] = m["categorie"].get(it.get("cat", ""), 0) + 1
            for t in set(it.get("themas", [])) | set(it.get("themas_tekst", [])):
                m["thema"][t] = m["thema"].get(t, 0) + 1
    for m in maanden.values():
        m.pop("_ids")
    return maanden


def run(force=False):
    cfg = yaml.safe_load((ROOT / "config" / "indicatoren.yml").read_text(encoding="utf-8"))
    nu = dt.datetime.now(dt.timezone.utc)
    if UIT.exists() and not force and os.environ.get("INDICATOREN", "").lower() != "ja":
        try:
            oud = json.loads(UIT.read_text(encoding="utf-8"))
            vorige = dt.datetime.fromisoformat(oud["opgehaald"])
            mislukt = any(str(v).startswith("fout") for v in oud.get("status", {}).values())
            if (nu - vorige).days < cfg.get("dagen_tussen_updates", 28) and not mislukt:
                print(f"Indicatoren: overgeslagen, laatst opgehaald {vorige.date()}")
                return None
        except Exception:
            pass
    uit = {"opgehaald": nu.isoformat(timespec="seconds"), "status": {}, "cbs": {}, "knmi": None}
    for t in cfg.get("cbs_tabellen", []):
        try:
            uit["cbs"][t["id"]] = cbs_tabel(t)
            uit["status"][t["naam"]] = f"ok ({uit['cbs'][t['id']]['aantal_rijen']} rijen)"
        except Exception as e:
            uit["status"][t["naam"]] = f"fout: {type(e).__name__}: {str(e)[:200]}"
    uit["cbs_catalogus"] = cbs_zoek(cfg.get("cbs_zoeken", []))
    try:
        uit["knmi"] = knmi_jaarreeks(cfg.get("knmi_station", 260))
        uit["status"]["KNMI"] = f"ok ({len(uit['knmi'])} jaren)"
    except Exception as e:
        uit["status"]["KNMI"] = f"fout: {type(e).__name__}: {str(e)[:200]}"
    try:
        uit["monitor_per_maand"] = archief_per_maand()
    except Exception as e:
        uit["status"]["Monitor"] = f"fout: {type(e).__name__}: {str(e)[:200]}"
    DATA.mkdir(exist_ok=True)
    UIT.write_text(json.dumps(uit, ensure_ascii=False), encoding="utf-8")
    print("Indicatoren:", json.dumps(uit["status"], ensure_ascii=False))
    return uit


if __name__ == "__main__":
    run(force=True)
