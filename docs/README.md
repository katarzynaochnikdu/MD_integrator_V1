# Medidesk Integrator — Dokumentacja Projektu

> **Wersja**: 2.0.0  
> **Autor**: Aga - Marketing  
> **Kontakt**: adminzoho@medidesk.com  
> **Repozytorium**: https://github.com/katarzynaochnikdu/MD_integrator_V1  
> **Produkcja**: https://md-integrator-v1.onrender.com

---

## Opis

Medidesk Integrator to aplikacja webowa (FastAPI) łącząca **Facebook Lead Ads** z systemem **Medidesk**. Leady z formularzy reklamowych na Facebooku są automatycznie przesyłane do Medidesk w czasie rzeczywistym za pomocą webhooków.

## Architektura

```
┌──────────────┐     webhook      ┌───────────────────┐     POST     ┌──────────┐
│  Facebook    │ ──────────────►  │  MD Integrator    │ ──────────►  │ Medidesk │
│  Lead Ads   │                  │  (FastAPI/Render)  │              │   API    │
└──────────────┘                  └───────────────────┘              └──────────┘
                                         │
                                    SQLite DB
                                  (integracje, leady,
                                   użytkownicy, sesje)
```

### Stos technologiczny

| Warstwa | Technologia |
|---|---|
| Backend | FastAPI + Uvicorn |
| Szablony HTML | Jinja2 + `base.html` layout |
| Pliki statyczne | `app/static/` via `StaticFiles` |
| Baza danych | SQLite (plik `medidesk.db`) |
|Auth | Facebook OAuth 2.0 + sesje cookie |
| Szyfrowanie tokenów | Fernet (cryptography) |
| Hosting | Render.com (Free tier, Frankfurt) |
| CI/CD | Auto-deploy z `main` branch GitHub |

### Struktura katalogów

```
Integrator/
├── app/
│   ├── main.py              # Główny router FastAPI + endpointy
│   ├── config.py             # Konfiguracja (pydantic-settings, env vars)
│   ├── db.py                 # SQLite schema + migracje
│   ├── fb_auth.py            # Facebook OAuth flow
│   ├── fb_client.py          # Facebook Graph API client
│   ├── webhook.py            # FB webhook handler (leady)
│   ├── medidesk_client.py    # Medidesk API client
│   ├── integrations_store.py # CRUD integracji
│   ├── users_store.py        # CRUD użytkowników + role
│   ├── mapping_ai.py         # AI-assisted field mapping
│   ├── lead_tracker.py       # Log leadów (events table)
│   ├── alerting.py           # Token expiry monitoring
│   ├── templates/            # Szablony Jinja2
│   │   ├── base.html         # Layout bazowy (head, meta, scripts)
│   │   ├── landing.html      # Strona logowania
│   │   ├── dashboard.html    # Panel zarządzania
│   │   ├── setup_wizard.html # Kreator integracji
│   │   ├── admin_login.html  # Login admina
│   │   ├── privacy.html      # Polityka prywatności (FB compliance)
│   │   ├── tos.html          # Regulamin (FB compliance)
│   │   ├── data_deletion.html # Instrukcja usunięcia danych
│   │   └── demo_contact.html # Demo page
│   └── static/               # Zasoby statyczne
│       ├── icon.jpg           # Ikona aplikacji
│       ├── theme.css          # Zmienne CSS (Light/Dark mode)
│       └── theme.js           # FOIT prevention (sync theme detection)
├── render.yaml               # Konfiguracja Render.com
├── requirements.txt          # Zależności Python
└── docs/                     # Dokumentacja
    ├── README.md              # ← Ten plik
    ├── DEPLOYMENT.md          # Wdrożenie i gotchas
    └── CHANGELOG.md           # Historia zmian
```

## Konfiguracja

Wszystkie zmienne środowiskowe mają prefix `MEDIDESK_` (zdefiniowane w `app/config.py`).

### Zmienne obowiązkowe (produkcja)

| Zmienna | Opis |
|---|---|
| `MEDIDESK_FB_APP_ID` | ID aplikacji Facebook |
| `MEDIDESK_FB_APP_SECRET` | Secret aplikacji Facebook |
| `MEDIDESK_FB_REDIRECT_URI` | Callback URL OAuth (np. `https://domena/auth/facebook/callback`) |
| `MEDIDESK_ENCRYPTION_KEY` | Klucz Fernet do szyfrowania tokenów FB |
| `MEDIDESK_FB_SESSION_SECRET` | Secret do podpisywania cookies sesji |
| `MEDIDESK_ADMIN_PASSWORD` | Hasło admina |
| `MEDIDESK_ADMIN_EMAIL` | Email konta administratora |
| `MEDIDESK_DATA_DIR` | Ścieżka do danych na Renderze (`/data`) |

### Zmienne opcjonalne

| Zmienna | Domyślna | Opis |
|---|---|---|
| `MEDIDESK_DEMO_PAGE_ENABLED` | `false` | Włącza stronę demo |
| `MEDIDESK_CORS_ORIGINS` | `""` | Dozwolone originy CORS (przecinkami) |
| `MEDIDESK_HTTP_TIMEOUT` | `15.0` | Timeout HTTP w sekundach |
| `MEDIDESK_MAKE_WEBHOOK_SEND_EMAIL` | `""` | Webhook Make.com do alertów |
| `MEDIDESK_TOKEN_EXPIRY_WARN_DAYS` | `14` | Alert X dni przed wygaśnięciem tokenu |

### Metadane aplikacji

Centralne ustawienia UI (wstrzykiwane do szablonów przez Jinja2):

| Zmienna | Domyślna | Gdzie widoczna |
|---|---|---|
| `MEDIDESK_APP_NAME` | `Integracja Leadów do Medidesk` | `<title>`, sidebar, landing |
| `MEDIDESK_APP_VERSION` | `2.0.0` | `<meta name="version">` |
| `MEDIDESK_APP_AUTHOR` | `Aga - Marketing` | `<meta name="author">` |
| `MEDIDESK_APP_ICON_PATH` | `/static/icon.jpg` | favicon, sidebar, landing |

## Uruchamianie lokalne

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

## Testy

```bash
pytest
```

## Dokumenty powiązane

- [DEPLOYMENT.md](DEPLOYMENT.md) — Wdrożenie na Render, znane problemy
- [CHANGELOG.md](CHANGELOG.md) — Historia zmian i wersji
