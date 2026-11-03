"""LS Exchange (ls-tc.de) als Datenquelle: Intraday-Verlauf und 5-Minuten-Kerzen.

    python trader-scanner/lsx.py bars SAP.DE        # heutige 5-Minuten-Kerzen ab 9:00 (Xetra-Start)
    python trader-scanner/lsx.py live SAP.DE RHM.DE --minutes 12  # Echtzeit dazu, jede neue Kerze sofort
    python trader-scanner/lsx.py live ALL --minutes 6              # das ganze Universum (Sammel-Abo)
    python trader-scanner/lsx.py live SAP.DE --single              # ein Abo je Aktie (wie die Webseite)

Die Kurse sind Mittelkurse (zwischen Geld und Brief) von LS Exchange, dem
Handelsplatz von Trade Republic. Inoffizielle Quelle, kein Volumen. Der
Chart-Verlauf kommt in 5-Minuten-Bloecken und hinkt 3 bis 8 Minuten hinterher;
die letzten Minuten fuellt der Echtzeit-Kanal (Lightstreamer, Polling) auf.
Standardbibliothek only.
"""
import ast
import datetime
import http.client
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

CHART = ("https://www.ls-tc.de/_rpc/json/instrument/chart/dataForInstrument"
         "?container=chart1&instrumentId=%s&marketId=1&quotetype=mid"
         "&series=intraday&type=&localeId=2")
UA = {"User-Agent": "Mozilla/5.0"}
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_UNIVERSE = os.path.join(HERE, "universe_dax.txt")
OPEN = datetime.time(9, 0)      # Xetra-Start: ab hier zaehlen die Kerzen
CLOSE = datetime.time(17, 30)   # Xetra-Schluss
BAR_MIN = 5
PUSH_HOST = "push.ls-tc.de"
LIVE_FIELDS = ["mid", "midTime", "bid", "ask"]


class LsxError(Exception):
    pass


# ---------------------------------------------------------------- Universum

def load_universe(path=DEFAULT_UNIVERSE):
    """Universum-Datei (TICKER|Name|ISIN|LSID) -> {TICKER: {"name", "isin", "lsid"}}.
    Zeilen ohne LS-Nummer fehlen im Ergebnis."""
    out = {}
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            parts = [p.strip() for p in ln.split("|")]
            if ln.lstrip().startswith("#") or len(parts) < 4 or not parts[3]:
                continue
            out[parts[0].upper()] = {"name": parts[1], "isin": parts[2], "lsid": parts[3]}
    return out


def lsid_for(ticker, universe=None):
    universe = universe if universe is not None else load_universe()
    info = universe.get(ticker.upper())
    if not info:
        raise LsxError("%s steht nicht mit LS-Nummer im Universum" % ticker)
    return info["lsid"]


# ---------------------------------------------------------------- Verlauf

def ls_time(ms):
    """LS-Zeitstempel -> naive Berliner Zeit. LS verpackt die Ortszeit, als waere sie UTC."""
    return datetime.datetime(1970, 1, 1) + datetime.timedelta(milliseconds=int(ms))


def parse_chart(doc):
    """Antwort von dataForInstrument -> [(berliner_zeit, kurs), ...], aelteste zuerst."""
    try:
        data = doc["series"]["intraday"]["data"]
    except (KeyError, TypeError):
        raise LsxError("keine Intraday-Daten in der Antwort")
    pts = [(ls_time(t), float(p)) for t, p in data if p is not None]
    pts.sort()
    return pts


