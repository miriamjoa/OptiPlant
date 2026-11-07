#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fecoc_download.py
=================
Laedt die offizielle FECOC-2016-Rechenmaschine der UPME herunter.
In dieser Excel-Datei stehen ALLE 57 Brennstoffe mit ihren CO2-, CH4- und
N2O-Faktoren sowie Heizwerten - das ist die Originalquelle, die wir brauchen.

Warum ein eigenes Skript:
  Die Datei liegt auf einem IIS-Server, der bei direkten Browser-Aufrufen
  gelegentlich den falschen Content-Type liefert. Mit einem normalen
  HTTP-GET und Browser-User-Agent funktioniert es.

Aufruf:
    python fecoc_download.py

Ergebnis:
    Excel_FECOC_2016_REV_GLP_Vers_usuario.xlsx  im aktuellen Ordner

Danach:
    python fecoc_lesen.py      (listet alle Faktoren auf)
"""

import os
import sys

try:
    import requests
except ImportError:
    sys.exit("Fehlt: requests   ->   python -m pip install requests")


BASE = "https://app.upme.gov.co/Calculadora_Emisiones1/new/"

DATEIEN = [
    # (Dateiname, Beschreibung)
    ("Excel_FECOC_2016_REV_GLP_Vers_usuario.xlsx",
     "Rechenmaschine mit allen Faktoren (HAUPTZIEL)"),
    ("Informe_Final_FECOC_Correcciones_UPME_FunNatura.pdf",
     "Abschlussbericht, Tab. 3/4/5 mit Faktoren und Unsicherheiten"),
    ("manual_fecoc.pdf",
     "Benutzerhandbuch"),
]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": ("application/vnd.openxmlformats-officedocument."
               "spreadsheetml.sheet,application/pdf,*/*"),
    "Accept-Language": "es-CO,es;q=0.9,en;q=0.8",
    "Referer": BASE + "ayuda.html",
}


def hole(name, beschreibung):
    url = BASE + name
    print("\n%s" % ("-" * 70))
    print("%s" % name)
    print("  %s" % beschreibung)
    print("  GET %s" % url)

    try:
        r = requests.get(url, headers=HEADERS, timeout=90, stream=True)
    except Exception as exc:
        print("  FEHLER: %s: %s" % (type(exc).__name__, exc))
        return False

    print("  HTTP %s | Content-Type: %s | %s Bytes"
          % (r.status_code, r.headers.get("content-type", "?"),
             r.headers.get("content-length", "?")))

    if r.status_code != 200:
        print("  -> nicht abrufbar")
        return False

    # Plausibilitaet: Excel beginnt mit PK, PDF mit %PDF
    inhalt = r.content
    if name.endswith(".xlsx"):
        if not inhalt.startswith(b"PK"):
            print("  WARNUNG: kein ZIP-Header - vermutlich eine HTML-Fehlerseite")
            print("  erste Bytes: %r" % inhalt[:120])
            return False
    elif name.endswith(".pdf"):
        if not inhalt.startswith(b"%PDF"):
            print("  WARNUNG: kein PDF-Header")
            print("  erste Bytes: %r" % inhalt[:120])
            return False

    with open(name, "wb") as fh:
        fh.write(inhalt)
    print("  gespeichert: %s (%.1f kB)" % (name, len(inhalt) / 1024))
    return True


def main():
    print("=" * 70)
    print("FECOC 2016 herunterladen")
    print("Quelle: UPME, Ministerio de Minas y Energia, Kolumbien")
    print("=" * 70)

    ok = 0
    for name, besch in DATEIEN:
        if hole(name, besch):
            ok += 1

    print("\n" + "=" * 70)
    if ok:
        print("%d von %d Dateien geladen." % (ok, len(DATEIEN)))
        print("\nNaechster Schritt:")
        print("  python fecoc_lesen.py")
    else:
        print("Keine Datei geladen.")
        print("\nAlternative: im Browser oeffnen und manuell speichern:")
        for name, _ in DATEIEN:
            print("  %s%s" % (BASE, name))
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
