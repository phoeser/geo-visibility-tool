"""Daten-Qualitäts-Tag pro Run.

Bewertet einen Run nach drei Dimensionen:
- LLMs: wie viele konfigurierte LLMs haben Daten geliefert?
- URLs: wie viele Seiten waren erreichbar?
- Why-Analyse: durchgelaufen oder gescheitert?

Output ist eine Ampel (green/yellow/red) plus Details, die in run["meta"]["data_quality"]
gespeichert werden. Spätere Bootstrap-/Volatilitäts-Module können dann anhand des
Tags entscheiden, welche Runs als Baseline taugen.

05.08.2026: Die LLM-Dimension unterscheidet "Engine ist ausgefallen" von
"Engine war heute laut Kosten-Intervall gar nicht dran" — siehe _check_llms.
"""

from __future__ import annotations

from typing import Dict, List, Any


# Schwellen
URL_OK_THRESHOLD = 0.95  # >= 95% erreichbar -> green
URL_WARN_THRESHOLD = 0.80  # 80-95% -> yellow, <80% -> red


def compute(run_dict: Dict[str, Any], cfg: Dict[str, Any],
            skipped_llms: Any = None) -> Dict[str, Any]:
    """Berechnet den Quality-Tag für einen Run.

    Args:
        run_dict: Der komplette Run vor Speicherung (mit products, page_tracking,
                  why_analysis, llms ...).
        cfg: Die geladene config.json (für die Liste konfigurierter LLMs).
        skipped_llms: IDs der Engines, die an diesem Tag laut Kosten-Intervall
                  (config llms[].interval_days/-offset) gar nicht abgefragt wurden.
                  Optional — fehlt der Wert, wird er aus run["skipped_llms"] bzw.
                  ersatzweise aus der Intervall-Regel + Run-Datum rekonstruiert.
                  Uebersprungene Engines sind KEIN Ausfall (siehe _check_llms).

    Returns:
        Dict mit:
          grade: "green" | "yellow" | "red"
          score: int 0-100 (grobe Heuristik, nicht statistisch)
          reasons: list[str] kurze Begründungen
          warnings: list[str] Auffälligkeiten
          details: dict mit Rohzahlen
    """
    details = {
        **_check_llms(run_dict, cfg, skipped_llms),
        **_check_urls(run_dict),
        **_check_why(run_dict),
    }

    reasons: List[str] = []
    warnings: List[str] = []

    # LLM-Bewertung. Nur ECHTE Ausfaelle (Engine war dran, hat aber nichts
    # geliefert) erzeugen eine Warnung und druecken das Grade. Engines, die
    # laut Kosten-Intervall heute gar nicht dran waren, sind neutral — auch
    # dann, wenn es keinen Vortageswert zum Fortschreiben gab.
    n_cfg_active = details["llms_configured_active"]
    n_today = details["llms_expected_today"]
    n_failed = len(details["llms_failed"])
    if n_cfg_active == 0:
        warnings.append("Keine LLMs konfiguriert oder aktiv")
        llm_grade = "red"
    elif n_today == 0:
        # Extremfall: heute war laut Intervall keine einzige Engine dran.
        reasons.append(f"keine der {n_cfg_active} LLMs heute laut Kostenintervall dran")
        llm_grade = "green"
    elif n_failed == 0:
        reasons.append(f"alle {n_today} heute geplanten LLMs erfolgreich")
        llm_grade = "green"
    elif n_failed == 1 and n_cfg_active >= 3:
        warnings.append(f"1 von {n_today} heute geplanten LLMs ausgefallen: {details['llms_failed'][0]}")
        llm_grade = "yellow"
    else:
        warnings.append(f"{n_failed} von {n_today} heute geplanten LLMs ausgefallen: {', '.join(details['llms_failed'])}")
        llm_grade = "red"

    # Neutrale Information (KEINE Warnung, kein Punktabzug): Intervall-Aussetzer.
    for _lid in details["llms_carried_forward"]:
        reasons.append(f"{_lid}: {details['llms_state_reasons'].get(_lid, 'fortgeschrieben')}")
    for _lid in details["llms_skipped_no_prev"]:
        reasons.append(f"{_lid}: {details['llms_state_reasons'].get(_lid, 'heute nicht dran')}")

    # URL-Bewertung
    if details["urls_total"] == 0:
        warnings.append("Keine URLs konfiguriert oder gefetcht")
        url_grade = "yellow"
        url_pct = None
    else:
        url_pct = details["urls_reachable"] / details["urls_total"]
        details["urls_reachable_pct"] = round(url_pct, 4)
        if url_pct >= URL_OK_THRESHOLD:
            reasons.append(f"{int(url_pct * 100)}% URLs erreichbar ({details['urls_reachable']}/{details['urls_total']})")
            url_grade = "green"
        elif url_pct >= URL_WARN_THRESHOLD:
            warnings.append(f"nur {int(url_pct * 100)}% URLs erreichbar ({details['urls_reachable']}/{details['urls_total']})")
            url_grade = "yellow"
        else:
            warnings.append(f"nur {int(url_pct * 100)}% URLs erreichbar ({details['urls_reachable']}/{details['urls_total']})")
            url_grade = "red"

    # Why-Bewertung — Why ist eine sekundaere Analyse. Sie kappt das Gesamt-Grade
    # NICHT auf rot, sondern hoechstens auf yellow. Rot kommt nur durch LLM/URL-Probleme.
    why_status = details["why_status"]
    if why_status == "ok":
        reasons.append(f"Why-Analyse OK ({details['why_products_ok']}/{details['why_products_total']})")
        why_grade = "green"
    elif why_status == "partial":
        warnings.append(f"Why-Analyse nur teilweise ({details['why_products_ok']}/{details['why_products_total']})")
        why_grade = "yellow"
    elif why_status == "skipped":
        warnings.append("Why-Analyse übersprungen (kein Client)")
        why_grade = "yellow"
    else:  # failed -> nur yellow, nicht rot
        warnings.append("Why-Analyse fehlgeschlagen (Kern-Lauf nicht betroffen)")
        why_grade = "yellow"

    # Gesamt-Grade: schlechtester der drei (red dominiert)
    order = {"green": 0, "yellow": 1, "red": 2}
    worst = max([llm_grade, url_grade, why_grade], key=lambda g: order[g])

    # Score (0-100)
    score = 100
    score -= n_failed * 20
    if url_pct is not None:
        score -= int(max(0, (1.0 - url_pct)) * 50)
    if why_grade == "yellow":
        score -= 10
    elif why_grade == "red":
        score -= 25
    score = max(0, min(100, score))

    out = {
        "grade": worst,
        "score": score,
        "reasons": reasons,
        "warnings": warnings,
        "details": details,
        # Marker fuer spaetere Stat-Module: nur GREEN-Runs als saubere Baseline
        "baseline_eligible": worst == "green",
    }

    # main.py legt vor dem Quality-Check ggf. run["data_quality"]["carried_forward_broken"]
    # ab (Engines mit 0 Nennungen, deren Vortageswert fortgeschrieben wurde). Da der
    # Rueckgabewert dieses Moduls run["data_quality"] komplett ersetzt, ging das Feld
    # bisher verloren — hier additiv uebernehmen.
    try:
        prev_dq = run_dict.get("data_quality")
        if isinstance(prev_dq, dict) and prev_dq.get("carried_forward_broken"):
            out["carried_forward_broken"] = prev_dq["carried_forward_broken"]
    except Exception:  # noqa: BLE001 - Quality-Tag darf den Lauf nie kippen
        pass
    return out


