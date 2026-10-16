"""Grondwaterindicator uit de BRO (Basisregistratie Ondergrond).

Werkwijze:
1. Per gebied uit config/grondwater.yml worden de peilbuizen opgezocht (via PDOK).
2. Alleen ondiepe filters met een lange meetreeks tellen mee.
3. Per buis en per zomer (april tot en met september) wordt de laagste stand bepaald.
4. Een zomer is "uitzonderlijk laag" als die laagste stand bij de laagste 10 procent
   van alle zomers van díe buis hoort.
5. Per jaar: het aandeel buizen met een uitzonderlijk lage zomerstand, landelijk en per gebied.

Het resultaat komt in data/grondwater.json, met een uitgebreide diagnose zodat de
werkwijze controleerbaar is.
"""
import datetime as dt
import json
import traceback
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
UIT = ROOT / "data" / "grondwater.json"


def _kolom(df, *namen):
    for n in namen:
        if n in df.columns:
            return n
    return None


def zomerminima(obs):
    """obs: DataFrame met een tijdindex (of tijdkolom) en een waardekolom. Geeft {jaar: laagste zomerstand}."""
    import pandas as pd
    df = obs.copy()
    if not isinstance(df.index, pd.DatetimeIndex):
        tk = _kolom(df, "time", "date", "datetime", "Tijdstip", "timestamp")
        if tk is None:
            return {}
        df.index = pd.to_datetime(df[tk], errors="coerce", utc=True)
    df = df[~df.index.isna()]
    vk = _kolom(df, "value", "waarde", "Value")
    if vk is None:
        return {}
    df = df[[vk]].rename(columns={vk: "v"})
    df["v"] = pd.to_numeric(df["v"], errors="coerce")
    df = df.dropna()
    df = df[(df.index.month >= 4) & (df.index.month <= 9)]
    uit = {}
    for jaar, groep in df.groupby(df.index.year):
        if len(groep) >= 6:  # handmatige metingen zijn vaak tweewekelijks
            uit[int(jaar)] = float(groep["v"].min())
    return uit


def run():
    cfg = yaml.safe_load((ROOT / "config" / "grondwater.yml").read_text(encoding="utf-8"))
    diag = {"gestart": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "gebieden": {}}
    buizen = []  # [{gebied, buis, minima}]
    try:
        import brodata
    except Exception as e:
        diag["fout"] = f"brodata niet beschikbaar: {e}"
        UIT.write_text(json.dumps({"diagnose": diag}, ensure_ascii=False), encoding="utf-8")
        return
    max_diepte = cfg.get("max_filterdiepte_m", 5)
    min_zomers = cfg.get("min_zomers", 15)
    half = cfg.get("halve_breedte_m", 3000)
    for g in cfg["gebieden"]:
        d = diag["gebieden"].setdefault(g["naam"], {})
        try:
            x, y = g["x"], g["y"]
            extent = [x - half, x + half, y - half, y + half]
            tubes = brodata.gm.get_data_in_extent(extent, kind=None)
            d["buizen_gevonden"] = int(len(tubes))
            d["kolommen"] = [str(c) for c in tubes.columns][:40]
            # ondiepe filters: bovenkant filter minder dan max_diepte onder maaiveld
            top = _kolom(tubes, "screen_top_position", "screenTopPosition")
            mv = _kolom(tubes, "ground_level_position", "groundLevelPosition", "surface_level_position", "tube_top_position", "tubeTopPosition")
            d["diepte_kolommen"] = [top, mv]
            sel = tubes
            if top and mv:
                import pandas as pd
                diepte = pd.to_numeric(tubes[mv], errors="coerce") - pd.to_numeric(tubes[top], errors="coerce")
                sel = tubes[(diepte >= 0) & (diepte <= max_diepte)]
            sel = sel.head(cfg.get("max_buizen_per_gebied", 25))
            d["buizen_ondiep"] = int(len(sel))
            if len(sel) == 0:
                continue
            obs = brodata.gm.get_observations(extent=extent, tubes=sel, kind="gld", as_csv=True,
                                              tmin="1990-01-01", silent=True, continue_on_error=True)
            if obs is None:
                d["fout"] = "geen meetreeksen gevonden"
                continue
            obs = obs.reset_index().set_index(["groundwaterMonitoringWell", "tubeNumber"])
            met = brodata.gmw.add_observations_to_tubes(sel, obs, kind="gld")
            telling = 0
            for sleutel, rij in met.iterrows():
                reeks = rij.get("observation")
                if reeks is None or not hasattr(reeks, "index") or len(reeks) == 0:
                    continue
                minima = zomerminima(reeks)
                if len(minima) >= min_zomers:
                    buizen.append({"gebied": g["naam"], "type": g.get("type", ""), "buis": str(sleutel), "minima": minima})
                    telling += 1
            d["buizen_met_lange_reeks"] = telling
        except Exception as e:
            d["fout"] = f"{type(e).__name__}: {str(e)[:300]}"
            d["traceback"] = traceback.format_exc()[-1200:]

    # indicator per jaar
    def aandeel(lijst):
        per_jaar = {}
        for b in lijst:
            waarden = sorted(b["minima"].values())
            grens = waarden[max(0, int(round(0.10 * len(waarden))) - 1)]
            for jaar, v in b["minima"].items():
                t = per_jaar.setdefault(jaar, [0, 0])
                t[1] += 1
                t[0] += v <= grens
        return [{"jaar": j, "aandeel": round(100 * a / n, 1), "buizen": n}
                for j, (a, n) in sorted(per_jaar.items()) if n >= max(5, len(lijst) // 3)]

    uit = {
        "opgehaald": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "methode": "aandeel ondiepe BRO-peilbuizen met een laagste zomerstand (april tot en met september) in de laagste 10 procent van de eigen meetreeks",
        "aantal_buizen": len(buizen),
        "landelijk": aandeel(buizen),
        "per_type": {t: aandeel([b for b in buizen if b["type"] == t]) for t in sorted({b["type"] for b in buizen})},
        "per_gebied": {g: len([b for b in buizen if b["gebied"] == g]) for g in sorted({b["gebied"] for b in buizen})},
        "diagnose": diag,
    }
    UIT.write_text(json.dumps(uit, ensure_ascii=False), encoding="utf-8")
    print(f"Grondwater: {len(buizen)} buizen met een lange reeks")


if __name__ == "__main__":
    run()
