# Plan — orientación angular de piezas de tubo (caras 1–4)

**Estado:** §A y §B (motor + solver) HECHOS, 2026-08-30. §C (extractor
OpenCASCADE), §D (declaración conversacional persistida), §E (web/IA) y §F
(diseño) siguen sin empezar — fuera de alcance de esta ronda a propósito.
(Tercera corrección de este documento — las dos primeras se equivocaron, la
historia está en el git log de este archivo.)
**Qué es:** el taller decide cómo queda girada cada pieza dentro del tubo, y el
motor calcula la separación que esa decisión realmente exige.

---

## 1 · El requisito

El taller controla la orientación angular de **cada pieza** — pieza por pieza o
diciéndoselo a Harriet — y puede pedir **separación extra**. Es opcional, y
**forzar una orientación que gasta más tramo es un caso de uso legítimo**, no un
error: si quiere las lengüetas todas en la misma cara porque ahí está la
costura, el plan obedece.

La máquina no importa: si es sierra, el operador suelta la mordaza y gira. No
modelamos sujeción ni particionamos barras por orientación.

---

## 2 · Qué cambia el anidado, y por qué (corregido)

**La separación entre dos piezas vecinas depende de cómo estén giradas las dos.**
Si las lengüetas de A y las de B caen en las **mismas caras**, chocan y el hueco
tiene que crecer para librarlas. Giradas a caras **distintas**, se entrelazan y
el hueco se encoge. Encima va lo que el usuario pida a mano.

Entonces el largo consumido deja de ser `largo + kerf` y pasa a ser
`largo + separación(extremo de ésta, principio de la siguiente)`:

```
separación(A→B) = kerf
                + máx sobre las caras COMPARTIDAS f de (saliente_A[f] + saliente_B[f])
                + extra_gap pedido por el usuario
```

Sin caras compartidas el término del medio es 0 y las piezas se entrelazan.
**Verificar esta fórmula contra una pieza real antes de construir sobre ella.**

Dos consecuencias:
- **Girar SÍ cambia el anidado** (a través de la separación). Marca el trabajo
  sucio y ofrece volver a anidar, igual que cambiar el kerf. *(Las dos versiones
  anteriores de este plan decían lo contrario. Estaban mal.)*
- El solver deja de sumar una constante entre piezas. FFD sigue sirviendo —
  coloca igual, sólo que consultando la separación del **par**— pero el orden
  ahora afecta el total, así que FFD deja de ser óptimo. **Válido primero,
  óptimo después.**

**Correctitud vs optimización — sólo lo primero es obligatorio:**
1. **Obligatorio.** Dadas las orientaciones que el taller eligió, calcular la
   separación correcta. Sin esto un plan con piezas giradas es *físicamente
   falso*: dice que caben donde las lengüetas chocan.
2. **Opcional, después.** Buscar orientaciones que compren menos tramos. Siempre
   sobreescribible por el taller.

---

## 3 · De dónde salen las lengüetas (resuelto)

El patrón "el motor mastica, la IA recibe la comida hecha" **ya existe**: la IA
llama `extract_parts` y recibe JSON; nunca ve bytes de archivo. Para **DXF** ya
es rico (`width_mm`, `height_mm`, `area`, `holes`, y el `contour` real con sus
barrenos). Para **IGES** hoy sólo da `profile`, `qty`, `length_mm` — `read_tube`
toma el eje más largo del encajonado y **tira el resto**. Ese es el hueco: no es
la arquitectura, es que el lector de tubo es un medidor de largos.

Se cierra por **dos vías, en este orden**:

- **(D) El taller lo declara.** Por pieza: saliente en mm y en qué caras, en cada
  extremo. Barato, sirve desde el primer día, y es el **override** cuando el CAD
  viene sucio o el extractor se equivoca.
- **(C) El extractor lo lee del sólido.** OpenCASCADE, **fuera de banda**, igual
  que `--solids` y `--shop-package` hoy. Precisa y sin captura manual.

**(D) primero.** Deja la función usable de inmediato y le da a (C) un oráculo
contra el cual validarse.

---

## 4 · Checklist

### A · Orientación y separación como dato *(motor)* — HECHO 2026-08-30
- [x] `Part.orientation_deg: float = 0.0` (rect: 0/90/180/270; redondo: continuo).
      Normalizado a `[0, 360)` en `__post_init__`; no rechaza valores que no son
      múltiplos de 90.
- [x] `Part.extra_gap_mm: float = 0.0`, `>= 0` (valida, lanza `ValueError` si no).
- [x] `Part.end_features` — por extremo (`"start"`/`"far"`), un
      `EndFeature(protrusion_mm, faces)`. Vacío/ausente = extremo plano, y
      entonces todo se comporta como hoy (propiedades `start_feature` /
      `far_feature` devuelven un `EndFeature()` por defecto).
- [x] CLI `--orient FILE=DEG`, `--extra-gap FILE=MM`,
      `--end-feature FILE=END:PROTRUSION_MM:FACES` (repetibles, como `--sets`;
      `nester/tube/cli.py`: `parse_orient` / `parse_extra_gap` /
      `parse_end_features`, aplicados por nombre de archivo en `_load_parts`).
- [x] `/v1` FileRef: los tres, opcionales y aditivos
      (`service/v1/routes.py::FileRef.orientation_deg/extra_gap_mm/end_features`
      + `EndFeatureRef`), enrutados a través de `InFile` y
      `service/core/engine.py::_orient_map/_extra_gap_map/_end_features_map`.
      `tests/test_service_contract.py` verde — el contrato congelado de
      Harriet (`service/harriet/`) no los lleva; pinneado en
      `tests/test_service_api.py::test_harriet_infile_never_carries_orientation_fields`.

