"""
Rodamientos Peraza — Sistema de Inventario
Backend: FastAPI + SQLite + Respaldos automáticos diarios
"""

import sqlite3
import shutil
import schedule
import threading
import time
import os
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional

# ============================================================
# CONFIGURACIÓN
# ============================================================
BASE_DIR   = Path(__file__).parent
DB_PATH    = BASE_DIR / "inventario.db"
BACKUP_DIR = BASE_DIR / "backups"
BACKUP_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Rodamientos Peraza — Inventario API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# BASE DE DATOS
# ============================================================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

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
    codigo:    str
    referencia: Optional[str] = ""
    categoria: str
    marca:     str
    cantidad:  float = 0
    costo:     float = 0
    precio1:   float = 0
    precio2:   float = 0
    stock_min: int = 1
    proveedor: Optional[str] = ""

class Movimiento(BaseModel):
    producto_id: int
    tipo:        str
    cantidad:    float
    nota:        Optional[str] = ""

class MovimientoCodigo(BaseModel):
    codigo:    str
    categoria: Optional[str] = ""
    marca:     Optional[str] = ""
    tipo:      str
    cantidad:  float
    nota:      Optional[str] = ""

# ============================================================
# RESPALDO AUTOMÁTICO
# ============================================================
def hacer_respaldo():
    fecha = datetime.now().strftime("%Y-%m-%d")
    archivo = BACKUP_DIR / f"respaldo_{fecha}.db"
    try:
        shutil.copy2(DB_PATH, archivo)
        tamanio = archivo.stat().st_size
        # Registrar en BD
        conn = get_db()
        conn.execute(
            "INSERT INTO respaldos (archivo, tamanio) VALUES (?, ?)",
            (str(archivo.name), tamanio)
        )
        conn.commit()
        conn.close()
        # Limpiar respaldos de más de 30 días
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
    print("⏰ Scheduler de respaldos iniciado (diario a medianoche)")

# ============================================================
# RUTAS — PRODUCTOS
# ============================================================
@app.get("/api/productos")
def listar_productos(categoria: str = "", estado: str = "", buscar: str = ""):
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

    # Filtrar por estado en Python
    if estado:
        def get_estado(p):
            if p["cantidad"] == 0: return "Sin Stock"
            if p["cantidad"] <= p["stock_min"]: return "Por Reponer"
            return "Activo"
        productos = [p for p in productos if get_estado(p) == estado]

    # Agregar estado calculado
    for p in productos:
        if p["cantidad"] == 0:       p["estado"] = "Sin Stock"
        elif p["cantidad"] <= p["stock_min"]: p["estado"] = "Por Reponer"
        else:                         p["estado"] = "Activo"
        p["valor_inventario"] = round(p["costo"] * p["cantidad"], 2)
        p["margen"] = round((p["precio1"] - p["costo"]) / p["precio1"], 4) if p["precio1"] > 0 else 0

    return productos

@app.get("/api/productos/{producto_id}")
def obtener_producto(producto_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM productos WHERE id = ?", (producto_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    return dict(row)

@app.post("/api/productos")
def crear_producto(p: Producto):
    conn = get_db()
    try:
        cursor = conn.execute("""
            INSERT INTO productos (codigo, referencia, categoria, marca, cantidad,
                                   costo, precio1, precio2, stock_min, proveedor, fecha_act)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, date('now'))
        """, (p.codigo, p.referencia, p.categoria, p.marca, p.cantidad,
              p.costo, p.precio1, p.precio2, p.stock_min, p.proveedor))
        conn.commit()
        nuevo_id = cursor.lastrowid
        conn.close()
        return {"ok": True, "id": nuevo_id, "mensaje": "Producto creado"}
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Ya existe un producto con ese código, categoría y marca")

@app.put("/api/productos/{producto_id}")
def actualizar_producto(producto_id: int, p: Producto):
    conn = get_db()
    conn.execute("""
        UPDATE productos SET codigo=?, referencia=?, categoria=?, marca=?,
               costo=?, precio1=?, precio2=?, stock_min=?, proveedor=?, fecha_act=date('now')
        WHERE id=?
    """, (p.codigo, p.referencia, p.categoria, p.marca,
          p.costo, p.precio1, p.precio2, p.stock_min, p.proveedor, producto_id))
    conn.commit()
    conn.close()
    return {"ok": True, "mensaje": "Producto actualizado"}

@app.delete("/api/productos/{producto_id}")
def eliminar_producto(producto_id: int):
    conn = get_db()
    conn.execute("UPDATE productos SET activo = 0 WHERE id = ?", (producto_id,))
    conn.commit()
    conn.close()
    return {"ok": True, "mensaje": "Producto eliminado"}

@app.get("/api/categorias")
def listar_categorias():
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT categoria FROM productos WHERE activo=1 ORDER BY categoria"
    ).fetchall()
    conn.close()
    return [r["categoria"] for r in rows]

