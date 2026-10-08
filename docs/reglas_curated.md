# Reglas de la capa curated

La capa curated convierte cada foto de raw (el zip del INEGI tal como llegó) en Parquet
tipado y normalizado. No cambia ningún valor de unidades: solo fija tipos, recorta
espacios, normaliza el nombre del modelo en columnas nuevas y suma las filas que la
fuente repite. Código: [`src/inegi_market/curate.py`](../src/inegi_market/curate.py).

```bash
python -m inegi_market.cli curate                                   # todas las fotos de raw
python -m inegi_market.cli curate --product venta --publication-date 2026-10-07
```

## Salida

```text
curated/<producto>/publication_date=AAAA-MM-DD/anio=AAAA/part-0.parquet   hechos, un archivo por año
curated/catalogos/<dim>/publication_date=AAAA-MM-DD/part-0.parquet        dimensiones de los catálogos
reports/curate/<producto>/publication_date=AAAA-MM-DD.json                 cifras de la curación
```

Bajo `curated/<producto>/` solo hay Parquet de hechos, para que el warehouse lo lea con
un comodín. `publication_date` y `anio` también van como columnas dentro de cada archivo.

## Columnas

| Producto | Columnas |
| --- | --- |
| venta | `publication_date`, `anio`, `mes`, `periodo`, `marca`, `modelo`, `modelo_origen`, `modelo_clave`, `tipo`, `segmento`, `origen`, `id_pais_origen`, `unidades`, `es_correccion`, `filas_origen`, `estatus` |
| produccion | igual que venta, sin `origen` ni `id_pais_origen` |
| exportacion | igual que produccion, más `id_pais_destino` |
| hibrido | `publication_date`, `anio`, `mes`, `periodo`, `id_entidad`, `veh_electricos`, `veh_hibridos_plugin`, `veh_hibridos`, `es_correccion`, `filas_origen`, `estatus` |

Tipos: `publication_date` y `periodo` son fechas; `anio`, `mes`, `id_pais_*`, las
unidades y `filas_origen` son enteros; `es_correccion` es booleano; el resto es texto.

Correspondencia con la fuente: `ANIO` → `anio`, `ID_MES` → `mes`, `MARCA` → `marca`,
`MODELO` → `modelo_origen`, `TIPO` → `tipo`, `SEGMENTO` → `segmento`, `ORIGEN` → `origen`,
`ID_PAIS_ORIGEN` → `id_pais_origen`, `ID_PAIS_DESTINO` → `id_pais_destino`,
`ID_ENTIDAD` → `id_entidad`, `UNI_VEH` → `unidades`, `VEH_ELECTR` → `veh_electricos`,
`VEH_HIBRIDAS_PLUGIN` → `veh_hibridos_plugin`, `VEH_HIBRIDAS` → `veh_hibridos`,
`ESTATUS` → `estatus`. `PROD_EST` y `COBERTURA` no pasan: son textos constantes.

## Reglas

1. **Lectura.** Los CSV de cada año se leen como texto, sin convertir "NA" ni vacíos en
   nulos. Si las columnas no son exactamente las esperadas, la curación se detiene.
2. **Espacios.** Se recortan al inicio y al final de cada valor. En ventas, 181 nombres
   de modelo traían espacios al final. Los espacios internos se conservan.
3. **Tipos.** `anio`, `mes`, códigos de país y unidades pasan a enteros. Un valor que no
   sea entero, o un mes fuera de 1–12, detiene la curación. `periodo` es el primer día
   del mes. `id_entidad` se queda como texto para conservar el cero a la izquierda
   (`"01"`).
4. **Modelo.**
   - `modelo_origen`: el nombre tal como viene, recortado.
   - `modelo`: sin el guion final que la fuente agrega a algunos modelos
     (`"Serie 2-"` → `"Serie 2"`). No se cambian mayúsculas.
   - `modelo_clave`: `modelo` en minúsculas y con espacios internos simples, para unir
     (`"Mdx"` → `"mdx"`).
