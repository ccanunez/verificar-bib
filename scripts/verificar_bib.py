"""Verifica una bibliografía (.bib o lista de referencias en texto) contra OpenAlex,
Crossref y arXiv, marca posibles citas falsas y entrega links para leer/descargar.

Uso: python3 verificar_bib.py <refs.bib|refs.txt> [--out informe.md] [--hilos 4]
     python3 verificar_bib.py --selftest
Sin API keys. Requiere curl.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher

OA = "https://api.openalex.org"
CR = "https://api.crossref.org"
AX = "https://export.arxiv.org/api/query"
OA_SELECT = "doi,title,publication_year,authorships,biblio,primary_location,best_oa_location,locations"
UMBRAL = 0.85  # similitud de título para considerar que es el mismo paper
q = urllib.parse.quote
FALLOS = []  # consultas que fallaron por cupo/servidor, no por inexistencia


def get(url, texto=False):
    if url.startswith(OA) and os.environ.get("OPENALEX_API_KEY"):  # sin key: cupo diario compartido por IP
        url += ("&" if "?" in url else "?") + "api_key=" + os.environ["OPENALEX_API_KEY"]
    # curl y no urllib: el Python de python.org en macOS no trae certificados raíz
    r = subprocess.run(["curl", "-sL", "-m", "30", "--retry", "3", "-A", "verificar-bib/1.0",
                        "-w", "\n%{http_code}", url], capture_output=True, text=True)
    cuerpo, _, codigo = r.stdout.rpartition("\n")
    if r.returncode or codigo == "429" or codigo.startswith("5"):
        FALLOS.append(f"{codigo or 'curl ' + str(r.returncode)} {url.split('?')[0]}")  # no es "no existe"
        return None
    if not codigo.startswith("2"):
        return None
    r.stdout = cuerpo
    if texto:
        return r.stdout
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


# ---------- parseo ----------

def parse_bib(t):
    """Parser BibTeX mínimo: llaves anidadas, comillas y valores numéricos.
    shortcut: ignora @string y concatenación con #; usar bibtexparser si el .bib los usa."""
    out, fin = [], 0
    for m in re.finditer(r"@(\w+)\s*\{", t):
        if m.start() < fin or m.group(1).lower() in ("string", "comment", "preamble"):
            continue
        try:
            i = t.index(",", m.end())
            e = {"clave": t[m.end():i].strip(), "tipo": m.group(1).lower()}
            i += 1
            while True:
                while t[i] in " \t\r\n,":
                    i += 1
                if t[i] == "}":
                    break
                k = t.index("=", i)
                nombre, i = t[i:k].strip().lower(), k + 1
                while t[i] in " \t\r\n":
                    i += 1
                if t[i] in '{"':
                    cierre, nivel, s = ("}" if t[i] == "{" else '"'), 0, i + 1
                    i += 1
                    while not (t[i] == cierre and nivel == 0):
                        nivel += (t[i] == "{") - (t[i] == "}")
                        i += 1
                    e[nombre], i = t[s:i], i + 1
                else:
                    s = i
                    while t[i] not in ",}\n":
                        i += 1
                    e[nombre] = t[s:i].strip()
            fin = i
            out.append(e)
        except (ValueError, IndexError):
            out.append({"clave": t[m.end():m.end() + 40].split(",")[0].strip(), "tipo": "error-de-parseo"})
    return out


def parse_texto(t):
    """Una referencia por párrafo (si hay líneas en blanco) o por línea."""
    bloques = re.split(r"\n\s*\n", t) if re.search(r"\n\s*\n", t.strip()) else t.splitlines()
    refs = [re.sub(r"^\s*(\[\d+\]|\d+[.)])\s*", "", " ".join(b.split())) for b in bloques]
    return [{"clave": f"ref{n}", "tipo": "texto", "raw": r} for n, r in enumerate((r for r in refs if len(r) > 15), 1)]


# ---------- normalización ----------

def limpiar(s):
    s = re.sub(r"\\[`'^\"~=.]\s*\{?([A-Za-z])\}?", r"\1", s or "")      # \"u, \'{e}
    s = re.sub(r"\\[uvHtcdbk]\s*\{([A-Za-z])\}", r"\1", s)              # \v{c}
    s = s.replace("\\&", "&")
    s = re.sub(r"\\[a-zA-Z]+\*?", " ", s)
    return " ".join(s.replace("{", "").replace("}", "").replace("~", " ").split())


def norm(s):
    s = "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())


