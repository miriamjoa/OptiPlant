#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xm_co2_hourly.py
================
Holt den stuendlichen CO2e-Emissionsfaktor des kolumbianischen Verbundnetzes (SIN)
aus der offiziellen XM-Sinergox-API und schreibt ihn in die OptiPlant-Profiles-Datei.

Quelle   : XM S.A. E.S.P. - API Sinergox
MetricId : factorEmisionCO2e      Entity: Sistema      Aufloesung: horaria
Endpoint : POST https://servapibi.xm.com.co/hourly
Doku     : https://github.com/EquipoAnaliticaXM/API_XM

Das Skript RECHNET NICHTS. Es liest ab, prueft die Vollstaendigkeit und schreibt
die Reihe unveraendert weiter. Keine Kalibrierung, keine Monatsmittel,
keine IPCC-Faktoren, kein Wirkungsgrad.

Befehle
-------
  python xm_co2_hourly.py test
      Ein einzelner Tag. Zeigt die Rohantwort. IMMER ZUERST ausfuehren.

  python xm_co2_hourly.py coverage 2022 2025
      Prueft je Jahr, ob Daten vorliegen und wie viele Stunden gefuellt sind.

  python xm_co2_hourly.py year 2025
      Volle Jahresabfrage in 30-Tage-Bloecken.
      Schreibt xm_co2e_hourly_2025.csv und xm_co2e_hourly_2025.xlsx

  python xm_co2_hourly.py patch 2025 --profiles Profiles_2025_Colombia_v9.xlsx
      Schreibt die Jahresreihe in eine KOPIE der Profiles-Datei:
        CO2_regulated  Zeile 5-8764,  Spalten B-G   (3 Standorte x 2 Counting Methods)
        Lcia_hourly    Zeile 6-8765,  Spalten C, S, AI  (climate_change je Standort)
      Das Original bleibt unberuehrt.

Abhaengigkeiten:  requests, pandas, openpyxl (ueber python -m pip)

DNS-Aufloesung
--------------
Die Zone xm.com.co hat TTL 10 s und liefert vielen Resolvern SERVFAIL.
Das Skript loest den Host deshalb automatisch ueber mehrere Wege auf:
System-Resolver -> DNS-over-HTTPS -> autoritative Nameserver -> Fallback.
Danach wird fest zu dieser IP verbunden, SNI und Host-Header bleiben korrekt.
Kein hosts-Eintrag, keine Admin-Rechte, keine IT-Freigabe noetig.

  python xm_co2_hourly.py test --start 2025-06-15
  python xm_co2_hourly.py year 2025
  python xm_co2_hourly.py patch 2025 --profiles <Datei>.xlsx --roundtrip

Eine bestimmte IP kann mit --ip erzwungen werden.
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Fehlt: requests   ->   pip install requests")

try:
    import pandas as pd
except ImportError:
    sys.exit("Fehlt: pandas     ->   pip install pandas")


# ----------------------------------------------------------------------------
# Konfiguration
# ----------------------------------------------------------------------------
BASE_URL = "https://servapibi.xm.com.co/hourly"
METRIC   = "factorEmisionCO2e"
ENTITY   = "Sistema"
HEADERS  = {"Content-Type": "application/json", "Accept": "application/json"}
TIMEOUT  = 90
MAX_DAYS = 30          # XM begrenzt das Abfragefenster; 30 Tage sind sicher

# --- Optionaler DNS-Bypass -------------------------------------------------
# Die Zone xm.com.co hat TTL 10 s und antwortet vielen Resolvern mit SERVFAIL.
# Mit --ip <adresse> wird die Verbindung direkt zu dieser IP aufgebaut,
# waehrend SNI und Host-Header den Domainnamen behalten. Kein hosts-Eintrag,
# keine Admin-Rechte noetig.
FORCED_IP = None
TARGET_HOST = "servapibi.xm.com.co"


