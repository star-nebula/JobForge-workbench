# JobForge Workbench

**Language / 语言 / 言語 / Idioma / 언어 / Langues / Sprachen:** [English](../README.md) | [简体中文](README_zh-CN.md) | [日本語](README_ja.md) | [한국어](README_ko.md) | [Français](README_fr.md) | [Deutsch](README_de.md) | [Español](README_es.md)

Lokale Bewerbungs-Werkbank: Lebenslauf → Keywords → Job-Scraping (BOSS Zhipin) → Match-Ranking, alles in einem Einzelplatz-Tool.

> **Plattform: nur Windows 10/11.** Der native Scraping-Kanal steuert Desktop-Chrome über Windows UI Automation (pywinauto / UIA TextPattern) und Tastatur-Maus-Automatisierung (pyautogui / pygetwindow); diese nur unter Windows verfügbaren Bibliotheken werden beim Serverstart importiert — unter macOS/Linux startet der Server gar nicht. Zudem sind Desktop-Chrome und Python 3.10+ erforderlich.

![Dashboard](../docs/screenshots/dashboard.png)

## Funktionen

- **Profil**: Lebenslauf-Upload und -Analyse, feldweiser Vergleich und Übernahme, lokale regelbasierte Bewertung mit Verbesserungsvorschlägen, Lebenslauf-Vorschau, PDF-Export
- **Smartes Scraping**: holt BOSS-Stellen anhand der Lebenslauf-Keywords (nativer Browser-Kanal), mit Scrape-Historie und Deduplizierung pro Stelle
- **Stellenmarkt**: Stellen werden in der DB gespeichert, JD-Details mit Textbereinigung, Sortierung nach Match, JD-Abrufstatistiken (geholt / nicht geholt / möglicherweise unvollständig — Klick auf die Pills filtert; möglicherweise unvollständige lassen sich im Detail-Modal manuell bestätigen oder erneut abrufen). Das Matching ist zweistufig: **Tag-Vorauswahl** (lokale Regeln über 4 Dimensionen: Skills / Absicht / Gehalt / Stadt; Skill-Nenner sind die Skill-Tags der Stelle, exakte Übereinstimmung nach Wortnormalisierung, Gehalt auf K normalisiert, echte 0–100 ohne Boden), berechnet beim Scraping; und **JD-Feinabgleich** (ein LLM liest den vollständigen JD plus harte Infos — Tags / Gehalt / Stadt), erzeugt im Detail-Modal oder in der Stapelanalyse; Karten tragen ein blaues „AI xx“-Abzeichen
- **KI-Funktionen** (Multi-Modell-Konfiguration, OpenAI-kompatibles Protokoll, Schlüssel nur im lokalen SQLite gespeichert): BOSS-Ansprachetexte, KI-Match-Analyse (läuft automatisch im Detail-Modal + Stapelanalyse mit einem Klick im Stellenmarkt), Lebenslauf-Feinschliff (Diff-Vergleich vor der Übernahme)
- **Bewerbungs-Pipeline**: Kanban mit Drag-and-drop über 6 Status (discovered / reviewing / applied / interviewing / rejected / offered)
- **Interview-Kalender**: Interview-Termine und Notizen aus der Spalte „im Interview“
- **Nachrichtenzentrale**: schreibgeschützte Synchronisierung der BOSS-Zhipin-Konversationen (CDP-Antwortabfangen)
- **Schwebendes Fortschrittsfenster**: bleibt während des Scrapings immer im Vordergrund, mit Pause-/Fortsetzen-/Stopp-Buttons. Das Fenster klaut keinen Fokus und lässt Mausklicks durch — es **unterbricht eine laufende Tastatur-Maus-Scraping-Sitzung also nicht**; per ⠿-Griff verschiebbar

## Projektstruktur

```
JobForge-workbench/
├─ src/jobforge/                 # Code: Python-Paket
│  ├─ server.py                  # FastAPI-Einstiegspunkt
│  ├─ paths.py                   # Einzige Quelle der Projektpfade (Code- und Datenort entkoppelt)
│  ├─ spider.py  fetch_jd.py  fetch_jd_native.py  fetch_gate.py
│  ├─ db.py  llm.py  profile_score.py
│  └─ tools/                     # Subprozess-Skripte, vom Server als `python -m jobforge.tools.*` gestartet
│     └─ hud.py  messages.py  grab_cookies.py
├─ web/job-workbench.html        # Frontend-Einzelseite (6 Ansichten)
├─ data/                         # Laufzeitdaten (nicht im Repo): jobs.db, cookies.json, messages.json, Gate-/Throttle-Dateien, chrome-profile/
├─ tests/                        # pytest-Regression (zuerst `pip install -r requirements-dev.txt`, dann `venv\Scripts\python.exe -m pytest tests/`; die Testanzahl wird nicht dokumentiert — ausführen zum Zählen)
├─ run.bat  setup.bat  requirements.txt  README.md
```

Alle Datenpfade stammen aus `paths.py`; kein Modul leitet sie aus seinem eigenen `__file__` her — beim Verschieben des Codes wandern die Daten nicht mit.
Das user-data-dir des CDP-gebuggten Chrome (mit der BOSS-Anmeldung) liegt ebenfalls im Datenverzeichnis: `data/chrome-profile/`.

Für Bewerbungsstatus gibt es genau ein Vokabular: das Frontend-`STATUS_META` (Kanban-Spalten, Statusauswahl im Detail-Modal und Dashboard-Pipeline leiten sich alle davon ab)
und das Backend-`db.VALID_STATUSES`. Beide Mengen sind identisch, jeder Status hat einen erreichbaren Schreibpfad, verankert durch
`tests/test_frontend_status_contract.py` — es gab einmal den Fall, dass 6 Status deklariert waren, das Kanban aber nur 4 Spalten zeichnete:
`rejected`/`offered` ließen sich in der UI nicht setzen und 114 Stellen in der DB behielten nur zwei Werte.

