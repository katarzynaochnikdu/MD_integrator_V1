# Wdrożenie — Medidesk Integrator

## Platforma: Render.com

- **Plan**: Free tier
- **Region**: Frankfurt
- **Auto-deploy**: Tak (z `main` branch)
- **URL**: https://md-integrator-v1.onrender.com

### Komendy

```yaml
buildCommand: pip install -r requirements.txt
startCommand: uvicorn app.main:app --host 0.0.0.0 --port $PORT
healthCheckPath: /
```

### Persistent Disk

Na planie Free brak persistent disk. Baza SQLite (`medidesk.db`) jest odtwarzana przy każdym deployu. Na planie płatnym ustaw `MEDIDESK_DATA_DIR=/data` i zamontuj dysk pod `/data`.

---

## Znane problemy i obejścia

### ⚠ Python 3.14 na Render (kwiecień 2026)

**Problem**: Render ignoruje `PYTHON_VERSION: "3.12.0"` z `render.yaml` i instaluje Python 3.14. To powoduje crash Jinja2 `LRUCache` (`unhashable type: 'dict'`).

**Obejście zastosowane w kodzie**:
```python
# app/main.py
_jinja_env = Environment(
    loader=FileSystemLoader(...),
    cache_size=0,  # Wyłącza LRUCache — Python 3.14 compat
)
```

**Zalecenie**: Wymuś wersję Pythona w panelu Render (Settings → Environment → Python Version) zamiast polegać na `render.yaml`.

### ⚠ Starlette TemplateResponse — zmiana sygnatury

**Problem**: Starlette >=0.28 zmienił sygnaturę `TemplateResponse`. Stara: `TemplateResponse(name, context)`. Nowa: `TemplateResponse(request, name, context)` lub keyword args.

**Obejście zastosowane w kodzie**:
```python
try:
    return templates.TemplateResponse(request=request, name=name, context=context)
except TypeError:
    return templates.TemplateResponse(name, context)
```

### Free tier — cold starts

Render Free tier usypia serwer po ~15 min nieaktywności. Pierwszy request po uśpieniu trwa 30-60 sekund. Rozwiązanie: przejście na płatny plan ($7/mies) lub dodanie zewnętrznego pinga (np. UptimeRobot).

---

## Zmienne środowiskowe na Render

Ustaw w: Dashboard → md-integrator-v1 → Environment → Environment Variables.

Obowiązkowe:
- `MEDIDESK_FB_APP_ID`
- `MEDIDESK_FB_APP_SECRET`
- `MEDIDESK_FB_REDIRECT_URI` = `https://md-integrator-v1.onrender.com/auth/facebook/callback`
- `MEDIDESK_ENCRYPTION_KEY` (wygeneruj: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)
- `MEDIDESK_FB_SESSION_SECRET`
- `MEDIDESK_ADMIN_PASSWORD`
- `MEDIDESK_ADMIN_EMAIL`

---

## Tagi i wersjonowanie

| Tag | Opis |
|---|---|
| `przed_refactoringiem` | Ostatni commit przed refaktoryzacją na Jinja2 |