# --- Automatische Aufloesung ----------------------------------------------
# Die Zone xm.com.co hat TTL 10 s und liefert vielen Resolvern SERVFAIL.
# Deshalb wird der Host ueber mehrere Strategien aufgeloest, bevor verbunden
# wird. Reihenfolge: System -> DoH -> autoritative Nameserver -> Fallback.

AUTHORITATIVE_NS = ["gorgona.internexa.net", "nuqui.internexa.net"]
DOH_ENDPOINTS = [
    ("Cloudflare", "https://cloudflare-dns.com/dns-query"),
    ("Google", "https://dns.google/resolve"),
]
FALLBACK_IPS = ["191.97.49.119", "179.1.12.119"]


def _dns_build(name, qtype=1):
    import random as _rnd
    import struct as _st
    tid = _rnd.randint(0, 65535)
    header = _st.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(_st.pack("B", len(part)) + part.encode()
                     for part in name.rstrip(".").split(".")) + b"\x00"
    return header + qname + _st.pack(">HH", qtype, 1)


def _dns_skip(data, i):
    while True:
        if data[i] & 0xC0 == 0xC0:
            return i + 2
        if data[i] == 0:
            return i + 1
        i += data[i] + 1


def _dns_parse(data):
    import struct as _st
    rcode = data[3] & 0x0F
    ancount = _st.unpack(">H", data[6:8])[0]
    i = _dns_skip(data, 12) + 4
    ips = []
    for _ in range(ancount):
        i = _dns_skip(data, i)
        rtype, _cls, _ttl, rdlen = _st.unpack(">HHIH", data[i:i + 10])
        i += 10
        rdata = data[i:i + rdlen]
        i += rdlen
        if rtype == 1 and rdlen == 4:
            ips.append(".".join(str(b) for b in rdata))
    return rcode, ips


def _dns_udp(server_ip, name, timeout=8):
    import socket as _sock
    packet = _dns_build(name)
    s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(packet, (server_ip, 53))
        data, _ = s.recvfrom(4096)
        return _dns_parse(data)
    finally:
        s.close()


def resolve_host(host=TARGET_HOST, verbose=True):
    """Loest host ueber mehrere Strategien auf. Gibt Liste von IPs zurueck."""
    import socket as _sock

    # 1) System
    try:
        infos = _sock.getaddrinfo(host, 443, _sock.AF_INET)
        ips = sorted({i[4][0] for i in infos})
        if ips:
            if verbose:
                print("DNS: %s -> %s (System-Resolver)" % (host, ", ".join(ips)))
            return ips
    except Exception:
        pass

    # 2) DoH
    for name, url in DOH_ENDPOINTS:
        try:
            r = requests.get(url, params={"name": host, "type": "A"},
                             headers={"accept": "application/dns-json"},
                             timeout=12)
            data = r.json()
            ips = [a.get("data") for a in (data.get("Answer") or [])
                   if a.get("type") == 1]
            if ips:
                if verbose:
                    print("DNS: %s -> %s (via %s)" % (host, ", ".join(ips), name))
                return ips
        except Exception:
            continue

    # 3) Autoritative Nameserver direkt
    ns_ips = []
    for ns in AUTHORITATIVE_NS:
        try:
            ns_ips.append(_sock.gethostbyname(ns))
            continue
        except Exception:
            pass
        for _name, url in DOH_ENDPOINTS:
            try:
                r = requests.get(url, params={"name": ns, "type": "A"},
                                 headers={"accept": "application/dns-json"},
                                 timeout=10)
                ans = [a.get("data") for a in (r.json().get("Answer") or [])
                       if a.get("type") == 1]
                if ans:
                    ns_ips.append(ans[0])
                    break
            except Exception:
                continue

    collected = []
    for ns_ip in ns_ips:
        try:
            _rcode, ips = _dns_udp(ns_ip, host)
            collected.extend(ips)
        except Exception:
            continue
    if collected:
        ips = sorted(set(collected))
        if verbose:
            print("DNS: %s -> %s (autoritative Nameserver)"
                  % (host, ", ".join(ips)))
        return ips

    # 4) Fallback
    if verbose:
        print("DNS: %s nicht aufloesbar, nutze Fallback-Liste %s"
              % (host, ", ".join(FALLBACK_IPS)))
    return list(FALLBACK_IPS)


