# verificar-bib

**Una skill para [Claude Code](https://claude.com/claude-code) que revisa una bibliografía referencia por referencia, detecta citas falsas o con datos erróneos y te entrega el link para leer y descargar cada paper.**

Una bibliografía acumula errores sin que nadie lo note: años cambiados, volúmenes o páginas mal copiados, DOIs que apuntan a otro artículo, autores en otro orden. Con los asistentes de IA aparece además un problema nuevo, las **citas inventadas**: referencias con autores plausibles, una revista real y un título que no existe. `verificar-bib` contrasta cada entrada con [OpenAlex](https://openalex.org), [Crossref](https://www.crossref.org) y [arXiv](https://arxiv.org), y después Claude revisa a mano lo dudoso antes de declarar falsa una cita.

Funciona con cualquier disciplina, aunque los enlaces a ADS la hacen especialmente útil en astronomía y física.

*English summary at the [end of this file](#english-summary).*

---

## Qué hace

| Paso | Qué ocurre |
|---|---|
| **1. Reunir las referencias** | Acepta un `.bib`, una lista en texto (la sección *References* de un paper, pegada o extraída de un PDF) o un proyecto LaTeX. En este último caso también cruza las claves de `\cite{}` con las del `.bib`. |
| **2. Verificación automática** | `scripts/verificar_bib.py` busca cada referencia por DOI, id de arXiv, título y año + volumen + página. Compara título, primer autor, año, volumen, página y DOI. |
| **3. Revisión manual** | Claude revisa cada referencia marcada con búsqueda web y conoce los falsos negativos típicos: libros, software, papers antiguos sin DOI y preprints recientes. |
| **4. Informe** | Escribe un informe en Markdown con el veredicto de cada referencia, los campos a corregir y los links. Solo edita tu `.bib` si se lo pides. |

### Veredictos
- ✅ **Verificada:** coinciden título, primer autor, año (±1, por la diferencia entre arXiv y la versión publicada), volumen y página.
- ⚠️ **Existe, pero con datos distintos:** año, volumen o página erróneos; el primer autor real es otro; el DOI no existe o apunta a otro paper; o **en ese volumen y página hay otro paper con otro título**, el patrón típico de una cita inventada que reutiliza una revista real.
- ❌ **No encontrada:** ninguna base la tiene, así que es una posible cita falsa. Claude la busca a mano antes de concluir nada.

### Links que entrega
Para cada paper encontrado: **DOI** · **ADS** · **arXiv** · **PDF de acceso abierto** (cuando existe). Para lo que no encuentra, deja búsquedas listas en Google Scholar y ADS.

### Principios
- **No acusa sin evidencia.** Que una referencia no esté en una base de datos no la vuelve falsa. Una cita solo se declara falsa si, por ejemplo, su DOI o su volumen y página pertenecen a otro paper y no existe nada con ese título.
- **No toca tu `.bib` sin permiso.** Propone las correcciones como parche.
- **No descarga PDFs** a menos que lo pidas: entrega los links.
- **No necesita API keys.**

---

## Instalación

Requiere [Claude Code](https://docs.claude.com/en/docs/claude-code) (CLI, app de escritorio o extensión de IDE), `python3` 3.9 o superior y `curl`. No usa paquetes externos de Python.

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

### Opcional: API key de OpenAlex
Sin key, OpenAlex comparte un cupo diario pequeño entre todos los usuarios de tu misma IP. Para bibliografías grandes o uso frecuente, crea una key gratuita en [openalex.org/settings/api](https://openalex.org/settings/api) y expórtala:

```bash
export OPENALEX_API_KEY=tu_key
```

---

## Uso

Desde Claude Code:

```
/verificar-bib refs.bib
```

O simplemente pide *"verifica mi bibliografía"*, *"¿son reales estas referencias?"* o *"dame los links de estos papers"*, y adjunta el archivo o pega la lista.

El script también funciona solo, sin Claude:

```bash
python3 ~/.claude/skills/verificar-bib/scripts/verificar_bib.py refs.bib --out informe.md
python3 ~/.claude/skills/verificar-bib/scripts/verificar_bib.py referencias.txt   # una referencia por línea
python3 ~/.claude/skills/verificar-bib/scripts/verificar_bib.py --selftest        # pruebas sin red
```

### Ejemplo
Un `.bib` de prueba con cuatro trampas: un paper inventado, un título inventado sobre un volumen y una página reales, un año cambiado y un DOI inexistente.

```
4 entradas · ✅ 0 verificadas · ⚠️ 3 con discrepancias · ❌ 1 no encontradas (posibles citas falsas)

### `Fake2020` (article)            ← no existe en ninguna base
### `Hibrida2019`
- ⚠️ en ese volumen/página hay otro paper: «Detection of Continuum Submillimeter
     Emission Associated with Candidate Protoplanets» (sim. título 0.27)
### `AnioMal`
- ⚠️ año: bib 2015, real 2018
- Leer/descargar: DOI · ADS · arXiv · PDF
### `DoiRoto`
- ⚠️ el DOI del bib (10.1088/0004-637X/799/1/99999) no existe
```

En una bibliografía real de tesis (136 entradas) tardó unos 12 minutos. Verificó 127 entradas automáticamente; las 9 restantes eran libros mal indexados o reimpresiones, y la revisión manual las confirmó como correctas.

---

## Limitaciones
- **Libros, capítulos, tesis y software** están mal indexados: suelen quedar marcados aunque sean correctos. Por eso existe la revisión manual.
- **Títulos muy cortos** (*Fluid Mechanics*, *Stellar Atmospheres*) pueden emparejarse con otra obra del mismo nombre.
- **Verifica que la referencia exista, no que diga lo que tu texto afirma.** Si lo pides, Claude puede abrir los papers y comprobar citas concretas, pero es lento.
- **El parser BibTeX es mínimo:** no resuelve `@string` ni concatenación con `#`.
- **arXiv limita la tasa de consultas.** En bibliografías largas algunos papers pueden quedar sin el link a arXiv; el informe avisa cuando falla alguna consulta.
- **ADS muestra una verificación humana a los navegadores automatizados.** Sus links funcionan para ti, pero Claude no puede abrirlos.

---

## Contribuir
Issues y pull requests son bienvenidos. Son instrucciones en Markdown más un script de Python sin dependencias.

| Archivo | Qué contiene |
|---|---|
| [`SKILL.md`](SKILL.md) | Flujo que sigue Claude: reunir, verificar, revisar a mano, entregar |
| [`scripts/verificar_bib.py`](scripts/verificar_bib.py) | Parser BibTeX y de texto, consultas a OpenAlex/Crossref/arXiv, veredictos e informe |

Si propones un cambio en los criterios de coincidencia, incluye la referencia que lo motivó y corre `--selftest`.

## Licencia
[MIT](LICENSE)

---

## English summary

**`verificar-bib` is a [Claude Code](https://claude.com/claude-code) skill that checks a bibliography entry by entry, flags fabricated or wrong citations and gives you a link to read and download each paper.**

1. **Input:** a `.bib` file, a plain-text reference list (e.g. a paper's *References* section) or a LaTeX project. For a LaTeX project it also cross-checks `\cite{}` keys against the `.bib`.
2. **Automatic check:** `scripts/verificar_bib.py` looks every entry up by DOI, arXiv id, title and year + volume + page in OpenAlex, Crossref and arXiv. It compares title, first author, year, volume, page and DOI. Each entry ends up ✅ verified, ⚠️ exists but with different data, or ❌ not found.

   The ⚠️ cases include a nonexistent DOI, a DOI pointing to another paper, a different first author and *"a different paper sits at that volume/page"*, the classic signature of a hallucinated citation that borrows a real journal.
3. **Manual review:** Claude checks every flagged entry with a web search before calling it fake. It accounts for the usual false negatives: books, software, pre-1990 papers without a DOI and recent preprints.
4. **Report:** a Markdown report with DOI · ADS · arXiv · open-access PDF links for every paper and the corrected BibTeX fields. Your `.bib` is never edited without permission.

No API keys or Python packages are needed. An optional free `OPENALEX_API_KEY` raises the rate limit. It writes in your language (Spanish by default).

**Install:**
```bash
git clone https://github.com/ccanunez/verificar-bib.git ~/.claude/skills/verificar-bib
```
**Use:** `/verificar-bib refs.bib`, or just ask Claude *"check my references for fake citations"*.
