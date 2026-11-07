#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xm_co2_fetch.py
===============
Fallback ohne Zusatzpakete: nutzt nur die Python-Standardbibliothek
(urllib, json, csv, datetime). Kein pip install noetig.

Holt den stuendlichen CO2e-Emissionsfaktor des kolumbianischen Verbundnetzes
aus der XM-Sinergox-API und schreibt eine CSV mit dem Stundenraster des Jahres.

Quelle   : XM S.A. E.S.P. - API Sinergox
MetricId : factorEmisionCO2e      Entity: Sistema
Endpoint : POST https://servapibi.xm.com.co/hourly

Aufruf
------
  python xm_co2_fetch.py 2025
  python xm_co2_fetch.py 2025 --test        (nur ein Tag, zeigt Rohantwort)

Ergebnis
--------
  xm_co2e_hourly_2025.csv   Spalten: timestamp, value_raw, kg_per_kWh, note

Das Skript rechnet nichts um ausser der Einheitennormierung g/kWh -> kg/kWh,
falls die API Werte groesser 1 liefert. Danach xm_co2_hourly.py patch <Jahr>
nutzen, um in die Profiles-Datei zu schreiben (dafuer wird openpyxl gebraucht).
"""

import csv
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

BASE_URL = "https://servapibi.xm.com.co/hourly"
METRIC = "factorEmisionCO2e"
ENTITY = "Sistema"
MAX_DAYS = 30
TIMEOUT = 90


def post(start, stop, metric=METRIC, entity=ENTITY, retries=4):
    body = json.dumps({
        "MetricId": metric,
        "StartDate": start.isoformat(),
        "EndDate": stop.isoformat(),
        "Entity": entity,
    }).encode("utf-8")
    req = urllib.request.Request(
        BASE_URL, data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST")

    last = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
            return json.loads(raw)
        except urllib.error.HTTPError as exc:
            last = "HTTP %s %s" % (exc.code, exc.read()[:300].decode("utf-8", "replace"))
        except Exception as exc:
            last = "%s: %s" % (type(exc).__name__, exc)
        if attempt < retries:
            wait = 2 ** attempt
            print("      Versuch %d fehlgeschlagen (%s) - warte %ds"
                  % (attempt, last, wait))
            time.sleep(wait)
    raise RuntimeError("Abruf %s..%s fehlgeschlagen: %s" % (start, stop, last))


def parse(payload, metric=METRIC):
    out = []
    for item in (payload.get("Items") or []):
        vals = item.get("Values") or {}
        v = vals.get(metric)
        if item.get("Date") and item.get("Hour") and v is not None:
            out.append((item["Date"], item["Hour"], v))
    for ent in (payload.get("Entities") or []):
        for v in (ent.get("Values") or []):
            val = v.get(metric)
            if v.get("Date") and v.get("Hour") and val is not None:
                out.append((v["Date"], v["Hour"], val))
    return out


def hours_in_year(year):
    leap = (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0))
    return 8784 if leap else 8760


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    year = int(argv[1])
    test_only = "--test" in argv

    if test_only:
        d = date(year, 6, 15)
        print("TESTABRUF %s" % d)
        payload = post(d, d)
        print("\nRohantwort (erste 1500 Zeichen):")
        print(json.dumps(payload, ensure_ascii=False)[:1500])
        rows = parse(payload)
        print("\nWerte: %d" % len(rows))
        for r in rows[:5]:
            print("  %s" % (r,))
        return 0

    print("JAHRESABFRAGE %d  (MetricId=%s, Entity=%s)" % (year, METRIC, ENTITY))
    data = {}
    cursor = date(year, 1, 1)
    end_year = date(year, 12, 31)
    block = 0
    while cursor <= end_year:
        block += 1
        stop = min(cursor + timedelta(days=MAX_DAYS - 1), end_year)
        print("  Block %2d: %s .. %s" % (block, cursor, stop), end="  ")
        try:
            payload = post(cursor, stop)
        except RuntimeError as exc:
            print("FEHLER - %s" % exc)
            cursor = stop + timedelta(days=1)
            continue
        rows = parse(payload)
        for d, h, v in rows:
            try:
                day = datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
                hour = int(h)
                val = float(v)
            except (ValueError, TypeError):
                continue
            if 1 <= hour <= 24:
                ts = datetime(day.year, day.month, day.day) + timedelta(hours=hour - 1)
                data[ts] = val
        print("%4d Werte" % len(rows))
        cursor = stop + timedelta(days=1)
        time.sleep(1.2)

    if not data:
        print("\nKeine Daten erhalten.")
        return 1

    # Einheit
    sample = sorted(data.values())
    median = sample[len(sample) // 2]
    if median > 1.0:
        unit, factor = "g/kWh", 0.001
    else:
        unit, factor = "kg/kWh", 1.0
    print("\nEinheit roh: %s (Median %.6f)" % (unit, median))

    n = hours_in_year(year)
    csv_name = "xm_co2e_hourly_%d.csv" % year
    filled = 0
    with open(csv_name, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", "value_raw", "kg_per_kWh", "note"])
        start = datetime(year, 1, 1)
        for i in range(n):
            ts = start + timedelta(hours=i)
            raw = data.get(ts)
            if raw is None:
                w.writerow([ts.isoformat(sep=" "), "", "", "FEHLT"])
            else:
                filled += 1
                w.writerow([ts.isoformat(sep=" "), raw,
                            round(raw * factor, 10), ""])

    print("Stunden erwartet: %d   gefuellt: %d   fehlend: %d"
          % (n, filled, n - filled))
    vals = [v * factor for v in data.values()]
    print("kg CO2e/kWh: min %.6f | max %.6f | mean %.6f"
          % (min(vals), max(vals), sum(vals) / len(vals)))
    print("\nCSV geschrieben: %s" % csv_name)
    print("Naechster Schritt: python xm_co2_hourly.py patch %d "
          "--profiles <Profiles-Datei>.xlsx" % year)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
