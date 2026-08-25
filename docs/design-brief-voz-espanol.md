# Brief de diseño — la voz en español de la página pública

**Para:** una ronda de Claude Design sobre el canvas de Harriet Nester (copy, no layout).
**De:** el repo del motor, 25 ago 2026, después de una auditoría de idioma de las
cuatro superficies en español (catálogo de la app, página pública, prompt de la
IA, y el PDF del plan de corte).
**Alcance:** sólo las decisiones de VOZ que son del canvas. Los defectos
mecánicos —acentos faltantes, gramática rota, voseo argentino— ya se están
arreglando en código y no son tema de esta ronda.

---

## 1. El diagnóstico, que no es el esperado

El dueño reportó que el español "se siente traducido". **No lo está.** El cuerpo
del texto es español mexicano nativo y bueno: *le subes*, *te regresa*, *listo
para el piso*, *cuando se te acaban a medio mes*, *la factura te la hacemos a
mano*, *Contesta una persona, de lunes a sábado*. Cero clichés de SaaS
traducido: no hay "impulsa", ni "potencia", ni "sin esfuerzo", ni "lleva tu
taller al siguiente nivel". Tuteo consistente, sin Title Case, sin vocabulario
peninsular.

**Lo que sí pasa:** tres señales de "esto es extranjero" viven juntas en los
primeros 400 píxeles —los únicos que casi todo visitante lee— y el lector juzga
ahí y después lee el buen texto a través de ese juicio.

> **Regla de esta ronda: no toques el cuerpo.** Reescribir lo que ya está bien
> es puro desgaste. Son ~10 decisiones, casi todas de una palabra.

---

## 2. El héroe — donde se hace el daño

### `nestero` — palabra inventada, en los tres lugares más leídos

`landing.hero.h1` · `landing.meta.title` · `landing.og.title` · `auth.hero.sub`

> `El nestero que entiende cómo hablas.`

Raíz inglesa (*nest*) con sufijo agentivo mexicano (*-ero*). El sufijo está
perfecto —herrero, soldador, plomero, fierrero— y por eso es peor: tiene la
**forma** del español de taller con una **raíz** que ningún herrero ha dicho.
Se lee como un extranjero imitando el oficio. Y va en el H1, en el `<title>` y
en la tarjeta de WhatsApp.

Propuesta (37 caracteres contra 36, no rompe el clamp del H1):
> `Le dices cómo cortas y te da el plan.`

Alternativa más cerca del gancho original:
> `El que te anida y entiende cómo le hablas.`

### `OPERADOR DE NESTING` — anglicismo en la ranura de marca

`landing.hero.eyebrow` y `landing.foot.tagline`

La página se contradice sola: en todos lados usa el verbo español —*Anidar
cuesta 1 crédito*, *{trial} anidados de prueba*, *el anidado a escala*— y sólo
en las dos ranuras de voz de marca aparece el anglicismo. `anidar/nido` está en
la lista de vocabulario protegido; `nesting` no.

> `ANIDADO DE TUBO Y LÁMINA · HECHO EN MÉXICO`

**Nota:** `nesting` como sustantivo de posicionamiento ("operador de nesting")
puede defenderse como categoría de producto. Es una decisión de marca, no un
error — pero hay que tomarla a propósito, no por inercia, y hoy convive con
`anidado` en la misma pantalla.

### `MX$499` — notación de casa de cambio

`src/lib/product.ts`

Es exactamente el marcador que usa un SaaS internacional para decir "aquí está
la versión mexicana de nuestro precio en dólares". En un dominio `.com.mx` cuyo
pie dice *Hecho en México*, la moneda no está en duda: se escribe `$499`. Y
`MX$0` en la fila gratis es doblemente raro junto a una fila que ya se llama
*Gratis*.

