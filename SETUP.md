# Setup-Anleitung — Schritt für Schritt

Diese Anleitung führt Sie in ~15 Minuten vom leeren GitHub-Account zum lauffähigen Dashboard.

> **Vorab:** Sie brauchen zwei API-Keys. Beide können Sie vorab in separaten Browser-Tabs besorgen:
> - **Google Gemini API-Key:** [aistudio.google.com/apikey](https://aistudio.google.com/apikey) (kostenlos)
> - **Anthropic Claude API-Key:** [console.anthropic.com](https://console.anthropic.com) (ca. 5–10 € auf den Account laden reicht lange)

---

## Schritt 1 — GitHub-Account

- Wenn noch nicht vorhanden: [github.com/signup](https://github.com/signup) (kostenlos, dauert 2 Minuten).
- Nach Login sollten Sie auf der Startseite `github.com` landen.

*[Screenshot-Platzhalter: GitHub-Startseite nach dem Login]*

---

## Schritt 2 — Neues Repository anlegen

1. Rechts oben auf das **"+"**-Symbol klicken → **"New repository"**.
2. **Repository name:** `geo-visibility-tool`
3. **Description** (optional): `LLM-Sichtbarkeits-Analyse für ERGO`
4. **Visibility:** unbedingt **Private** auswählen (API-Keys sind zwar in Secrets geschützt, aber Daten sollten nicht öffentlich sein)
5. **Initialize this repository with:** Keine Häkchen setzen (wir bringen unsere eigenen Dateien mit)
6. Knopf **"Create repository"** unten drücken

*[Screenshot-Platzhalter: "New repository"-Formular komplett ausgefüllt]*

---

## Schritt 3 — Projektdateien hochladen

Sie haben zwei Optionen.

### Option A — Drag & Drop im Browser (empfohlen für Nicht-Entwickler)

1. Auf der Seite des neuen (leeren) Repos sehen Sie einen Link **"uploading an existing file"** → anklicken.
2. Öffnen Sie auf Ihrem Rechner den Ordner `C:\Users\hoese\OneDrive\Paul\ClaudeProjekte\GEO\Geo`.
3. Wählen Sie mit **Strg+A** ALLE enthaltenen Dateien und Ordner aus.
4. Ziehen Sie sie ins Browser-Fenster (in den markierten Drag-Bereich).
5. Warten Sie, bis alle Dateien hochgeladen sind.
6. Unten **Commit message:** *Initial import* eintippen.
7. **"Commit changes"** klicken.

*[Screenshot-Platzhalter: Upload-Maske mit allen Dateien sichtbar]*

**Wichtig:** Die versteckten Ordner `.github` und `.gitignore` müssen mitkommen. Im Windows-Explorer unter **Ansicht → Ausgeblendete Elemente** einschalten, falls sie fehlen.

### Option B — Git-Befehlszeile (für Geübte)

```bash
cd "C:\Users\hoese\OneDrive\Paul\ClaudeProjekte\GEO\Geo"
git init -b main
git remote add origin https://github.com/<Ihr-Name>/geo-visibility-tool.git
git add .
git commit -m "Initial import"
git push -u origin main
```

---

## Schritt 4 — API-Keys als Secrets hinterlegen

1. Im Repo oben auf **"Settings"** klicken (ganz rechts im Menü).
2. Linke Seitenleiste: **"Secrets and variables"** aufklappen → **"Actions"** auswählen.
3. Grüner Knopf **"New repository secret"**.
4. Folgende **zwei** Secrets anlegen (Name exakt so schreiben, Groß-/Kleinschreibung beachten):

| Name | Value |
|------|-------|
| `ANTHROPIC_API_KEY` | Ihr Claude-Key (beginnt mit `sk-ant-api...`) |
| `GOOGLE_API_KEY` | Ihr Gemini-Key (String aus Google AI Studio) |

*[Screenshot-Platzhalter: "New repository secret"-Formular]*

Nach dem Speichern sehen Sie die Secrets in der Liste, die Werte selbst sind maskiert — das ist richtig so.

---

## Schritt 5 — Ersten Analyse-Lauf starten

1. Oben im Repo auf **"Actions"** klicken.
2. Falls eine Meldung erscheint wie *"Workflows aren't being run on this forked repository"*: Auf **"I understand my workflows, go ahead and enable them"** klicken.
3. Links sehen Sie den Workflow **"Analyze Visibility"** — anklicken.
4. Rechts erscheint ein Hinweis **"This workflow has a workflow_dispatch event trigger"** mit dem Knopf **"Run workflow"** → anklicken.
5. Ein Dropdown öffnet sich:
   - **dry_run:** `false` lassen (es sei denn, Sie wollen nur testen ohne API-Kosten)
   - **limit:** leer lassen (= alle 20 Prompts pro Produkt)
6. Grüner Knopf **"Run workflow"** drücken.

*[Screenshot-Platzhalter: "Run workflow"-Dropdown mit Feldern]*

Nach ~3-5 Minuten ist der Lauf fertig. Sie sehen einen grünen Haken neben dem Lauf in der Liste. Ein Klick auf den Lauf zeigt die Logs — hier können Sie sehen, wie Gemini und Claude angesprochen wurden.

---

## Schritt 6 — Dashboard aktivieren (GitHub Pages)

1. Im Repo **"Settings" → "Pages"** aufrufen.
2. Bei **"Source"** das Dropdown auf **"Deploy from a branch"** stellen.
3. Darunter:
   - **Branch:** `main`
   - **Folder:** `/dashboard`
4. Knopf **"Save"** drücken.
5. GitHub zeigt nach ~60 Sekunden oben eine Meldung wie *"Your site is live at `https://<Ihr-Name>.github.io/geo-visibility-tool/`"* — diesen Link als Lesezeichen speichern.

*[Screenshot-Platzhalter: GitHub Pages-Einstellungen mit dem Live-Link]*

Öffnen Sie den Link — Sie sehen das Dashboard mit den Ergebnissen des ersten Laufs.

---

## Schritt 7 — Produkte / Wettbewerber anpassen

Alles Inhaltliche steuern Sie über `data/config.json` und `data/prompts/*.json`:

- **Marke ändern:** `data/config.json` → `brand`-Sektion
- **Produkte hinzufügen/ändern:** `data/config.json` → `products`-Liste
- **Wettbewerber anpassen (max. 10):** `data/config.json` → `competitors`-Liste
- **Prompts pro Produkt ändern:** `data/prompts/<product_id>.json`

Sie können die Dateien direkt im GitHub-Browser-Editor ändern (Stift-Symbol oben rechts in der Datei-Ansicht), Änderungen speichern und dann einen neuen Lauf starten.

---

## Schritt 8 — Impact-Analyse bei Folge-Läufen

Ab dem **zweiten Lauf** vergleicht das Tool automatisch mit dem vorherigen Lauf:
- Veränderungen in Share of Voice, Rang, Zitierungen werden als Deltas ausgewiesen
- Die Executive Summary (von Claude generiert) liest die Deltas + Website-Diffs und interpretiert sie
- Im Dashboard sehen Sie die "Top 10 Veränderungen"-Tabelle

---

## Troubleshooting

**"Workflow failed" mit Fehlermeldung `ANTHROPIC_API_KEY fehlt`:**
Secrets falsch benannt. Namen müssen exakt `ANTHROPIC_API_KEY` und `GOOGLE_API_KEY` lauten.

**"HTTP 401 Unauthorized":**
API-Key ist falsch oder abgelaufen. Neu erstellen und das Secret aktualisieren.

**Dashboard zeigt "Noch keine Läufe vorhanden":**
- Ist der erste Lauf wirklich erfolgreich durchgelaufen? (Actions → grüner Haken?)
- Ist GitHub Pages aktiviert? (Settings → Pages → Source: main /dashboard)
- Ein F5-Hard-Reload im Browser (Strg+Shift+R) hilft manchmal.

**Fehler beim Webseiten-Scraping:**
ERGO kann manche IP-Bereiche (z. B. GitHub-Actions-Runner) blocken. In dem Fall erscheint der Scrape-Fehler in der Workflow-Log-Ausgabe. Lösung: Später erneut laufen lassen oder auf einen anderen Runner wechseln.

**API-Kosten überwachen:**
Ein Lauf kostet ca. 0,30 – 1,70 € je nach Modell. Das Anthropic-Dashboard zeigt den Verbrauch minutengenau.

---

## Erweiterungen später

- **Wöchentlich automatisch ausführen:** In `.github/workflows/analyze.yml` die `schedule:` Zeilen auskommentieren.
- **Weitere LLMs:** In `data/config.json` bei `llms` den gewünschten Eintrag auf `"enabled": true` stellen und den passenden API-Key als Secret hinzufügen. In `analyzer/llm_clients.py` einen Client für OpenAI / xAI / Perplexity ergänzen (Pattern analog zu den bestehenden Clients).
- **E-Mail-Benachrichtigung bei signifikanten Änderungen:** Am Ende des Workflows per Webhook oder E-Mail-Action auslösen.
