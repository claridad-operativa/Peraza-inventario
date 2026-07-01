"""
Rodamientos Peraza — Sistema de Inventario
Backend: FastAPI + SQLite (sobre Volume de Railway) + Respaldos consistentes + Autenticacion

Variables de entorno que usa (ver pasos de despliegue):
  DATA_DIR       Carpeta persistente donde vive la base y los respaldos (ej: /data). Si no se define, usa la carpeta de la app (solo para pruebas locales).
  SECRET_KEY     Clave para firmar los tokens de sesion. OBLIGATORIA en produccion.
  APP_USER       Usuario de acceso. OBLIGATORIO en produccion.
  APP_PASSWORD   Contraseña de acceso. OBLIGATORIO en produccion.
  TOKEN_HORAS    Horas de validez de la sesion (por defecto 12).
  CORS_ORIGINS   Dominios externos permitidos, separados por coma. Vacio = solo mismo dominio (lo normal aqui).
  TZ             Zona horaria del servidor. Definir TZ=America/Caracas para que las fechas queden en hora de Venezuela.
"""

import sqlite3
import shutil
import schedule
import threading
import time
import os
import re
import hmac
import hashlib
import base64
import json
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, Literal

# ============================================================
# CONFIGURACION
# ============================================================
BASE_DIR = Path(__file__).parent

# La base y los respaldos viven en DATA_DIR (el Volume de Railway).
# Orden de preferencia:
#   1) DATA_DIR (si lo defines tu)
#   2) RAILWAY_VOLUME_MOUNT_PATH (lo pone Railway solo al montar un volumen)
#   3) la carpeta de la app (solo para pruebas locales)
DATA_DIR = Path(
    os.getenv("DATA_DIR")
    or os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    or str(BASE_DIR)
)
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH    = DATA_DIR / "inventario.db"
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

# Snapshot inicial incluido en la imagen (commiteado en git). Solo se usa
# para sembrar el volumen la PRIMERA vez que esta vacio.
SEED_DB = BASE_DIR / "inventario.db"

# --- Autenticacion ---
SECRET_KEY   = os.getenv("SECRET_KEY", "")
APP_USER     = os.getenv("APP_USER", "")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
TOKEN_HORAS  = int(os.getenv("TOKEN_HORAS", "12"))
AUTH_LISTA   = bool(SECRET_KEY and APP_USER and APP_PASSWORD)

# --- CORS ---
_cors = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]