def install_ip_bypass(ip):
    """Lenkt Verbindungen zu TARGET_HOST auf eine feste IP um (SNI bleibt)."""
    global FORCED_IP
    FORCED_IP = ip
    try:
        from urllib3.util import connection as u3conn
    except ImportError:
        return False
    original = u3conn.create_connection

    def patched(address, *args, **kwargs):
        host, port = address
        if host == TARGET_HOST:
            address = (ip, port)
        return original(address, *args, **kwargs)

    u3conn.create_connection = patched
    return True

# --- Zielstruktur der Profiles-Datei (verifiziert an Profiles_2025_Colombia_v9) ---
# CO2_regulated: Anker "Index" in Zeile 4, Daten ab Zeile 5, 8760 Zeilen, Spalten B-G
CO2_SHEET, CO2_ROW_FIRST, CO2_ROW_LAST = "CO2_regulated", 5, 8764
CO2_COLS = [2, 3, 4, 5, 6, 7]          # B..G

# Lcia_hourly: Anker "Index" in Zeile 5, Daten ab Zeile 6, 8760 Zeilen
# climate_change liegt je Standort in Spalte C (La Guajira), S (Santa Marta),
# AI (Valle del Cauca). Die uebrigen 15 Wirkungskategorien werden NICHT angefasst.
LCIA_SHEET, LCIA_ROW_FIRST, LCIA_ROW_LAST = "Lcia_hourly", 6, 8765
LCIA_COLS = [3, 19, 35]                # C, S, AI


# ----------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------
def post_metric(start, end, metric=METRIC, entity=ENTITY, retries=4, verbose=True):
    """Ein POST gegen die XM-API mit Retry und exponentiellem Backoff."""
    body = {
        "MetricId": metric,
        "StartDate": start.isoformat(),
        "EndDate":   end.isoformat(),
        "Entity":    entity,
    }
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(BASE_URL, json=body, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json()
            last = "HTTP %s: %s" % (r.status_code, r.text[:300])
        except Exception as exc:
            last = "%s: %s" % (type(exc).__name__, exc)
        if attempt < retries:
            wait = 2 ** attempt
            if verbose:
                print("      Versuch %d fehlgeschlagen (%s) - warte %ds"
                      % (attempt, last, wait))
            time.sleep(wait)
    raise RuntimeError("Abruf %s..%s fehlgeschlagen: %s" % (start, end, last))


def parse_response(payload, metric=METRIC):
    """Unterstuetzt beide Antwortformen der XM-API."""
    rows = []

    # Variante A - hourly-Endpoint:
    # {"Items":[{"Date":"2025-01-01","Hour":"01","Values":{"factorEmisionCO2e":"..."}}]}
    for item in (payload.get("Items") or []):
        d, h = item.get("Date"), item.get("Hour")
        vals = item.get("Values") or {}
        v = vals.get(metric)
        if d is not None and h is not None and v is not None:
            rows.append((d, h, v))

    # Variante B - lists-Endpoint:
    # {"Entities":[{"Values":[{"Date":"...","Hour":"01","<metric>":"..."}]}]}
    for ent in (payload.get("Entities") or []):
        for v in (ent.get("Values") or []):
            d, h = v.get("Date"), v.get("Hour")
            val = v.get(metric)
            if d is not None and h is not None and val is not None:
                rows.append((d, h, val))

    return rows


def rows_to_frame(rows):
    """Baut einen sortierten, eindeutigen Stundenindex. XM nummeriert Stunden 01..24."""
    if not rows:
        return pd.DataFrame(columns=["timestamp", "value_raw"])

    df = pd.DataFrame(rows, columns=["date", "hour", "value_raw"])
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df["hour"] = pd.to_numeric(df["hour"], errors="coerce")
    df["value_raw"] = pd.to_numeric(df["value_raw"], errors="coerce")
    df = df.dropna(subset=["date", "hour", "value_raw"])
    df = df[(df["hour"] >= 1) & (df["hour"] <= 24)]

    # Stunde 01 = 00:00-01:00  ->  Zeitstempel 00:00
    # Stunde 24 = 23:00-24:00  ->  Zeitstempel 23:00
    df["timestamp"] = [
        datetime.combine(d, datetime.min.time()) + timedelta(hours=int(h) - 1)
        for d, h in zip(df["date"], df["hour"])
    ]
    df = df.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    return df[["timestamp", "value_raw"]].reset_index(drop=True)


def detect_unit(series):
    """Erkennt anhand der Groessenordnung, ob g/kWh oder kg/kWh geliefert wird."""
    med = float(series.median())
    if med > 1.0:
        return "g/kWh", 1.0 / 1000.0
    return "kg/kWh", 1.0


def fetch_range(start, end, metric=METRIC, entity=ENTITY):
    payload = post_metric(start, end, metric=metric, entity=entity, verbose=False)
    return rows_to_frame(parse_response(payload, metric=metric))


def fetch_year(year, metric=METRIC, entity=ENTITY):
    """Jahresabfrage in 30-Tage-Bloecken."""
    frames = []
    cursor = date(year, 1, 1)
    end_year = date(year, 12, 31)
    block = 0
    while cursor <= end_year:
        block += 1
        stop = min(cursor + timedelta(days=MAX_DAYS - 1), end_year)
        print("  Block %2d: %s .. %s" % (block, cursor, stop), end="  ")
        try:
            df = fetch_range(cursor, stop, metric=metric, entity=entity)
        except RuntimeError as exc:
            print("FEHLER - %s" % exc)
            cursor = stop + timedelta(days=1)
            continue
        frames.append(df)
        print("%4d Werte" % len(df))
        cursor = stop + timedelta(days=1)
        time.sleep(1.2)

    if not frames:
        return pd.DataFrame(columns=["timestamp", "value_raw"])
    out = pd.concat(frames, ignore_index=True)
    return (out.sort_values("timestamp")
               .drop_duplicates("timestamp")
               .reset_index(drop=True))


def hours_in_year(year):
    leap = (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0))
    return 8784 if leap else 8760


