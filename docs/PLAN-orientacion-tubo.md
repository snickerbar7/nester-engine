# Plan — orientación angular de piezas de tubo (caras 1–4)

**Estado:** definido, sin empezar. Escrito 2026-08-30, corregido el mismo día.
**Qué es:** darle al taller **control** sobre cómo queda cada pieza girada en el
tubo. No es un optimizador.

---

## 1 · El requisito, en una línea

El taller decide la orientación angular de **cada pieza**, pieza por pieza o
diciéndoselo a Harriet, y puede además pedir **separación extra** entre piezas.
Es **opcional** y **no persigue ahorrar material**: si alguien quiere las
lengüetas todas en la misma cara —porque ahí está la costura, o porque le da la
gana— el plan lo obedece aunque gaste más tramo.

> *"la prioridad no es ahorrar espacio, es dejar que el usuario haga lo que
> quiera en el espacio 3D del tubo. Le damos el control y la IA ejecuta."*

La máquina **no importa**: si es sierra, el operador suelta la mordaza y gira.
No hay que modelar sujeción, ni sierra vs láser, ni particionar barras.

---

## 2 · Lo que esto SÍ y NO cambia (leer antes de codear)

- **Girar una pieza NO cambia el anidado.** Largo es largo; el empaque 1D no se
  entera. Cambia el **dibujo**, el **sólido exportado** y la **instrucción** del
  plan — no el conteo de tramos. **No dispares un re-anidado al girar.**
- **La separación extra SÍ cambia el anidado.** Sale de la misma bolsa que el
  largo útil, así que FFD la absorbe sin tocar el solver: es
  `largo + kerf + separación_extra`. Eso sí marca el trabajo como sucio y pide
  "volver a anidar", igual que cambiar el kerf.
- **La costura no está en el CAD y no hace falta que esté.** Es del tramo
  físico, la ve el operador. Lo único que damos es una forma de **simularla** en
  la UI para decidir con ella a la vista, y de imprimir la instrucción.
- **`solid_nest.py` hoy NORMALIZA la rotación** (`CLAUDE.md:283`, "todas las
  piezas comparten una orientación") — hay que **respetar** la elegida en vez de
  normalizarla. Es el único sitio donde el cambio quita comportamiento actual.

---

## 3 · Checklist

### Motor (este repo)
- [ ] `Part.orientation_deg: float = 0.0` en `nester/tube/model.py`. 0 = como
      vino del CAD. Rectangular: 0/90/180/270. Redondo: cualquier ángulo (no
      cambia nada geométricamente, pero la costura y las features sí giran).
- [ ] `Part.extra_gap_mm: float = 0.0` — separación extra **después** de esa
      pieza. Se suma al largo que consume; el solver no cambia.
- [ ] Validar: `extra_gap_mm >= 0`; ángulo normalizado a [0,360).
- [ ] `Placement` los propaga al resultado para que el plan y el visor los vean.
- [ ] CLI: `--orient FILENAME=DEG` y `--extra-gap FILENAME=MM` (repetibles, como
      `--sets`).
- [ ] `/v1` FileRef: `orientation_deg` y `extra_gap_mm`, opcionales, aditivos.
      **El contrato congelado de Harriet no los lleva** — verificar con
      `tests/test_service_contract.py`.
- [ ] `nester/tube/report.py`: cada pieza dice su giro en la lista de cortes y
      en la etiqueta; el dibujo del tramo marca la cara. La separación extra se
      dibuja distinto de la merma — es intencional, no desperdicio.
- [ ] `nester/tube/iges_nest.py` + `solid_nest.py`: girar la sección alrededor
      del eje X por pieza. **Quitar la normalización**, o dejarla sólo como
      default cuando nadie pidió orientación.
- [ ] Tests: la orientación viaja íntegra archivo → JSON → PDF → IGES/STEP;
      girar no mueve el conteo de tramos; `extra_gap` sí lo mueve y nunca
      desborda una barra; conservación de piezas con ambos.

### Web / IA (repo harriet-nester)
- [ ] `orientation_deg` y `extra_gap_mm` en el tipo de pieza y en el cuerpo que
      se manda al motor.
- [ ] Harriet los sabe poner por conversación: *"gira la P-03 90 grados"*,
      *"todas con la lengüeta hacia arriba"*, *"déjame 20 mm entre la 4 y la 5"*.
      Herramienta nueva, no un parámetro de trabajo.
- [ ] El **visor 3D** dibuja la pieza girada — es donde el taller verifica.
- [ ] Girar → sólo redibuja. Cambiar separación → marca sucio y ofrece
      "volver a anidar".

### Diseño (Claude Design, lo corre Marlon)
- [ ] Brief: control de rotación por pieza en el visor 3D (4 caras en
      rectangular, continuo en redondo), **simulador de costura** que el usuario
      coloca donde se la imagina, y control de separación extra.
- [ ] Que la UI deje **forzar** una orientación que gasta más material sin pelear
      con el usuario — es el caso de uso principal, no un error.

---

## 4 · Fuera de alcance (a propósito)
- **Optimizar la rotación.** No buscamos la orientación que ahorra tramo. Si algún
  día se quiere, es otra conversación y otro solver.
- **Leer el 3D para proponer orientación.** La IA ejecuta lo que el taller pide.
  Proponer desde el ensamble (oclusión, qué cara se ve) queda anotado como idea
  futura: necesitaría el **ensamble** como entrada nueva, no una pieza por
  archivo, y OpenCASCADE ya está en `.venv-cad` si algún día se hace.
- **Features de tubo en 3D** (barrenos, destajes, ingletes). El motor sigue
  cortando largos rectos. La orientación es un dato que el taller declara, no
  algo que el motor deduzca de la geometría.
