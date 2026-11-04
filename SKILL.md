---
name: verificar-bib
description: Verifica una bibliografía (archivo .bib, lista de referencias de un paper/tesis, o el proyecto LaTeX que las cita) contra OpenAlex, Crossref, arXiv, OpenLibrary y NASA ADS; detecta citas falsas o inventadas (alucinadas), papers retractados, datos erróneos (año, volumen, página, DOI, orden de autores), duplicados, claves \cite sin entrada y preprints ya publicados; comprueba si cada paper respalda lo que el texto afirma al citarlo; sugiere referencias faltantes; escribe un .bib corregido (DOIs, eprints, siglas protegidas, macros AAS) y entrega para cada paper los links para leerlo y descargarlo (DOI, ADS, arXiv, PDF de acceso abierto). Úsala cuando el usuario diga "verifica/revisa/corrobora mi bibliografía", "¿son reales estas referencias?", "detecta citas falsas", "¿esta cita dice lo que afirmo?", "¿me falta citar algo?", "check my references/bib", "find fake citations", "dame los links de estos papers", o invoque `/verificar-bib …`.
---

# Verificar bibliografía y encontrar los papers

Objetivo: para cada referencia, decidir si existe, si sus datos son correctos, si sigue siendo
válida (no retractada) y si respalda lo que el texto afirma; y entregar dónde leerla. Escribir en
el idioma del usuario (por defecto español).

## Argumentos

`/verificar-bib <refs.bib | refs.txt | carpeta-del-manuscrito> [profundo] [huecos] [parche]`

- **`profundo`:** además verifica las afirmaciones (paso 4). Es lo más caro: ofrecerlo, no hacerlo
  sin que el usuario lo pida.
- **`huecos`:** sugiere referencias faltantes (`--huecos 20`).
- **`parche`:** escribe un `.bib` corregido, sin tocar el original.

## 1. Conseguir las referencias

- **`.bib`:** usarlo directo.
- **Lista en texto** (pegada en el chat, sacada de un PDF o de la sección *References* de un paper):
  guardarla en el scratchpad como `.txt`, una referencia por línea o por párrafo. Si viene de un PDF,
  extraer solo la sección de referencias.
- **Proyecto LaTeX:** localizar el `.bib` (`\bibliography{…}` o `\addbibresource{…}`) y pasar la
  carpeta con `--tex`. El script cruza las claves de `\cite` (natbib y biblatex) con el `.bib` y
  extrae la oración de cada cita.

## 2. Correr el verificador

```bash
python3 -I ~/.claude/skills/verificar-bib/scripts/verificar_bib.py <refs> --out <scratchpad>/informe_bib.md --json <scratchpad>/informe_bib.json [--tex <carpeta>] [--contextos <scratchpad>/contextos.md] [--huecos 20] [--parche <scratchpad>/refs.corregido.bib] [--macros-aas]
```

Por cada referencia consulta, en orden: el DOI (y el resolvedor de doi.org si nadie lo tiene), el id
de arXiv, el título en OpenAlex, OpenLibrary (libros), ADS (si existe `ADS_API_TOKEN`), año + volumen
+ página, Crossref bibliográfico y arXiv. Al paper encontrado le agrega retractaciones y correcciones
(Retraction Watch, vía OpenAlex y Crossref), la versión publicada si el bib cita un preprint, y los
links de acceso abierto. Veredictos:

- 🚩 **RETRACTADA:** existe, pero fue retractada o retirada. No debe citarse como evidencia.
- ✅ **OK:** coinciden título, primer autor, año (±1, por arXiv vs. publicado), volumen y página.
  En libros no se exige el año, porque las bases indexan otras ediciones.
- ⚠️ **DISCREPANCIA:** existe, pero algo no cuadra: año, volumen, página, orden de autores, DOI
  inexistente o que apunta a otro paper, o **en ese volumen/página hay otro paper con otro título**
  (patrón típico de una cita inventada que reutiliza una revista real).
- ❌ **NO ENCONTRADA:** ninguna base la tiene, así que es una posible cita falsa. Va con el
  candidato más parecido como pista.

El informe agrega duplicados (mismo paper con dos claves, o clave repetida), el cruce con el `.tex`
(claves citadas sin entrada, que saldrán como `[?]`, y entradas nunca citadas), las referencias
faltantes y notas (correcciones publicadas, preprints ya publicados, ediciones de libros).

Notas de ejecución:
- Hay caché en `~/.cache/verificar-bib` (30 días): volver a correrlo tras editar el `.bib` es casi
  instantáneo. `--sin-cache` la ignora.
- arXiv se consulta como máximo una vez cada 3 s. Con más de ~50 referencias, correrlo en background.
- Si el informe trae el aviso "consultas fallaron", las ❌ pueden ser falsos negativos: repetir más tarde.
- Sin `OPENALEX_API_KEY`, OpenAlex da un cupo diario chico por IP. `ADS_API_TOKEN` activa ADS, que
  en astronomía tiene libros, proceedings y papers antiguos que las otras bases no tienen.
  Si el usuario las tiene, que las exporte él. Nunca crear keys por el usuario ni leer su valor.
- `--strict` sale con código 1 si hay ❌, 🚩 o claves faltantes (útil antes de la entrega final).