def align_to_year(df, year):
    """Legt die Reihe auf ein lueckenloses Stundenraster des Jahres."""
    n = hours_in_year(year)
    grid = pd.DataFrame({"timestamp": pd.date_range(
        datetime(year, 1, 1), periods=n, freq="h")})
    merged = grid.merge(df, on="timestamp", how="left")
    filled = int(merged["value_raw"].notna().sum())
    return merged, n, filled


# ----------------------------------------------------------------------------
# Befehle
# ----------------------------------------------------------------------------
def cmd_test(args):
    start = date.fromisoformat(args.start)
    print("=" * 74)
    print("TESTABRUF  %s  |  MetricId=%s  Entity=%s" % (start, METRIC, ENTITY))
    print("=" * 74)

    body = {"MetricId": METRIC, "StartDate": start.isoformat(),
            "EndDate": start.isoformat(), "Entity": ENTITY}
    print("\nPOST %s" % BASE_URL)
    print("Body: %s\n" % json.dumps(body))

    try:
        r = requests.post(BASE_URL, json=body, headers=HEADERS, timeout=TIMEOUT)
    except Exception as exc:
        print("VERBINDUNGSFEHLER: %s: %s" % (type(exc).__name__, exc))
        print("\nPruefe: Internet? Firewall/Proxy? Firmennetz blockiert xm.com.co?")
        return 1

    print("HTTP %s" % r.status_code)
    if r.status_code != 200:
        print("Antwort (gekuerzt): %s" % r.text[:800])
        return 1

    payload = r.json()
    print("\n--- Rohantwort (erste 1500 Zeichen) ---")
    print(json.dumps(payload, ensure_ascii=False)[:1500])
    print("--- Ende ---\n")

    rows = parse_response(payload)
    print("Erkannte Werte: %d" % len(rows))
    if not rows:
        print("\nKeine Werte geparst. Moegliche Gruende:")
        print("  - Metrik fuer dieses Datum leer (Wochenende/Feiertag pruefen)")
        print("  - Feldname in 'Values' weicht ab -> Rohantwort oben ansehen")
        print("  - Metrik umbenannt -> alternativ testen:")
        print("      --metric EmisionesCO2Eq --entity Recurso")
        return 1

    df = rows_to_frame(rows)
    unit, factor = detect_unit(df["value_raw"])
    df["kg_per_kWh"] = df["value_raw"] * factor

    print("\nErste 5 Werte:")
    print(df.head().to_string(index=False))
    print("\nEinheit erkannt: %s   (Median roh = %.6f)"
          % (unit, df["value_raw"].median()))
    print("Als kg/kWh      : min %.6f | max %.6f | mean %.6f"
          % (df["kg_per_kWh"].min(), df["kg_per_kWh"].max(),
             df["kg_per_kWh"].mean()))
    print("\nPlausibilitaet: kolumbianisches Netz liegt typisch bei "
          "0,05-0,25 kg CO2e/kWh.")
    if not (0.005 <= df["kg_per_kWh"].mean() <= 0.60):
        print("  ACHTUNG: Mittelwert ausserhalb. Einheit / Metrik pruefen!")
    else:
        print("  OK - Groessenordnung passt.")
    return 0