def primer_apellido(e):
    if e.get("author"):
        a = limpiar(e["author"]).split(" and ")[0]
        return norm(a.split(",")[0] if "," in a else a.split()[-1])
    m = re.match(r"\s*([^,\d(]+)", e.get("raw", ""))
    return norm(m.group(1)).split()[-1] if m and norm(m.group(1)) else ""


def anio(e):
    m = re.search(r"\b(1[89]\d\d|20\d\d)\b", e.get("year") or e.get("date") or e.get("raw", ""))
    return int(m.group(1)) if m else None


def digitos(s):
    return re.sub(r"\D", "", str(s or "").split("-")[0].split("–")[0])


# ---------- fuentes ----------

def de_openalex(w):
    b, oa = w.get("biblio") or {}, w.get("best_oa_location") or {}
    arxiv = next((l["landing_page_url"].replace("http://", "https://") for l in w.get("locations") or []
                  if "arxiv.org" in (l.get("landing_page_url") or "")), None)
    return {"fuente": "OpenAlex", "title": w.get("title") or "", "year": w.get("publication_year"),
            "authors": [a["author"]["display_name"] for a in w.get("authorships") or []],
            "journal": ((w.get("primary_location") or {}).get("source") or {}).get("display_name"),
            "volume": b.get("volume"), "page": b.get("first_page"),
            "doi": (w.get("doi") or "").removeprefix("https://doi.org/") or None,
            "pdf": oa.get("pdf_url"), "oa": oa.get("landing_page_url"), "arxiv": arxiv}


def de_crossref(it):
    return {"fuente": "Crossref", "title": (it.get("title") or [""])[0],
            "year": ((it.get("issued") or {}).get("date-parts") or [[None]])[0][0],
            "authors": [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in it.get("author") or []],
            "journal": (it.get("container-title") or [None])[0], "volume": it.get("volume"),
            "page": (it.get("page") or it.get("article-number") or "").split("-")[0] or None,
            "doi": it.get("DOI"), "pdf": None, "oa": None, "arxiv": None}


def arxiv(query):
    x = get(f"{AX}?{query}", texto=True)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        entradas = ET.fromstring(x).findall("a:entry", ns) if x else []
    except ET.ParseError:
        return []
    out = []
    for e in entradas:
        url = e.findtext("a:id", "", ns).replace("http://", "https://")
        out.append({"fuente": "arXiv", "title": " ".join(e.findtext("a:title", "", ns).split()),
                    "year": int(e.findtext("a:published", "0", ns)[:4] or 0),
                    "authors": [a.findtext("a:name", "", ns) for a in e.findall("a:author", ns)],
                    "journal": "arXiv", "volume": None, "page": None,
                    "doi": e.findtext("{http://arxiv.org/schemas/atom}doi", None, ns),
                    "pdf": re.sub(r"v\d+$", "", url.replace("/abs/", "/pdf/")), "oa": None,
                    "arxiv": re.sub(r"v\d+$", "", url)})
    return out


def vol_pag(e):
    """(volumen, página) del bib o, en texto libre, los dos primeros números tras el año."""
    if not e.get("raw"):
        return e.get("volume"), re.split(r"[-–]", e.get("pages") or "")[0].strip() or None
    a = anio(e)
    resto = e["raw"].split(str(a), 1)[-1] if a else ""
    nums = re.findall(r"\b([A-Z]?\d{1,6})\b", resto)
    return (nums[0], nums[1]) if len(nums) >= 2 else (None, None)


def por_volumen(e):
    """Busca en OpenAlex por año + volumen + primera página (refs sin título, o títulos inventados)."""
    a, (v, p) = anio(e), vol_pag(e)
    if not (a and v and p):
        return []
    out = []
    for pag in dict.fromkeys([p, digitos(p)]):
        f = f"publication_year:{a - 1}-{a + 1},biblio.volume:{digitos(v)},biblio.first_page:{pag}"
        r = get(f"{OA}/works?" + urllib.parse.urlencode({"filter": f, "per-page": 5, "select": OA_SELECT}))
        out += [de_openalex(w) for w in (r or {}).get("results", [])]
        if out:
            break
    return out


def openalex_doi(doi):
    w = get(f"{OA}/works/doi:{q(doi)}?select={OA_SELECT}")
    return de_openalex(w) if w and w.get("title") else None


# ---------- comparación ----------

