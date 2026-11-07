#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
simem_download.py
=================
Holt die Heat-Rate-relevanten Daten aus SIMEM (Sistema de Informacion de
Mineria y Energia, www.simem.co). SIMEM ist das offene Datenportal von
Minenergia/XM und ist - anders als servapibi.xm.com.co - aus Europa
erreichbar.

Warum SIMEM und nicht servapibi:
  servapibi nimmt aus EU keine TCP-Verbindungen auf Port 443 an (geprueft
  04.11.2026). SIMEM laeuft ueber www.simem.co und antwortet.

Relevante Variablen im SIMEM-Katalog:
  ConsumoCombustible   Brennstoffverbrauch je Anlage, stuendlich
  Gene                 Reale Erzeugung je Ressource, stuendlich
  CapEfectivaNeta      Netto-Effektivleistung je Anlage
  PB_Nal               Boersenpreis national, stuendlich

Aufruf:
    python simem_download.py --katalog
        Zeigt alle Variablen mit 'combust', 'gene', 'capac' im Namen.

    python simem_download.py --var ConsumoCombustible --start 2025-01-01 --end 2025-12-31
        Laedt eine Variable als CSV.

SIMEM-Besonderheit: Das API-Fenster ist bei stuendlicher Aufloesung auf
1 Tag begrenzt, bei monatlicher auf 24 Tage. Das Skript stueckelt
automatisch.

