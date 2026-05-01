"""Daten-Qualitäts-Tag pro Run.

Bewertet einen Run nach drei Dimensionen:
- LLMs: wie viele konfigurierte LLMs haben Daten geliefert?
- URLs: wie viele Seiten waren erreichbar?
- Why-Analyse: durchgelaufen oder gescheitert?

Output ist eine Ampel (green/yellow/red) plus Details, die in run["meta"]["data_quality"]
gespeichert werden. Spätere Bootstrap-/Volatilitäts-Module können dann anhand des
Tags entscheiden, welche Runs als Baseline taugen.
"""

from __future__ import annotations

from typing import Dict, List, Any


# Schwellen
URL_OK_THRESHOLD = 0.95  # >= 95% erreichbar -> green
URL_WARN_THRESHOLD = 0.80  # 80-95% -> yellow, <80% -> red


def compute(run_dict: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Berechnet den Quality-Tag für einen Run.

    Args:
        run_dict: Der komplette Run vor Speicherung (mit products, page_tracking,
                  why_analysis, llms ...).
        cfg: Die geladene config.json (für die Liste konfigurierter LLMs).

    Returns:
        Dict mit:
          grade: "green" | "yellow" | "red"
          score: int 0-100 (grobe Heuristik, nicht statistisch)
          reasons: list[str] kurze Begründungen
          warnings: list[str] Auffälligkeiten
          details: dict mit Rohzahlen
    """
    details = {
        **_check_llms(run_dict, cfg),
        **_check_urls(run_dict),
        **_check_why(run_dict),
    }

    reasons: List[str] = []
    warnings: List[str] = []

    # LLM-Bewertung
    n_cfg_active = details["llms_configured_active"]
    n_failed = len(details["llms_failed"])
    if n_cfg_active == 0:
        warnings.append("Keine LLMs konfiguriert oder aktiv")
        llm_grade = "red"
    elif n_failed == 0:
        reasons.append(f"alle {n_cfg_active} LLMs erfolgreich")
        llm_grade = "green"
    elif n_failed == 1 and n_cfg_active >= 3:
        warnings.append(f"1 von {n_cfg_active} LLMs ausgefallen: {details['llms_failed'][0]}")
        llm_grade = "yellow"
    else:
        warnings.append(f"{n_failed} von {n_cfg_active} LLMs ausgefallen: {', '.join(details['llms_failed'])}")
        llm_grade = "red"

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

    return {
        "grade": worst,
        "score": score,
        "reasons": reasons,
        "warnings": warnings,
        "details": details,
        # Marker fuer spaetere Stat-Module: nur GREEN-Runs als saubere Baseline
        "baseline_eligible": worst == "green",
    }


# ----------------------------------------------------------------------
# Sub-Checks
# ----------------------------------------------------------------------

def _check_llms(run_dict: Dict, cfg: Dict) -> Dict[str, Any]:
    """Welche konfigurierten/aktivierten LLMs haben Daten geliefert?"""
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

    llms_failed = [l for l in llms_configured_active if l not in llms_with_data]

    return {
        "llms_configured": len(llms_configured),
        "llms_configured_active": len(llms_configured_active),
        "llms_with_data": sorted(llms_with_data),
        "llms_failed": llms_failed,
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
