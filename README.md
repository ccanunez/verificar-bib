# verificar-bib

[![tests](https://github.com/ccanunez/verificar-bib/actions/workflows/tests.yml/badge.svg)](https://github.com/ccanunez/verificar-bib/actions/workflows/tests.yml)

**Una skill para [Claude Code](https://claude.com/claude-code) que revisa una bibliografía referencia por referencia, detecta citas falsas, retractadas o con datos erróneos, comprueba que cada paper respalde lo que tu texto afirma y te entrega el link para leer y descargar cada uno.**

Una bibliografía acumula errores sin que nadie lo note: años cambiados, volúmenes o páginas mal copiados, DOIs que apuntan a otro artículo, autores en otro orden, papers que fueron retractados después de citarlos. Con los asistentes de IA aparece además un problema nuevo, las **citas inventadas**: referencias con autores plausibles, una revista real y un título que no existe. Y el error más difícil de ver es otro: el paper existe, pero **no dice lo que le atribuyes**.

`verificar-bib` contrasta cada entrada con [OpenAlex](https://openalex.org), [Crossref](https://www.crossref.org), [arXiv](https://arxiv.org), [OpenLibrary](https://openlibrary.org) y, si tienes token, [NASA ADS](https://ui.adsabs.harvard.edu). Después Claude revisa a mano lo dudoso y, si lo pides, lee cada cita de tu manuscrito junto al abstract del paper citado.

Funciona con cualquier disciplina, aunque ADS y las macros AAS la hacen especialmente útil en astronomía y física.

*English summary at the [end of this file](#english-summary).*

---

## Qué hace

| Paso | Qué ocurre |
|---|---|
| **1. Reunir las referencias** | Acepta un `.bib`, una lista en texto (la sección *References* de un paper, pegada o extraída de un PDF) o tu proyecto LaTeX. |
| **2. Verificación automática** | Busca cada referencia por DOI, id de arXiv, título, ISBN y año + volumen + página. Compara título, primer autor, año, volumen, página y DOI, y consulta retractaciones y correcciones (Retraction Watch). |
| **3. Revisión manual** | Claude revisa cada referencia marcada con búsqueda web y conoce los falsos negativos típicos: libros, software, papers antiguos sin DOI, versiones de congreso con el mismo título. |
| **4. Afirmaciones** *(modo profundo)* | Para cada `\cite` de tu `.tex`, compara la oración con el abstract del paper (y con el texto completo si hace falta) y la clasifica como respalda / en parte / no respalda. |
| **5. Informe y parche** | Escribe el veredicto de cada referencia con sus links, las correcciones propuestas y, si quieres, un `.bib` corregido aparte. Solo cambia tu `.bib` si se lo pides. |

### Veredictos
- ✅ **Verificada:** coinciden título, primer autor, año (±1, por la diferencia entre arXiv y la versión publicada), volumen y página.
- ⚠️ **Existe, pero con datos distintos:** año, volumen o página erróneos; el primer autor real es otro; el DOI no existe o apunta a otro paper; o **en ese volumen y página hay otro paper con otro título**, el patrón típico de una cita inventada que reutiliza una revista real.
- 🚩 **Retractada:** el paper existe, pero fue retractado o retirado.
- ❌ **No encontrada:** ninguna base la tiene, así que es una posible cita falsa. Viene con el candidato más parecido como pista, y Claude la busca a mano antes de concluir nada.

### Además detecta
- **Preprints ya publicados:** citas el arXiv y el paper ya salió en una revista.
- **Correcciones y fe de erratas** publicadas sobre un paper que citas.
- **Duplicados:** el mismo paper bajo dos claves, o una clave repetida en el `.bib`.
- **Cruce con el `.tex`:** claves citadas que no están en el `.bib` (saldrían como `[?]`) y entradas que nunca citas.
- **Referencias faltantes:** papers que citan varias de tus referencias y tú no.

### Links que entrega
Para cada paper: **DOI** · **ADS** · **arXiv** · **PDF de acceso abierto** (cuando existe) · **ISBN** (libros). Para lo que no encuentra, deja búsquedas listas en Google Scholar y ADS.

### `.bib` corregido (opcional)
Escribe un archivo aparte, sin tocar el tuyo, que:
- agrega `doi` y `eprint`/`archivePrefix` a las entradas verificadas;
- pone llaves a las siglas (`{PDS} 70`, `{ALMA}`, `{3D}`) para que `plainnat` o `aasjournals` no las pasen a minúsculas;
- con `--macros-aas`, abrevia las revistas (`\apj`, `\aap`, `\mnras`…).

Nunca cambia año, volumen ni página: esas correcciones se proponen en el informe y las decides tú.

### Principios
- **No acusa sin evidencia.** Que una referencia no esté en una base de datos no la vuelve falsa. Una cita solo se declara falsa si, por ejemplo, su DOI o su volumen y página pertenecen a otro paper y no existe nada con ese título.
- **No toca tu `.bib` sin permiso.**
- **No descarga PDFs** a menos que lo pidas: entrega los links.
- **No necesita API keys ni paquetes de Python.**

---

## Instalación

Requiere [Claude Code](https://docs.claude.com/en/docs/claude-code) (CLI, app de escritorio o extensión de IDE), `python3` 3.9 o superior y `curl`.

**Para todos tus proyectos** (recomendado):

```bash
git clone https://github.com/ccanunez/verificar-bib.git ~/.claude/skills/verificar-bib
```

**Solo para un proyecto** (desde la raíz del proyecto):

```bash
git clone https://github.com/ccanunez/verificar-bib.git .claude/skills/verificar-bib
```

Si la skill no aparece, abre una sesión nueva de Claude Code para que se recargue la lista de skills.

**Para actualizarla:**

```bash
git -C ~/.claude/skills/verificar-bib pull
```

### Opcional: API keys
- **OpenAlex** (más cupo): sin key, OpenAlex comparte un cupo diario pequeño entre todos los usuarios de tu IP. Crea una gratis en [openalex.org/settings/api](https://openalex.org/settings/api).
- **NASA ADS** (recomendado en astronomía): activa ADS como fuente, que tiene libros, proceedings y papers antiguos que las otras bases no tienen. Crea el token en [ui.adsabs.harvard.edu/user/settings/token](https://ui.adsabs.harvard.edu/user/settings/token).

```bash
export OPENALEX_API_KEY=tu_key
export ADS_API_TOKEN=tu_token
```

---

## Uso

Desde Claude Code:

```
/verificar-bib refs.bib
/verificar-bib manuscrito/ profundo huecos parche
```

O simplemente pide *"verifica mi bibliografía"*, *"¿son reales estas referencias?"*, *"¿esta cita dice lo que afirmo?"* o *"¿me falta citar algo?"*, y adjunta el archivo o pega la lista.

El script también funciona solo, sin Claude:

```bash
python3 ~/.claude/skills/verificar-bib/scripts/verificar_bib.py refs.bib --out informe.md
```

| Opción | Qué hace |
|---|---|
| `--out informe.md` | Informe en Markdown (por defecto, a la pantalla) |
| `--json res.json` | Todos los resultados en JSON, para tus propios scripts |
| `--tex manuscrito/` | Cruza las claves de `\cite` (natbib y biblatex) con el `.bib` |
| `--contextos ctx.md` | Con `--tex`: la oración de cada cita junto al abstract del paper |
| `--parche refs.corregido.bib` | `.bib` corregido aparte (DOIs, eprints, siglas protegidas) |
| `--macros-aas` | Con `--parche`: revistas como `\apj`, `\aap`, `\mnras` |
| `--huecos 20` | Sugiere hasta 20 referencias faltantes |
| `--strict` | Sale con código 1 si hay ❌, 🚩 o claves faltantes (útil antes de entregar) |
| `--sin-cache` | Ignora la caché (`~/.cache/verificar-bib`, 30 días) |
| `--selftest` | Pruebas sin red |

También acepta una lista en texto con una referencia por línea: `verificar_bib.py referencias.txt`.

### Ejemplo
Un `.bib` de prueba ([`tests/falsas.bib`](tests/falsas.bib)) con trampas:

```
9 entradas · ✅ 4 verificadas · ⚠️ 3 con discrepancias · ❌ 1 no encontradas · 🚩 1 retractadas

## 🚩 Retractadas
### `Retractada`   ← Wakefield et al. 1998, The Lancet
- ⚠️ el paper fue RETRACTADO (Retraction Watch, vía OpenAlex/Crossref)
## ❌ No encontradas
### `Inventada`    ← no existe en ninguna base
## ⚠️ Existe, pero con datos distintos
### `Hibrida`
- ⚠️ en ese volumen/página hay otro paper: «Detection of Continuum Submillimeter
     Emission Associated with Candidate Protoplanets» (sim. título 0.27)
### `AnioMal`
- ⚠️ año: bib 2015, real 2018
### `DoiRoto`
- ⚠️ el DOI del bib (10.1088/0004-637X/799/1/99999) no existe
## 📄 Cruce con el .tex
- `NoExiste` — primer uso en `manuscrito.tex:4`
## ✅ Verificadas
| `Preprint` | Keppler (2018) | DOI · ADS · arXiv · PDF | 📄 citas el preprint, pero ya está publicado: … A&A 617 A44 |
```

En una bibliografía real de tesis (136 entradas), la primera corrida tardó unos 12 minutos y verificó 127 entradas automáticamente. Las 9 restantes eran libros mal indexados o reimpresiones, y la revisión manual las confirmó como correctas. Las corridas siguientes usan la caché.

---

## Limitaciones
- **Libros, capítulos, tesis y software** están peor indexados: pueden quedar marcados aunque sean correctos. Para eso existe la revisión manual, y ADS ayuda mucho.
- **Títulos muy cortos** (*Fluid Mechanics*, *Stellar Atmospheres*) pueden emparejarse con otra obra del mismo nombre.
- **La verificación de afirmaciones** parte del abstract. Cuando el abstract no basta, Claude abre el texto completo, pero un paper sin versión abierta puede quedar como "no verificable".
- **Referencias faltantes** se calculan con lo que citan tus referencias en OpenAlex. Son sugerencias, no una revisión bibliográfica.
- **El parche no protege nombres propios** (*Jupiter*, *Kepler*): solo siglas.
- **El parser BibTeX es mínimo:** no resuelve `@string` ni concatenación con `#`.
- **ADS muestra una verificación humana a los navegadores automatizados.** Sus links funcionan para ti, pero Claude no puede abrirlos.

---

## Contribuir
Issues y pull requests son bienvenidos. Son instrucciones en Markdown más un script de Python sin dependencias.

| Archivo | Qué contiene |
|---|---|
| [`SKILL.md`](SKILL.md) | Flujo que sigue Claude: reunir, verificar, revisar a mano, afirmaciones, entregar |
| [`scripts/verificar_bib.py`](scripts/verificar_bib.py) | Parsers, fuentes, veredictos, cruce con `.tex`, parche, huecos e informe |
| [`tests/regresion.py`](tests/regresion.py) | Prueba con red sobre los fixtures de [`tests/`](tests/) |

Antes de proponer un cambio:

```bash
python3 scripts/verificar_bib.py --selftest   # sin red, segundos
python3 tests/regresion.py                    # con red, 1-3 min
```

Si cambias los criterios de coincidencia, agrega a `tests/falsas.bib` la referencia que lo motivó, con su veredicto esperado en `regresion.py`. La CI corre `--selftest` en Python 3.9 y 3.13 en cada push; la regresión con red se lanza a mano (*Actions → tests → Run workflow*).

## Licencia
[MIT](LICENSE)

---

## English summary

**`verificar-bib` is a [Claude Code](https://claude.com/claude-code) skill that checks a bibliography entry by entry. It flags fabricated, retracted or wrong citations, checks that each paper supports what your text claims, and gives you a link to read and download each one.**

1. **Input:** a `.bib` file, a plain-text reference list (e.g. a paper's *References* section) or your LaTeX project.
2. **Automatic check:** looks every entry up by DOI, arXiv id, title, ISBN and year + volume + page in OpenAlex, Crossref, arXiv, OpenLibrary and, with a token, NASA ADS. Each entry ends up ✅ verified, ⚠️ exists but with different data, 🚩 retracted (Retraction Watch data), or ❌ not found.

   The ⚠️ cases include a wrong year, volume or page, a different first author, a nonexistent DOI or one pointing to another paper, and *"a different paper sits at that volume/page"*, the signature of a hallucinated citation that borrows a real journal.

   It also reports:
   - preprints that have since been published;
   - published corrections;
   - duplicates;
   - `\cite` keys missing from the `.bib`, and entries that are never cited;
   - possibly missing references: papers cited by several of your references but not by you.
3. **Manual review:** Claude checks every flagged entry with a web search before calling it fake. It accounts for the usual false negatives: books, software, pre-1990 papers and conference versions with the same title.
4. **Claim check** *(deep mode)*: for every `\cite`, compares the sentence in your `.tex` with the paper's abstract, and with the full text when needed. Each use is classified as supports / partially / does not support.
5. **Report and patch:** a Markdown/JSON report with DOI · ADS · arXiv · open-access PDF · ISBN links. Optionally, a corrected `.bib` written to a separate file: it adds DOIs and eprints, protects acronyms in braces and can use AAS journal macros. It never changes years, volumes or pages on its own.

There is no API key or Python package to install. A local cache makes reruns near-instant, and arXiv is queried politely (once every 3 s). The `--strict` flag returns exit code 1 for use before submission. It writes in your language (Spanish by default).

**Install:**
```bash
git clone https://github.com/ccanunez/verificar-bib.git ~/.claude/skills/verificar-bib
```
**Use:** `/verificar-bib refs.bib`, `/verificar-bib manuscript/ profundo`, or just ask Claude *"check my references for fake citations"*.
