"""
Script de migración: carga el inventario Excel a SQLite
Uso: python cargar_inventario.py ruta/al/archivo.xlsx
"""
import sqlite3
import sys
from pathlib import Path

def cargar_desde_excel(ruta_excel: str, db_path: str = "inventario.db"):
    try:
        import openpyxl
    except ImportError:
        print("Instalando openpyxl...")
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "openpyxl"], check=True)
        import openpyxl

    print(f"📂 Leyendo: {ruta_excel}")
    wb = openpyxl.load_workbook(ruta_excel, read_only=True, data_only=True)
    ws = wb['Inventario']
    filas = list(ws.iter_rows(values_only=True))
    headers = filas[0]
    print(f"   Columnas: {headers}")
    print(f"   Total filas: {len(filas)-1}")

    conn = sqlite3.connect(db_path)

    # Detectar índices de columnas
    h = [str(c).strip() if c else '' for c in headers]
    idx = {
        'codigo':   h.index('Codigo')                if 'Codigo'                in h else None,
        'ref':      h.index('Referencia / Aplicacion') if 'Referencia / Aplicacion' in h else None,
        'cat':      h.index('Categoria')              if 'Categoria'              in h else None,
        'marca':    h.index('Marca')                  if 'Marca'                  in h else None,
        'cantidad': h.index('Cantidad')               if 'Cantidad'               in h else None,
        'costo':    h.index('Costo')                  if 'Costo'                  in h else None,
        'precio1':  h.index('Precio 1')               if 'Precio 1'               in h else None,
        'precio2':  h.index('Precio 2')               if 'Precio 2'               in h else None,
        'stock_min':h.index('Stock Min')              if 'Stock Min'              in h else None,
        'proveedor':h.index('Proveedor')              if 'Proveedor'              in h else None,
    }

    insertados = 0
    omitidos   = 0

    for fila in filas[1:]:
        if not any(c is not None for c in fila):
            continue

        def val(key, default=''):
            i = idx.get(key)
            if i is None: return default
            v = fila[i]
            return v if v is not None else default

        codigo    = str(val('codigo', '')).strip()
        categoria = str(val('cat', '')).strip()
        marca     = str(val('marca', '')).strip()

        if not codigo or not categoria or not marca:
            omitidos += 1
            continue

        try:
            conn.execute("""
                INSERT OR IGNORE INTO productos
                (codigo, referencia, categoria, marca, cantidad, costo,
                 precio1, precio2, stock_min, proveedor)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                codigo,
                str(val('ref', '')).strip(),
                categoria,
                marca,
                float(val('cantidad', 0) or 0),
                float(val('costo', 0) or 0),
                float(val('precio1', 0) or 0),
                float(val('precio2', 0) or 0),
                int(val('stock_min', 1) or 1),
                str(val('proveedor', '')).strip(),
            ))
            insertados += 1
        except Exception as e:
            print(f"  ⚠️ Error en fila {codigo}: {e}")
            omitidos += 1

    conn.commit()
    conn.close()
    print(f"\n✅ Carga completada:")
    print(f"   Insertados: {insertados}")
    print(f"   Omitidos:   {omitidos}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python cargar_inventario.py archivo.xlsx")
        sys.exit(1)
    cargar_desde_excel(sys.argv[1])