def cmd_coverage(args):
    print("=" * 74)
    print("ABDECKUNGSPRUEFUNG  %d .. %d" % (args.year_from, args.year_to))
    print("=" * 74)
    print("Je Jahr werden zwei 30-Tage-Fenster geprueft (Januar und Juli).\n")

    for year in range(args.year_from, args.year_to + 1):
        total = 0
        for month in (1, 7):
            s = date(year, month, 1)
            e = min(s + timedelta(days=MAX_DAYS - 1), date(year, 12, 31))
            try:
                df = fetch_range(s, e)
            except RuntimeError as exc:
                print("  %d %02d  FEHLER: %s" % (year, month, exc))
                continue
            total += len(df)
            if len(df):
                unit, factor = detect_unit(df["value_raw"])
                kg = df["value_raw"] * factor
                print("  %d %02d  %3d Werte | %s | mean %.5f kg/kWh"
                      % (year, month, len(df), unit, kg.mean()))
            else:
                print("  %d %02d  keine Werte" % (year, month))
            time.sleep(1.2)
        print("  --> %d: %d von 60 geprueften Stunden gefuellt\n"
              % (year, total))
    return 0


def cmd_year(args):
    year = args.year
    print("=" * 74)
    print("JAHRESABFRAGE %d  (MetricId=%s, Entity=%s)" % (year, args.metric, args.entity))
    print("=" * 74)

    df = fetch_year(year, metric=args.metric, entity=args.entity)
    if df.empty:
        print("\nKeine Daten erhalten.")
        return 1

    unit, factor = detect_unit(df["value_raw"])
    df["kg_per_kWh"] = df["value_raw"] * factor
    df["g_per_kWh"] = df["kg_per_kWh"] * 1000.0

    aligned, n_expected, n_filled = align_to_year(df, year)
    print("\n" + "-" * 74)
    print("Erwartet: %d Stunden   Erhalten: %d   Fehlend: %d"
          % (n_expected, n_filled, n_expected - n_filled))
    if n_filled != n_expected:
        missing = aligned[aligned["value_raw"].isna()]["timestamp"]
        print("Erste fehlende Zeitstempel: %s"
              % ", ".join(str(x) for x in missing.head(8)))
        print("Hinweis: fehlende Stunden bleiben im Blatt leer -> Modell liest 0.")
        print("         Vor dem Lauf entscheiden, ob interpoliert werden soll.")

    print("\nEinheit roh: %s" % unit)
    print("kg CO2e/kWh : min %.6f | max %.6f | mean %.6f"
          % (df["kg_per_kWh"].min(), df["kg_per_kWh"].max(),
             df["kg_per_kWh"].mean()))
    print("g  CO2e/kWh : min %.2f | max %.2f | mean %.2f"
          % (df["g_per_kWh"].min(), df["g_per_kWh"].max(),
             df["g_per_kWh"].mean()))

    tmp = df.copy()
    tmp["monat"] = pd.to_datetime(tmp["timestamp"]).dt.month
    print("\nMonatsmittel (nur zur Kontrolle, die Reihe bleibt stuendlich):")
    mm = tmp.groupby("monat")["g_per_kWh"].agg(["mean", "min", "max", "count"])
    for m, row in mm.iterrows():
        print("  %2d  mean %6.2f | min %6.2f | max %6.2f | n=%d"
              % (m, row["mean"], row["min"], row["max"], row["count"]))

    csv_path = Path("xm_co2e_hourly_%d.csv" % year)
    aligned.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print("\nCSV geschrieben: %s" % csv_path.resolve())

    xlsx_path = Path("xm_co2e_hourly_%d.xlsx" % year)
    meta = pd.DataFrame({
        "Feld": ["Quelle", "MetricId", "Entity", "Abrufzeitpunkt", "Jahr",
                 "Stunden erwartet", "Stunden erhalten", "Einheit Rohwert",
                 "Einheit normiert", "min kg CO2e/kWh", "max kg CO2e/kWh",
                 "mean kg CO2e/kWh", "Bearbeitung"],
        "Wert": ["XM S.A. E.S.P. - API Sinergox", args.metric, args.entity,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"), year,
                 n_expected, n_filled, unit, "kg CO2e/kWh",
                 round(df["kg_per_kWh"].min(), 8),
                 round(df["kg_per_kWh"].max(), 8),
                 round(df["kg_per_kWh"].mean(), 8),
                 "unveraendert aus API uebernommen, keine Kalibrierung"],
    })
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as xw:
        aligned.to_excel(xw, sheet_name="Stundenreihe", index=False)
        meta.to_excel(xw, sheet_name="Quelle_und_Metadaten", index=False)
    print("Excel geschrieben: %s" % xlsx_path.resolve())
    return 0


