# Plan — orientación angular de piezas de tubo (caras 1–4)

**Estado:** propuesta, sin empezar. Escrito 2026-08-30.
**Origen:** Marlon quiere que el taller pueda girar cada pieza entre las 4 caras
del perfil, que eso **cambie el anidado**, y (fase 2) que la IA lea el 3D para
proponer la orientación.

---

## 1 · Qué es verdad hoy, antes de diseñar nada

- **La costura no está en el CAD.** Un tubo modelado en Fusion es ideal: cuatro
  caras, sin cordón de soldadura. La posición angular de la costura es una
  propiedad del tramo físico que el taller saca del rack, y nadie la sabe hasta
  que la ve. El software **no puede detectarla, sólo instruir** dónde debe
  quedar — o, como pidió Marlon, dejar que el usuario la **simule** en la UI
  para decidir con ella a la vista.
- **Un tramo tiene UNA costura.** Todas las piezas cortadas de ese tramo la
  heredan en la misma posición angular. `CLAUDE.md:283` ya lo dice para el
  exportador de sólidos: *"un solo tubo de stock sólo se puede sujetar de una
  manera"*.
- **Hoy una pieza de tubo es un ESCALAR.** `nester/tube/iges.py` toma el eje más
  largo del encajonado y descarta el resto. No hay sección, no hay perfil de
  extremo, no hay posición de barrenos. `nester/tube/model.py` no tiene ningún
  campo de rotación, orientación ni cara. **El solver no tiene nada que girar.**
- Por lo tanto: girar una pieza **no cambia el anidado actual** — largo es
  largo. Para que cambie, el motor tiene que aprender geometría que hoy tira a
  la basura. Esa es la parte cara de esto, y hay que decidirla con los ojos
  abiertos.

---

## 2 · LA PREGUNTA QUE BLOQUEA TODO (responder antes de construir)

Marlon: *"dos tubos con insertos (macho) en los extremos, en caras 1 y 3. Girar
uno 90° para que queden en los lados y en el otro arriba y abajo — así ganamos
espacio."*

Entiendo el resultado (se gana largo de tramo) pero **no cuál es el mecanismo
físico**, y el mecanismo decide qué hay que modelar. Los candidatos:

- [ ] **(a) Separación mínima entre features.** El láser no puede cortar dos
      barrenos/ranuras demasiado cerca entre piezas vecinas; si las features de
      la pieza A y de la B caen en la misma cara y muy juntas, hace falta dejar
      material extra. Girando 90° una de las dos, caen en caras distintas y las
      piezas se acercan. → hay que modelar **posición longitudinal + cara de
      cada feature**, y la separación pasa a depender del PAR de piezas vecinas.
- [ ] **(b) Perfiles de extremo que embonan.** Los extremos no son cuadrados
      (inglete, boca de pescado, destaje). Dos extremos complementarios se
      pueden encimar o compartir corte según la rotación relativa. → hay que
      modelar el **perfil 3D del extremo**; es territorio de CAM de tubo real
      (Lantek Flex3d, TRUMPF).
- [ ] **(c) La costura es la restricción.** No se corta una feature encima del
      cordón (queda feo y es más débil). Como la costura del tramo es fija, las
      rotaciones permitidas de cada pieza se acoplan entre sí y el anidado se
      **particiona** por orientación.
- [ ] **(d) Otra cosa.** Marlon dibuja el caso y lo modelamos con eso.

**Sin esta respuesta no se empieza.** (a) y (c) son mucho más baratas que (b).
El segundo caso que mencionó —"otro usuario las quiere en las mismas caras, con
un split de material de desperdicio entre ellas"— dice que la orientación
también es una **preferencia del taller**, no sólo una optimización: la UI tiene
que permitir forzarla aunque cueste material.

---

## 3 · Fases

