import os
import json
import uuid
import time
import hmac
import hashlib
import secrets
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, Body, HTTPException, UploadFile, File, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from filelock import FileLock

app = FastAPI(title="Enterprise Link System")

# ============================================================
# CONFIG AUTH
# ============================================================
# 🔐 Secreto compartido con tu "otro sistema"
#   - En Linux: export ELS_AUTH_SECRET="algo-largo-y-random"
#   - En systemd: Environment="ELS_AUTH_SECRET=..."
AUTH_SECRET = os.getenv("ELS_AUTH_SECRET", "").strip() or "CHANGE_ME_SUPER_SECRET"
#http://localhost:8000/auth/login?user=admin&key=e9e83b5649a18a43f3c68d566caaf4bf51b2f1245526a7af66f527c83725def8&next=/manager
SESSION_COOKIE = "els_session"
SESSION_TTL_SECONDS = int(os.getenv("ELS_SESSION_TTL_SECONDS", "43200"))  # 12h
LOGIN_WINDOW_SECONDS = int(os.getenv("ELS_LOGIN_WINDOW_SECONDS", "300"))  # 5 min
COOKIE_SECURE = os.getenv("ELS_COOKIE_SECURE", "0") == "1"  # pon 1 si usas https

# ============================================================
# Paths
# ============================================================
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
DB_FILE = BASE_DIR / "db.json"
LOCK_FILE = BASE_DIR / "db.json.lock"

UPLOADS_DIR = BASE_DIR / "uploads"
ASSETS_DIR = UPLOADS_DIR / "assets"
ASSETS_DIR.mkdir(parents=True, exist_ok=True)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Static (assets)
app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")


# ============================================================
# Helpers / Defaults
# ============================================================
def default_theme() -> Dict[str, Any]:
    return {
        "text_color": "#ffffff",
        "brand": {
            "title": "",
            "subtitle": "Enlaces rápidos • estilo corporativo",
            "logo_type": "emoji",   # emoji | asset | image_url
            "logo_value": "✨",
        },
        "bg_type": "color",
        "bg_value": "#0f172a",
        "bg_overlay_opacity": 0.35,
        "bg_overlay_color": "#000000",
        "bg_blur_px": 0,
        "bg_zoom": 1.0,
        "card_color": "rgba(255,255,255,0.10)",
        "card_border": "rgba(255,255,255,0.12)",
        "card_radius": 16,
        "btn_bg": "rgba(255,255,255,0.12)",
        "btn_text": "#ffffff",
        "btn_border": "rgba(255,255,255,0.14)",
        "btn_radius": 16,
    }


def normalize_screen(screen: Dict[str, Any]) -> Dict[str, Any]:
    screen = dict(screen or {})
    screen.setdefault("id", uuid.uuid4().hex)
    screen.setdefault("folder_id", "default")
    screen.setdefault("slug", uuid.uuid4().hex[:6])
    screen.setdefault("title", "Sin Título")

    theme = screen.get("theme") or {}

    # Compat: si antes guardabas brand en screen.brand, lo migramos a theme.brand
    if isinstance(screen.get("brand"), dict) and "brand" not in theme:
        theme["brand"] = screen["brand"]

    merged = default_theme()
    merged.update(theme)

    # Normalizar BRAND
    brand = merged.get("brand") or {}
    if not isinstance(brand, dict):
        brand = {}

    brand.setdefault("title", screen.get("title") or "Sin Título")
    brand.setdefault("subtitle", "Enlaces rápidos • estilo corporativo")
    brand.setdefault("logo_type", "emoji")
    brand.setdefault("logo_value", "✨")

    brand["logo_type"] = str(brand.get("logo_type") or "emoji")
    brand["logo_value"] = str(brand.get("logo_value") or "✨")

    merged["brand"] = brand
    screen["theme"] = merged

    # Conveniencia para tu landing
    screen["brand"] = merged["brand"]

    links = screen.get("links") or []
    norm_links = []
    for l in links:
        l = dict(l or {})
        l.setdefault("label", "Link")
        l.setdefault("url", "#")
        l.setdefault("icon_type", "emoji")
        l.setdefault("icon_value", "🔗")
        l.setdefault("style", {})
        norm_links.append(l)
    screen["links"] = norm_links

    return screen