def intraday(lsid, timeout=15):
    """Heutiger 1-Minuten-Verlauf einer Aktie bei LS (Mittelkurse)."""
    req = urllib.request.Request(CHART % lsid, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return parse_chart(json.load(resp))


# ---------------------------------------------------------------- Kerzen

def bars(points, day, upto, open_t=OPEN, close_t=CLOSE, minutes=BAR_MIN):
    """Kurspunkte -> 5-Minuten-Kerzen auf dem Raster ab open_t (9:00, 9:05, ...).

    Nur abgeschlossene Kerzen (Beginn + 5 Minuten <= upto). Eine Kerze ohne
    einen einzigen Punkt (Kurs hat sich nicht bewegt) wird flach auf den
    letzten bekannten Kurs gesetzt. Vor dem ersten Kurs des Tages gibt es keine Kerze.
    Ergebnis: [{"start", "o", "h", "l", "c", "n"}, ...], n = Zahl der Punkte."""
    step = datetime.timedelta(minutes=minutes)
    start = datetime.datetime.combine(day, open_t)
    end = min(datetime.datetime.combine(day, close_t), upto)
    prev = None
    for t, p in points:                 # letzter Kurs vor Handelsbeginn
        if t.date() == day and t < start:
            prev = p
    out, i = [], 0
    pts = [(t, p) for t, p in points if t.date() == day and t >= start]
    while start + step <= end:
        bucket = []
        while i < len(pts) and pts[i][0] < start + step:
            bucket.append(pts[i][1])
            i += 1
        if bucket:
            out.append({"start": start, "o": bucket[0], "h": max(bucket), "l": min(bucket),
                        "c": bucket[-1], "n": len(bucket)})
            prev = bucket[-1]
        elif prev is not None:
            out.append({"start": start, "o": prev, "h": prev, "l": prev, "c": prev, "n": 0})
        start += step
    return out


# ---------------------------------------------------------------- Echtzeit

SESSION_RE = re.compile(r"start\('([^']+)'")
CALL_RE = re.compile(r"\bd\((\d+,\s*\d+.*?)\);")   # nur echte Kurszeilen: d(Tabelle, Item, ...)


class Abbruch(Exception):
    """Der Server hat die Session verloren oder antwortet nicht: neu verbinden."""


def parse_updates(text):
    """bind_session-Antwort -> [(tabelle, item, [werte...]), ...].
    Eine Zahl n statt eines Werts heisst: die naechsten n Felder sind unveraendert (None)."""
    out = []
    for m in CALL_RE.finditer(text):
        args = ast.literal_eval("(" + m.group(1).replace("null", "None") + ",)")
        vals = []
        for v in args[2:]:
            if isinstance(v, int) and not isinstance(v, bool):
                vals.extend([None] * v)
            else:
                vals.append(v)
        out.append((args[0], args[1], vals))
    return out


def parse_ls_iso(text):
    """'2026-11-03T10:59:04.388' -> naive Berliner Zeit (LS liefert Ortszeit)."""
    try:
        return datetime.datetime.fromisoformat(str(text)[:23])
    except ValueError:
        return None


class LiveFeed:
    """Echtzeit-Mittelkurse von LS ueber Lightstreamer (HTTP-Polling, eine offene Verbindung).
    Verbindet bei Problemen von selbst neu.

    group=True:  alle Aktien in EINEM Abo (Tabelle 1, Item = Position in der Liste).
                 Ein einziger control-Aufruf statt einem pro Aktie. Standard, weil der
                 Server Abo-Anfragen oft mit "sync error" ablehnt und jede einzelne
                 Anfrage ein neues Risiko ist.
    group=False: eine Tabelle (ein Abo) pro Aktie, wie die Webseite es macht.

        feed = LiveFeed(["34313", "34223"], group=True)
        ticks = feed.poll()      # [(lsid, berliner_zeit, mittelkurs), ...]
    """

    def __init__(self, lsids, log=None, group=True):
        self.lsids = [str(x) for x in lsids]
        self.group = group
        self.table = {n: lsid for n, lsid in enumerate(self.lsids, start=1)}
        self.state = {lsid: {} for lsid in self.lsids}
        self.log = log or (lambda msg: None)
        self.conn = None
        self.session = None
        self.phase = 2101
        self.reconnects = 0

    # -- HTTP
    def _post(self, endpoint, params, versuche=3):
        body = urllib.parse.urlencode(params)
        headers = {"User-Agent": UA["User-Agent"],
                   "Content-Type": "application/x-www-form-urlencoded"}
        fehler = ""
        for n in range(1, versuche + 1):
            try:
                if self.conn is None:
                    self.conn = http.client.HTTPSConnection(PUSH_HOST, timeout=10)
                self.conn.request("POST", "/lightstreamer/" + endpoint, body, headers)
                resp = self.conn.getresponse()
                text = resp.read().decode("utf-8", "replace")
                if resp.status == 200:
                    return text
                fehler = "HTTP %d" % resp.status
            except (OSError, http.client.HTTPException) as e:
                fehler = str(e) or e.__class__.__name__
            self.log("   (%s: Versuch %d von %d fehlgeschlagen: %s)" % (endpoint, n, versuche, fehler))
            self.close()
            time.sleep(1)
        raise Abbruch("%s: %s" % (endpoint, fehler))

    def close(self):
        if self.conn:
            try:
                self.conn.close()
            except OSError:
                pass
        self.conn = None

    def _bind(self, wait_ms):
        self.phase += 1
        text = self._post("bind_session.js", {
            "LS_session": self.session, "LS_phase": str(self.phase), "LS_cause": "loop",
            "LS_polling": "true", "LS_polling_millis": "0",
            "LS_idle_millis": str(wait_ms), "LS_container": "lsc"})
        if "loop(" not in text and not CALL_RE.search(text):
            raise Abbruch("unerwartete Antwort: " + text.strip()[:120])
        return text

    def _connect(self):
        text = self._post("create_session.js", {
            "LS_phase": "2101", "LS_cause": "new.api", "LS_polling": "true",
            "LS_polling_millis": "0", "LS_idle_millis": "0", "LS_client_version": "6.1",
            "LS_adapter_set": "WALLSTREETONLINE", "LS_container": "lsc"})
        m = SESSION_RE.search(text)
        if not m:
            raise Abbruch("keine Session: " + text.strip()[:120])
        self.session, self.phase = m.group(1), 2101
        pending = self._bind(0)             # erst binden, dann abonnieren
        if self.group:
            abos = [(1, " ".join("%s@1" % x for x in self.lsids), "alle")]
        else:
            abos = [(table, "%s@1" % lsid, lsid) for table, lsid in self.table.items()]
        versuche_gesamt = 0
        for table, ls_id, name in abos:
            for versuch in range(1, 11):
                versuche_gesamt += 1
                antwort = self._post("control.js", {
                    "LS_session": self.session, "LS_op": "add", "LS_table": str(table),
                    "LS_id": ls_id, "LS_schema": " ".join(LIVE_FIELDS),
                    "LS_mode": "MERGE", "LS_data_adapter": "QUOTE", "LS_snapshot": "true"})
                if "= null" in antwort:
                    break
                pending += self._bind(0)
                time.sleep(0.3)
            else:
                raise Abbruch("Abo fuer LS %s klappt nicht" % name)
        self.log("Echtzeit verbunden: %d Aktien, %s, %d Abo-Anfragen fuer %d Abos (Session %s)" % (
            len(self.lsids), "ein Sammel-Abo" if self.group else "ein Abo je Aktie",
            versuche_gesamt, len(abos), self.session[:10]))
        return pending

    # -- oeffentlich
    def poll(self, wait_ms=3000):
        """Neue Kurse abholen (wartet bis zu wait_ms, wenn gerade keiner kommt).
        -> [(lsid, berliner_zeit, mittelkurs), ...]. Fehler fuehren zu einem Neuaufbau."""
        try:
            text = self._connect() if self.session is None else ""
            text += self._bind(wait_ms)
        except Abbruch as e:
            self.reconnects += 1
            self.log("!! Echtzeit unterbrochen (%s), verbinde neu ..." % e)
            self.session = None
            self.close()
            time.sleep(2)
            return []
        ticks = []
        for table, item, vals in parse_updates(text):
            if self.group:
                lsid = self.lsids[item - 1] if table == 1 and 0 < item <= len(self.lsids) else None
            else:
                lsid = self.table.get(table)
            if lsid is None:
                continue
            st = self.state[lsid]
            for name, v in zip(LIVE_FIELDS, vals):
                if v is not None:
                    st[name] = v
            t = parse_ls_iso(st.get("midTime"))
            try:
                mid = float(st.get("mid"))
            except (TypeError, ValueError):
                continue
            if t is not None:
                ticks.append((lsid, t, mid))
        return ticks


def merge_points(chart_pts, ticks):
    """Chart-Verlauf + Echtzeit-Ticks: der Verlauf bis zu seinem letzten Punkt,
    danach nur die Ticks. -> [(berliner_zeit, kurs), ...], aelteste zuerst."""
    cut = chart_pts[-1][0] + datetime.timedelta(minutes=1) if chart_pts else None
    later = [(t, p) for t, p in ticks if cut is None or t >= cut]
    return sorted(chart_pts + later)


# ---------------------------------------------------------------- Kommandozeile

def cmd_bars(ticker):
    lsid = lsid_for(ticker)
    pts = intraday(lsid)
    if not pts:
        raise LsxError("LS hat fuer %s heute noch keine Kurse geliefert" % ticker)
    now = datetime.datetime.now().replace(microsecond=0)
    day = pts[-1][0].date()
    upto = min(now, pts[-1][0] + datetime.timedelta(minutes=1))
    kerzen = bars(pts, day, upto)
    print("%s (LS %s): %d Kurspunkte von %s bis %s, jetzt %s (Abstand %.1f Min)" % (
        ticker.upper(), lsid, len(pts), pts[0][0].strftime("%H:%M"), pts[-1][0].strftime("%H:%M"),
        now.strftime("%H:%M:%S"), (now - pts[-1][0]).total_seconds() / 60))
    print("Zeit    Eroeffn.   Hoch       Tief       Schluss   Punkte")
    for b in kerzen:
        print("%s   %-10.3f %-10.3f %-10.3f %-10.3f %d" % (
            b["start"].strftime("%H:%M"), b["o"], b["h"], b["l"], b["c"], b["n"]))
    print("%d abgeschlossene 5-Minuten-Kerzen seit 9:00." % len(kerzen))


def cmd_live(tickers, minutes, group=True):
    """Verlauf + Echtzeit fuer ein paar Aktien. Der Verlauf wird jede Minute neu geholt,
    damit sich die Luecke zwischen seinem Ende und dem Start der Echtzeit-Ticks schliesst.
    Jede neu abgeschlossene 5-Minuten-Kerze wird sofort ausgegeben."""
    universe = load_universe()
    if [t.upper() for t in tickers] == ["ALL"]:
        tickers = list(universe)
    ids = {t.upper(): lsid_for(t, universe) for t in tickers}
    by_id = {lsid: t for t, lsid in ids.items()}
    chart = {t: intraday(lsid) for t, lsid in ids.items()}
    ticks = {t: [] for t in ids}
    shown = {}
    for t in ids:                              # bis hierhin abgeschlossene Kerzen nicht nochmal zeigen
        done = bars(chart[t], chart[t][-1][0].date(), datetime.datetime.now()) if chart[t] else []
        shown[t] = done[-1]["start"] if done else None
    feed = LiveFeed(list(ids.values()), log=print, group=group)
    t0 = last_refresh = time.time()
    while time.time() - t0 < minutes * 60:
        for lsid, tt, mid in feed.poll():
            ticks[by_id[lsid]].append((tt, mid))
        if time.time() - last_refresh >= 60:   # Verlauf auffrischen (Server-Cache: 60 s)
            for t, lsid in ids.items():
                try:
                    chart[t] = intraday(lsid)
                except (OSError, ValueError, LsxError) as e:
                    print("   (Verlauf %s nicht aktualisiert: %s)" % (t, e))
            last_refresh = time.time()
        now = datetime.datetime.now()
        for t in ids:
            pts = merge_points(chart[t], ticks[t])
            if not pts:
                continue
            upto = max(now, pts[-1][0])        # PC-Uhr darf etwas nachgehen
            for b in bars(pts, pts[-1][0].date(), upto):
                if shown[t] is not None and b["start"] <= shown[t]:
                    continue
                chart_end = chart[t][-1][0] if chart[t] else None
                quelle = "Verlauf" if chart_end and chart_end >= b["start"] + datetime.timedelta(minutes=4) else "Echtzeit"
                first_tick = ticks[t][0][0] if ticks[t] else None
                luecke = (quelle == "Echtzeit" and chart_end is not None and first_tick is not None
                          and chart_end < b["start"] + datetime.timedelta(minutes=4)
                          and first_tick > max(b["start"], chart_end + datetime.timedelta(minutes=1)))
                print("%s  NEUE KERZE %-8s %s  O %-9.3f H %-9.3f T %-9.3f S %-9.3f %3d Punkte, aus %s%s" % (
                    time.strftime("%H:%M:%S"), t, b["start"].strftime("%H:%M"), b["o"], b["h"], b["l"],
                    b["c"], b["n"], quelle, "  (LUECKE: Anfang fehlt)" if luecke else ""))
                shown[t] = b["start"]
    feed.close()
    print()
    for t in ids:
        print("%s: Verlauf zuletzt bis %s, %d Echtzeit-Ticks." % (
            t, chart[t][-1][0].strftime("%H:%M") if chart[t] else "-", len(ticks[t])))
    print("Neu verbunden: %d-mal." % feed.reconnects)

def main(argv):
    try:
        if len(argv) == 2 and argv[0] == "bars":
            return cmd_bars(argv[1])
        if len(argv) >= 2 and argv[0] == "live":
            args, minutes = argv[1:], 3.0
            group = "--single" not in args
            args = [a for a in args if a not in ("--single", "--group")]
            if "--minutes" in args:
                k = args.index("--minutes")
                minutes = float(args[k + 1])
                args = args[:k] + args[k + 2:]
            if args:
                return cmd_live(args, minutes, group)
    except (LsxError, OSError, ValueError) as e:
        raise SystemExit("Fehler: %s" % e)
    raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])