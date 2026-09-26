# -*- coding: utf-8 -*-
"""Monatliche GEO-Messung fuer biohackingkompakt.de (getrennt vom ERGO-Cockpit).

Stellt die Fragen aus bk/fragen.json an KI-Suchen MIT Websuche und prueft:
  - zitiert:  biohackingkompakt.de steht in den vom Anbieter ausgewiesenen Quellen
  - genannt:  "Biohacking Kompakt" oder die Domain steht im Antworttext
Schreibt data/bk/runs/<datum>.json und data/bk/bericht.md.
Nutzt nur die Client-Klassen aus analyzer/llm_clients.py; das ERGO-Setup bleibt unberuehrt.
"""
import argparse, datetime as dt, json, os, re, sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from analyzer import llm_clients as L  # noqa: E402

# Neutraler System-Prompt statt des Versicherungs-Prompts (nur in diesem Prozess).
L.SYSTEM_PROMPT = "Du bist ein hilfreicher Assistent. Antworte auf Deutsch."

DOMAIN = "biohackingkompakt.de"
NENNUNG = re.compile(r"biohacking\s*kompakt|biohackingkompakt\.de", re.I)
OUT = ROOT / "data" / "bk"

ENGINES = [
    ("gemini", "Gemini (Google-Suche)", lambda: L.GeminiClient(os.environ["GOOGLE_API_KEY"], model="gemini-2.5-flash", max_tokens=1200, temperature=0.3), "GOOGLE_API_KEY"),
    ("chatgpt_web", "ChatGPT (Websuche)", lambda: chatgpt_client(), "OPENAI_API_KEY"),
    ("perplexity", "Perplexity", lambda: L.PerplexityClient(os.environ["PERPLEXITY_API_KEY"], model="sonar", max_tokens=1200, temperature=0.3), "PERPLEXITY_API_KEY"),
]


def chatgpt_client():
    """ChatGPT mit Websuche ueber die Responses-API. gpt-4o-mini-search-preview ist
    abgekuendigt (Lauf 26.09.2026: HTTP 404). GPT-5-Modelle akzeptieren keine
    temperature und brauchen Platz fuer Reasoning-Tokens."""
    c = L.OpenAIWebSearchClient(os.environ["OPENAI_API_KEY"], model="gpt-5-mini", api="responses",
                                max_tokens=4000, temperature=0.3)
    orig = c._call_responses

    def ohne_temperatur(prompt):
        r = orig(prompt)
        r["payload"].pop("temperature", None)
        r["payload"]["reasoning"] = {"effort": "low"}
        return r
    c._call_responses = ohne_temperatur
    return c


def host(u):
    try:
        h = urlparse(u).netloc.lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def src_host(s):
    d = (s.get("domain") or "").strip().lower()
    if d.startswith("www."):
        d = d[4:]
    return d or host(s.get("url_resolved") or s.get("url", ""))


def auswerten(resp):
    src = [s for s in (resp.sources or []) if s.get("url")]
    belegt = [s for s in src if s.get("src_typ", L.SRC_ANNOTATION) == L.SRC_ANNOTATION]
    return {
        "zitiert": any(src_host(s).endswith(DOMAIN) for s in (belegt or src)),
        "genannt": bool(NENNUNG.search(resp.text or "")),
        "quellen": [s.get("url_resolved") or s.get("url") for s in src],
        "domains": sorted({src_host(s) for s in src if src_host(s)}),
        "fehler": resp.error,
        "text": (resp.text or "")[:4000],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    fragen = json.loads((ROOT / "bk" / "fragen.json").read_text(encoding="utf-8"))
    if a.limit:
        fragen = fragen[: a.limit]
    datum = dt.date.today().isoformat()
    lauf = {"datum": datum, "domain": DOMAIN, "fragen": fragen, "engines": {}}
    for eid, name, mk, env in ENGINES:
        if a.dry_run:
            client = None
        elif not os.getenv(env):
            print(f"[WARN] {env} fehlt - {name} uebersprungen"); continue
        else:
            client = mk()
        erg = []
        for f in fragen:
            if client is None:
                r = {"zitiert": False, "genannt": False, "quellen": [], "domains": ["beispiel.de"], "fehler": None, "text": "(dry-run)"}
            else:
                try:
                    r = auswerten(client.ask(f))
                except Exception as e:  # ein Fehler kippt nicht den ganzen Lauf
                    r = {"zitiert": False, "genannt": False, "quellen": [], "domains": [], "fehler": str(e)[:300], "text": ""}
            r["frage"] = f
            erg.append(r)
            print(f"[{eid}] {'Z' if r['zitiert'] else '-'}{'N' if r['genannt'] else '-'} {f}")
        lauf["engines"][eid] = {"name": name, "ergebnisse": erg}
    OUT.joinpath("runs").mkdir(parents=True, exist_ok=True)
    OUT.joinpath("runs", f"{datum}.json").write_text(json.dumps(lauf, ensure_ascii=False, indent=1), encoding="utf-8")
    bericht(lauf)


def bericht(lauf):
    runs = sorted(OUT.joinpath("runs").glob("*.json"))
    vor = json.loads(runs[-2].read_text(encoding="utf-8")) if len(runs) > 1 else None
    n = len(lauf["fragen"])
    z = [f"# GEO-Messung biohackingkompakt.de – {lauf['datum']}", "",
         f"{n} Fragen je KI-Suche, jeweils mit Websuche. **Zitiert** = die Seite steht in den vom Anbieter ausgewiesenen Quellen. **Genannt** = der Name steht im Antworttext.", "",
         "| KI-Suche | zitiert | genannt | Vormonat zitiert |", "|---|---|---|---|"]
    alle = Counter()
    for eid, e in lauf["engines"].items():
        zi = sum(r["zitiert"] for r in e["ergebnisse"]); ge = sum(r["genannt"] for r in e["ergebnisse"])
        fe = sum(1 for r in e["ergebnisse"] if r.get("fehler"))
        v = "–"
        if vor and eid in vor["engines"]:
            v = str(sum(r["zitiert"] for r in vor["engines"][eid]["ergebnisse"]))
        z.append(f"| {e['name']} | {zi} von {n} | {ge} von {n} | {v} |" + (f" ({fe} Fehler)" if fe else ""))
        for r in e["ergebnisse"]:
            alle.update(r["domains"])
    z += ["", "## Fragen, bei denen die Seite zitiert oder genannt wurde", ""]
    treffer = [(e["name"], r["frage"], "zitiert" if r["zitiert"] else "genannt") for e in lauf["engines"].values() for r in e["ergebnisse"] if r["zitiert"] or r["genannt"]]
    z += [f"- {a}: {b} ({c})" for a, b, c in treffer] or ["- noch keine"]
    z += ["", "## Meistzitierte Quellen (alle KI-Suchen zusammen)", ""]
    z += [f"{i}. {d} – {c}" for i, (d, c) in enumerate(alle.most_common(20), 1)]
    OUT.joinpath("bericht.md").write_text("\n".join(z) + "\n", encoding="utf-8")
    print("\n".join(z))


if __name__ == "__main__":
    main()