# ============================================================
# AUTH (hash como clave + sesiones locales)
# ============================================================
def _hmac_sig(user: str, ts: int) -> str:
    """
    Firma recomendada (replay-resistant): HMAC_SHA256(secret, f"{user}:{ts}")
    """
    msg = f"{user}:{ts}".encode("utf-8")
    return hmac.new(AUTH_SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _simple_key(user: str) -> str:
    """
    Key simple (replayable): SHA256(f"{user}:{secret}")
    """
    msg = f"{user}:{AUTH_SECRET}".encode("utf-8")
    return hashlib.sha256(msg).hexdigest()


def _sid_to_dbkey(sid: str) -> str:
    """
    Guardamos sesiones por un hash del sid + secret (no guardamos el sid en claro).
    """
    return hashlib.sha256(f"{sid}:{AUTH_SECRET}".encode("utf-8")).hexdigest()


def _cleanup_sessions(db: Dict[str, Any]) -> None:
    now = int(time.time())
    sessions = db.get("sessions") or {}
    if not isinstance(sessions, dict):
        sessions = {}
    dead = []
    for k, v in sessions.items():
        try:
            exp = int((v or {}).get("expires_at", 0))
        except Exception:
            exp = 0
        if exp <= now:
            dead.append(k)
    for k in dead:
        sessions.pop(k, None)
    db["sessions"] = sessions


def _create_session(db: Dict[str, Any], user: str) -> str:
    sid = secrets.token_urlsafe(32)
    key = _sid_to_dbkey(sid)
    now = int(time.time())
    db.setdefault("sessions", {})
    db["sessions"][key] = {
        "user": user,
        "created_at": now,
        "expires_at": now + SESSION_TTL_SECONDS,
    }
    return sid


def _get_session_user(db: Dict[str, Any], sid: str) -> Optional[str]:
    if not sid:
        return None
    key = _sid_to_dbkey(sid)
    sess = (db.get("sessions") or {}).get(key)
    if not sess:
        return None
    now = int(time.time())
    if int(sess.get("expires_at", 0)) <= now:
        # expirada
        (db.get("sessions") or {}).pop(key, None)
        return None
    return str(sess.get("user") or "")


def _delete_session(db: Dict[str, Any], sid: str) -> None:
    if not sid:
        return
    key = _sid_to_dbkey(sid)
    (db.get("sessions") or {}).pop(key, None)


def require_login(request: Request) -> str:
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        raise HTTPException(status_code=401, detail="No autenticado")
    db = load_db()
    user = _get_session_user(db, sid)
    if not user:
        save_db(db)  # por si limpió expirada
        raise HTTPException(status_code=401, detail="Sesión inválida o expirada")
    # refrescar TTL opcional (sliding)
    # db["sessions"][_sid_to_dbkey(sid)]["expires_at"] = int(time.time()) + SESSION_TTL_SECONDS
    # save_db(db)
    return user


# ============================================================
# DATABASE ENGINE
# ============================================================
def load_db() -> Dict[str, Any]:
    with FileLock(str(LOCK_FILE)):
        if not DB_FILE.exists():
            initial_data = {
                "folders": [{"id": "default", "name": "General"}],
                "screens": [],
                "assets": [],
                "sessions": {}
            }
            DB_FILE.write_text(json.dumps(initial_data, ensure_ascii=False, indent=2), encoding="utf-8")
            return initial_data

        try:
            data = json.loads(DB_FILE.read_text(encoding="utf-8") or "{}")
        except Exception:
            data = {}

        data.setdefault("folders", [{"id": "default", "name": "General"}])
        data.setdefault("screens", [])
        data.setdefault("assets", [])
        data.setdefault("sessions", {})

        # Normalize screens theme
        data["screens"] = [normalize_screen(s) for s in (data.get("screens") or [])]

        # Limpieza de sesiones expiradas
        _cleanup_sessions(data)

        return data


def save_db(data: Dict[str, Any]) -> None:
    with FileLock(str(LOCK_FILE)):
        DB_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ============================================================
# AUTH ROUTES (login/logout por query)
# ============================================================






FAIL_REDIRECT_URL = "https://gestion.salchimonster.com/tools/link-tree"

def _wants_json(request: Request, json_mode: int) -> bool:
    if json_mode == 1:
        return True
    accept = (request.headers.get("accept") or "").lower()
    return "application/json" in accept

def _fail(request: Request, json_mode: int, status_code: int, detail: str):
    # Si el cliente quiere JSON -> devolvemos JSON error
    if _wants_json(request, json_mode):
        return JSONResponse(
            {"status": "error", "detail": detail},
            status_code=status_code
        )

    # Si es browser -> redirigimos al link-tree
    resp = RedirectResponse(url=FAIL_REDIRECT_URL, status_code=302)
    # opcional: limpiar cookie si existía
    resp.delete_cookie(key=SESSION_COOKIE, path="/")
    return resp


@app.get("/auth/login")
async def auth_login(
    request: Request,
    user: str = Query(..., description="Usuario que firma el otro sistema"),
    # MODO recomendado
    ts: Optional[int] = Query(None, description="Unix timestamp"),
    sig: Optional[str] = Query(None, description="HMAC SHA256 hex"),
    # MODO simple
    key: Optional[str] = Query(None, description="SHA256(user:secret) hex"),
    # salida
    next: str = Query("/manager", description="Redirect después de login"),
    json_mode: int = Query(0, description="1 para responder JSON en vez de redirect"),
):
    user = (user or "").strip()
    if not user:
        return _fail(request, json_mode, 400, "user requerido")

    ok = False

    # ✅ Modo recomendado: user + ts + sig (válido por ventana de tiempo)
    if ts is not None and sig:
        now = int(time.time())
        if abs(now - int(ts)) > LOGIN_WINDOW_SECONDS:
            return _fail(request, json_mode, 401, "Login expirado (ts fuera de ventana)")
        expected = _hmac_sig(user, int(ts))
        ok = hmac.compare_digest(expected, str(sig).strip().lower())

    # ✅ Modo simple: user + key (replayable)
    elif key:
        expected = _simple_key(user)
        ok = hmac.compare_digest(expected, str(key).strip().lower())

    else:
        return _fail(request, json_mode, 400, "Debe enviar (ts + sig) o (key)")

    if not ok:
        return _fail(request, json_mode, 401, "Credenciales inválidas")

    # ✅ Login OK -> crear sesión y redirigir / responder json
    db = load_db()
    sid = _create_session(db, user)
    save_db(db)

    if _wants_json(request, json_mode):
        resp = JSONResponse({"status": "ok", "user": user})
    else:
        resp = RedirectResponse(url=next or "/manager", status_code=302)

    resp.set_cookie(
        key=SESSION_COOKIE,
        value=sid,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=SESSION_TTL_SECONDS,
        path="/",
    )
    return resp



 

LOGOUT_REDIRECT_URL = "https://gestion.salchimonster.com/tools/link-tree"

def _wants_json(request: Request, json_mode: int) -> bool:
    if json_mode == 1:
        return True
    accept = (request.headers.get("accept") or "").lower()
    return "application/json" in accept



@app.get("/auth/logout")
async def auth_logout(
    request: Request,
    next: str = Query("/", description="Redirect después de logout"),
    json_mode: int = Query(0, description="1 para responder JSON en vez de redirect"),
):
    sid = request.cookies.get(SESSION_COOKIE)

    if sid:
        db = load_db()
        _delete_session(db, sid)
        save_db(db)

    # ✅ Respuesta JSON si lo piden
    if _wants_json(request, json_mode):
        resp = JSONResponse({"status": "ok"})
    else:
        # ✅ Si no es JSON: redirigir al link-tree (o usa next si tú quieres)
        # Opción A (lo que pediste): siempre link-tree
        resp = RedirectResponse(url=LOGOUT_REDIRECT_URL, status_code=302)

        # Opción B (si quieres respetar next cuando venga):
        # resp = RedirectResponse(url=(next or LOGOUT_REDIRECT_URL), status_code=302)

    resp.delete_cookie(key=SESSION_COOKIE, path="/")
    return resp






@app.get("/auth/me")
async def auth_me(user: str = Depends(require_login)):
    return JSONResponse({"user": user})


# ============================================================
# Views (PROTEGIDO manager)
# ============================================================
@app.get("/manager", response_class=HTMLResponse)
async def manager(request: Request, user: str = Depends(require_login)):
    db = load_db()
    return templates.TemplateResponse("manager.html", {
        "request": request,
        "folders": db["folders"],
        "screens": db["screens"],
        "assets": db["assets"],
        "user": user,
    })


# Public
@app.get("/s/{slug}", response_class=HTMLResponse)
async def view_screen(request: Request, slug: str):
    db = load_db()
    screen = next((s for s in db["screens"] if s["slug"] == slug), None)
    if not screen:
        return HTMLResponse("<h1>404 - Pantalla no encontrada</h1>", status_code=404)

    screen = normalize_screen(screen)
    return templates.TemplateResponse("landing.html", {"request": request, "screen": screen})


# ============================================================
# API: Preview (PROTEGIDO)
# ============================================================
@app.post("/api/screens/preview", response_class=HTMLResponse)
async def preview_screen(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    user: str = Depends(require_login),
):
    screen = normalize_screen(payload)
    return templates.TemplateResponse("landing.html", {"request": request, "screen": screen})


# ============================================================
# API: Folders (PROTEGIDO)
# ============================================================
@app.post("/api/folders/create")
async def create_folder(data: dict = Body(...), user: str = Depends(require_login)):
    db = load_db()
    new_folder = {"id": uuid.uuid4().hex[:8], "name": data.get("name", "Nueva Carpeta")}
    db["folders"].append(new_folder)
    save_db(db)
    return JSONResponse(new_folder)


@app.post("/api/folders/delete")
async def delete_folder(data: dict = Body(...), user: str = Depends(require_login)):
    folder_id = data.get("id")
    if folder_id == "default":
        return JSONResponse({"error": "No se puede borrar la carpeta General"}, status_code=400)

    db = load_db()
    db["folders"] = [f for f in db["folders"] if f["id"] != folder_id]
    for s in db["screens"]:
        if s.get("folder_id") == folder_id:
            s["folder_id"] = "default"

    save_db(db)
    return JSONResponse({"status": "ok"})


# ============================================================
# API: Assets (PROTEGIDO)
# ============================================================
MAX_ASSET_MB = 10
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp"}


@app.get("/api/assets/list")
async def list_assets(user: str = Depends(require_login)):
    db = load_db()
    return JSONResponse({"assets": db["assets"]})


@app.post("/api/assets/upload")
async def upload_asset(file: UploadFile = File(...), user: str = Depends(require_login)):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Solo se permiten imágenes")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"Extensión no permitida: {ext}")

    content = await file.read()
    if len(content) > MAX_ASSET_MB * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"Imagen demasiado grande (máx {MAX_ASSET_MB}MB)")

    asset_id = uuid.uuid4().hex[:10]
    safe_name = f"{asset_id}{ext}"
    out_path = ASSETS_DIR / safe_name
    out_path.write_bytes(content)

    url = f"/assets/{safe_name}"
    asset = {"id": asset_id, "name": file.filename, "url": url, "kind": "image"}

    db = load_db()
    db["assets"].append(asset)
    save_db(db)

    return JSONResponse(asset)