## 3. Revisar a mano lo dudoso (no saltarse esto)

El script es un filtro. **No declarar falsa una cita solo porque el script no la encontró.**
Para cada ❌, 🚩 y ⚠️ no trivial:

1. Buscar con WebSearch el título exacto entre comillas, y "primer autor + año + revista".
2. Pensar en los falsos negativos típicos, que **no** son citas falsas:
   - Libros, capítulos, tesis, proceedings antiguos (`@book`, `@phdthesis`, `@inproceedings`):
     se indexan mal o con la fecha de una reimpresión. Un título corto (*Fluid Mechanics*) puede
     emparejarse con otra obra del mismo nombre.
   - Software y datos (`@misc`, `@software`: FARGO3D, RADMC-3D, astropy…): verificar en ASCL,
     Zenodo o GitHub, y la publicación que el software pide citar.
   - Papers pre-1990 sin DOI: ADS los tiene aunque OpenAlex no.
   - Un artículo y su versión de congreso con el mismo título (p.ej. Shakura & Sunyaev 1973 en
     A&A y en IAU Symp. 55): la base puede devolver la otra versión.
   - Preprints recientes que todavía no salen en revista.
   - Autores corporativos o nombres en caracteres no latinos.
3. Para un ⚠️ de "orden de autores", revisar si la clave (`Casassus2025`) y el `\citet{}` del texto
   atribuyen el trabajo a quien no es primer autor. Eso sí hay que corregirlo.
4. Para un 🚩, confirmar la retractación en el aviso oficial y avisar al usuario dónde lo cita.
5. Veredicto final por referencia:
   - **real y correcta**;
   - **real con datos a corregir:** dar los campos BibTeX corregidos;
   - **retractada:** decir dónde se cita y sugerir reemplazo o contexto;
   - **no verificable:** se buscó y no apareció; pedirle la fuente al usuario;
   - **falsa:** solo con evidencia, p.ej. el DOI o el volumen/página pertenecen a otro paper y no
     existe nada con ese título.

Agregar una sección "Revisión manual" al principio del informe con estos veredictos.

## 4. Verificar afirmaciones (modo `profundo`)

Requiere `--tex` y `--contextos`. El archivo de contextos trae, por referencia citada, el abstract
del paper y cada oración del `.tex` que lo cita (`archivo:línea`). Para cada uso:

1. Comparar lo que afirma la oración con el abstract. Clasificar como **respalda**, **respalda en
   parte** (p.ej. el paper lo muestra para otro régimen o con más cautela), **no respalda** o
   **no verificable con el abstract**.
2. Para los **no verificables** y los **no respalda**, abrir el texto completo (el arXiv o el PDF del
   informe) y buscar el pasaje concreto antes de concluir. Citar la sección o página del paper.
3. Juzgar la afirmación, no el estilo. Señalar citas genéricas ("ver revisión de…") como aceptables.
4. Escribir el resultado en una tabla, `archivo:línea | clave | veredicto | evidencia del paper |
   sugerencia`, al final del informe. Ordenar primero los **no respalda**.

Con muchas citas (más de ~60), preguntar al usuario si revisar todas o priorizar: las que sostienen
resultados propios, valores numéricos y comparaciones directas.

## 5. Referencias faltantes (`huecos`)

`--huecos N` lista los papers citados por al menos dos de las referencias del usuario que él no cita,
ordenados por cuántas de sus referencias los citan. No todos aplican: filtrar por relevancia para el
tema del trabajo (leer el título y, si hace falta, el abstract) y presentar solo los que vale la pena
considerar, con una línea de por qué.

## 6. Entregar

- El informe Markdown con los links (DOI · ADS · arXiv · PDF · ISBN) y la revisión manual. Si el
  usuario lo va a compartir, ofrecer publicarlo como Artifact.
- Un resumen en el chat: cuántas OK, cuántas con datos a corregir, retractadas, posiblemente falsas
  y por qué; y, en modo profundo, cuántas afirmaciones no están respaldadas.
- **No editar el `.bib` del usuario sin permiso.** `--parche` escribe un archivo aparte que solo
  agrega `doi`/`eprint` en entradas ✅, protege siglas con llaves y, con `--macros-aas`, abrevia
  revistas (`\apj`, `\aap`, `\mnras`…; requiere `aas_macros.sty` o AASTeX). Nunca cambia año,
  volumen ni página. Esas correcciones se proponen y se aplican solo si el usuario dice que sí.
  Mostrar el `diff` antes de reemplazar el original.
- Los links son para leer y descargar. **No descargar PDFs** salvo que el usuario lo pida
  explícitamente. En ese caso, guardarlos en una carpeta nueva y vacía.

## Gotchas

- Con `plainnat`/`aasjournals` los títulos pasan a minúsculas. El parche protege las siglas
  (`{PDS} 70`, `{ALMA}`, `{3D}`), pero no los nombres propios (*Jupiter*, *Kepler*): revisarlos.
- El parser BibTeX es mínimo: no resuelve `@string` ni concatenación con `#`. Avisar si el `.bib`
  los usa.
- Los enlaces de ADS le muestran una verificación humana a los navegadores automatizados. Son para el
  usuario, no para que Claude los abra.
- Pruebas: `--selftest` (sin red) y `tests/regresion.py` (con red, fixtures con citas trampa).