5. **Duplicados por clave natural: se suman** (decisión del autor, 2026-10-07).
   `filas_origen` dice cuántas filas de la fuente forman cada fila curated.

   | Producto | Clave natural |
   | --- | --- |
   | venta | `anio`, `mes`, `marca`, `modelo_origen`, `tipo`, `segmento`, `origen`, `id_pais_origen` |
   | produccion | `anio`, `mes`, `marca`, `modelo_origen`, `tipo`, `segmento` |
   | exportacion | `anio`, `mes`, `marca`, `modelo_origen`, `tipo`, `segmento`, `id_pais_destino` |
   | hibrido | `anio`, `mes`, `id_entidad` |

   La clave usa `modelo_origen` y no `modelo`: en la fuente conviven, en el mismo mes y
   con el mismo origen, nombres con y sin guion (p. ej., "Tacoma" y "Tacoma-" en
   2012-10). Con el modelo normalizado se fusionarían 113 claves de ventas y 5 de
   producción que la fuente reporta por separado. Las comparaciones a un grano más
   grueso (diff, warehouse) agregan por `modelo_clave` después.

   Si dos filas repetidas traen estatus distintos, la curación se detiene (no pasa en
   las fotos reales).
6. **Negativos.** Se conservan. `es_correccion` es verdadero si alguna fila de la fuente
   del grupo era negativa.
7. **Ceros.** Las filas con cero unidades se conservan.
8. **Meses.** No se inventan filas: un mes sin datos en la fuente no aparece.
9. **Estatus y foto.** Cada fila conserva `estatus` y `publication_date`.

## Catálogos

| Catálogo del zip | Dimensión | Columnas |
| --- | --- | --- |
| `tc_mes.csv` | `dim_mes` | `mes` (entero), `nombre_mes` |
| `tc_pais_origen.csv` | `dim_pais_origen` | `id_pais` (entero), `pais` |
| `tc_pais_destino.csv` | `dim_pais_destino` | `id_pais` (entero), `pais` |
| `tc_entidad_federativa.csv` | `dim_entidad` | `id_entidad` (texto), `entidad` |

Cada dimensión lleva también `publication_date`. El catálogo de entidades trae 33
entradas: las 32 entidades más `99` "No especificado".

## Reporte

`reports/curate/<producto>/publication_date=D.json` registra: sha256 del zip de raw,
filas leídas y escritas, grupos duplicados y filas sumadas, unidades en grupos
duplicados, total de unidades, filas con corrección negativa, filas con cero
unidades, primer y último periodo, filas con modelo terminado en guion y archivos
escritos.

## Verificación con las dos fotos reales (2026-10-08)

| Producto | Foto 2026-09-09 | Foto 2026-10-07 |
| --- | --- | --- |
| venta | 106,709 → 106,572 filas; 137 grupos sumados; 87 correcciones | 107,376 → 107,239; 137 grupos; 87 correcciones |
| produccion | 17,841 → 17,750; 91 grupos; 7 correcciones | 17,955 → 17,863; 92 grupos; 7 correcciones |
| exportacion | 190,678 filas; sin duplicados; 145 correcciones | 192,382; sin duplicados; 146 correcciones |
| hibrido | 4,186 filas; sin duplicados | 4,219 filas; sin duplicados |

- Los totales mensuales de curated cuadran con la suma directa de los CSV en las 8
  fotos, en todos los meses.
- Coinciden con las cifras oficiales anotadas: septiembre de 2026 (ventas 129,274;
  producción 301,803; exportación 277,369), agosto de 2026 (ventas 129,362 en la foto
  de septiembre y 129,361 en la de octubre) y mayo de 2025 (ventas 121,110; producción
  356,188; exportación 301,112).
- Los duplicados sumados pesan 0.110 % de las unidades de ventas y 0.233 % de las de
  producción.
- Volver a curar las 8 fotos tarda 7.6 s y deja los 162 archivos Parquet idénticos
  byte a byte.