def cmd_patch(args):
    try:
        import openpyxl
    except ImportError:
        sys.exit("Fehlt: openpyxl   ->   pip install openpyxl")

    year = args.year
    src = Path(args.profiles)
    if not src.exists():
        sys.exit("Profiles-Datei nicht gefunden: %s" % src)

    csv_path = Path("xm_co2e_hourly_%d.csv" % year)
    if csv_path.exists() and not args.refetch:
        print("Nutze vorhandene %s" % csv_path)
        aligned = pd.read_csv(csv_path, parse_dates=["timestamp"])
        if "kg_per_kWh" not in aligned.columns:
            unit, factor = detect_unit(aligned["value_raw"])
            aligned["kg_per_kWh"] = aligned["value_raw"] * factor
    else:
        print("Rufe Jahresreihe %d neu ab ..." % year)
        df = fetch_year(year, metric=args.metric, entity=args.entity)
        unit, factor = detect_unit(df["value_raw"])
        df["kg_per_kWh"] = df["value_raw"] * factor
        aligned, _, _ = align_to_year(df, year)

    n_expected = CO2_ROW_LAST - CO2_ROW_FIRST + 1
    if len(aligned) != n_expected:
        sys.exit("Reihe hat %d Werte, das Blatt braucht %d. Abbruch."
                 % (len(aligned), n_expected))
    if aligned["kg_per_kWh"].isna().any():
        n_nan = int(aligned["kg_per_kWh"].isna().sum())
        print("\nWARNUNG: %d Stunden ohne Wert. Diese Zeilen bleiben leer." % n_nan)
        if not args.allow_gaps:
            sys.exit("Abbruch. Mit --allow-gaps trotzdem schreiben, "
                     "oder zuerst Luecken klaeren.")

    out = src.with_name(src.stem + "_CO2patched" + src.suffix)
    shutil.copy2(src, out)
    print("Arbeitskopie: %s" % out)

    wb = openpyxl.load_workbook(out)

    for needed in (CO2_SHEET, LCIA_SHEET):
        if needed not in wb.sheetnames:
            print("\nBlatt '%s' nicht gefunden. Vorhandene Blaetter:" % needed)
            for name in wb.sheetnames:
                print("   - %s" % name)
            sys.exit("Abbruch.")

    # --- Kontrolle der Zielstruktur ---
    print("\n--- Struktur vor dem Schreiben ---")
    ws_co2 = wb[CO2_SHEET]
    for r in range(1, 6):
        print("  %s r%d: %s" % (CO2_SHEET, r,
              [ws_co2.cell(row=r, column=c).value for c in range(1, 8)]))
    ws_lcia = wb[LCIA_SHEET]
    for r in range(2, 6):
        print("  %s r%d: %s" % (LCIA_SHEET, r,
              [ws_lcia.cell(row=r, column=c).value for c in range(1, 8)]))

    if args.dry_run:
        print("\n--dry-run: nichts geschrieben.")
        return 0

    values = aligned["kg_per_kWh"].tolist()

    # --- CO2_regulated: B..G, Zeile 5..8764 ---
    for i, v in enumerate(values):
        r = CO2_ROW_FIRST + i
        for c in CO2_COLS:
            ws_co2.cell(row=r, column=c,
                        value=None if pd.isna(v) else float(v))
    print("\nGeschrieben: %s!B%d:G%d  (%d Zeilen x %d Spalten)"
          % (CO2_SHEET, CO2_ROW_FIRST, CO2_ROW_LAST,
             len(values), len(CO2_COLS)))

    # --- Lcia_hourly: climate_change je Standort, Zeile 6..8765 ---
    for i, v in enumerate(values):
        r = LCIA_ROW_FIRST + i
        for c in LCIA_COLS:
            ws_lcia.cell(row=r, column=c,
                         value=None if pd.isna(v) else float(v))
    print("Geschrieben: %s!C%d:C%d, S%d:S%d, AI%d:AI%d  (climate_change)"
          % (LCIA_SHEET, LCIA_ROW_FIRST, LCIA_ROW_LAST,
             LCIA_ROW_FIRST, LCIA_ROW_LAST,
             LCIA_ROW_FIRST, LCIA_ROW_LAST))

    # --- Metadatenblatt ---
    meta_name = "XM_Metadaten"
    if meta_name in wb.sheetnames:
        del wb[meta_name]
    ms = wb.create_sheet(meta_name)
    rows = [
        ("Quelle", "XM S.A. E.S.P. - API Sinergox"),
        ("MetricId", args.metric),
        ("Entity", args.entity),
        ("Jahr", year),
        ("Abrufzeitpunkt", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("Stunden", len(values)),
        ("Einheit im Blatt", "kg CO2e/kWh"),
        ("min", round(float(aligned["kg_per_kWh"].min()), 8)),
        ("max", round(float(aligned["kg_per_kWh"].max()), 8)),
        ("mean", round(float(aligned["kg_per_kWh"].mean()), 8)),
        ("Bearbeitung", "unveraendert aus API, keine Kalibrierung, "
                        "keine Monatsmittel, kein IPCC-Faktor"),
        ("Geschrieben in", "%s!B%d:G%d und %s climate_change"
                           % (CO2_SHEET, CO2_ROW_FIRST, CO2_ROW_LAST, LCIA_SHEET)),
        ("Nicht angefasst", "Lcia_hourly uebrige 15 Wirkungskategorien, "
                            "Renewable_criterion, Flux, Price"),
    ]
    for i, (k, v) in enumerate(rows, start=1):
        ms.cell(row=i, column=1, value=k)
        ms.cell(row=i, column=2, value=v)

    wb.save(out)
    print("Metadaten: Blatt '%s'" % meta_name)

    # --- LibreOffice-Roundtrip ---
    if args.roundtrip:
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if soffice:
            print("\nLibreOffice-Roundtrip ...")
            rt_dir = out.parent / "_rt"
            subprocess.run([soffice, "--headless", "--calc",
                            "--convert-to", "xlsx", "--outdir",
                            str(rt_dir), str(out)],
                           check=False, capture_output=True)
            rt = rt_dir / out.name
            if rt.exists():
                shutil.move(str(rt), str(out))
                print("Roundtrip abgeschlossen.")
            else:
                print("Roundtrip fehlgeschlagen - Datei bleibt wie geschrieben.")
        else:
            print("\nLibreOffice nicht gefunden - Roundtrip uebersprungen.")

    print("\nFertig: %s" % out.resolve())
    print("\nVor dem Julia-Lauf:")
    print("  1) Blatt '%s' Zeile 5-10 und Zeile 8764 sichten" % CO2_SHEET)
    print("  2) Blatt '%s' Spalte C, S, AI gegenpruefen" % LCIA_SHEET)
    print("  3) Blatt '%s' lesen (Quelle, min/max/mean)" % meta_name)
    print("  4) Datei einmal in LibreOffice oeffnen und speichern "
          "(sonst liest Julia Nullen)")
    return 0


# ----------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(
        description="XM Sinergox: stuendlicher CO2e-Netzfaktor Kolumbien "
                    "-> OptiPlant CO2_regulated",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("test", help="Ein einzelner Tag, zeigt die Rohantwort")
    t.add_argument("--start", default="2025-06-15", help="Datum YYYY-MM-DD")
    t.add_argument("--metric", default=METRIC)
    t.add_argument("--entity", default=ENTITY)
    t.add_argument("--ip", default=None,
                   help="Feste IP statt DNS (umgeht SERVFAIL)")
    t.set_defaults(func=cmd_test)

    c = sub.add_parser("coverage", help="Verfuegbarkeit je Jahr pruefen")
    c.add_argument("year_from", type=int)
    c.add_argument("year_to", type=int)
    c.set_defaults(func=cmd_coverage)

    y = sub.add_parser("year", help="Volle Jahresabfrage")
    y.add_argument("year", type=int)
    y.add_argument("--metric", default=METRIC)
    y.add_argument("--entity", default=ENTITY)
    y.add_argument("--ip", default=None,
                   help="Feste IP statt DNS (umgeht SERVFAIL)")
    y.set_defaults(func=cmd_year)

    pa = sub.add_parser("patch", help="In die Profiles-Datei schreiben")
    pa.add_argument("year", type=int)
    pa.add_argument("--profiles", required=True, help="Pfad zur Profiles-Datei")
    pa.add_argument("--metric", default=METRIC)
    pa.add_argument("--entity", default=ENTITY)
    pa.add_argument("--ip", default=None,
                   help="Feste IP statt DNS (umgeht SERVFAIL)")
    pa.add_argument("--refetch", action="store_true",
                    help="Jahresreihe neu abrufen statt CSV nutzen")
    pa.add_argument("--dry-run", action="store_true")
    pa.add_argument("--allow-gaps", action="store_true",
                    help="Trotz fehlender Stunden schreiben")
    pa.add_argument("--roundtrip", action="store_true",
                    help="LibreOffice-Roundtrip")
    pa.set_defaults(func=cmd_patch)

    args = p.parse_args()

    # IP bestimmen: explizit uebergeben, sonst automatisch aufloesen
    ip = getattr(args, "ip", None)
    if not ip:
        try:
            candidates = resolve_host(TARGET_HOST, verbose=True)
            ip = candidates[0] if candidates else None
        except Exception as exc:
            print("Aufloesung fehlgeschlagen: %s" % exc)
            ip = None

    if ip:
        if install_ip_bypass(ip):
            print("Verbinde fest zu %s (SNI bleibt %s)\n" % (ip, TARGET_HOST))
        else:
            print("WARNUNG: Bypass nicht moeglich (urllib3 fehlt).\n")

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