### B · Separación dependiente del par *(solver)* — HECHO 2026-08-30
- [x] Función `clearance(prev, next, kerf)` en `nester/tube/packing.py` con la
      fórmula de §2 (kerf incluido), más `_rotated_faces` para aplicar
      `orientation_deg` a las caras declaradas antes de comparar.
- [x] `nester/tube/packing.py::_pack`/`_append` la consultan por PAR en vez de
      sumar `kerf` fijo; `BarLayout.consumed_length` ahora deriva de la
      posición real de la última pieza colocada, no de una suma uniforme.
- [x] **Invariante:** ninguna barra se desborda con la separación real.
      Property test `tests/test_tube_orientation.py::
      test_no_bar_ever_overflows_with_random_orientation_and_features` — 30
      seeds, reconstruye cada `start`/`end` de forma independiente (nunca
      confía en `consumed_length`/`remnant`, que están bajo prueba) y también
      pinnea conservación de cantidad (cada pieza colocada exactamente una
      vez, o en `unplaceable`).
- [x] **Invariante:** con `end_features` vacío el resultado es **idéntico** al
      de hoy, pieza por pieza. Pinneado dos veces: a nivel de solver
      (`test_zero_change_when_unused`) y end-to-end sobre `samples/` — el
      CLI `--json` de antes y de después son idénticos salvo por las claves
      nuevas y aditivas (`orientation_deg`/`gap_before`/`gap_reason`/
      `gap_story`), que además valen todas su default (`0.0`/`()`/`""`) en
      cada corte. Comparación hecha con `git stash` (código viejo) vs. el
      working tree (código nuevo) sobre los 5 IGES de `samples/`, ver el
      mensaje del commit de esta ronda.
- [x] El plan reporta la separación por hueco y **por qué** (`entrelazadas` /
      `caras compartidas` / `extra del taller`), no un número mudo:
      `Placement.gap_before` + `gap_reason` (constantes `GAP_*` en
      `nester/tube/model.py`), `gap_story_es()` para el texto en español,
      surfaceado en `<job>_corte.json` (`cuts[].gap_story`) y en la columna de
      descripción de la hoja "Lista de cortes" del PDF (silencioso cuando no
      hay historia que contar).

### C · Extractor de extremos *(nuevo, OpenCASCADE, fuera de banda)*
- [ ] Script propio bajo `.venv-cad`, invocado como `solid_nest.py`. **No entra
      al camino del servicio**: OCC no cabe en `.venv` y meterlo en la imagen de
      Render es pesado (tamaño, build, memoria con dos hilos de solver).
- [ ] Método: eje = arista más larga del encajonado; **sección nominal** = la
      sección en la mitad del tubo; luego seccionar en estaciones cerca de cada
      extremo y comparar contra la nominal. Donde falta material, hay saliente:
      se registra su largo y las caras que conserva.
- [ ] Salida: JSON con `end_features` por archivo — la misma forma que (D)
      produce a mano, para que el resto del sistema no sepa cuál lo generó.
- [ ] Degradar, nunca fallar: sin OCC o con geometría rara, devuelve vacío y el
      trabajo sigue con lo declarado.
- [ ] Validar contra los IGES reales de `samples/` y contra una pieza con
      lengüetas de verdad que Marlon consiga. **Sin esa pieza no se cierra.**
- [ ] Tests: la sección nominal es estable; un tubo recto da `end_features`
      vacío (y por B eso significa cero cambio de comportamiento).

### D · Declaración del taller *(la vía barata, primero)*
- [ ] Harriet pregunta y anota: *"¿la P-03 lleva lengüeta? ¿de cuánto y en qué
      caras?"* Se guarda en la pieza y se reusa en trabajos siguientes.
- [ ] Lo declarado **gana** sobre lo extraído, y el plan dice cuál usó.

### E · Web / IA
- [ ] Los tres campos en el tipo de pieza y en el cuerpo que va al motor.
- [ ] Harriet ejecuta por conversación: *"gira la P-03 90 grados"*, *"todas con
      la lengüeta arriba"*, *"20 mm entre la 4 y la 5"*.
- [ ] El **visor 3D** dibuja la pieza girada y el hueco real entre piezas — es
      donde el taller verifica que las lengüetas libran.
- [ ] Girar o cambiar separación → sucio → "volver a anidar".

### F · Diseño *(Claude Design, lo corre Marlon)*
- [ ] Brief: control de rotación por pieza en el visor 3D, **simulador de
      costura** que el usuario coloca donde se la imagina, control de separación.
- [ ] La UI deja forzar una orientación que gasta más material **sin pelear**.

---

## 5 · Fuera de alcance (a propósito)
- **Optimizar la rotación automáticamente.** Después de que §2.1 exista.
- **Que la IA proponga orientación leyendo el ensamble** (qué cara se ve). Haría
  falta el **ensamble** como entrada nueva, no una pieza por archivo.
- **Features de tubo completas** (barrenos, destajes, ingletes en cualquier
  parte). El extractor mira **los extremos**, que es lo que mueve la separación.

## 6 · Riesgos
- **La fórmula de §2 es una hipótesis.** Sale de la descripción de Marlon, no de
  una pieza medida. Validar antes de construir el solver encima.
- **`solid_nest.py` NORMALIZA la rotación hoy** (`CLAUDE.md:283`) — hay que
  respetar la elegida. Es lo único que este trabajo le quita al comportamiento
  actual.
- **Girar puede subir el conteo de tramos.** Se reporta medido y sin esconderlo,
  misma doctrina que la regla de decline de retazos (E24).
