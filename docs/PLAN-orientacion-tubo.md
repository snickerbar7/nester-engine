# Plan — orientación angular de piezas de tubo (caras 1–4)

**Estado:** §A y §B (motor + solver) HECHOS, 2026-08-30 — **y CORREGIDOS el
mismo día**: la fórmula de §2 sumaba las lengüetas declaradas de cada pieza
como si fuera un dato medido; no lo era, y se reemplazó por
`StockSpec.shared_face_penalty_mm`, explícito y con default 0. §C (extractor
OpenCASCADE), §D (declaración conversacional persistida), §E (web/IA) y §F
(diseño) siguen sin empezar — fuera de alcance de esta ronda a propósito.
(Cuarta corrección de este documento — las tres primeras se equivocaron, la
historia está en el git log de este archivo.)
**Qué es:** el taller decide cómo queda girada cada pieza dentro del tubo, y el
motor calcula la separación que esa decisión realmente exige — sin inventar
ningún milímetro que nadie midió.

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

## 2 · Qué cambia el anidado, y por qué (corregido — dos veces)

**La separación entre dos piezas vecinas depende de cómo estén giradas las dos.**
Si las lengüetas de A y las de B caen en las **mismas caras**, el motor las
etiqueta `caras compartidas`; en caras **distintas**, `entrelazadas`. Eso es un
**estado**, observable desde lo que el taller declaró. **Cuánto vale en
milímetros que compartan cara NO lo es** — nadie lo ha medido — y ésa es la
corrección de esta sección.

Entonces el largo consumido deja de ser `largo + kerf` y pasa a ser
`largo + separación(extremo de ésta, principio de la siguiente)`:

```
separación(A→B) = kerf
                + StockSpec.shared_face_penalty_mm   (si comparten cara; si no, 0)
                + extra_gap pedido por el usuario
```

Sin caras compartidas el término del medio es 0 y las piezas se entrelazan.

**Qué NO es `shared_face_penalty_mm` — la corrección.** La primera versión de
esta fórmula no tenía ese término: sumaba `saliente_A + saliente_B`, el
`protrusion_mm` que cada pieza ya trae declarado. Parecía razonable — el motor
"ya sabía" cuánto sobresale cada lengüeta — pero nadie había verificado que la
separación física real que necesitan dos lengüetas del mismo lado sea
exactamente esa suma. Por separado, una ronda de diseño (para la pantalla del
visor 3D) estimó **12.0 mm** para la misma situación física, y su propio texto
la marcó como hipótesis, no como medición. Dos números inventados que ni
siquiera concuerdan entre sí es la señal de que ninguno estaba fundamentado, y
el peor lugar para un número no fundamentado es un plan de corte que el taller
va a ejecutar con la sierra encendida.

**La corrección:** `shared_face_penalty_mm` es una **constante explícita y
configurable, con default 0** — un allowance de máquina, vive en `StockSpec`
junto a `kerf`/`front_trim`/`back_trim`, no en la pieza, porque si el costo es
real viene de la sierra o de la mordaza, no de la geometría. En 0 (el default),
`separación(A→B) = kerf` **siempre**, sin importar qué orientación o
`end_features` traigan las piezas — el motor no infiere nada de los
`protrusion_mm` declarados. El taller lo sube (`--shared-face-penalty MM` /
`shared_face_penalty_mm` en `/v1`) sólo cuando alguien mide el número real.

**La etiqueta y el cargo son dos cosas separadas.** Un hueco puede seguir
etiquetado `caras compartidas` (`Placement.gap_reason`) con cargo cero — el
taller ve el estado, no un número inventado. Lo que el hueco **cobró** en mm
vive aparte, en `Placement.gap_terms` (`kerf` siempre, más
`shared_face_penalty`/`extra` sólo cuando de verdad sumaron), y es lo que
`gap_story_es()` convierte en texto: `"ranura de corte (kerf) 3.0 mm · extra
del taller +20.0 mm"`. Con el penalty en 0, un hueco `caras compartidas` no
imprime línea — igual que un hueco de puro kerf, silencioso.

Dos consecuencias:
- **Girar PUEDE cambiar el anidado** (a través de la separación) — pero sólo
  una vez que el taller fijó un `shared_face_penalty_mm` real o pidió
  `extra_gap_mm`. En el default, girar cambia la etiqueta y el dibujo, nunca el
  largo consumido. *(Las tres versiones anteriores de este plan se
  equivocaron: primero dijeron que girar nunca cambia el anidado; luego, que
  siempre lo cambia por una cifra que el motor mismo inventaba.)*