Abhaengigkeiten: requests
"""

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta

try:
    import requests
except ImportError:
    sys.exit("Fehlt: requests   ->   python -m pip install requests")

KATALOG_URL = "https://www.simem.co/backend-datos/vars/listado_variables.json"
BASE_INFO = "https://www.simem.co/backend-files/api/detalle-datos-publicos"
BASE_DATA = "https://www.simem.co/backend-files/api/datos-publicos"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/csv, */*",
    "Referer": "https://www.simem.co/",
}

# Aus dem Katalog bekannte Werte (Stand 04.11.2026)
BEKANNT = {
    "ConsumoCombustible": {
        "dataset_id": "C41FE8",
        "value_column": "ValorTexto",
        "date_column": "FechaHora",
        "dimensions": ["CodigoSICAgente", "CodigoPlanta"],
        "name": "Consumo de combustible (Brennstoffverbrauch)",
    },
    "Gene": {
        "dataset_id": None,
        "value_column": "Valor",
        "date_column": "FechaHora",
        "dimensions": ["CodigoPlanta"],
        "name": "Generacion real (reale Erzeugung)",
    },
    "CapEfectivaNeta": {
        "dataset_id": "FADED0",
        "value_column": "Valor",
        "date_column": "FechaInicio",
        "dimensions": ["CodigoPlanta", "CodigoSICAgente"],
        "name": "Capacidad efectiva neta",
    },
    "PB_Nal": {
        "dataset_id": "EC6945",
        "value_column": "Valor",
        "date_column": "FechaHora",
        "dimensions": [],
        "name": "Precio de bolsa nacional",
    },
}


def lade_katalog():
    print("Lade Variablenkatalog ...")
    r = requests.get(KATALOG_URL, headers=HEADERS, timeout=60)
    r.raise_for_status()
    daten = r.json()
    return daten.get("variable", {})


def zeige_katalog(suche=None):
    var = lade_katalog()
    print("\n%d Variablen im Katalog\n" % len(var))
    if suche:
        begriffe = [s.lower() for s in suche]
        treffer = {k: v for k, v in var.items()
                   if any(b in k.lower() or b in str(v.get("name", "")).lower()
                          for b in begriffe)}
    else:
        treffer = var

    print("%-30s %-9s %-14s %s" % ("Variable", "Dataset", "Wert-Spalte", "Name"))
    print("-" * 104)
    for k, v in sorted(treffer.items()):
        print("%-30s %-9s %-14s %s"
              % (k, v.get("dataset_id", "?"), v.get("value_column", "?"),
                 str(v.get("name", ""))[:56]))
    print("\n%d Treffer" % len(treffer))
    return treffer


def hole_metadaten(dataset_id):
    url = "%s?datasetId=%s" % (BASE_INFO, dataset_id)
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.json()


def hole_block(dataset_id, start, end):
    url = "%s?startDate=%s&endDate=%s&datasetId=%s" % (
        BASE_DATA, start.isoformat(), end.isoformat(), dataset_id)
    r = requests.post(url, headers=HEADERS, timeout=180)
    if r.status_code != 200:
        return None, "HTTP %s: %s" % (r.status_code, r.text[:160])
    try:
        return r.json(), None
    except Exception as exc:
        return None, "JSON-Fehler: %s" % exc


def lade_variable(var_name, start, end, granularitaet="Horaria"):
    var = lade_katalog()
    if var_name not in var:
        print("Variable '%s' nicht im Katalog." % var_name)
        print("Aehnliche Namen:")
        for k in var:
            if var_name.lower()[:5] in k.lower():
                print("   %s" % k)
        return None

    info = var[var_name]
    ds = info.get("dataset_id")
    wert = info.get("value_column")
    datum = info.get("date_column")
    dims = info.get("dimensions") or []

    print("\nVariable : %s" % var_name)
    print("Name     : %s" % info.get("name"))
    print("Dataset  : %s" % ds)
    print("Wert     : %s" % wert)
    print("Datum    : %s" % datum)
    print("Dims     : %s" % ", ".join(dims))

    if not ds:
        print("\nKein dataset_id hinterlegt - Katalog unvollstaendig.")
        return None

    # Metadaten fuer Granularitaet
    try:
        meta = hole_metadaten(ds)
        res = (meta.get("result", {}) or {}).get("metadata", {}) or {}
        print("Metadata : granularity=%s" % res.get("granularity", "?"))
    except Exception as exc:
        print("Metadata : nicht abrufbar (%s)" % type(exc).__name__)

    # Blockgroesse
    block_tage = 1 if granularitaet == "Horaria" else 24
    print("\nBlockgroesse: %d Tag(e)\n" % block_tage)

    alle = []
    cursor = start
    n = 0
    while cursor <= end:
        stop = min(cursor + timedelta(days=block_tage - 1), end)
        n += 1
        daten, fehler = hole_block(ds, cursor, stop)
        if fehler:
            print("  Block %3d %s..%s  FEHLER: %s" % (n, cursor, stop, fehler))
        else:
            recs = daten.get("result", {}).get("records", [])
            if recs:
                alle.extend(recs)
                print("  Block %3d %s..%s  %6d Saetze" % (n, cursor, stop, len(recs)))
            else:
                print("  Block %3d %s..%s  leer" % (n, cursor, stop))
        cursor = stop + timedelta(days=1)
        time.sleep(0.35)

    if not alle:
        print("\nKeine Daten erhalten.")
        return None

    # CSV schreiben
    spalten = []
    for rec in alle[:1]:
        spalten = list(rec.keys())
    datei = "simem_%s_%s_%s.csv" % (var_name, start, end)
    with open(datei, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write(",".join(spalten) + "\n")
        for rec in alle:
            zeile = []
            for s in spalten:
                v = rec.get(s, "")
                v = "" if v is None else str(v)
                if "," in v or '"' in v:
                    v = '"%s"' % v.replace('"', '""')
                zeile.append(v)
            fh.write(",".join(zeile) + "\n")

    print("\n%d Saetze gesamt" % len(alle))
    print("Spalten: %s" % ", ".join(spalten))
    print("CSV: %s" % datei)
    return datei


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--katalog", action="store_true",
                    help="Variablenkatalog anzeigen")
    ap.add_argument("--suche", nargs="*", default=None,
                    help="Filterbegriffe fuer den Katalog")
    ap.add_argument("--var", default=None, help="Variable laden")
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2025-01-31")
    ap.add_argument("--gran", default="Horaria",
                    choices=["Horaria", "Diaria", "Mensual"])
    args = ap.parse_args()

    if args.katalog or not args.var:
        zeige_katalog(args.suche or ["consum", "gene", "capac", "heat"])
        if not args.var:
            return 0

    s = date.fromisoformat(args.start)
    e = date.fromisoformat(args.end)
    lade_variable(args.var, s, e, args.gran)
    return 0


if __name__ == "__main__":
    sys.exit(main())