# ============================================================
# API: Screens (PROTEGIDO)
# ============================================================
@app.post("/api/screens/save")
async def save_screen(data: dict = Body(...), user: str = Depends(require_login)):
    db = load_db()

    slug = (data.get("slug", "") or "").strip().lower().replace(" ", "-")
    if not slug:
        slug = uuid.uuid4().hex[:6]

    new_entry = normalize_screen({
        "id": data.get("id") or uuid.uuid4().hex,
        "folder_id": data.get("folder_id", "default"),
        "slug": slug,
        "title": data.get("title", "Sin Título"),
        "theme": data.get("theme", {}),
        "links": data.get("links", []),
    })

    # Update or Create
    for i, s in enumerate(db["screens"]):
        if s["id"] == new_entry["id"]:
            db["screens"][i] = new_entry
            save_db(db)
            return JSONResponse({"status": "ok", "slug": new_entry["slug"]})

    # Slug unique
    if any(s["slug"] == new_entry["slug"] for s in db["screens"]):
        new_entry["slug"] = f"{new_entry['slug']}-{uuid.uuid4().hex[:4]}"

    db["screens"].append(new_entry)
    save_db(db)
    return JSONResponse({"status": "ok", "slug": new_entry["slug"]})


@app.post("/api/screens/delete")
async def delete_screen(data: dict = Body(...), user: str = Depends(require_login)):
    screen_id = data.get("id")
    db = load_db()
    db["screens"] = [s for s in db["screens"] if s["id"] != screen_id]
    save_db(db)
    return JSONResponse({"status": "deleted"})


# ============================================================
# RUN
# ============================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
