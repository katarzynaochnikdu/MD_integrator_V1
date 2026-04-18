# Work Order #001 — Dark Mode (Jasny/Ciemny/Systemowy)

**Data utworzenia**: 2026-04-18  
**Worker**: ⚙️ Implementer  
**Priorytet**: 🟡 Normalny  
**Snapshot**: `przed_refactoringiem` (tag istnieje)  
**Status**: 🔄 W trakcie (fundament gotowy, wdrożenie zmiennych CSS — do zrobienia)

---

## Cel

Wdrożyć system-wide przełącznik motywu **Jasny / Ciemny / Systemowy** we wszystkich widokach aplikacji Integrator.

## Kontekst / Inwentaryzacja

### Architektura i Technologia
- **Vanilla CSS/JS** — brak zewnętrznych bibliotek (React/Vue/Tailwind)
- **Style inline** — reguły CSS osadzone w `<style>` wewnątrz szablonów Jinja2 (`app/templates/*.html`)
- **Pliki statyczne** — `app/static/` zamontowany via `StaticFiles` ✅ (już zrobione)
- **Szablon bazowy** — `base.html` z `theme.css` i `theme.js` w `<head>` ✅ (już zrobione)

### Komponenty wymagające modyfikacji kolorów

| Komponent | Plik(i) | Obecne kolory | Uwagi |
|---|---|---|---|
| **Tła główne** | dashboard, setup_wizard, landing | `#F8F9FA`, `#FFFFFF` | Najbardziej widoczne |
| **Sidebar** | dashboard | `#FFFFFF`, border `#E5E7EB` | + hover states |
| **Teksty główne** | wszystkie | `#111827`, `#374151` | Tytuły, paragrafy |
| **Teksty pomocnicze** | wszystkie | `#6B7280`, `#52525b`, `#71717a` | Szare opisy |
| **Obramowania** | wszystkie | `#E5E7EB`, `#F3F4F6` | Karty, dividers, tabele |
| **Karty (card)** | dashboard, setup_wizard | `#FFFFFF` bg, shadow `rgba(0,0,0,0.1)` | |
| **KPI cards** | dashboard | `.kpi-blue .value: #60a5fa` etc. | Kolory wartości OK w dark |
| **Integration cards** | dashboard | `#FFFFFF` bg, flow `#F9FAFB` | |
| **Chip-y mapowania** | dashboard, setup_wizard | `rgba(34,197,94,0.1)`, `rgba(59,130,246,0.1)` | Czytelność na dark tle |
| **Formularze (input/select)** | setup_wizard, dashboard | `#FFFFFF` bg, border `#E5E7EB` | |
| **Modalne okna (dialog)** | dashboard | `#FFFFFF` bg, shadow | |
| **Toast / alert** | dashboard, setup_wizard | `#14532d`/`#450a0a` | Już ciemne — mogą zostać |
| **Login overlay** | dashboard | `#F8F9FA` bg | |
| **Demo banner** | dashboard, setup_wizard | gradient `#f59e0b → #d97706` | Może zostać |
| **Admin login** | admin_login | `#0a0c10` bg (dark by design) | JUŻ ciemne — nie zmieniać |
| **Tabele (th/td)** | dashboard | border `#E5E7EB`/`#F3F4F6` | |
| **Hover states** | wiele plików | `rgba(0,0,0,0.03)`, `rgba(59,130,246,0.05)` | |

### Co jest już gotowe ✅

1. **`app/static/theme.css`** — zmienne CSS zdefiniowane (`:root` + `[data-theme='dark']`)
2. **`app/static/theme.js`** — logika FOIT, sync detection, `localStorage`, `matchMedia`
3. **`app/templates/base.html`** — ładuje `theme.css` + `theme.js` we wszystkich widokach
4. **`render_template()`** — centralne renderowanie z metadanymi

---

## Zakres