@app.get("/api/marcas")
def listar_marcas():
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT marca FROM productos WHERE activo=1 ORDER BY marca"
    ).fetchall()
    conn.close()
    return [r["marca"] for r in rows]

# ============================================================
# RUTAS — MOVIMIENTOS
# ============================================================
@app.post("/api/movimientos")
def registrar_movimiento(m: Movimiento):
    conn = get_db()
    prod = conn.execute(
        "SELECT * FROM productos WHERE id = ? AND activo = 1", (m.producto_id,)
    ).fetchone()
    if not prod:
        conn.close()
        raise HTTPException(status_code=404, detail="Producto no encontrado")

    stock_antes = prod["cantidad"]
    if m.tipo == "salida" and m.cantidad > stock_antes:
        conn.close()
        raise HTTPException(status_code=400,
            detail=f"Stock insuficiente. Disponible: {stock_antes} unidades")

    stock_desp = stock_antes + m.cantidad if m.tipo == "entrada" else stock_antes - m.cantidad

    conn.execute("""
        INSERT INTO movimientos (producto_id, tipo, cantidad, stock_antes, stock_desp, nota)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (m.producto_id, m.tipo, m.cantidad, stock_antes, stock_desp, m.nota))

    conn.execute(
        "UPDATE productos SET cantidad = ?, fecha_act = date('now') WHERE id = ?",
        (stock_desp, m.producto_id)
    )
    conn.commit()
    conn.close()
    return {"ok": True, "stock_anterior": stock_antes, "stock_nuevo": stock_desp}

@app.post("/api/movimientos/por-codigo")
def registrar_movimiento_codigo(m: MovimientoCodigo):
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
    mov = Movimiento(producto_id=prod["id"], tipo=m.tipo, cantidad=m.cantidad, nota=m.nota)
    return registrar_movimiento(mov)

@app.get("/api/movimientos")
def listar_movimientos(limite: int = 100, producto_id: int = 0):
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
def dashboard():
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
@app.get("/api/respaldos")
def listar_respaldos():
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
def generar_respaldo_manual():
    hacer_respaldo()
    return {"ok": True, "mensaje": "Respaldo generado exitosamente"}

@app.get("/api/respaldos/descargar/{archivo}")
def descargar_respaldo(archivo: str):
    ruta = BACKUP_DIR / archivo
    if not ruta.exists():
        raise HTTPException(status_code=404, detail="Respaldo no encontrado")
    return FileResponse(ruta, filename=archivo, media_type="application/octet-stream")

# ============================================================
# STARTUP
# ============================================================
@app.on_event("startup")
async def startup():
    init_db()
    hacer_respaldo()       # Respaldo al arrancar
    iniciar_scheduler()    # Scheduler diario
    print("🚀 Servidor Rodamientos Peraza iniciado")

@app.get("/api/status")
def status():
    return {
        "ok": True,
        "sistema": "Rodamientos Peraza — Inventario",
        "version": "1.0",
        "fecha": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

# Servir el frontend
FRONTEND_DIR = BASE_DIR / "frontend"
if not FRONTEND_DIR.exists():
    FRONTEND_DIR = BASE_DIR.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")