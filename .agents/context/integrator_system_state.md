# System State — Medidesk Integrator

> Ostatnia aktualizacja: 2026-04-18T21:26:00+02:00

## Status projektu

| Element | Status | Uwagi |
|---|---|---|
| Produkcja (Render) | ✅ Działa | https://md-integrator-v1.onrender.com |
| Lokalny dev | ✅ Działa | `uvicorn app.main:app --reload` |
| Testy | ⚠️ Brak CI | `pytest` lokalnie |
| Git | ✅ Czyste | branch `main`, tag `przed_refactoringiem` |

## Ostatni deploy

- **Commit**: `c1b1ddf` — `rename: prefix all agent files with integrator_`
- **Wersja**: 2.0.0
- **Python na Renderze**: 3.14 (UWAGA: ignoruje render.yaml!)

## Aktywne Work Ordery

| WO | Tytuł | Status | Plik |
|---|---|---|---|
| #001 | Dark Mode (Jasny/Ciemny/Systemowy) | 🔄 W trakcie | `.agents/work_orders/integrator_wo001_dark_mode.md` |

## Aktywne prace

- [x] Refaktoring na Jinja2 + base.html
- [x] Centralizacja metadanych (config.py)
- [x] Fundament theme (CSS vars + JS)
- [x] System agentów (.agents/)
- [x] Dokumentacja projektu (docs/)
- [ ] **WO#001**: Wdrożenie Dark Mode (zamiana hardcoded kolorów na CSS vars)
- [ ] **WO#001**: Przełącznik theme w UI (toggle w dashboardzie)
- [ ] **WO#001**: Weryfikacja czytelności komponentów w trybie ciemnym

## Znane problemy

| Problem | Status | Obejście |
|---|---|---|
| Python 3.14 na Render — Jinja2 LRUCache crash | ✅ Obejście | `cache_size=0` w Environment |
| Starlette TemplateResponse zmiana sygnatury | ✅ Obejście | try/except w `render_template()` |
| Render Free tier — cold starts 30-60s | ℹ️ Akceptowane | Plan Free |
| `_invite_html()` — inline HTML (nie Jinja2) | ℹ️ Celowe | Izolowany endpoint |

## Tagi bezpieczeństwa

| Tag | Commit | Opis |
|---|---|---|
| `przed_refactoringiem` | `f96b4eb` | Przed migracją na Jinja2 |