# ----------------------------------------------------------------------
# Sub-Checks
# ----------------------------------------------------------------------

def _interval_skipped_llms(run_dict: Dict, cfg: Dict) -> List[str]:
    """Rekonstruiert die heute uebersprungenen Engines aus der Intervall-Regel.

    Nur Fallback fuer Laeufe/Backfills ohne run["skipped_llms"] (alle Laeufe vor
    dem 05.08.2026). Dieselbe Formel wie in analyzer/main.py:
    interval_days > 1 und (ordinal(Run-Datum) + interval_offset) % interval_days != 0.
    """
    out: List[str] = []
    try:
        from datetime import date
        raw = run_dict.get("started_at") or run_dict.get("run_id") or ""
        d = None
        if isinstance(raw, str) and len(raw) >= 10:
            try:
                d = date(int(raw[0:4]), int(raw[5:7]), int(raw[8:10]))
            except (TypeError, ValueError):
                d = None
        if d is None:
            return []
        ordn = d.toordinal()
        for l in (cfg.get("llms", []) or []):
            lid = l.get("id")
            if not lid or not l.get("enabled"):
                continue
            try:
                iv = int(l.get("interval_days") or 1)
                off = int(l.get("interval_offset") or 0)
            except (TypeError, ValueError):
                continue
            if iv > 1 and ((ordn + off) % iv != 0):
                out.append(lid)
    except Exception:  # noqa: BLE001 - Quality-Tag darf den Lauf nie kippen
        return []
    return out