def sim_titulo(e, c):
    ct = norm(c["title"]).split()
    if not ct:
        return 0.0
    if e.get("title"):
        et = norm(limpiar(e["title"])).split()
        cobertura = len(set(et) & set(ct)) / len(set(et)) if len(et) >= 4 else 0  # bib con título truncado
        return max(SequenceMatcher(None, " ".join(et), " ".join(ct)).ratio(), cobertura)
    raw = set(norm(e.get("raw")).split())  # texto libre: ¿está el título del candidato en la referencia?
    return len(set(ct) & raw) / len(set(ct))


def comparar(e, c):
    s = sim_titulo(e, c)
    ap = primer_apellido(e)
    autor = bool(ap) and any(ap in norm(a) for a in c["authors"][:3])
    primero = norm(c["authors"][0]) if c["authors"] else ""
    # nombre no latino (p.ej. «王») no se puede comparar: no penalizar el orden
    orden_ok = not (autor and primero) or ap in primero
    ea, ca = anio(e), c["year"]
    anio_ok = ea is None or ca is None or abs(ea - ca) <= 1  # arXiv vs publicado
    if e.get("raw"):
        toks = set(norm(e["raw"]).split())
        vol_ok = bool(c["volume"]) and norm(str(c["volume"])) in toks
        pag_ok = bool(digitos(c["page"])) and digitos(c["page"]) in {digitos(x) for x in toks}
    else:
        vol_ok = bool(e.get("volume") and c["volume"]) and digitos(e["volume"]) == digitos(c["volume"])
        pag_ok = bool(e.get("pages") and c["page"]) and digitos(e["pages"]) == digitos(c["page"])
    puntaje = s + 0.3 * autor + 0.2 * (anio_ok and ea is not None) + 0.15 * vol_ok + 0.15 * pag_ok
    return {"sim": s, "autor": autor, "orden_ok": orden_ok, "anio_ok": anio_ok, "vol_ok": vol_ok, "pag_ok": pag_ok, "puntaje": puntaje}


def elegir(e, cands):
    puntuados = [(comparar(e, c), c) for c in cands if c and c.get("title")]
    return max(puntuados, key=lambda p: p[0]["puntaje"], default=(None, None))


def veredicto(e, m, c, doi_bib, doi_roto):
    """('OK'|'DISCREPANCIA'|'NO ENCONTRADA', [problemas])"""
    probs = []
    if doi_roto:
        probs.append(f"el DOI del bib ({doi_bib}) no existe")
    if not c:
        return "NO ENCONTRADA", probs
    venue = m["vol_ok"] and m["pag_ok"]
    mismo = m["sim"] >= UMBRAL or (e.get("raw") and venue and m["autor"])
    if not mismo:
        if venue and m["anio_ok"]:
            return "DISCREPANCIA", probs + [f"en ese volumen/página hay otro paper: «{c['title']}» (sim. título {m['sim']:.2f})"]
        return "NO ENCONTRADA", probs
    if not m["autor"]:
        probs.append(f"primer autor no coincide (bib: {primer_apellido(e) or '?'}; real: {', '.join(c['authors'][:3]) or '?'})")
    elif not m["orden_ok"]:
        probs.append(f"orden de autores: el primer autor real es {c['authors'][0]}, no {primer_apellido(e)}")
    reimpresion = e["tipo"] in ("book", "inbook", "incollection") and (c["year"] or 0) > (anio(e) or 9999)
    if not m["anio_ok"] and not reimpresion:  # libros: las bases suelen indexar la reimpresión
        probs.append(f"año: bib {anio(e)}, real {c['year']}")
    if not e.get("raw"):
        if e.get("volume") and c["volume"] and not m["vol_ok"]:
            probs.append(f"volumen: bib {e['volume']}, real {c['volume']}")
        if e.get("pages") and c["page"] and not m["pag_ok"]:
            probs.append(f"página: bib {e['pages']}, real {c['page']}")
    if doi_bib and c["doi"] and doi_bib.lower() != c["doi"].lower() and not doi_roto:
        probs.append(f"DOI del bib ({doi_bib}) apunta a otro paper; el correcto es {c['doi']}")
    return ("DISCREPANCIA" if probs else "OK"), probs


