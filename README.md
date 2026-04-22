# GEO Visibility Tool — LLM-Sichtbarkeits- & Impact-Analyse

Tool zur systematischen Messung der Sichtbarkeit einer Marke und ihrer Produkte in den großen LLMs (aktuell **Gemini** und **Claude**, erweiterbar auf ChatGPT, Grok, Perplexity) inkl. Webseiten-Monitoring und automatischer Impact-Analyse bei Veränderungen.

Inspiriert von [peec.ai](https://peec.ai/), aber kostenfrei auf GitHub-Infrastruktur lauffähig.

---

## Was das Tool tut

1. **Konfiguration:** Sie definieren eine Marke (z. B. ERGO), bis zu 10 Produkte und bis zu 10 Wettbewerber in einer zentralen `config.json`.
2. **Prompt-Bibliothek:** Für jedes Produkt sind 20 realistische Nutzer-Fragen hinterlegt (z. B. *"Welche Zahnzusatzversicherung ist 2026 die beste?"*).
3. **LLM-Abfragen:** Auf Knopfdruck werden alle Prompts an alle konfigurierten LLMs geschickt.
4. **Metriken-Auswertung:** Aus jeder Antwort wird extrahiert:
   - **Share of Voice** — wie oft wird Ihre Marke im Vergleich zu Wettbewerbern genannt?
   - **Position/Rang** — an welcher Stelle taucht Ihre Marke in Listen auf?
   - **Quellen-Zitierung** — wird Ihre Website als Quelle verlinkt?
5. **Webseiten-Monitoring:** Die Produktseiten (z. B. `ergo.de/.../zahnzusatzversicherung`) werden als HTML-Snapshot gespeichert und beim nächsten Lauf gegen den vorherigen Stand diffed.
6. **Impact-Analyse:** Eine KI vergleicht den aktuellen mit dem letzten Lauf und erstellt eine Executive Summary der Veränderungen.
7. **Dashboard:** Eine statische Website (GitHub Pages) visualisiert alle Ergebnisse interaktiv.

---

## Architektur (Kurzfassung)

```
.github/workflows/analyze.yml  → GitHub-Actions-Workflow (manuell startbar)
         │
         ▼
analyzer/main.py               → Orchestrierung der Analyse
         │
   ┌─────┴─────┬─────────────┬──────────────┐
   ▼           ▼             ▼              ▼
Scraper     LLM-Clients    Metriken     Impact-Analyse
(HTML)      (Gemini +      (SoV,        (LLM-basiert,
            Claude)         Rang,        vergleicht
                           Zitierung)    Läufe)
         │
         ▼
data/runs/<timestamp>.json     → Ergebnisse (committed zurück ins Repo)
data/snapshots/<product>.html  → Webseiten-Snapshot
         │
         ▼
dashboard/ (GitHub Pages)       → Interaktives Dashboard
```

---

## Einrichtung (Schritt-für-Schritt)

> **Aufwand:** ca. 15 Minuten. Sie brauchen keinen Programmier-Skill — nur einen GitHub-Account und zwei API-Keys.

### Schritt 1 — GitHub-Account

Falls noch nicht vorhanden, registrieren Sie sich kostenlos unter **[github.com/signup](https://github.com/signup)**.

### Schritt 2 — Repository anlegen

1. Oben rechts auf **"+"** → **"New repository"** klicken.
2. Name: `geo-visibility-tool` (frei wählbar).
3. Sichtbarkeit: **Private** (empfohlen, da API-Keys involviert sind).
4. Häkchen bei **"Add a README file"** entfernen (wir bringen unsere eigene mit).
5. **"Create repository"** klicken.

### Schritt 3 — Dateien hochladen

Variante A (einfach, per Web-Upload):
1. Im neuen Repo auf **"uploading an existing file"** klicken.
2. Den **kompletten Inhalt** des Ordners `GEO` hochziehen (inkl. `.github`, `analyzer`, `data`, `dashboard`, `README.md`, `requirements.txt`).
3. Unten **"Commit changes"** klicken.

Variante B (Git-Befehlszeile, für Geübte):
```bash
cd <lokaler-GEO-Ordner>
git init
git remote add origin https://github.com/<Ihr-Name>/geo-visibility-tool.git
git add .
git commit -m "Initial import"
git push -u origin main
```

### Schritt 4 — API-Keys als Secrets hinterlegen

1. Im Repo oben auf **Settings** klicken.
2. Linke Seitenleiste: **Secrets and variables → Actions**.
3. Knopf **"New repository secret"** — dann zweimal folgende Secrets anlegen:

| Name | Wert |
|------|------|
| `GOOGLE_API_KEY` | Ihr Google-AI-Studio-Key ([aistudio.google.com/apikey](https://aistudio.google.com/apikey)) |
| `ANTHROPIC_API_KEY` | Ihr Claude-API-Key ([console.anthropic.com](https://console.anthropic.com)) |

### Schritt 5 — Analyse zum ersten Mal starten

1. Im Repo oben auf **Actions** klicken.
2. Falls ein Warnhinweis erscheint: **"I understand my workflows, go ahead and enable them"** bestätigen.
3. In der Liste links **"Analyze Visibility"** anklicken.
4. Rechts oben den Knopf **"Run workflow"** → **"Run workflow"** drücken.
5. Nach ~5 Minuten ist der Lauf fertig. Die Ergebnisse liegen in `data/runs/` als neuer Commit.

### Schritt 6 — Dashboard aktivieren (GitHub Pages)

1. Im Repo **Settings → Pages** öffnen.
2. Bei **Source** → **Deploy from a branch** auswählen.
3. **Branch:** `main`, **Folder:** `/dashboard`.
4. **Save** drücken.
5. Nach ~1 Min erscheint oben ein Link wie `https://<Ihr-Name>.github.io/geo-visibility-tool/` — das ist Ihr Dashboard.

### Schritt 7 — Marke, Produkte und Wettbewerber anpassen

Editieren Sie im Repo direkt im Browser die Datei `data/config.json`:
- Marke ändern
- Produkte hinzufügen/entfernen (max. 10)
- Wettbewerber anpassen (max. 10)
- Prompts je Produkt in `data/prompts/*.json` individualisieren

Nach dem Speichern einfach in **Actions** einen neuen Lauf starten.

---

## Kosten-Übersicht

| Posten | Kosten |
|--------|--------|
| GitHub (Actions + Pages + Private Repo) | **0 €** |
| Gemini API (ein Lauf, ~120 Anfragen) | **~0,00 € – 0,20 €** (großzügiges Free-Tier) |
| Claude API (ein Lauf, ~120 Anfragen) | **~0,30 € – 1,50 €** (je nach Modell) |
| **Summe pro Lauf** | **~0,30 € – 1,70 €** |

---

## Erweiterungen (später)

- ChatGPT per OpenAI-API hinzufügen → `analyzer/llm_clients.py` erweitern
- Grok per xAI-API
- Perplexity per Perplexity-API
- Automatisierung via `schedule:` im GitHub-Workflow (z. B. wöchentlich)
- Slack-/E-Mail-Benachrichtigung bei signifikanten Änderungen

---

## Lizenz

Intern / privat. Nicht zur Weitergabe ohne Rücksprache.
