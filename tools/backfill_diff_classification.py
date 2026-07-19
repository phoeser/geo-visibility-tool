#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Holt die Diff-Klassifikation fuer Alt-Events nach.

Hintergrund: Der Klassifikator war bis zum 19.07.2026 zu 99,7 % ausgefallen —
gemini-2.5-flash ist ein Thinking-Modell, dessen Thinking-Tokens gegen
maxOutputTokens zaehlten; es blieb 1 Token Ausgabe uebrig (Commit 144b018).
Die betroffenen Events tragen {"error": "invalid json"} statt einer Klassifikation.

Nachholbar sind sie, weil added_lines/removed_lines in events.jsonl gespeichert
sind — es braucht also keinen neuen Crawl, nur neue Modell-Aufrufe.

Eigenschaften:
  • CHECKPOINT: Fortschritt in --state; ein Abbruch verliert nichts.
  • IDEMPOTENT: Bereits brauchbar klassifizierte Events werden uebersprungen.
  • SCHREIBT IN PLACE: events.jsonl wird zeilenweise ersetzt, Reihenfolge bleibt.
  • --limit fuer Probelaeufe, --dry-run schreibt nichts.

Aufruf:
    GOOGLE_API_KEY=<key> python3 tools/backfill_diff_classification.py --limit 20 --dry-run
    GOOGLE_API_KEY=<key> python3 tools/backfill_diff_classification.py
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyzer.diff_classifier import classify_diff  # noqa: E402

PAGES = Path("data/pages")


def _needs_backfill(ev):
    if ev.get("event_type") != "change":
        return False
    c = ev.get("classification")
    if isinstance(c, dict) and c.get("type"):
        return False
    return bool(ev.get("added_lines") or ev.get("removed_lines"))


def collect(limit=None):
    """Liefert (pfad, zeilennummer, event) fuer alle nachholbaren Events."""
    todo = []
    for f in sorted(PAGES.glob("*/*/events.jsonl")):
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except Exception:  # noqa: BLE001
            continue
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if _needs_backfill(ev):
                todo.append((str(f), i, ev))
                if limit and len(todo) >= limit:
                    return todo
    return todo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--state", default="/tmp/backfill_class_state.json")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.environ.get("GOOGLE_API_KEY") and not os.environ.get("GEMINI_API_KEY"):
        sys.exit("FEHLER: GOOGLE_API_KEY / GEMINI_API_KEY nicht gesetzt.")

    todo = collect(args.limit or None)
    print(f"[backfill] {len(todo)} Events nachzuklassifizieren")
    if not todo:
        return 0

    state_path = Path(args.state)
    done = {}
    if state_path.exists():
        try:
            done = json.loads(state_path.read_text(encoding="utf-8"))
            print(f"[backfill] Checkpoint gefunden: {len(done)} bereits erledigt")
        except Exception:  # noqa: BLE001
            done = {}

    def key(p, i):
        return f"{p}#{i}"

    pending = [(p, i, ev) for (p, i, ev) in todo if key(p, i) not in done]
    print(f"[backfill] offen: {len(pending)}")

    def work(item):
        p, i, ev = item
        try:
            res = classify_diff(ev.get("url", ""), ev.get("added_lines") or [],
                                ev.get("removed_lines") or [], ev.get("summary") or "")
        except Exception as ex:  # noqa: BLE001
            res = {"error": str(ex)[:120]}
        return key(p, i), res

    ok = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(work, it) for it in pending]
        for n, fu in enumerate(as_completed(futs), 1):
            k, res = fu.result()
            done[k] = res
            if isinstance(res, dict) and res.get("type"):
                ok += 1
            else:
                fail += 1
            if n % 50 == 0:
                state_path.write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
                rate = n / max(time.time() - t0, 1)
                print(f"[backfill] {n}/{len(pending)} ({ok} ok, {fail} Fehler) "
                      f"{rate:.1f}/s, Rest ~{(len(pending)-n)/max(rate,0.01)/60:.1f} min")
    state_path.write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
    print(f"[backfill] Modellaufrufe fertig: {ok} brauchbar, {fail} Fehler")

    if args.dry_run:
        print("[backfill] DRY-RUN — Dateien unveraendert.")
        return 0

    # Zurueckschreiben, Datei fuer Datei
    byfile = {}
    for (p, i, ev) in todo:
        byfile.setdefault(p, []).append(i)
    changed = 0
    for p, idxs in byfile.items():
        path = Path(p)
        lines = path.read_text(encoding="utf-8").splitlines()
        dirty = False
        for i in idxs:
            res = done.get(key(p, i))
            if not (isinstance(res, dict) and res.get("type")):
                continue
            try:
                ev = json.loads(lines[i])
            except (json.JSONDecodeError, IndexError):
                continue
            ev["classification"] = res
            ev["classification_backfilled"] = True
            lines[i] = json.dumps(ev, ensure_ascii=False)
            dirty = True
        if dirty:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            changed += 1
    print(f"[backfill] {changed} Dateien aktualisiert")
    return 0


if __name__ == "__main__":
    sys.exit(main())