def verificar(e):
    if e["tipo"] == "error-de-parseo":
        return e, "NO ENCONTRADA", ["no se pudo parsear la entrada"], None, None
    titulo = limpiar(e.get("title", ""))
    doi_bib = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", (e.get("doi") or "").strip(), flags=re.I) or None
    cands, doi_roto = [], False
    if doi_bib:
        c = openalex_doi(doi_bib)
        if not c:
            it = get(f"{CR}/works/{q(doi_bib)}")
            c = de_crossref(it["message"]) if it else None
        doi_roto = c is None
        cands.append(c)
    eprint = e.get("eprint") or (re.search(r"arxiv[:/ ]*(\d{4}\.\d{4,5}|[a-z\-]+/\d{7})", e.get("raw", ""), re.I) or [None, None])[1]
    if eprint:
        cands += arxiv("id_list=" + q(eprint))
    if titulo:
        r = get(f"{OA}/works?" + urllib.parse.urlencode({"search": titulo, "per-page": 5, "select": OA_SELECT}))
        cands += [de_openalex(w) for w in (r or {}).get("results", [])]
    if e.get("raw"):
        cands += por_volumen(e)
    m, c = elegir(e, cands)
    if not c or veredicto(e, m, c, doi_bib, doi_roto)[0] != "OK":
        if not e.get("raw"):
            cands += por_volumen(e)
        consulta = e.get("raw") or " ".join(filter(None, [titulo, primer_apellido(e), str(anio(e) or ""),
                                                          limpiar(e.get("journal", ""))]))
        r = get(f"{CR}/works?" + urllib.parse.urlencode({"query.bibliographic": consulta, "rows": 5}))
        cands += [de_crossref(it) for it in ((r or {}).get("message") or {}).get("items", [])]
        if titulo:
            cands += arxiv("search_query=" + q(f'ti:"{titulo}"') + "&max_results=3")
        m, c = elegir(e, cands)
    v, probs = veredicto(e, m, c, doi_bib, doi_roto)
    if v != "NO ENCONTRADA" and c:
        if c["fuente"] != "OpenAlex" and c["doi"]:  # completar links de acceso abierto
            extra = openalex_doi(c["doi"])
            if extra:
                c = {**c, **{k: c[k] or extra[k] for k in ("pdf", "oa", "arxiv", "journal", "volume", "page")}}
        if not c["arxiv"]:
            pre = [p for p in arxiv("search_query=" + q(f'ti:"{c["title"]}"') + "&max_results=2")
                   if SequenceMatcher(None, norm(p["title"]), norm(c["title"])).ratio() >= UMBRAL]
            if pre:
                c = {**c, "arxiv": pre[0]["arxiv"], "pdf": c["pdf"] or pre[0]["pdf"]}
    return e, v, probs, m, c


# ---------- informe ----------

def busqueda(t):
    return (f"[Google Scholar](https://scholar.google.com/scholar?q={q(t)}) · "
            f"[ADS](https://ui.adsabs.harvard.edu/search/q={q(t)})")


def links(c, e):
    if not c:
        return busqueda(limpiar(e.get("title", "")) or e.get("raw", ""))
    ls = []
    if c["doi"]:
        ls.append(f"[DOI](https://doi.org/{c['doi']})")
        ls.append(f"[ADS](https://ui.adsabs.harvard.edu/abs/{c['doi']}/abstract)")
    if c["arxiv"]:
        ls.append(f"[arXiv]({c['arxiv']})")
    if c["pdf"]:
        ls.append(f"[PDF]({c['pdf']})")
    elif c["oa"] and c["oa"] != c["arxiv"]:
        ls.append(f"[acceso abierto]({c['oa']})")
    return " · ".join(ls) if c["doi"] or c["arxiv"] else " · ".join(ls + [busqueda(c["title"])])


def describir_bib(e):
    if e.get("raw"):
        return e["raw"]
    return (f"{limpiar(e.get('author', '?'))} ({e.get('year', '?')}). «{limpiar(e.get('title', '?'))}». "
            f"{limpiar(e.get('journal', e.get('booktitle', '')))} {e.get('volume', '')} {e.get('pages', '')}").strip()


def describir_real(c):
    a = ", ".join(c["authors"][:3]) + (" et al." if len(c["authors"]) > 3 else "")
    return f"{a} ({c['year']}). «{c['title']}». {c['journal'] or ''} {c['volume'] or ''} {c['page'] or ''}".strip()


