# Brief de diseño — el motor 2D dejó de tener las tres limitaciones

**Para:** una ronda nueva de Claude Design sobre el canvas de Harriet Nester.
**De:** el repo del motor (`nester-engine`), 25 ago 2026.
**Estado:** el motor ya está construido, probado y documentado. Esta ronda diseña
la UI que lo consume. Contrato primero, diseño después — como siempre.

---

## 1. Qué cambió

El `design/README.md` del producto tiene hoy una sección titulada **"Lo que el
motor 2D NO hace todavía (no mostrarlo como funcional)"** con exactamente tres
puntos, y la app los imprime como advertencias:

1. Anidar piezas dentro de los barrenos de otras piezas.
2. Aplicar el inventario de retazos de lámina al solver.
3. Calcular peso/kg en 2D.

**Los tres ya los hace.** Esa sección del README hay que reescribirla y las tres
advertencias tienen que cambiar de naturaleza. Ese es el corazón de la ronda.

La regla que ordena todo lo demás:

> Una advertencia de limitación ahora habla de **este trabajo**, no del motor
> para siempre. Si la opción está apagada, la tarjeta dice qué se dejó sobre la
> mesa y cuál es el interruptor. Si está encendida, la tarjeta deja de ser una
> disculpa y se convierte en una **instrucción para el operador**.

Lo que NO cambia: la IA nunca inventa un número, todo dato que falta se dibuja
como raya (—), el tono es de jefe de taller, y toda cifra sigue viniendo de una
llamada al motor.

---

## 2. Las tres capacidades, con su dato real

### A · Anidado en barrenos (`nest_in_holes`)

El motor mete piezas chicas dentro de los barrenos de las piezas ya colocadas.
Medido en un trabajo real: una lámina pasó de **40.9 % a 74.3 %** de
aprovechamiento.

**Viene apagado por defecto, y esa decisión es de diseño, no de ingeniería.**
Tiene una consecuencia en la máquina: esas piezas salen **dentro del recorte
(el slug)** del barreno. Alguien tiene que levantar ese recorte entero y no
tirarlo con el esqueleto; y si la máquina deja caer los recortes, hay que poner
puente o soporte antes de cortar. Por eso el taller lo tiene que **elegir**.

Datos por colocación:
- `in_hole_of` — índice de la pieza anfitriona en la misma lámina (o `null`).
- `totals.parts_in_holes` — cuántas en todo el trabajo.

Lo que el diseño tiene que resolver:
- Un control en **01 · Parámetros de la lámina** con la consecuencia escrita,
  no un toggle pelón. El taller decide con la consecuencia a la vista.
- En el lienzo y en los mini-mapas: una pieza dentro de un barreno se tiene que
  **leer distinta** de una pieza sobre la lámina. Hoy todas se dibujan igual.
- En el detalle de la hoja: los chips de esas piezas van marcados aparte.
- Un antes/después honesto cuando se enciende: *"Subí la lámina 1 de 40.9 % a
  74.3 % metiendo 46 piezas en los barrenos."*

### B · Retazos de lámina (`extra_sheets` / `min_remnant_mm`)

Dos mitades, y las dos cierran el mismo circuito.

**Entra:** el rack alimenta el trabajo. Cada retazo es una pieza física con
etiqueta, se usa **una sola vez**, y el motor gasta **el más chico que sirva**
antes de mandar a comprar hoja nueva — así los retazos grandes quedan libres
para las piezas grandes. Un retazo donde no cabe nada **no se quema**: se queda
en el rack y el trabajo lo dice.

**Sale:** cada lámina reporta el rectángulo que le queda (`leftover`), ya
descontada la separación, listo para cizallar y dar de alta.

Consecuencia estructural que el diseño no puede ignorar: **las láminas de un
trabajo ya no son todas del mismo tamaño.** Cada una trae su propia medida y su
`source` (`"nueva"` o la etiqueta del retazo). Los mini-mapas y el lienzo hoy
asumen una sola proporción de hoja. Ya no.

Datos:
- `totals.sheets` (abiertas) vs **`totals.sheets_to_buy`** (a comprar) —
  ya no son el mismo número, y el de compra es el que va a la orden.
- `totals.remnants_used` — etiquetas.
- `remnants_unused[]` — `{label, width, height}`, siguen en el rack.
- `sheets[].source`, `sheets[].width`, `sheets[].height`.
- `sheets[].leftover` y `reclaimable[]` — `{sheet, x, y, width, height}`.

Lo que el diseño tiene que resolver:
- Cómo se **eligen** retazos al armar el trabajo. La pantalla **Retazos** ya
  tiene "usar en el trabajo" y filtro Lámina; ahora es una acción real. ¿Se
  eligen desde ahí, desde el bloque 01, o la IA los ofrece en el chat
  ("tengo 2 retazos de esta lámina en el rack, ¿los uso?")? Probablemente las
  tres, y hay que decidir cuál es la principal.
- **Resumen de compra**: hoy dice "hojas". Ahora tiene que separar *a comprar*
  de *retazos usados*, sin volverlo un tablero.
