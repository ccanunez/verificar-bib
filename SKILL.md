---
name: verificar-bib
description: Verifica una bibliografía (archivo .bib, lista de referencias de un paper/tesis, o el .tex que las cita) contra OpenAlex, Crossref y arXiv; detecta citas falsas o inventadas (alucinadas), datos erróneos (año, volumen, página, DOI, orden de autores) y entrega para cada paper los links para leerlo y descargarlo (DOI, ADS, arXiv, PDF de acceso abierto). Úsala cuando el usuario diga "verifica/revisa/corrobora mi bibliografía", "¿son reales estas referencias?", "detecta citas falsas", "check my references/bib", "find fake citations", "dame los links de estos papers", o invoque `/verificar-bib …`.
---

# Verificar bibliografía y encontrar los papers

Objetivo: para cada referencia, decidir si existe, si sus datos son correctos y dónde leerla.
Escribir en el idioma del usuario (por defecto español).

## 1. Conseguir las referencias

- **`.bib`** → usarlo directo.
- **Lista en texto** (pegada en el chat, sacada de un PDF, o la sección References de un paper)
  → guardarla en el scratchpad como `.txt`, una referencia por línea o por párrafo. Si viene
  de un PDF, extraer sólo la sección de referencias.
- **`.tex` o proyecto LaTeX** → localizar el `.bib` (`\bibliography{…}` / `\addbibresource{…}`)
  y además cruzar claves: `grep -oh '\\cite[a-z]*\*\?\(\[[^]]*\]\)*{[^}]*}'` en los `.tex`
  vs. las claves del `.bib`. Informar claves citadas que no existen en el `.bib` y entradas
  del `.bib` que nunca se citan.

## 2. Correr el verificador

```bash
python3 -I ~/.claude/skills/verificar-bib/scripts/verificar_bib.py <refs.bib|refs.txt> --out <scratchpad>/informe_bib.md
```

Consulta, por referencia: DOI (si hay) → arXiv id (si hay) → búsqueda por título en OpenAlex →
búsqueda por año+volumen+página → Crossref bibliográfico → arXiv. Clasifica en:

- ✅ **OK**: título, primer autor, año (±1, por arXiv vs. publicado), volumen y página coinciden.
- ⚠️ **DISCREPANCIA**: el paper existe pero algo no cuadra: año, volumen, página, orden de autores,
  DOI inexistente o que apunta a otro paper, o **en ese volumen/página hay otro paper con otro título**
  (patrón típico de cita inventada que reutiliza una revista real).
- ❌ **NO ENCONTRADA**: ninguna base la tiene → posible cita falsa.

Más de ~50 referencias: correrlo en background. Sin API key, OpenAlex da un cupo diario chico
compartido por IP. Si el usuario tiene una key gratuita (openalex.org/settings/api), exportarla
como `OPENALEX_API_KEY`. No crear la key por el usuario.

## 3. Revisar a mano lo dudoso (no saltarse esto)

El script es un filtro. **No declarar falsa una cita sólo porque el script no la encontró.**
Para cada ❌ y cada ⚠️ no trivial:

1. Buscar con WebSearch el título exacto entre comillas y "primer autor + año + revista".
2. Pensar en los falsos negativos típicos, que **no** son citas falsas:
   - Libros, capítulos, tesis, proceedings antiguos, circulares (`@book`, `@phdthesis`,
     `@inproceedings`): las bases los indexan mal. Buscar por ISBN, editorial o repositorio.
   - Software y datos (`@misc`, `@software`: FARGO3D, RADMC-3D, astropy…): verificar en ASCL,
     Zenodo o GitHub, y la publicación que el software pide citar.
   - Papers pre-1990 sin DOI (p.ej. QJRAS, MNRAS antiguos): ADS los tiene aunque OpenAlex no.
   - Preprints recientes que todavía no salen en revista: buscar en arXiv.
   - Títulos traducidos o abreviados, o nombres con caracteres no latinos.
3. Para un ⚠️ de "orden de autores", revisar si la clave (`Casassus2025`) y el `\citet{}` del
   texto atribuyen el trabajo a quien no es primer autor; eso sí hay que corregirlo.
4. Veredicto final por referencia: **real y correcta**, **real con datos a corregir**
   (dar los campos BibTeX corregidos), **no verificable** (se buscó y no apareció; pedir la
   fuente al usuario) o **falsa** (sólo si hay evidencia: p.ej. el DOI o el volumen/página
   pertenecen a otro paper y no existe nada con ese título).

Si el usuario lo pide, ir más allá de la existencia: abrir el paper (arXiv/PDF) y comprobar que
dice lo que el `.tex` afirma al citarlo. Hacerlo sólo para las citas que indique, porque es caro.

## 4. Entregar

- El informe Markdown con los links (DOI · ADS · arXiv · PDF), con las correcciones de la
  revisión manual aplicadas. Si el usuario lo va a compartir, ofrecer publicarlo como Artifact.
- Un resumen en el chat: cuántas OK, cuántas con datos a corregir, cuáles son posiblemente
  falsas y por qué.
- **No editar el `.bib` sin permiso.** Ofrecer el parche con los campos corregidos y aplicarlo
  sólo si el usuario dice que sí.
- Los links son para leer y descargar. **No descargar PDFs** salvo que el usuario lo pida
  explícitamente. En ese caso, guardarlos en una carpeta nueva y vacía.

## Gotchas

- Con `plainnat`/`aasjournals` los títulos pasan a minúsculas: al proponer correcciones,
  proteger acrónimos y nombres propios con llaves (`{PDS} 70`, `{ALMA}`).
- El parser BibTeX es mínimo: no resuelve `@string` ni concatenación con `#`. Si el `.bib` los
  usa, avisar de que esas entradas pueden salir mal parseadas.
- Los enlaces de ADS (`/abs/<DOI>`) le muestran una verificación humana a los navegadores
  automatizados. Son para el usuario, no para que Claude los abra.