def _check_llms(run_dict: Dict, cfg: Dict, skipped_llms: Any = None) -> Dict[str, Any]:
    """Welche konfigurierten/aktivierten LLMs haben Daten geliefert?

    Unterscheidet vier Zustaende je aktiver Engine (details["llms_state"]):
      mit_daten                 heute frisch abgefragt, mind. eine verwertbare Antwort
      fortgeschrieben           Intervall-Aussetzer, Vortageswert per
                                _carry_forward_llm uebernommen (run["carried_forward"])
      uebersprungen_ohne_vortag Intervall-Aussetzer OHNE Vorgeschichte — kein Defekt,
                                aber an diesem Tag fehlen Daten
      ausgefallen               war heute dran, hat aber nichts geliefert
                                -> nur DAS ist eine Warnung und senkt Grade/Score
    """
    cfg_llms = cfg.get("llms", []) or []
    llms_configured = [l.get("id") for l in cfg_llms if l.get("id")]
    llms_configured_active = [l.get("id") for l in cfg_llms if l.get("id") and l.get("enabled")]

    # Welche LLMs haben in mindestens einem Produkt mindestens eine non-error Antwort?
    llms_with_data = set()
    for prod in (run_dict.get("products") or {}).values():
        for entry in (prod.get("per_llm") or []):
            llm_id = entry.get("llm")
            results = entry.get("results") or []
            if not llm_id:
                continue
            ok = any(not r.get("error") and (r.get("response_text") or "").strip()
                     for r in results)
            if ok:
                llms_with_data.add(llm_id)

    # --- Zustandsbestimmung je aktiver Engine -------------------------------
    # 1) Uebersprungene: bevorzugt der durchgereichte Wert aus main.py, sonst das
    #    im Run gespeicherte Feld, sonst aus der Intervall-Regel rekonstruiert.
    if skipped_llms is None:
        skipped_llms = run_dict.get("skipped_llms")
    if skipped_llms is None:
        skipped_llms = _interval_skipped_llms(run_dict, cfg)
    skipped = {l for l in (skipped_llms or []) if l}

    # 2) Fortgeschriebene: _carry_forward_llm haengt nur an, wenn wirklich Daten
    #    aus dem Vortag uebernommen wurden.
    carried = {l for l in (run_dict.get("carried_forward") or []) if l}

    iv_by_id = {}
    for l in cfg_llms:
        if l.get("id"):
            iv_by_id[l["id"]] = l.get("interval_days")

    states: Dict[str, str] = {}
    state_reasons: Dict[str, str] = {}
    for lid in llms_configured_active:
        iv = iv_by_id.get(lid)
        iv_txt = f"interval_days={iv}" if iv else "interval_days"
        if lid in carried:
            states[lid] = "fortgeschrieben"
            state_reasons[lid] = (f"laut {iv_txt} heute nicht dran, Vortageswert fortgeschrieben"
                                  if lid in skipped else
                                  "heute keine eigenen Daten, Vortageswert fortgeschrieben")
        elif lid in llms_with_data:
            states[lid] = "mit_daten"
        elif lid in skipped:
            states[lid] = "uebersprungen_ohne_vortag"
            state_reasons[lid] = (f"laut {iv_txt} heute nicht dran, "
                                  "keine Vorgeschichte zum Fortschreiben")
        else:
            states[lid] = "ausgefallen"
            state_reasons[lid] = "war heute dran, hat aber keine verwertbare Antwort geliefert"

    def _ids(state: str) -> List[str]:
        return sorted([l for l, s in states.items() if s == state])

    # llms_failed enthaelt ab 05.08.2026 nur noch ECHTE Ausfaelle. Intervall-
    # Aussetzer (mit oder ohne Vortageswert) stehen in llms_skipped* und sind
    # neutrale Information — sie duerfen Grade und Score nicht senken.
    llms_failed = _ids("ausgefallen")
    llms_skipped_no_prev = _ids("uebersprungen_ohne_vortag")
    llms_carried = _ids("fortgeschrieben")
    llms_fresh = _ids("mit_daten")

    return {
        "llms_configured": len(llms_configured),
        "llms_configured_active": len(llms_configured_active),
        "llms_with_data": sorted(llms_with_data),
        "llms_failed": llms_failed,
        # additive Felder (Cockpit liest die bestehenden weiter unveraendert)
        "llms_state": states,
        "llms_state_reasons": state_reasons,
        "llms_fresh": llms_fresh,
        "llms_carried_forward": llms_carried,
        "llms_skipped_no_prev": llms_skipped_no_prev,
        "llms_skipped_today": sorted([l for l in skipped if l in llms_configured_active]),
        # wie viele aktive Engines waren heute ueberhaupt dran?
        "llms_expected_today": len(llms_fresh) + len(llms_failed),
    }