- El solver deja de sumar una constante entre piezas. FFD sigue sirviendo —
  coloca igual, sólo que consultando la separación del **par**— pero el orden
  ahora afecta el total, así que FFD deja de ser óptimo. **Válido primero,
  óptimo después.**

**Correctitud vs optimización — sólo lo primero es obligatorio:**
1. **Obligatorio.** Dadas las orientaciones que el taller eligió Y el
   `shared_face_penalty_mm` que el taller fijó (default 0), calcular la
   separación correcta — nunca una que el motor adivinó.
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

### B · Separación dependiente del par *(solver)* — HECHO 2026-08-30, CORREGIDO el mismo día
- [x] Función `clearance(prev, next, kerf, shared_face_penalty_mm=0.0)` en
      `nester/tube/packing.py` con la fórmula de §2 (kerf incluido), más
      `_rotated_faces` para aplicar `orientation_deg` a las caras declaradas
      antes de comparar. `shared_face_penalty_mm` es el parámetro corregido:
      viene de `StockSpec`, no de sumar `protrusion_mm` de las piezas — ver la
      entrada RESUELTO en §6.
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
- [x] El plan reporta la separación por hueco y **por qué**, no un número
      mudo — y desde la corrección, separa dos cosas antes mezcladas:
      `Placement.gap_reason` (constantes `GAP_*` en `nester/tube/model.py`) es
      el ESTADO del hueco (`entrelazadas`/`caras compartidas`/`extra`), se
      conserva aunque no cobre nada; `Placement.gap_terms` es el DESGLOSE de lo
      que de verdad cobró (`kerf` siempre, `shared_face_penalty`/`extra` sólo
      si sumaron mm reales). `gap_story_es()` ahora renderiza `gap_terms`, no
      `gap_reason` — con el penalty en 0 un hueco `caras compartidas` no
      imprime línea, silencioso igual que un kerf plano. Surfaceado en
      `<job>_corte.json` (`cuts[].gap_before`/`gap_reason`/`gap_story`) y en la
      columna de descripción de la hoja "Lista de cortes" del PDF.
- [x] **Corrección (mismo día):** `StockSpec.shared_face_penalty_mm` (default
      0, valida `>= 0`) reemplaza la suma de `protrusion_mm` que la fórmula
      original usaba. CLI `--shared-face-penalty MM`; `/v1`
      `NestRequest.shared_face_penalty_mm` (aditivo, default 0, ausente del
      contrato congelado de Harriet — `tests/test_service_contract.py` verde).
      Invariante reforzado: con el penalty en 0, el anidado es idéntico al de
      antes de E26 **aunque las piezas SÍ traigan `end_features`/
      `orientation_deg`** — pinneado en
      `tests/test_tube_orientation.py::
      test_zero_change_with_features_and_rotation_when_penalty_is_default` y,
      end-to-end, diffeando `--json` sobre `samples/` con `--orient`/
      `--end-feature` declarados en un archivo: sólo cambia el
      `orientation_deg` de ese corte, ninguna barra ni posición se mueve.

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
- **RESUELTO — la fórmula de §2 sumaba una hipótesis no medida (`saliente_A +
  saliente_B`).** Corregido el mismo día: el término se volvió
  `StockSpec.shared_face_penalty_mm`, explícito, con default 0 — el motor ya
  no deriva ningún número de los `protrusion_mm` declarados. Ver §2. Sigue
  pendiente que alguien MIDA el valor real de una colisión de lengüetas; hasta
  entonces el default (0, sin cargo) es la respuesta honesta, no un placeholder
  a corregir en silencio.
- **`solid_nest.py` NORMALIZA la rotación hoy** (`CLAUDE.md:283`) — hay que
  respetar la elegida. Es lo único que este trabajo le quita al comportamiento
  actual.
- **Girar puede subir el conteo de tramos — pero SÓLO si el taller fijó
  `shared_face_penalty_mm` o `extra_gap_mm`.** En el default (0) girar nunca
  cambia el conteo, sólo la etiqueta y el dibujo — ver el invariante reforzado
  en §2 y `tests/test_tube_orientation.py::
  test_zero_change_with_features_and_rotation_when_penalty_is_default`. Cuando
  sí sube, se reporta medido y sin esconderlo, misma doctrina que la regla de
  decline de retazos (E24).