- El **sobrante recuperable** dibujado sobre la lámina, con una acción que lo
  **da de alta en el rack**. Ese es el círculo que se cierra: el trabajo comió
  del rack y le devuelve material.
- Un retazo que no se usó tiene que verse sin sonar a error.
- La pantalla Retazos hoy está pensada en **largo** (tramos). Un retazo de
  lámina es **ancho × alto + área**. La tabla necesita servir a los dos.

### C · Kilos (`density_kg_m3`)

`nester/materials.py` es una **tabla de consulta, no un estimador**. Resuelve
nombres de taller — "acero inoxidable 304", "lámina negra", "aluminio 6061",
"galvanizada cal 14" — a kg/m³. El match es por palabra completa y **gana el que
está más a la derecha**, porque un nombre comercial se va afinando de izquierda
a derecha: "acero inoxidable **430**" es 7700, no los 8000 del 304.

`peso = área × espesor × densidad`.

**Si no hay densidad conocida o no hay espesor, no hay kilos.** No se supone
ninguna. Un kilo inventado se convierte en una compra equivocada, y el producto
entero está construido sobre que la IA no inventa números. `GET /v1/materials`
resuelve un nombre y devuelve `resolved: null` cuando no lo conoce — `null` es
una respuesta real.

Datos (sólo presentes cuando se puede pesar): `totals.parts_kg`,
`totals.to_buy_kg`, `totals.drop_kg`, `totals.stock_kg`,
`params.density_kg_m3`.

Lo que el diseño tiene que resolver:
- Dónde viven los kilos. **Kg a comprar** es el número que va a la orden de
  compra; **kg en piezas** es lo que entrega el taller. No son lo mismo.
- La **densidad como dato con procedencia**, igual que kerf y zona muerta: la
  IA la sacó del nombre del material (badge `DEL MATERIAL`) o la escribió el
  usuario (badge `TÚ`). Editable, siempre visible.
- El estado **"no puedo pesar este trabajo"**: qué falta (el espesor, la
  densidad, o los dos), por qué no se inventa, y el campo para resolverlo. Este
  estado tiene que verse tan resuelto como el estado con kilos — es una postura
  del producto, no una carencia.
- La pantalla Retazos ya tiene una columna **Peso** y un KPI **ACERO GUARDADO**.
  Ahora pueden ser reales para lámina.

---

## 3. Pantallas a tocar

| Pantalla | Qué cambia |
|---|---|
| **WorkspaceLamina · 01 Parámetros** | anidado en barrenos (con su consecuencia), retazo mínimo, elección de retazos, densidad con procedencia |
| **WorkspaceLamina · 03 Nido** | láminas de distinto tamaño, etiqueta de origen por hoja, sobrante dibujado, piezas en barreno legibles |
| **ResultadosLamina** | a comprar vs retazos usados, kilos, sobrante recuperable con acción, piezas en barreno marcadas |
| **Retazos** | lámina de primera clase (ancho × alto + área), alta de sobrantes desde un trabajo terminado, "usar en el trabajo" real |
| **Advertencias 2D** | las tres tarjetas cambian de naturaleza según la opción esté prendida o apagada |
| **Plan de corte lámina (PDF)** | **ya está hecho en el motor** — úsalo como referencia de qué dice el taller, no lo rediseñes |

El PDF ya imprime todo esto: resumen de compra con "A COMPRAR" y la línea
"+ 2 retazo(s) del rack", la banda de cuentas con `kg a comprar · kg en piezas ·
kg de sobrante`, el rectángulo de sobrante dibujado en ámbar punteado sobre la
hoja, las piezas en barreno como renglón propio marcado `EN BARRENO`, y las
cuatro tarjetas de "antes de cortar" ya condicionadas. **La app tiene que contar
la misma historia que el papel.** Si el papel y la pantalla discrepan, el papel
tiene razón: es el que llega a la máquina.

---

## 4. Lo que no se toca

- Los tokens, la retícula, el tono de jefe de taller, la ranura uniforme de
  badges, la escala de color por pieza.
- La arquitectura de tres bloques del workspace y el panel de conversación.
- La regla de la procedencia (`SUPUESTO` / `TÚ`) — las opciones nuevas se
  suman a ese sistema, no inventan otro.
- Que ningún dato faltante se rellene: raya, y la razón.
- El cobro, los créditos y los umbrales de saldo.

## 5. Riesgo específico de esta ronda

Son tres capacidades a la vez y todas empujan hacia meter más controles y más
KPIs en un workspace que ya está lleno. **El riesgo no es que se vea pobre, es
que se vea como un panel de configuración de software de nesting** — que es
exactamente el producto del que estamos huyendo. El taller no debería tener que
entender "anidado en barrenos" para que le sirva: la IA lo ofrece cuando conviene
("hay 6 placas con barrenos grandes, puedo meter ahí las 44 orejas y te ahorro
una hoja — pero salen dentro del recorte"), y el control queda para quien quiera
tocarlo.

Prefiero tres decisiones bien tomadas que doce controles nuevos.
