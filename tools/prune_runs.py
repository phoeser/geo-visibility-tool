#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rollierendes Aufbewahrungsfenster fuer die Roh-Antworten der eigenen Crawls.

Hintergrund: data/runs/*.json enthaelt je Lauf die vollstaendigen LLM-Antworten
samt Quellen und Metriken (~7 MB, taeglicher Lauf, ~2,5 GB/Jahr). Aufbewahrt
werden die Laeufe der letzten RETENTION_DAYS Tage.

WARUM 12 MONATE UND NICHT 6 (Entscheidung 19.07.2026, mit Zahlen belegt):
  • Die Antworttexte sind nur 13 % eines Laufs (Quellen 20 %, page_tracking 17 %,
    Metriken 9 %) — an der Aufbewahrungsfrist haengt also weit weniger Volumen,
    als es zunaechst scheint.
  • Git behaelt geloeschte Dateien ohnehin in der Historie (siehe unten). Die Frist
    steuert nur den Auscheckstand, nicht das Repo-Wachstum. Die Ersparnis durch
    ein kuerzeres Fenster ist damit gering.
  • Dem steht ein realer Analyse-Verlust gegenueber: Saisonalitaet (Kfz-Wechsel-
    saison, Jahreswechsel) braucht einen vollen Jahreszyklus, sonst kann das
    Treibermodell Saison nicht von Wirkung trennen. Und: nach einer Methoden-
    aenderung rechnet tools/backfill_brand_metrics.py die Metriken ueber ALLE
    Laeufe neu (so geschehen am 17.07. bei den Zweitdomains). Geloeschte Laeufe
    sind nicht nachrechenbar — die Frist ist zugleich die Grenze, bis zu der die
    Zeitreihe nach einer Umstellung noch reparierbar ist.

WICHTIG — was das Skript NICHT leistet: Git behaelt geloeschte Dateien in der
Historie. Der ausgecheckte Arbeitsstand schrumpft, die Repo-Groesse nicht.
Wer die Historie wirklich verkleinern will, braucht einen History-Rewrite
(git filter-repo) — das ist ein bewusster, separater Eingriff und passiert hier
absichtlich nicht.

Sicherungen:
  • latest.json und index.json werden nie geloescht.
  • Der juengste Lauf bleibt immer erhalten, auch wenn er aelter als das Fenster ist
    (sonst koennte ein laengerer Ausfall das gesamte Verzeichnis leeren).
  • MIN_KEEP Laeufe bleiben immer erhalten, damit Korrelation/Backfill nicht
    ohne Datengrundlage dastehen.
  • Ohne --apply wird nur berichtet (Dry-Run ist Default).

Aufruf:
    python3 tools/prune_runs.py                 # Bericht, loescht nichts
    python3 tools/prune_runs.py --apply         # loescht wirklich
    python3 tools/prune_runs.py --days 365 --apply
"""
import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

RETENTION_DAYS = 365          # rollierend 12 Monate (Entscheidung Paul, 19.07.2026)
MIN_KEEP = 30                 # Untergrenze, unabhaengig vom Datum
RUNS_DIR = Path("data/runs")
SPECIAL = {"index.json", "latest.json"}
STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})T")


def _run_date(p: Path):
    m = STAMP.match(p.name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=RETENTION_DAYS)
    ap.add_argument("--min-keep", type=int, default=MIN_KEEP)
    ap.add_argument("--runs-dir", default=str(RUNS_DIR))
    ap.add_argument("--apply", action="store_true", help="Wirklich loeschen (sonst Dry-Run)")
    args = ap.parse_args()

    runs_dir = Path(args.runs_dir)
    if not runs_dir.is_dir():
        print(f"[prune_runs] {runs_dir} existiert nicht — nichts zu tun.")
        return

    files = [p for p in runs_dir.glob("*.json") if p.name not in SPECIAL]
    dated = [(p, _run_date(p)) for p in files]
    undated = [p for p, d in dated if d is None]
    dated = sorted([(p, d) for p, d in dated if d is not None], key=lambda t: t[1])

    if undated:
        print(f"[prune_runs] {len(undated)} Datei(en) ohne erkennbares Datum — bleiben unangetastet: "
              + ", ".join(p.name for p in undated[:5]))

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    total_mb = sum(p.stat().st_size for p, _ in dated) / 1024 / 1024
    print(f"[prune_runs] {len(dated)} datierte Laeufe, {total_mb:.0f} MB, "
          f"Fenster {args.days} Tage (Stichtag {cutoff:%Y-%m-%d})")

    # Kandidaten = aelter als Stichtag; die juengsten MIN_KEEP bleiben immer
    keep_young = {p for p, _ in dated[-args.min_keep:]} if args.min_keep else set()
    victims = [(p, d) for p, d in dated if d < cutoff and p not in keep_young]

    if not victims:
        print(f"[prune_runs] Nichts zu loeschen. Aeltester Lauf: "
              f"{dated[0][1]:%Y-%m-%d} ({dated[0][0].name})" if dated else "[prune_runs] Keine Laeufe.")
        return

    freed = sum(p.stat().st_size for p, _ in victims) / 1024 / 1024
    print(f"[prune_runs] {len(victims)} Laeufe aelter als das Fenster ({freed:.0f} MB)")
    print(f"[prune_runs]   von {victims[0][1]:%Y-%m-%d} bis {victims[-1][1]:%Y-%m-%d}")

    if not args.apply:
        print("[prune_runs] DRY-RUN — nichts geloescht. Mit --apply ausfuehren.")
        return

    for p, _ in victims:
        p.unlink()
    print(f"[prune_runs] {len(victims)} Dateien geloescht, {freed:.0f} MB frei.")

    # index.json auf den verbliebenen Bestand eindampfen, damit er nicht auf
    # geloeschte Laeufe zeigt.
    idx_path = runs_dir / "index.json"
    if idx_path.exists():
        try:
            idx = json.loads(idx_path.read_text(encoding="utf-8"))
            names = {p.name for p in runs_dir.glob("*.json")}
            runs = idx.get("runs")
            if isinstance(runs, list):
                before = len(runs)
                idx["runs"] = [r for r in runs
                               if not isinstance(r, dict)
                               or not r.get("file")
                               or Path(str(r.get("file"))).name in names]
                idx["count"] = len(idx["runs"])
                idx["pruned_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                idx["retention_days"] = args.days
                idx_path.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
                print(f"[prune_runs] index.json bereinigt: {before} -> {len(idx['runs'])} Eintraege")
        except Exception as ex:  # noqa: BLE001
            print(f"[prune_runs] WARNUNG: index.json nicht bereinigt ({ex})")


if __name__ == "__main__":
    main()
