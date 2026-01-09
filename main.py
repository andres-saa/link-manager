import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request, Body, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from filelock import FileLock

app = FastAPI(title="Enterprise Link System")

# Rutas
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


# -----------------------------
# Helpers / Defaults
# -----------------------------
def default_theme() -> Dict[str, Any]:
    return {
        "text_color": "#ffffff",

        # ✅ BRAND (logo + titulo + subtitulo)
        "brand": {
            "title": "",  # si viene vacío, normalize_screen lo rellena con screen.title
            "subtitle": "Enlaces rápidos • estilo corporativo",
            "logo_type": "emoji",   # emoji | asset | image_url
            "logo_value": "✨",     # emoji o url (/assets/... o https://...)
        },

        # Background
        "bg_type": "color",
        "bg_value": "#0f172a",
        "bg_overlay_opacity": 0.35,
        "bg_overlay_color": "#000000",
        "bg_blur_px": 0,
        "bg_zoom": 1.0,

        # Cards / Buttons default
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

    # ✅ Compat: si antes guardabas brand en screen.brand, lo migramos a theme.brand
    if isinstance(screen.get("brand"), dict) and "brand" not in theme:
        theme["brand"] = screen["brand"]

    merged = default_theme()
    merged.update(theme)

    # ✅ Normalizar BRAND
    brand = merged.get("brand") or {}
    if not isinstance(brand, dict):
        brand = {}

    brand.setdefault("title", screen.get("title") or "Sin Título")
    brand.setdefault("subtitle", "Enlaces rápidos • estilo corporativo")
    brand.setdefault("logo_type", "emoji")
    brand.setdefault("logo_value", "✨")

    # Limpieza básica
    brand["logo_type"] = str(brand.get("logo_type") or "emoji")
    brand["logo_value"] = str(brand.get("logo_value") or "✨")

    merged["brand"] = brand
    screen["theme"] = merged

    # ✅ Conveniencia para tu landing: puedes usar screen.brand directo
    screen["brand"] = merged["brand"]

    # Links normalize
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



# -----------------------------
# DATABASE ENGINE
# -----------------------------
def load_db() -> Dict[str, Any]:
    with FileLock(str(LOCK_FILE)):
        if not DB_FILE.exists():
            initial_data = {
                "folders": [{"id": "default", "name": "General"}],
                "screens": [],
                "assets": []
            }
            DB_FILE.write_text(json.dumps(initial_data, ensure_ascii=False, indent=2), encoding="utf-8")
            return initial_data

        try:
            data = json.loads(DB_FILE.read_text(encoding="utf-8") or "{}")
        except Exception:
            data = {}

        # Compat / migrations
        data.setdefault("folders", [{"id": "default", "name": "General"}])
        data.setdefault("screens", [])
        data.setdefault("assets", [])

        # Normalize screens theme
        data["screens"] = [normalize_screen(s) for s in (data.get("screens") or [])]

        return data


def save_db(data: Dict[str, Any]) -> None:
    with FileLock(str(LOCK_FILE)):
        DB_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# -----------------------------
# Views
# -----------------------------
@app.get("/manager", response_class=HTMLResponse)
async def manager(request: Request):
    db = load_db()
    return templates.TemplateResponse("manager.html", {
        "request": request,
        "folders": db["folders"],
        "screens": db["screens"],
        "assets": db["assets"],
    })


@app.get("/s/{slug}", response_class=HTMLResponse)
async def view_screen(request: Request, slug: str):
    db = load_db()
    screen = next((s for s in db["screens"] if s["slug"] == slug), None)
    if not screen:
        return HTMLResponse("<h1>404 - Pantalla no encontrada</h1>", status_code=404)

    screen = normalize_screen(screen)
    return templates.TemplateResponse("landing.html", {"request": request, "screen": screen})


# -----------------------------
# API: Preview (sin guardar)
# -----------------------------
@app.post("/api/screens/preview", response_class=HTMLResponse)
async def preview_screen(request: Request, payload: Dict[str, Any] = Body(...)):
    screen = normalize_screen(payload)
    return templates.TemplateResponse("landing.html", {"request": request, "screen": screen})
# -----------------------------
# API: Folders
# -----------------------------
@app.post("/api/folders/create")
async def create_folder(data: dict = Body(...)):
    db = load_db()
    new_folder = {"id": uuid.uuid4().hex[:8], "name": data.get("name", "Nueva Carpeta")}
    db["folders"].append(new_folder)
    save_db(db)
    return JSONResponse(new_folder)


@app.post("/api/folders/delete")
async def delete_folder(data: dict = Body(...)):
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


# -----------------------------
# API: Assets (subir y listar)
# -----------------------------
MAX_ASSET_MB = 10
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp"}

@app.get("/api/assets/list")
async def list_assets():
    db = load_db()
    return JSONResponse({"assets": db["assets"]})


@app.post("/api/assets/upload")
async def upload_asset(file: UploadFile = File(...)):
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






# -----------------------------
# API: Screens (save/delete)
# -----------------------------
@app.post("/api/screens/save")
async def save_screen(data: dict = Body(...)):
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
async def delete_screen(data: dict = Body(...)):
    screen_id = data.get("id")
    db = load_db()
    db["screens"] = [s for s in db["screens"] if s["id"] != screen_id]
    save_db(db)
    return JSONResponse({"status": "deleted"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