app = FastAPI(title="Rodamientos Peraza — Inventario API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors,        # vacio = solo mismo dominio (no se necesita CORS en el setup normal)
    allow_credentials=False,    # usamos tokens Bearer, no cookies
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# AUTENTICACION (token firmado con HMAC, sin dependencias extra)
# ============================================================
def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")

def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def crear_token(user: str) -> str:
    payload = {"user": user, "exp": int(time.time()) + TOKEN_HORAS * 3600}
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64e(hmac.new(SECRET_KEY.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"

def verificar_token(token: str):
    try:
        body, sig = token.split(".", 1)
        esperado = _b64e(hmac.new(SECRET_KEY.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, esperado):
            return None
        payload = json.loads(_b64d(body))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload.get("user")
    except Exception:
        return None

def requiere_auth(authorization: str = Header(default="")):
    """Protege los endpoints de datos. Falla CERRADO si no hay auth configurada."""
    if not AUTH_LISTA:
        raise HTTPException(
            status_code=503,
            detail="Autenticacion no configurada. Defina SECRET_KEY, APP_USER y APP_PASSWORD en el servidor."
        )
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="No autorizado")
    user = verificar_token(authorization[7:])
    if not user:
        raise HTTPException(status_code=401, detail="Sesion invalida o expirada")
    return user

class Login(BaseModel):
    usuario:  str
    password: str

@app.post("/api/login")
def login(c: Login):
    if not AUTH_LISTA:
        raise HTTPException(
            status_code=503,
            detail="Autenticacion no configurada. Defina SECRET_KEY, APP_USER y APP_PASSWORD en el servidor."
        )
    ok_user = hmac.compare_digest(c.usuario.strip().lower(), APP_USER.strip().lower())
    ok_pass = hmac.compare_digest(c.password, APP_PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")
    return {"token": crear_token(APP_USER), "expira_horas": TOKEN_HORAS}

# ============================================================
# BASE DE DATOS
# ============================================================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def _contar_productos(path: Path) -> int:
    """Cuantos productos hay en una base. -1 si la tabla no existe / no se puede leer."""
    try:
        c = sqlite3.connect(path)
        n = c.execute("SELECT COUNT(*) FROM productos").fetchone()[0]
        c.close()
        return int(n)
    except Exception:
        return -1

def sembrar_si_necesario():
    """
    Copia el snapshot incluido en la imagen al volumen SOLO si el volumen
    todavia no tiene datos. Nunca sobreescribe una base con productos.
    """
    if SEED_DB.resolve() == DB_PATH.resolve():
        return  # pruebas locales: misma carpeta, no hay nada que sembrar
    if not SEED_DB.exists():
        return
    necesita = (not DB_PATH.exists()) or (_contar_productos(DB_PATH) <= 0)
    semilla_tiene_datos = _contar_productos(SEED_DB) > 0
    if necesita and semilla_tiene_datos:
        shutil.copy2(SEED_DB, DB_PATH)
        print(f"📦 Base sembrada desde la imagen al volumen ({_contar_productos(DB_PATH)} productos)")

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS productos (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo      TEXT NOT NULL,
            referencia  TEXT DEFAULT '',
            categoria   TEXT NOT NULL,
            marca       TEXT NOT NULL,
            cantidad    REAL DEFAULT 0,
            costo       REAL DEFAULT 0,
            precio1     REAL DEFAULT 0,
            precio2     REAL DEFAULT 0,
            stock_min   INTEGER DEFAULT 1,
            proveedor   TEXT DEFAULT '',
            fecha_act   TEXT DEFAULT (date('now')),
            activo      INTEGER DEFAULT 1,
            UNIQUE(codigo, categoria, marca)
        );

        CREATE TABLE IF NOT EXISTS movimientos (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            producto_id INTEGER NOT NULL,
            tipo        TEXT NOT NULL CHECK(tipo IN ('entrada','salida')),
            cantidad    REAL NOT NULL,
            stock_antes REAL NOT NULL,
            stock_desp  REAL NOT NULL,
            nota        TEXT DEFAULT '',
            fecha       TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY(producto_id) REFERENCES productos(id)
        );

        CREATE TABLE IF NOT EXISTS respaldos (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            archivo  TEXT NOT NULL,
            fecha    TEXT DEFAULT (datetime('now','localtime')),
            tamanio  INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_prod_codigo   ON productos(codigo);
        CREATE INDEX IF NOT EXISTS idx_prod_cat      ON productos(categoria);
        CREATE INDEX IF NOT EXISTS idx_mov_fecha     ON movimientos(fecha);
        CREATE INDEX IF NOT EXISTS idx_mov_producto  ON movimientos(producto_id);
    """)
    conn.commit()
    conn.close()
    print("✅ Base de datos inicializada")

# ============================================================
# MODELOS
# ============================================================
class Producto(BaseModel):
    codigo:     str
    referencia: Optional[str] = ""
    categoria:  str
    marca:      str
    cantidad:   float = 0
    costo:      float = 0
    precio1:    float = 0
    precio2:    float = 0
    stock_min:  int = 1
    proveedor:  Optional[str] = ""

class Movimiento(BaseModel):
    producto_id: int
    tipo:        Literal["entrada", "salida"]
    cantidad:    float
    nota:        Optional[str] = ""

class MovimientoCodigo(BaseModel):
    codigo:    str
    categoria: Optional[str] = ""
    marca:     Optional[str] = ""
    tipo:      Literal["entrada", "salida"]
    cantidad:  float
    nota:      Optional[str] = ""

# ============================================================
# RESPALDO (consistente, seguro para WAL)
# ============================================================
def hacer_respaldo():
    fecha = datetime.now().strftime("%Y-%m-%d")
    archivo = BACKUP_DIR / f"respaldo_{fecha}.db"
    try:
        # API de respaldo online de SQLite: consistente aun con WAL activo.
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(archivo)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
        tamanio = archivo.stat().st_size

        conn = get_db()
        conn.execute(
            "INSERT INTO respaldos (archivo, tamanio) VALUES (?, ?)",
            (str(archivo.name), tamanio)
        )
        conn.commit()
        conn.close()

        limpiar_respaldos_antiguos()
        print(f"✅ Respaldo creado: {archivo.name} ({tamanio/1024:.1f} KB)")
    except Exception as e:
        print(f"❌ Error en respaldo: {e}")

def limpiar_respaldos_antiguos():
    archivos = sorted(BACKUP_DIR.glob("respaldo_*.db"))
    if len(archivos) > 30:
        for archivo in archivos[:-30]:
            archivo.unlink()
            print(f"🗑 Respaldo eliminado: {archivo.name}")

def iniciar_scheduler():
    schedule.every().day.at("00:00").do(hacer_respaldo)
    def run():
        while True:
            schedule.run_pending()
            time.sleep(60)
    hilo = threading.Thread(target=run, daemon=True)
    hilo.start()
    print("⏰ Scheduler de respaldos iniciado (diario a medianoche, hora del servidor)")

# ============================================================
# RUTAS — PRODUCTOS
# ============================================================
@app.get("/api/productos")
def listar_productos(categoria: str = "", estado: str = "", buscar: str = "",
                     _: str = Depends(requiere_auth)):
    conn = get_db()
    query = "SELECT * FROM productos WHERE activo = 1"
    params = []
    if categoria:
        query += " AND categoria = ?"
        params.append(categoria)
    if buscar:
        query += " AND (codigo LIKE ? OR referencia LIKE ? OR marca LIKE ?)"
        params += [f"%{buscar}%", f"%{buscar}%", f"%{buscar}%"]
    query += " ORDER BY categoria, codigo"
    rows = conn.execute(query, params).fetchall()
    conn.close()

    productos = [dict(r) for r in rows]

    def get_estado(p):
        if p["cantidad"] == 0:                 return "Sin Stock"
        if p["cantidad"] <= p["stock_min"]:    return "Por Reponer"
        return "Activo"

    if estado:
        productos = [p for p in productos if get_estado(p) == estado]

    for p in productos:
        p["estado"] = get_estado(p)
        p["valor_inventario"] = round(p["costo"] * p["cantidad"], 2)
        p["margen"] = round((p["precio1"] - p["costo"]) / p["precio1"], 4) if p["precio1"] > 0 else 0

    return productos

@app.get("/api/productos/{producto_id}")
def obtener_producto(producto_id: int, _: str = Depends(requiere_auth)):
    conn = get_db()
    row = conn.execute("SELECT * FROM productos WHERE id = ?", (producto_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    return dict(row)

@app.post("/api/productos")
def crear_producto(p: Producto, _: str = Depends(requiere_auth)):
    conn = get_db()
    try:
        cursor = conn.execute("""
            INSERT INTO productos (codigo, referencia, categoria, marca, cantidad,
                                   costo, precio1, precio2, stock_min, proveedor, fecha_act)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, date('now'))
        """, (p.codigo, p.referencia, p.categoria, p.marca, p.cantidad,
              p.costo, p.precio1, p.precio2, p.stock_min, p.proveedor))
        nuevo_id = cursor.lastrowid
        conn.execute("""
            INSERT INTO movimientos (producto_id, tipo, cantidad, stock_antes, stock_desp, nota)
            VALUES (?, 'entrada', ?, 0, ?, 'Producto creado')
        """, (nuevo_id, p.cantidad, p.cantidad))
        conn.commit()
        conn.close()
        return {"ok": True, "id": nuevo_id, "mensaje": "Producto creado"}
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Ya existe un producto con ese codigo, categoria y marca")

@app.put("/api/productos/{producto_id}")
def actualizar_producto(producto_id: int, p: Producto, _: str = Depends(requiere_auth)):
    conn = get_db()
    prod_actual = conn.execute("SELECT cantidad FROM productos WHERE id=?", (producto_id,)).fetchone()
    if not prod_actual:
        conn.close()
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    cant_antes = prod_actual["cantidad"]

    # IMPORTANTE: ahora 'cantidad' SI se actualiza (antes no, y el inventario quedaba desincronizado).
    conn.execute("""
        UPDATE productos SET codigo=?, referencia=?, categoria=?, marca=?,
               cantidad=?, costo=?, precio1=?, precio2=?, stock_min=?, proveedor=?, fecha_act=date('now')
        WHERE id=?
    """, (p.codigo, p.referencia, p.categoria, p.marca,
          p.cantidad, p.costo, p.precio1, p.precio2, p.stock_min, p.proveedor, producto_id))

    if p.cantidad != cant_antes:
        tipo = 'entrada' if p.cantidad > cant_antes else 'salida'
        diff = abs(p.cantidad - cant_antes)
        conn.execute("""
            INSERT INTO movimientos (producto_id, tipo, cantidad, stock_antes, stock_desp, nota)
            VALUES (?, ?, ?, ?, ?, 'Ajuste manual desde edicion')
        """, (producto_id, tipo, diff, cant_antes, p.cantidad))
    else:
        conn.execute("""
            INSERT INTO movimientos (producto_id, tipo, cantidad, stock_antes, stock_desp, nota)
            VALUES (?, 'entrada', 0, ?, ?, 'Informacion del producto actualizada')
        """, (producto_id, cant_antes, cant_antes))
    conn.commit()
    conn.close()
    return {"ok": True, "mensaje": "Producto actualizado"}

@app.delete("/api/productos/{producto_id}")
def eliminar_producto(producto_id: int, _: str = Depends(requiere_auth)):
    conn = get_db()
    prod = conn.execute("SELECT cantidad FROM productos WHERE id=?", (producto_id,)).fetchone()
    cant = prod["cantidad"] if prod else 0
    conn.execute("UPDATE productos SET activo = 0 WHERE id = ?", (producto_id,))
    conn.execute("""
        INSERT INTO movimientos (producto_id, tipo, cantidad, stock_antes, stock_desp, nota)
        VALUES (?, 'salida', ?, ?, 0, 'Producto eliminado del inventario')
    """, (producto_id, cant, cant))
    conn.commit()
    conn.close()
    return {"ok": True, "mensaje": "Producto eliminado"}

@app.get("/api/categorias")
def listar_categorias(_: str = Depends(requiere_auth)):
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT categoria FROM productos WHERE activo=1 ORDER BY categoria"
    ).fetchall()
    conn.close()
    return [r["categoria"] for r in rows]

@app.get("/api/marcas")
def listar_marcas(_: str = Depends(requiere_auth)):
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT marca FROM productos WHERE activo=1 ORDER BY marca"
    ).fetchall()
    conn.close()
    return [r["marca"] for r in rows]

# ============================================================
# RUTAS — MOVIMIENTOS
# ============================================================
def _aplicar_movimiento(producto_id: int, tipo: str, cantidad: float, nota: str):
    conn = get_db()
    prod = conn.execute(
        "SELECT * FROM productos WHERE id = ? AND activo = 1", (producto_id,)
    ).fetchone()
    if not prod:
        conn.close()
        raise HTTPException(status_code=404, detail="Producto no encontrado")

    stock_antes = prod["cantidad"]
    if tipo == "salida" and cantidad > stock_antes:
        conn.close()
        raise HTTPException(status_code=400,
            detail=f"Stock insuficiente. Disponible: {stock_antes} unidades")

    stock_desp = stock_antes + cantidad if tipo == "entrada" else stock_antes - cantidad

    conn.execute("""
        INSERT INTO movimientos (producto_id, tipo, cantidad, stock_antes, stock_desp, nota)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (producto_id, tipo, cantidad, stock_antes, stock_desp, nota))
    conn.execute(
        "UPDATE productos SET cantidad = ?, fecha_act = date('now') WHERE id = ?",
        (stock_desp, producto_id)
    )
    conn.commit()
    conn.close()
    return {"ok": True, "stock_anterior": stock_antes, "stock_nuevo": stock_desp}

@app.post("/api/movimientos")
def registrar_movimiento(m: Movimiento, _: str = Depends(requiere_auth)):
    return _aplicar_movimiento(m.producto_id, m.tipo, m.cantidad, m.nota)

@app.post("/api/movimientos/por-codigo")
def registrar_movimiento_codigo(m: MovimientoCodigo, _: str = Depends(requiere_auth)):
    conn = get_db()
    query = "SELECT * FROM productos WHERE codigo = ? AND activo = 1"
    params = [m.codigo]
    if m.categoria:
        query += " AND categoria = ?"
        params.append(m.categoria)
    if m.marca:
        query += " AND marca = ?"
        params.append(m.marca)
    prod = conn.execute(query, params).fetchone()
    conn.close()
    if not prod:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    return _aplicar_movimiento(prod["id"], m.tipo, m.cantidad, m.nota)

@app.get("/api/movimientos")
def listar_movimientos(limite: int = 100, producto_id: int = 0,
                       _: str = Depends(requiere_auth)):
    conn = get_db()
    if producto_id:
        rows = conn.execute("""
            SELECT m.*, p.codigo, p.categoria, p.marca
            FROM movimientos m JOIN productos p ON m.producto_id = p.id
            WHERE m.producto_id = ?
            ORDER BY m.fecha DESC LIMIT ?
        """, (producto_id, limite)).fetchall()
    else:
        rows = conn.execute("""
            SELECT m.*, p.codigo, p.categoria, p.marca
            FROM movimientos m JOIN productos p ON m.producto_id = p.id
            ORDER BY m.fecha DESC LIMIT ?
        """, (limite,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ============================================================
# RUTAS — DASHBOARD
# ============================================================
@app.get("/api/dashboard")
def dashboard(_: str = Depends(requiere_auth)):
    conn = get_db()

    totales = conn.execute("""
        SELECT
            COUNT(*)                                    AS referencias,
            COALESCE(SUM(cantidad), 0)                  AS unidades,
            COALESCE(SUM(costo * cantidad), 0)          AS valor,
            SUM(CASE WHEN cantidad = 0 THEN 1 ELSE 0 END)              AS sin_stock,
            SUM(CASE WHEN cantidad > 0 AND cantidad <= stock_min
                     THEN 1 ELSE 0 END)                 AS por_reponer
        FROM productos WHERE activo = 1
    """).fetchone()

    alertas = conn.execute("""
        SELECT id, codigo, categoria, marca, cantidad, stock_min
        FROM productos
        WHERE activo = 1 AND cantidad <= stock_min
        ORDER BY cantidad ASC
        LIMIT 20
    """).fetchall()

    categorias = conn.execute("""
        SELECT categoria,
               COUNT(*)                         AS productos,
               COALESCE(SUM(cantidad), 0)       AS unidades,
               COALESCE(SUM(costo*cantidad), 0) AS valor
        FROM productos WHERE activo = 1
        GROUP BY categoria ORDER BY valor DESC
    """).fetchall()

    conn.close()

    def est(cantidad, stock_min):
        if cantidad == 0: return "Sin Stock"
        if cantidad <= stock_min: return "Por Reponer"
        return "Activo"

    return {
        "referencias": totales["referencias"],
        "unidades":    round(totales["unidades"], 0),
        "valor":       round(totales["valor"], 2),
        "sin_stock":   totales["sin_stock"],
        "por_reponer": totales["por_reponer"],
        "alertas":     [{**dict(a), "estado": est(a["cantidad"], a["stock_min"])} for a in alertas],
        "categorias":  [dict(c) for c in categorias],
    }

# ============================================================
# RUTAS — RESPALDOS
# ============================================================
SAFE_BACKUP = re.compile(r"^respaldo_\d{4}-\d{2}-\d{2}\.db$")

@app.get("/api/respaldos")
def listar_respaldos(_: str = Depends(requiere_auth)):
    archivos = sorted(BACKUP_DIR.glob("respaldo_*.db"), reverse=True)
    resultado = []
    for f in archivos[:30]:
        resultado.append({
            "archivo": f.name,
            "fecha": f.stem.replace("respaldo_", ""),
            "tamanio_kb": round(f.stat().st_size / 1024, 1)
        })
    return resultado

@app.post("/api/respaldos/generar")
def generar_respaldo_manual(_: str = Depends(requiere_auth)):
    hacer_respaldo()
    return {"ok": True, "mensaje": "Respaldo generado exitosamente"}

@app.get("/api/respaldos/descargar/{archivo}")
def descargar_respaldo(archivo: str, _: str = Depends(requiere_auth)):
    # Solo se permite el patron exacto de nombre de respaldo (evita path traversal).
    if not SAFE_BACKUP.match(archivo):
        raise HTTPException(status_code=400, detail="Nombre de respaldo invalido")
    ruta = (BACKUP_DIR / archivo).resolve()
    if BACKUP_DIR.resolve() not in ruta.parents or not ruta.exists():
        raise HTTPException(status_code=404, detail="Respaldo no encontrado")
    return FileResponse(ruta, filename=archivo, media_type="application/octet-stream")

# ============================================================
# STATUS (abierto, para diagnostico y healthcheck)
# ============================================================
@app.get("/api/status")
def status():
    return {
        "ok": True,
        "sistema": "Rodamientos Peraza — Inventario",
        "version": "1.1",
        "auth_configurada": AUTH_LISTA,
        "usando_volumen": str(DATA_DIR) != str(BASE_DIR),
        "data_dir": str(DATA_DIR),
        "fecha": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

# ============================================================
# STARTUP
# ============================================================
@app.on_event("startup")
async def startup():
    sembrar_si_necesario()   # Copia el snapshot al volumen si esta vacio
    init_db()                # Crea tablas/indices si faltan
    hacer_respaldo()         # Respaldo al arrancar
    iniciar_scheduler()      # Scheduler diario
    if not AUTH_LISTA:
        print("⚠️  ATENCION: autenticacion NO configurada. La API rechazara las peticiones (503) hasta definir SECRET_KEY, APP_USER y APP_PASSWORD.")
    print("🚀 Servidor Rodamientos Peraza iniciado")

# Servir el frontend (debe ir al final, despues de las rutas /api)
FRONTEND_DIR = BASE_DIR / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