El código dice que fue deliberado ("MX$499.00 en una tarjeta de plan se lee como
campo de formulario"), así que es decisión del dueño — pero es la señal más
fuerte de página traducida que hay en el héroe.

---

## 3. Un error de oficio que sí cuesta un cliente

`landing.how.3.sheet` · `landing.alt.files` · `landing.alt.nido`

> `lámina · hoja 1,220 × 2,440 · 12 placas`
> `Doce placas acomodadas sobre una hoja de lámina 4x8 calibre 16`

En el comercio del acero mexicano **placa** es placa gruesa (≥ 3/16″, se vende
por espesor); **lámina** es hoja delgada que se vende por **calibre**. El pie
literalmente dice "lámina · hoja · 12 **placas**", y el alt pone doce placas
sobre una lámina calibre 16 — una contradicción en sus términos. Lo que sale
cortado de una lámina son **piezas**.

Esta es la línea que le dice a un herrero que quien escribió no ha pisado un
taller. **No es preferencia, es un error factual.** → `12 piezas`.

---

## 4. Seis decisiones más, de una palabra cada una

| Dónde | Hoy | Propuesta | Por qué |
|---|---|---|---|
| `landing.how.4.body` | `verificar de un ojo` | `de un vistazo` | **No es modismo en ningún español.** Las formas son *a ojo* o *de un vistazo*. Es el tropiezo de no-nativo más claro del cuerpo |
| `landing.video.caption` · `landing.alt.hero` | `el workspace de un trabajo` | `la pantalla de un trabajo` | Prohibido por las reglas de voz del propio README público, y la app llama a esa pantalla **Trabajo** |
| `landing.price.taller.tag` | `EL DE TODOS` | `EL DEL TALLER` | Es la ranura que en inglés dice *MOST POPULAR*: vago en español, y una afirmación de popularidad que el producto no puede probar |
| `landing.how.3.tube` | `92.3 % neto` | `92.3 % aprovechado` | `neto` es taquigrafía ajena; la página ya usa **aprovechamiento** dos veces |
| `landing.shots.label` + `.note` | `EL PRODUCTO TRABAJANDO` / `capturas del producto trabajando, no maquetas` | `CAPTURAS DEL PRODUCTO` / `del producto trabajando, no dibujitos` | Las mismas palabras dos veces en tres pulgadas; y *maqueta* para un herrero es una **maqueta física**, no un mockup. La página ya tiene la palabra buena: *no dibujitos* |
| `landing.price.note` | `el anidado que llegó a resultado` | `el anidado que sí salió` | *llegar a resultado* no es modismo, es "reached a result" armado literal |
| `landing.how.6.title` | `Tus retazos y tus máquinas cuentan` | `Anida con tus retazos y con el kerf de tu sierra` | Calque de "count" = *importar*; en español *cuentan* se lee "se cuentan", justo lo ambiguo aquí |

---

## 5. El ritmo, que es la causa difusa

**16 rayas espaciadas** como pausa dramática en el copy público:
*"…el que pediste — no para medir."*, *"…puede equivocarse — por eso…"*.

Eso es el hábito del guión inglés. El español usa coma o dos puntos para una
amplificación final, y reserva la raya para un inciso **en par** —que este mismo
archivo hace bien en otros lados (*—dudas, correcciones, borrado—*). Ninguna
línea está mal por sí sola; juntas ponen un ritmo anglosajón encima de prosa
nativa, y es parte de por qué "se siente traducido" sin que se pueda señalar
una frase.

Es decisión de estilo de casa. Si se toma, se toma de una vez y para todo.

**Menor:** `PTR 40x40` y `4x8` usan `x` de ASCII mientras `1,220 × 2,440` usa `×`.
Y `sólo` va acentuado 8 veces y sin acento 1 (la RAE quitó el acento en 2010;
acentuado se lee *mexicano de antes*, no *extranjero* — pero que sea consistente).

---

## 6. Legal — cuatro calcos en un documento por lo demás bueno

El registro legal está bien y es deliberado (*"Está escrito para leerse, no para
cubrirnos."* se lo gana). Cuatro cosas se leen traducidas a máquina:

- `legal.terms.s8.p1`: **`tal como está`** es "as is" palabra por palabra; el
  término de arte en México es **`tal cual`**. En el mismo párrafo, **`arriba`**
  (×2) es el "up" de sysadmin — en español se dice *en línea* / *disponible*.
  Para un dueño de taller, *"que el servicio esté arriba"* se lee como un lugar.
- `legal.terms.s7.p1`: **`raspes`** para *scrape* — y en un taller de metal
  *raspar* tiene un significado físico real y equivocado. → `extraigas`.
  **`están contados`** idiomáticamente significa *tienen los días contados*; se
  quiere decir *tienen un tope diario*.
- `legal.privacy.s6.p1`: **`la puerta de entrada`** es una metáfora interna del
  código publicada en un documento legal. Un aviso nombra cosas, no las evoca.
  → `la pantalla de acceso`.
- `legal.contactLine`: `duda de esta página` → `duda sobre esta página`
  (*duda de X* es dudar de su veracidad).

**No es de idioma, pero se siente igual:** el aviso de privacidad no nombra
**domicilio del responsable** y los términos no nombran jurisdicción. La LFPDPPP
art. 16 espera lo primero, y un lector mexicano de un aviso busca la dirección
— su ausencia se lee como "esto se adaptó de una plantilla gringa", que
alimenta exactamente la impresión que estamos corrigiendo.

---

## 7. Lo que NO se toca

- Todo el cuerpo de `landing.hero.lead`, los CTAs, la línea de prueba y la nota
  de precio: ya son el registro correcto.
- `CUALQUIER COSA, ESCRÍBENOS` · `Contesta una persona, de lunes a sábado.` ·
  `LO LEGAL` · la pantalla 404 completa. Es lo mejor que hay; que ninguna pasada
  de reescritura las toque.
- El vocabulario de oficio: tramo, retazo, lámina, calibre, PTR, solera,
  barreno, merma, kerf, láser, plasma, CNC, CAD, DXF.
- Los tokens, la retícula y el layout. Esta ronda es de palabras.