### Fase 0 — decidir (no escribir código)
- [ ] Responder §2 con un dibujo o un archivo real.
- [ ] ¿En qué máquina importa? **Sierra con mordaza fija** = una orientación por
      tramo, restricción real de anidado. **Láser de tubo con eje rotatorio** =
      la máquina gira el stock por corte y la restricción desaparece.
- [ ] ¿El taller ya manda archivos con features, o hoy sólo manda tubo recto?
      Si es lo segundo, esto es una apuesta a un cliente que todavía no existe.

### Fase 1 — orientación como dato, sin leer 3D
Entrega valor sin tocar el solver ni el lector de IGES.
- [ ] `Part` gana `orientation_deg` (0/90/180/270; 0 = como vino del CAD).
- [ ] `--orient FILENAME=DEG` en el CLI y `orientation_deg` en el `FileRef` de
      `/v1` (aditivo; el contrato congelado de Harriet no lo lleva).
- [ ] El plan de corte imprime la instrucción por tramo y por pieza:
      *"sujeta el tramo con la costura hacia atrás; P-03 gira 90°"*.
- [ ] El visor 3D / `solid_nest.py` dibuja la pieza girada — hoy **normaliza** la
      rotación (`CLAUDE.md:283`), habría que respetar la elegida.
- [ ] Simulador de costura en la UI: el usuario marca en qué cara la imagina y
      la app se lo pinta. **No es un dato del archivo, es una nota del taller.**
- [ ] Tests: la orientación viaja íntegra archivo → plan → PDF → IGES.

### Fase 2 — que la orientación CAMBIE el anidado
Aquí está el trabajo de verdad, y depende de §2.
- [ ] Modelar lo mínimo que exige el mecanismo elegido (features con cara y
      posición, o perfil de extremo).
- [ ] Extender el lector: hoy `read_tube` tira todo menos el largo.
- [ ] El solver deja de empacar escalares. Con (a) o (b) la separación depende
      del **par** de piezas vecinas → deja de ser FFD puro y pasa a ser un
      empaque **dependiente de la secuencia**. Medir antes de prometer.
- [ ] Con (c): particionar barras por orientación, como ya se agrupa por perfil.
      Esto **puede subir el conteo de tramos** — hay que reportarlo honestamente,
      igual que la regla de decline de retazos.
- [ ] Property tests: ninguna pieza se pierde ni se duplica al girar; ninguna
      barra se desborda; girar nunca empeora el conteo salvo cuando el taller lo
      forzó a propósito.

### Fase 3 — la IA propone la orientación leyendo el 3D
- [ ] **Entrada nueva: el ENSAMBLE.** Hoy el pipeline recibe una pieza por
      archivo; "qué cara se ve" no existe para un miembro suelto.
- [ ] Leer STEP/OBJ: ya hay OpenCASCADE en `.venv-cad` (`solid_nest.py`).
- [ ] Calcular **exposición** por cara (oclusión con los otros miembros), no
      "verla". Renderizar una imagen y que un modelo de visión juzgue es
      demasiado frágil para algo que decide un corte.
- [ ] "Oculta" es un juicio, no una medición: en un barandal es el lado de abajo
      y el del muro. O una regla declarada, o Harriet pregunta una vez cuál es
      el lado público.
- [ ] Harriet **propone**, el taller **sobreescribe**. Nunca al revés.

---

## 4 · Riesgos y decisiones tomadas
- **Girar no es gratis en el conteo.** Si la orientación se vuelve restricción,
  puede hacer falta más tramo. Se reporta medido, nunca se esconde — misma
  doctrina que E24.
- **No hacer esto por vision.** La geometría decide; la IA comunica.
- **La UI va por Claude Design.** De este repo sale el contrato del motor y el
  brief; el lienzo lo corre Marlon (`/design-round`).
- **Puede que no aplique todavía.** Si los talleres mandan tubo recto sin
  features, la fase 2 no tiene a quién servirle. La fase 1 sí: instruir la
  sujeción es útil desde el primer trabajo.