Hinweis: `.bat`-Dateien müssen CRLF-Zeilenenden behalten (`.gitattributes` deklariert `*.bat text eol=crlf`) — unter „Codepage-Wechsel via `chcp` + chinesische Kommentare + nacktes LF“ parst cmd.exe byte-verschoben und verschluckt still den Anfang der Zeile `set "PYTHONPATH=..."`; sichtbar als `ModuleNotFoundError: No module named 'jobforge'` beim Start.

Manueller Start (IDE / Kommandozeile) benötigt `src` in `PYTHONPATH`, sonst schlägt `import jobforge` fehl:

```
set PYTHONPATH=%CD%\src
venv\Scripts\python.exe -m jobforge.server
```

## Architektur

| Datei | Aufgabe |
|---|---|
| `src/jobforge/server.py` | FastAPI-Einstiegspunkt, `127.0.0.1:8080` (`--port` änderbar, nützlich zum Debuggen einer zweiten Instanz) |
| `src/jobforge/paths.py` | Einzige Quelle der Projektpfade: `PROJECT_ROOT` / `DATA_DIR` / `WEB_DIR` |
| `src/jobforge/spider.py` / `fetch_jd_native.py` | Scraping von Stellenliste und JD-Details (nativ über Tastatur/Maus + UIA-TextPattern-Kanal) |
| `src/jobforge/fetch_jd.py` | Haupteingang für den JD-Abruf (zuerst nativ, CDP als Rückfallebene) + JD-Textbereinigung |
| `src/jobforge/tools/hud.py` | Schwebendes Fortschrittsfenster (eigener Prozess; die drei Fensterauflagen — immer vorn + kein Fokusklau + Klickdurchlässigkeit — sind im Dateikopfname vermerkt) |
| `src/jobforge/fetch_gate.py` | Prozessübergreifendes Pause/Stopp-Gate: Signal landet in `data/fetch_gate.json`, geteilt vom Server-Thread und den Scraping-Subprozessen |
| `src/jobforge/tools/messages.py` | BOSS-Nachrichtensync (Playwright CDP verbindet sich mit dem Browser auf Port 9222 und fängt Seitenantworten ab) |
| `src/jobforge/db.py` | SQLite (WAL): Stellen / Nachrichten / Profil / Scrape-Historie |
| `src/jobforge/profile_score.py` | Lokale regelbasierte Bewertungs-Engine für Profile (13 Prüfungen) |
| `src/jobforge/llm.py` | LLM-Funktionsschicht (OpenAI-kompatibler Chat-Client + drei Funktionsbausteine: Ansprache / Match-Analyse / Lebenslauf-Feinschliff) |
| `web/job-workbench.html` | Frontend-Einzelseite (6 Ansichten) |
| `src/jobforge/tools/grab_cookies.py` | Holt die Login-Cookies des Browsers und schreibt `data/cookies.json` |

## Verwendung

1. `setup.bat` doppelklicken, um das venv anzulegen und Abhängigkeiten zu installieren
2. `run.bat` doppelklicken zum Starten, dann <http://127.0.0.1:8080> im Browser öffnen
3. Desktop-Chrome öffnen und bei zhipin.com anmelden, dann ist Scraping möglich (das Scraping belegt Tastatur und Maus für ca. 8–15 s; der Nachrichtenabruf braucht den Browser mit Debug-Port 9222)
4. Über das ⚙ oben rechts ein KI-Modell konfigurieren, dann stehen Ansprachetexte / Match-Analyse / Lebenslauf-Feinschliff zur Verfügung (funktioniert mit DeepSeek, Qwen, Zhipu, Ollama und jedem OpenAI-kompatiblen Dienst)
5. Voraussetzung für die KI-Stapelanalyse: Desktop-Chrome ist geöffnet und bei zhipin.com angemeldet (Fenster nicht minimieren); die Analyse belegt Tastatur und Maus, 3 Fehlschläge in Folge lösen einen automatischen Abbruch aus. Der Server hat eine Einzelinstanz-Sperre — ein doppelter Start wird abgelehnt
6. Der Scrape-Fortschritt zeigt sich im schwebenden Fenster (manuell über „🪟 Fortschrittsfenster“ in der Kopfleiste öffnen; es öffnet sich auch automatisch beim Scraping-Start):
   - **Pause** hält nur an sicheren Punkten (Stellengrenzen, Throttle-Wartezeiten) und zerschneidet nie eine einzelne Tastatur-/Mausaktion; die Pausenzeit zählt nicht aufs Throttling, nach dem Fortsetzen also kein erneutes Warten
   - **Stopp** greift binnen Sekunden (beendet auch den laufenden Scraping-Subprozess); bereits geholte Stellen und abgeschlossene KI-Analysen bleiben erhalten
   - Das Fenster erscheint standardmäßig unten rechts; per ⠿-Griff verschieben. Nach Task-Ende bleibt es einige Sekunden stehen, zeigt das Ergebnis und schließt sich selbst; ✕ schließt sofort

## Datenschutz

`data/` (`jobs.db`, `messages.json`, `cookies.json`, `fetch_gate.json`, `hud_pos.json` usw.) und das Browser-Profil (`chrome-profile/`, regulärer Ort `data/chrome-profile`) sind über `.gitignore` ausgeschlossen und werden nie committet.
