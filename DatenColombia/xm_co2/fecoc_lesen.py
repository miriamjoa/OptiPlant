#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fecoc_lesen.py
==============
Liest die FECOC-2016-Excel der UPME aus und zeigt alle Brennstoffe mit
CO2-, CH4- und N2O-Faktoren sowie Heizwerten.

Aufruf:
    python fecoc_lesen.py
    python fecoc_lesen.py --suche carbon
    python fecoc_lesen.py --alle          (auch leere Zeilen zeigen)

Erwartet die Datei Excel_FECOC_2016_REV_GLP_Vers_usuario.xlsx im gleichen Ordner.
"""

import argparse
import sys
from pathlib import Path

try:
    import openpyxl
except ImportError:
    sys.exit("Fehlt: openpyxl   ->   python -m pip install openpyxl")

DATEI = "Excel_FECOC_2016_REV_GLP_Vers_usuario.xlsx"


def zeige_blatt(ws, max_rows=200, max_cols=20):
    print("\n" + "=" * 78)
    print("BLATT: %s   (%d x %d)" % (ws.title, ws.max_row, ws.max_column))
    print("=" * 78)
    n = min(ws.max_row, max_rows)
    for r in range(1, n + 1):
        werte = []
        for c in range(1, min(ws.max_column, max_cols) + 1):
            v = ws.cell(row=r, column=c).value
            if v is None:
                werte.append("")
            elif isinstance(v, float):
                werte.append(("%.4f" % v).rstrip("0").rstrip("."))
            else:
                werte.append(str(v)[:28])
        if any(werte):
            print("r%-4d | %s" % (r, " | ".join(werte)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suche", default=None, help="nur Zeilen mit diesem Text")
    ap.add_argument("--alle", action="store_true", help="auch leere Zeilen")
    ap.add_argument("--datei", default=DATEI)
    args = ap.parse_args()

    p = Path(args.datei)
    if not p.exists():
        print("Datei nicht gefunden: %s" % p.resolve())
        print("Bitte zuerst ausfuehren:  python fecoc_download.py")
        return 1

    print("Lese: %s (%.1f kB)" % (p.name, p.stat().st_size / 1024))
    wb = openpyxl.load_workbook(p, data_only=True, read_only=True)
    print("\nBlaetter (%d):" % len(wb.sheetnames))
    for i, s in enumerate(wb.sheetnames, 1):
        print("  %2d. %s" % (i, s))

    if args.suche:
        print("\n" + "=" * 78)
        print("SUCHE: %s" % args.suche)
        print("=" * 78)
        begriff = args.suche.lower()
        for name in wb.sheetnames:
            ws = wb[name]
            for r, row in enumerate(ws.iter_rows(max_col=20, values_only=True), 1):
                if any(isinstance(v, str) and begriff in v.lower() for v in row):
                    zellen = ["%.4f" % v if isinstance(v, float) else str(v)
                              for v in row if v is not None]
                    print("  [%s] r%-4d %s" % (name, r, " | ".join(zellen)))
    else:
        # Die Faktorblaetter heissen typischerweise nach Brennstoffart
        for name in wb.sheetnames:
            low = name.lower()
            if any(k in low for k in ("sólid", "solid", "liqu", "gas", "factor",
                                      "combust", "resumen", "base", "result")):
                zeige_blatt(wb[name], max_rows=120)

    wb.close()
    print("\n" + "=" * 78)
    print("Hinweis: FECOC gibt Faktoren in kg CO2/TJ (teils auch kg/GJ).")
    print("  1 kg/GJ = 1000 kg/TJ.  Vor dem Vergleich mit IPCC umrechnen!")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