### DO (w zakresie):
- [ ] Rozbudować `theme.css` o pełną mapę zmiennych pokrywającą wszystkie komponenty
- [ ] Zamienić hardcoded kolory w `dashboard.html` na `var(--nazwa)`
- [ ] Zamienić hardcoded kolory w `setup_wizard.html` na `var(--nazwa)`
- [ ] Zamienić hardcoded kolory w `landing.html` na `var(--nazwa)`
- [ ] Zamienić hardcoded kolory w `admin_login.html` na `var(--nazwa)` (zachować ciemny design)
- [ ] Zamienić hardcoded kolory w `privacy.html`, `tos.html`, `data_deletion.html`, `demo_contact.html`
- [ ] Dodać przełącznik theme w UI (sidebar dashboard + topbar setup_wizard)
- [ ] Przetestować czytelność chipów mapowania w dark mode
- [ ] Przetestować responsive (mobile) w dark mode

### NIE RÓB (poza zakresem):
- Nie modyfikuj logiki backendowej (webhook, API, auth)
- Nie zmieniaj struktury szablonów (base.html dziedziczenie)
- Nie dodawaj zewnętrznych bibliotek CSS
- Nie modyfikuj `_invite_html()` (inline HTML, izolowany)

## Mapa zmiennych CSS (do rozbudowania)

```css
:root {
    /* ── Tła ── */
    --bg-primary: #F8F9FA;       /* główne tło body */
    --bg-surface: #FFFFFF;       /* karty, sidebar, modalne */
    --bg-surface-hover: #F9FAFB; /* flow-box, lead-detail */
    --bg-input: #FFFFFF;         /* pola formularzy */
    --bg-overlay: rgba(0,0,0,0.5); /* backdrop modali */

    /* ── Tekst ── */
    --text-primary: #111827;     /* tytuły, główna treść */
    --text-secondary: #374151;   /* opisy, podtytuły */
    --text-muted: #6B7280;       /* szare pomocnicze */
    --text-dim: #52525b;         /* bardzo subtelne */

    /* ── Obramowania ── */
    --border-primary: #E5E7EB;   /* karty, sidebar, dividers */
    --border-subtle: #F3F4F6;    /* wewnętrzne separatory */

    /* ── Akcenty (bez zmian w dark) ── */
    --accent-blue: #3b82f6;
    --accent-purple: #a855f7;
    --accent-green: #22c55e;
    --accent-red: #ef4444;
    --accent-yellow: #f59e0b;

    /* ── Shadows ── */
    --shadow-sm: 0 1px 3px rgba(0,0,0,0.1);
    --shadow-lg: 0 20px 25px -5px rgba(0,0,0,0.15);
}

[data-theme="dark"] {
    --bg-primary: #0f1117;
    --bg-surface: #1a1b25;
    --bg-surface-hover: #22232e;
    --bg-input: #13141b;
    --bg-overlay: rgba(0,0,0,0.7);

    --text-primary: #e4e4e7;
    --text-secondary: #a1a1aa;
    --text-muted: #71717a;
    --text-dim: #52525b;

    --border-primary: #27272a;
    --border-subtle: #1e2030;

    --shadow-sm: 0 1px 3px rgba(0,0,0,0.3);
    --shadow-lg: 0 20px 25px -5px rgba(0,0,0,0.4);
}
```

## Kryteria akceptacji

- [ ] Wszystkie 8 widoków renderują się poprawnie w Light mode (brak regresji)
- [ ] Wszystkie 8 widoków renderują się poprawnie w Dark mode
- [ ] Toggle w sidebar działa (Light → Dark → System → Light)
- [ ] Preferencja zapisuje się w `localStorage` i przetrwa reload
- [ ] Tryb "System" reaguje na zmianę `prefers-color-scheme` w OS
- [ ] Brak "białego błysku" (FOIT) przy ładowaniu strony w trybie ciemnym
- [ ] Chipy mapowania czytelne w dark mode
- [ ] Produkcja (Render) działa bez błędów po deploy

## Kolejność realizacji (rekomendowana)

1. Rozbuduj `theme.css` (pełna mapa zmiennych)
2. Zacznij od `landing.html` (najprostszy, szybki test)
3. `dashboard.html` (największy, najważniejszy)
4. `setup_wizard.html` (drugi co do wielkości)
5. Pozostałe widoki (privacy, tos, data_deletion, demo_contact)
6. Dodaj toggle UI
7. QA Gate — pełne testy wizualne Light + Dark
8. Deploy + test na Render