def _check_urls(run_dict: Dict) -> Dict[str, Any]:
    """Wie viele Seiten waren erreichbar (HTTP 2xx)?"""
    pt = run_dict.get("page_tracking") or {}
    events = pt.get("events_this_run") or []
    total = len(events)
    if total == 0:
        return {"urls_total": 0, "urls_reachable": 0, "urls_failed_count": 0}

    reachable = 0
    for e in events:
        # Event-Status: 'ok' / 'error' / 'first_seen' / 'changed' / 'unchanged'
        # Erreichbar wenn kein Error-Status und kein Error-Feld
        if e.get("error"):
            continue
        # Status-Code falls vorhanden
        status = e.get("status")
        if status is not None and isinstance(status, int):
            if 200 <= status < 400:
                reachable += 1
        else:
            # Kein Status -> aus event-type ableiten
            if not e.get("error"):
                reachable += 1

    return {
        "urls_total": total,
        "urls_reachable": reachable,
        "urls_failed_count": total - reachable,
    }


def _check_why(run_dict: Dict) -> Dict[str, Any]:
    """Status der Why-Analyse.

    Datenstruktur: run_dict["why_analysis"] = {
        "<product_id>": {
            "<brand_name>": {"reasons_mentioned": ..., "key_topics": [...], ...},
            ...
        },
        ...
    }
    Ein Produkt zählt als OK, wenn mindestens eine Marke darunter mindestens ein
    nicht-leeres Why-Feld hat (reasons_mentioned/reasons_absent/key_topics).
    """
    why = run_dict.get("why_analysis")
    if why is None:
        return {"why_status": "skipped", "why_products_ok": 0, "why_products_total": 0}
    if isinstance(why, dict) and why.get("error"):
        return {"why_status": "failed", "why_products_ok": 0, "why_products_total": 0,
                "why_error": str(why.get("error"))[:200]}
    if not why or not isinstance(why, dict):
        return {"why_status": "skipped", "why_products_ok": 0, "why_products_total": 0}

    total = len(why)
    ok = 0
    for prod_id, prod_data in why.items():
        if not isinstance(prod_data, dict):
            continue
        # mind. eine Marke mit nicht-leerem reasons_mentioned ODER key_topics ODER reasons_absent
        for brand_name, brand_data in prod_data.items():
            if not isinstance(brand_data, dict):
                continue
            if (brand_data.get("reasons_mentioned") or
                brand_data.get("reasons_absent") or
                brand_data.get("key_topics")):
                ok += 1
                break  # ein Treffer pro Produkt reicht

    if total == 0:
        return {"why_status": "skipped", "why_products_ok": 0, "why_products_total": 0}
    if ok == total:
        return {"why_status": "ok", "why_products_ok": ok, "why_products_total": total}
    if ok == 0:
        return {"why_status": "failed", "why_products_ok": ok, "why_products_total": total}
    return {"why_status": "partial", "why_products_ok": ok, "why_products_total": total}