def informe(nombre, res):
    g = {v: [r for r in res if r[1] == v] for v in ("NO ENCONTRADA", "DISCREPANCIA", "OK")}
    L = [f"# Verificación de bibliografía — `{nombre}`", "",
         f"{len(res)} entradas · ✅ {len(g['OK'])} verificadas · ⚠️ {len(g['DISCREPANCIA'])} con discrepancias · "
         f"❌ {len(g['NO ENCONTRADA'])} no encontradas (posibles citas falsas)", ""]
    if FALLOS:
        L += [f"> **Aviso:** {len(FALLOS)} consultas fallaron por cupo o error del servidor (p.ej. `{FALLOS[0]}`). "
              "Las ❌ pueden ser falsos negativos: repetir más tarde o con `OPENALEX_API_KEY`.", ""]
    if g["NO ENCONTRADA"]:
        L += ["## ❌ No encontradas — posibles citas falsas", ""]
        for e, _, probs, m, c in g["NO ENCONTRADA"]:
            L += [f"### `{e['clave']}` ({e['tipo']})", f"- **El bib dice:** {describir_bib(e)}"]
            L += [f"- {p}" for p in probs]
            L += [f"- **Buscar a mano:** {links(None, e)}", ""]
    if g["DISCREPANCIA"]:
        L += ["## ⚠️ Existe, pero con datos distintos", ""]
        for e, _, probs, m, c in g["DISCREPANCIA"]:
            L += [f"### `{e['clave']}`", f"- **El bib dice:** {describir_bib(e)}",
                  f"- **Paper real** ({c['fuente']}): {describir_real(c)}"]
            L += [f"- ⚠️ {p}" for p in probs]
            L += [f"- **Leer/descargar:** {links(c, e)}", ""]
    if g["OK"]:
        L += ["## ✅ Verificadas", "", "| clave | paper | leer / descargar |", "|---|---|---|"]
        for e, _, _, m, c in g["OK"]:
            a = (c["authors"] or ["?"])[0].split()[-1]
            L.append(f"| `{e['clave']}` | {a} ({c['year']}) — {c['title'].replace('|', '/')} | {links(c, e)} |")
    return "\n".join(L) + "\n"


# ---------- main ----------

def selftest():
    bib = r"""@article{A, author = {M\"uller, A. and others}, title = {Dust in {PDS} 70},
      journal = "ApJ", volume = 5, pages = {L2--L9}, year = {2021}}
      @string{x = "y"}
      @misc{B,title={Nested {a {b}} c},}"""
    es = parse_bib(bib)
    assert [e["clave"] for e in es] == ["A", "B"], es
    assert limpiar(es[0]["author"]) == "Muller, A. and others" and es[0]["volume"] == "5", es[0]
    assert es[1]["title"] == "Nested {a {b}} c", es[1]
    assert primer_apellido(es[0]) == "muller" and anio(es[0]) == 2021
    c = {"title": "Dust in PDS 70", "authors": ["Anna Müller"], "year": 2021, "volume": "5", "page": "L2",
         "doi": None, "fuente": "x", "journal": "ApJ"}
    assert veredicto(es[0], comparar(es[0], c), c, None, False) == ("OK", [])
    c2 = {**c, "year": 2015}
    assert veredicto(es[0], comparar(es[0], c2), c2, None, False)[0] == "DISCREPANCIA"
    t = parse_texto("[1] Müller, A. et al. 2021, ApJ, 5, L2\n[2] Otro, B. 2020, A&A, 600, A1")
    assert len(t) == 2 and primer_apellido(t[0]) == "muller", t
    assert vol_pag(t[1]) == ("600", "A1") and vol_pag(es[0]) == ("5", "L2"), (vol_pag(t[1]), vol_pag(es[0]))
    assert veredicto(t[0], comparar(t[0], c), c, None, False)[0] == "OK"
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("archivo", nargs="?")
    ap.add_argument("--out", help="escribir el informe Markdown aquí (por defecto, stdout)")
    ap.add_argument("--hilos", type=int, default=4)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.archivo:
        ap.error("falta el archivo")
    t = open(a.archivo, encoding="utf-8", errors="replace").read()
    entradas = parse_bib(t) if re.search(r"@\w+\s*\{", t) else parse_texto(t)
    print(f"{len(entradas)} referencias; consultando OpenAlex/Crossref/arXiv…", file=sys.stderr)
    with ThreadPoolExecutor(a.hilos) as ex:
        res = list(ex.map(verificar, entradas))
    md = informe(a.archivo.rsplit("/", 1)[-1], res)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(md)
        n = {v: sum(r[1] == v for r in res) for v in ("OK", "DISCREPANCIA", "NO ENCONTRADA")}
        print(f"informe en {a.out}: {n}", file=sys.stderr)
    else:
        print(md)


if __name__ == "__main__":
    main()
