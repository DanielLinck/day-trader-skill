"""LS-Exchange-Nummern (instrumentId) fuer eine Universum-Datei ermitteln.

    python trader-scanner/ls_ids.py trader-scanner/universe_dax.txt

Liest Zeilen im Format TICKER|Name|ISIN, sucht jede ISIN bei ls-tc.de und
schreibt die Nummer als vierte Spalte zurueck: TICKER|Name|ISIN|LSID.
Zeilen, die schon eine Nummer haben, werden uebersprungen.
"""
import json, sys, time, urllib.parse, urllib.request

SEARCH = "https://www.ls-tc.de/_rpc/json/.lstc/instrument/search/main?q=%s&localeId=2"


def search(query):
    req = urllib.request.Request(SEARCH % urllib.parse.quote(query),
                                 headers={"User-Agent": "Mozilla/5.0"})
    text = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
    doc = json.loads(text)
    return doc if isinstance(doc, list) else []


def find_id(name, isin):
    """-> (instrumentId, LS-Name) oder (None, Hinweistext)."""
    hits = [h for h in search(isin)
            if str(h.get("isin", "")).upper() == isin.upper() and h.get("categorySymbol") == "STK"]
    if len(hits) == 1:
        return hits[0]["instrumentId"], hits[0].get("displayname", "")
    if len(hits) > 1:
        return None, "mehrere Treffer: " + ", ".join("%s (%s)" % (h["instrumentId"], h.get("displayname")) for h in hits)
    alt = [h for h in search(name) if h.get("categorySymbol") == "STK"][:5]
    return None, "ISIN nicht gefunden. Treffer fuer den Namen: " + (
        ", ".join("%s %s (%s)" % (h.get("instrumentId"), h.get("displayname"), h.get("isin")) for h in alt) or "keine")


def main(path):
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    out, ok, offen = [], 0, 0
    for ln in lines:
        parts = [p.strip() for p in ln.split("|")]
        if not ln.strip() or ln.lstrip().startswith("#") or len(parts) < 3:
            out.append(ln)
            continue
        if len(parts) >= 4 and parts[3]:
            out.append(ln)
            ok += 1
            continue
        ticker, name, isin = parts[:3]
        try:
            lsid, info = find_id(name, isin)
        except Exception as e:  # Netzwerk- oder Formatfehler: Zeile unveraendert lassen
            lsid, info = None, "Fehler: %s" % e
        if lsid:
            out.append("%s|%s|%s|%s" % (ticker, name, isin, lsid))
            print("%-9s %-24s -> %-6s %s" % (ticker, name, lsid, info))
            ok += 1
        else:
            out.append(ln)
            print("%-9s %-24s -> OFFEN  %s" % (ticker, name, info))
            offen += 1
        time.sleep(0.3)  # hoeflich bleiben
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    print("\n%d mit Nummer, %d offen. Datei aktualisiert: %s" % (ok, offen, path))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])