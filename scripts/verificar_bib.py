"""Verifica una bibliografía (.bib o lista de referencias en texto) contra OpenAlex, Crossref,
arXiv, OpenLibrary y, con token, NASA ADS. Marca citas falsas, retractadas o con datos erróneos
y entrega links para leer/descargar. Además puede cruzarla con los .tex, extraer el contexto de
cada cita (para verificar afirmaciones), escribir un .bib corregido y sugerir referencias faltantes.

Uso:
  python3 verificar_bib.py refs.bib [--out informe.md] [--json res.json]
          [--tex manuscrito/] [--contextos contextos.md]
          [--parche refs.corregido.bib] [--macros-aas] [--huecos 20]
          [--strict] [--sin-cache] [--hilos 4]
  python3 verificar_bib.py --selftest
Variables opcionales: OPENALEX_API_KEY (más cupo), ADS_API_TOKEN (activa ADS). Requiere curl.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher

OA = "https://api.openalex.org"
CR = "https://api.crossref.org"
AX = "https://export.arxiv.org/api/query"
OL = "https://openlibrary.org/search.json"
ADS = "https://api.adsabs.harvard.edu/v1/search/query"
HANDLE = "https://doi.org/api/handles/"
OA_SELECT = ("id,doi,title,publication_year,authorships,biblio,primary_location,"
             "best_oa_location,locations,is_retracted,type")
ADS_FL = "bibcode,title,author,year,pub,volume,page,doi,identifier,abstract"
LIBROS = ("book", "inbook", "incollection", "phdthesis", "mastersthesis")
UMBRAL = 0.85  # similitud de título para considerar que es el mismo paper
CACHE = os.path.expanduser("~/.cache/verificar-bib")
TTL = 30 * 86400
USAR_CACHE = [True]
FALLOS = []         # consultas que fallaron por cupo/servidor: no significan "no existe"
NO_EXISTE = set()   # URLs que respondieron 404
_404 = "\x00404"
_AX_LOCK, _AX_T = threading.Lock(), [0.0]
q = urllib.parse.quote
AVISOS = {"correction": "corrección publicada", "erratum": "fe de erratas", "corrigendum": "corrigendum",
          "addendum": "addendum", "clarification": "aclaración",
          "expression_of_concern": "🚩 expresión de preocupación del editor"}


# ---------- red (con caché) ----------

def _leer(cuerpo, texto):
    if cuerpo == _404:
        return None
    if texto:
        return cuerpo
    try:
        return json.loads(cuerpo)
    except json.JSONDecodeError:
        return None


def get(url, texto=False, headers=()):
    h = hashlib.sha1(url.encode()).hexdigest()
    ruta = os.path.join(CACHE, h[:2], h)
    if USAR_CACHE[0]:
        try:
            if time.time() - os.path.getmtime(ruta) < TTL:
                with open(ruta, encoding="utf-8") as f:
                    cuerpo = f.read()
                if cuerpo == _404:
                    NO_EXISTE.add(url)
                return _leer(cuerpo, texto)
        except OSError:
            pass
    real = url
    if url.startswith(OA) and os.environ.get("OPENALEX_API_KEY"):  # sin key: cupo diario compartido por IP
        real += ("&" if "?" in url else "?") + "api_key=" + os.environ["OPENALEX_API_KEY"]
    # curl y no urllib: el Python de python.org en macOS no trae certificados raíz
    # OpenAlex sin key, al agotar el cupo, a veces deja la conexión colgada: fallar rápido (~1 min máx.)
    cmd = ["curl", "-sL", "-m", "25", "--retry", "2", "--retry-max-time", "60", "-A", "verificar-bib/2.0",
           "-w", "\n%{http_code}"]
    if headers:
        cmd += ["-H", "@-"]  # por stdin: el token no queda en la lista de procesos
    cmd.append(real)

    def correr():
        # respaldo de -m: tras suspender el equipo, curl puede quedar colgado indefinidamente
        try:
            return subprocess.run(cmd, input="\n".join(headers), capture_output=True, text=True, timeout=150)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(cmd, 28, "\n", "")

    if url.startswith(AX):
        with _AX_LOCK:  # arXiv pide >= 3 s entre consultas
            time.sleep(max(0.0, 3 - (time.time() - _AX_T[0])))
            r = correr()
            _AX_T[0] = time.time()
    else:
        r = correr()
    cuerpo, _, codigo = r.stdout.rpartition("\n")
    if r.returncode or codigo == "429" or codigo.startswith("5"):
        FALLOS.append(f"{codigo or 'curl ' + str(r.returncode)} {url.split('?')[0]}")
        return None
    if codigo == "404":
        NO_EXISTE.add(url)
        cuerpo = _404
    elif not codigo.startswith("2"):
        return None
    valor = _leer(cuerpo, texto)
    if USAR_CACHE[0] and (valor is not None or cuerpo == _404):
        try:
            os.makedirs(os.path.dirname(ruta), exist_ok=True)
            tmp = f"{ruta}.{threading.get_ident()}"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(cuerpo)
            os.replace(tmp, ruta)
        except OSError:
            pass
    return valor


# ---------- parseo ----------

def parse_bib(t):
    """Parser BibTeX mínimo: llaves anidadas, comillas y valores numéricos. Guarda la posición
    de cada valor (_pos) y del cierre de la entrada (_fin) para poder parchear el archivo.
    shortcut: ignora @string y concatenación con #; usar bibtexparser si el .bib los usa."""
    out, fin = [], 0
    for m in re.finditer(r"@(\w+)\s*\{", t):
        tipo = m.group(1).lower()
        if m.start() < fin or tipo in ("string", "comment", "preamble"):
            continue
        try:
            i = t.index(",", m.end())
            e = {"clave": t[m.end():i].strip(), "tipo": tipo, "_pos": {}}
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
                    e[nombre], e["_pos"][nombre], i = t[s:i], (s, i), i + 1
                else:
                    s = i
                    while t[i] not in ",}\n":
                        i += 1
                    e[nombre], e["_pos"][nombre] = t[s:i].strip(), (s, i)
            e["_fin"], fin = i, i
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


def corporativo(e):
    """Autor colectivo: «{Gaia Collaboration}», «{ALMA Partnership} and …»."""
    a = (e.get("author") or "").strip()
    primero = re.split(r"\s+and\s+", a)[0]
    return bool(re.fullmatch(r"\{[^{}]+\}", primero.strip())) or bool(
        re.search(r"collaboration|partnership|consortium|team\b", primero, re.I))


def primer_apellido(e):
    if e.get("author"):
        a = limpiar(e["author"]).split(" and ")[0]
        return norm(a.split(",")[0] if "," in a else a.split()[-1])
    m = re.match(r"\s*([^,\d(]+)", e.get("raw", ""))
    return norm(m.group(1)).split()[-1] if m and norm(m.group(1)) else ""


def anio(e):
    m = re.search(r"\b(1[6-9]\d\d|20\d\d)\b", e.get("year") or e.get("date") or e.get("raw", ""))
    return int(m.group(1)) if m else None


def digitos(s):
    return re.sub(r"\D", "", str(s or "").split("-")[0].split("–")[0])


def arxiv_id(url):
    m = re.search(r"arxiv\.org/(?:abs|pdf)/(.+?)(?:v\d+)?(?:\.pdf)?$", url or "")
    return m.group(1) if m else None


def es_preprint_bib(e):
    if e.get("raw"):
        antes = re.split(r"arxiv", e["raw"], flags=re.I)
        return len(antes) > 1 and not re.search(r"\b\d{1,4},\s*[A-Z]?\d+\b", antes[0])
    revista = norm(limpiar(e.get("journal", "")))
    return "arxiv" in revista or (not revista and bool(e.get("eprint")) and e["tipo"] not in LIBROS)


# ---------- fuentes ----------

CAMPOS = dict(fuente="", title="", year=None, authors=(), journal=None, volume=None, page=None, doi=None,
              pdf=None, oa=None, arxiv=None, bibcode=None, isbn=None, ol=None, oa_id=None,
              retractado=False, abstract=None, pub_doi=None, preprint=False)


def cand(**kw):
    return {**CAMPOS, **kw}


def abstract_oa(w):
    inv = (w or {}).get("abstract_inverted_index") or {}
    palabras = sorted((i, p) for p, idx in inv.items() for i in idx)
    return " ".join(p for _, p in palabras).removeprefix("Abstract ") or None


def de_openalex(w):
    b, oa = w.get("biblio") or {}, w.get("best_oa_location") or {}
    arxiv = next((l["landing_page_url"].replace("http://", "https://") for l in w.get("locations") or []
                  if "arxiv.org" in (l.get("landing_page_url") or "")), None)
    doi = (w.get("doi") or "").removeprefix("https://doi.org/") or None
    return cand(fuente="OpenAlex", title=w.get("title") or "", year=w.get("publication_year"),
                authors=[a["author"]["display_name"] for a in w.get("authorships") or []],
                journal=((w.get("primary_location") or {}).get("source") or {}).get("display_name"),
                volume=b.get("volume"), page=b.get("first_page"), doi=doi,
                pdf=oa.get("pdf_url"), oa=oa.get("landing_page_url"), arxiv=arxiv,
                oa_id=(w.get("id") or "").rsplit("/", 1)[-1] or None, retractado=bool(w.get("is_retracted")),
                abstract=abstract_oa(w),
                preprint=w.get("type") == "preprint" or (doi or "").lower().startswith("10.48550"))


def de_crossref(it):
    tipos = {(u.get("type") or "").lower() for u in it.get("updated-by") or []}
    return cand(fuente="Crossref", title=(it.get("title") or [""])[0],
                year=((it.get("issued") or {}).get("date-parts") or [[None]])[0][0],
                authors=[f"{a.get('given', '')} {a.get('family', '')}".strip() for a in it.get("author") or []],
                journal=(it.get("container-title") or [None])[0], volume=it.get("volume"),
                page=(it.get("page") or it.get("article-number") or "").split("-")[0] or None,
                doi=it.get("DOI"), retractado=bool(tipos & {"retraction", "withdrawal", "removal"}),
                preprint=it.get("type") == "posted-content")


def de_ads(d):
    ax = next((i.split(":", 1)[1] for i in d.get("identifier") or [] if i.lower().startswith("arxiv:")), None)
    return cand(fuente="ADS", title=(d.get("title") or [""])[0],
                year=int(d["year"]) if str(d.get("year", "")).isdigit() else None,
                authors=d.get("author") or [], journal=d.get("pub"), volume=d.get("volume"),
                page=(d.get("page") or [None])[0], doi=(d.get("doi") or [None])[0], bibcode=d.get("bibcode"),
                arxiv=f"https://arxiv.org/abs/{ax}" if ax else None, pdf=f"https://arxiv.org/pdf/{ax}" if ax else None,
                abstract=d.get("abstract"), preprint="arxiv" in norm(d.get("pub")))


def de_openlibrary(d):
    isbns = d.get("isbn") or []
    return cand(fuente="OpenLibrary", title=": ".join(filter(None, [d.get("title"), d.get("subtitle")])),
                year=d.get("first_publish_year"), authors=d.get("author_name") or [],
                journal=(d.get("publisher") or [None])[0],
                isbn=next((i for i in isbns if len(i) == 13), isbns[0] if isbns else None),
                ol="https://openlibrary.org" + d["key"] if d.get("key") else None)


def arxiv(query):
    x = get(f"{AX}?{query}", texto=True)
    ns = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}
    try:
        entradas = ET.fromstring(x).findall("a:entry", ns) if x else []
    except ET.ParseError:
        return []
    out = []
    for e in entradas:
        url = re.sub(r"v\d+$", "", e.findtext("a:id", "", ns).replace("http://", "https://"))
        if "/abs/" not in url:
            continue
        out.append(cand(fuente="arXiv", title=" ".join(e.findtext("a:title", "", ns).split()),
                        year=int(e.findtext("a:published", "0", ns)[:4] or 0) or None,
                        authors=[a.findtext("a:name", "", ns) for a in e.findall("a:author", ns)],
                        journal="arXiv", arxiv=url, pdf=url.replace("/abs/", "/pdf/"),
                        abstract=" ".join(e.findtext("a:summary", "", ns).split()) or None,
                        pub_doi=e.findtext("x:doi", None, ns), preprint=True))
    return out


def openalex_doi(doi, campos=OA_SELECT):
    w = get(f"{OA}/works/doi:{q(doi)}?select={campos}")
    return de_openalex(w) if w and w.get("title") else None


def crossref_doi(doi):
    it = get(f"{CR}/works/{q(doi)}")
    return de_crossref(it["message"]) if it and it.get("message") else None


def ads(consulta, filas=5):
    tok = os.environ.get("ADS_API_TOKEN")
    if not tok:
        return []
    r = get(f"{ADS}?" + urllib.parse.urlencode({"q": consulta, "fl": ADS_FL, "rows": filas}),
            headers=(f"Authorization: Bearer {tok}",))
    return [de_ads(d) for d in ((r or {}).get("response") or {}).get("docs", [])]


def ads_buscar(e, doi_bib, titulo):
    if not os.environ.get("ADS_API_TOKEN"):
        return []
    out = []
    if doi_bib:
        out += ads(f'doi:"{doi_bib}"')
    elif titulo:
        out += ads('title:"' + titulo.replace('"', "") + '"')
    a, (v, p) = anio(e), vol_pag(e)
    if e.get("raw") and a and v and p:
        out += ads(f'first_author:"{primer_apellido(e)}" year:{a} volume:{digitos(v)} page:{p}')
    return out


def openlibrary(e):
    t = limpiar(e.get("title", ""))
    if not t:
        return []
    r = get(f"{OL}?" + urllib.parse.urlencode({
        "title": t, "author": primer_apellido(e), "limit": 5,
        "fields": "title,subtitle,author_name,first_publish_year,isbn,key,publisher"}))
    return [de_openlibrary(d) for d in (r or {}).get("docs", [])]


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
    corp = corporativo(e)
    ap = primer_apellido(e)
    autor = corp or (bool(ap) and any(ap in norm(a) for a in list(c["authors"])[:3]))
    primero = norm(c["authors"][0]) if c["authors"] else ""
    # nombre no latino (p.ej. «王») no se puede comparar: no penalizar el orden
    orden_ok = corp or not (autor and primero) or ap in primero
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
    return {"sim": round(s, 3), "autor": autor, "orden_ok": orden_ok, "anio_ok": anio_ok,
            "vol_ok": vol_ok, "pag_ok": pag_ok, "puntaje": round(puntaje, 3)}


def elegir(e, cands):
    pre = es_preprint_bib(e)
    puntuados = [(comparar(e, c), c) for c in cands if c and c.get("title")]
    # a igualdad, preferir la versión (preprint o publicada) que cita el bib
    return max(puntuados, key=lambda p: p[0]["puntaje"] + 0.05 * (p[1]["preprint"] == pre), default=(None, None))


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
        probs.append(f"primer autor no coincide (bib: {primer_apellido(e) or '?'}; real: {', '.join(list(c['authors'])[:3]) or '?'})")
    elif not m["orden_ok"]:
        probs.append(f"orden de autores: el primer autor real es {c['authors'][0]}, no {primer_apellido(e)}")
    if not m["anio_ok"] and e["tipo"] not in LIBROS:  # libros: las bases indexan otras ediciones
        probs.append(f"año: bib {anio(e)}, real {c['year']}")
    if not e.get("raw"):
        if e.get("volume") and c["volume"] and not m["vol_ok"]:
            probs.append(f"volumen: bib {e['volume']}, real {c['volume']}")
        if e.get("pages") and c["page"] and not m["pag_ok"]:
            probs.append(f"página: bib {e['pages']}, real {c['page']}")
    if doi_bib and c["doi"] and doi_bib.lower() != c["doi"].lower() and not doi_roto:
        probs.append(f"DOI del bib ({doi_bib}) apunta a otro paper; el correcto es {c['doi']}")
    return ("DISCREPANCIA" if probs else "OK"), probs


def mismo_titulo(a, b):
    return SequenceMatcher(None, norm(a["title"]), norm(b["title"])).ratio() >= UMBRAL


def completar(e, c, cands, notas):
    """Links de acceso abierto, retractaciones/correcciones, versión publicada y preprint."""
    c = dict(c)
    if c["fuente"] != "OpenAlex" and c["doi"]:
        extra = openalex_doi(c["doi"])
        if extra:
            for k in ("pdf", "oa", "arxiv", "journal", "volume", "page", "oa_id"):
                c[k] = c[k] or extra[k]
            c["retractado"] = c["retractado"] or extra["retractado"]
    if c["doi"] and not c["doi"].lower().startswith("10.48550"):
        it = get(f"{CR}/works/{q(c['doi'])}")
        for u in ((it or {}).get("message") or {}).get("updated-by") or []:
            tipo = (u.get("type") or "").lower().replace("-", "_")
            if tipo in ("retraction", "withdrawal", "removal"):
                c["retractado"] = True
            elif tipo:
                notas.append(f"📝 {AVISOS.get(tipo, tipo)}: https://doi.org/{u.get('DOI')}")
    publicada = lambda x: bool(x and not x["preprint"] and x["journal"] and "arxiv" not in norm(x["journal"]))
    pub = c if publicada(c) else None
    if c["preprint"]:
        pub = next((x for x in cands if publicada(x) and mismo_titulo(x, c)), None)
        if not pub and c["pub_doi"]:
            pub = openalex_doi(c["pub_doi"]) or crossref_doi(c["pub_doi"])
        if pub:
            c = {**pub, "arxiv": c["arxiv"] or pub["arxiv"], "pdf": pub["pdf"] or c["pdf"],
                 "abstract": pub["abstract"] or c["abstract"], "retractado": pub["retractado"] or c["retractado"]}
    if pub and es_preprint_bib(e):
        notas.append(f"📄 citas el preprint, pero ya está publicado: {describir_real(pub)} — DOI {pub['doi']}")
    if not c["arxiv"] and e["tipo"] not in LIBROS and c["title"]:
        pre = [p for p in arxiv("search_query=" + q(f'ti:"{c["title"]}"') + "&max_results=2") if mismo_titulo(p, c)]
        if pre:
            c["arxiv"], c["pdf"] = pre[0]["arxiv"], c["pdf"] or pre[0]["pdf"]
    if e["tipo"] in LIBROS and anio(e) and c["year"] and abs(anio(e) - c["year"]) > 1:
        notas.append(f"📚 la base indexa la edición de {c['year']}; el bib cita la de {anio(e)}")
    return c


def resultado(e, v, probs, notas=(), m=None, c=None):
    return {"e": e, "v": v, "probs": list(probs), "notas": list(notas), "m": m, "c": c}


def verificar(e):
    if e["tipo"] == "error-de-parseo":
        return resultado(e, "NO ENCONTRADA", ["no se pudo parsear la entrada"])
    titulo = limpiar(e.get("title", ""))
    doi_bib = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", (e.get("doi") or "").strip(), flags=re.I) or None
    cands, notas, doi_roto = [], [], False
    if doi_bib:
        c = openalex_doi(doi_bib) or crossref_doi(doi_bib)
        if not c:
            h = HANDLE + q(doi_bib)
            existe = get(h)
            doi_roto = h in NO_EXISTE
            if existe:
                notas.append("el DOI existe pero no está en OpenAlex ni Crossref (¿DataCite/Zenodo?)")
        cands.append(c)
    eprint = e.get("eprint") or (re.search(r"arxiv[:/ ]*(\d{4}\.\d{4,5}|[a-z\-]+/\d{7})",
                                           e.get("raw", ""), re.I) or [None, None])[1]
    if eprint:
        cands += arxiv("id_list=" + q(eprint))
    if titulo:
        r = get(f"{OA}/works?" + urllib.parse.urlencode({"search": titulo, "per-page": 5, "select": OA_SELECT}))
        cands += [de_openalex(w) for w in (r or {}).get("results", [])]
        if e["tipo"] in LIBROS:
            cands += openlibrary(e)
    cands += ads_buscar(e, doi_bib, titulo)
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
    if v == "NO ENCONTRADA":
        if c:  # no es el paper, pero ayuda a la revisión manual (¿título mal copiado?)
            notas.append(f"candidato más parecido: {describir_real(c)} (sim. título {m['sim']:.2f})")
        return resultado(e, v, probs, notas, m)
    c = completar(e, c, cands, notas)
    if c["retractado"]:
        v = "RETRACTADA"
        probs.insert(0, "el paper fue RETRACTADO (Retraction Watch, vía OpenAlex/Crossref): no citarlo como evidencia")
    return resultado(e, v, probs, notas, m, c)


# ---------- .tex: cruce de claves y contexto de cada cita ----------

CITE = re.compile(r"\\(?:no|auto|paren|text|foot|super|full)?[cC]ite[a-zA-Z]*\*?\s*(?:\[[^\]]*\]\s*){0,2}\{([^}]*)\}")
FIN_FRASE = re.compile(r"(?<!\bal)(?<!e\.g)(?<!i\.e)(?<!\bFig)(?<!\bEq)(?<!\bEc)(?<!\bSec)(?<!\bcf)(?<!\bvs)"
                       r"(?<!\bec)(?<!\bp)\.\s|\n\s*\n|\\(?:(?:sub)*section|chapter|paragraph|caption)\*?\{[^}]*\}"
                       r"|\\(?:begin|end)\{[^}]*\}|\\item\b")


def contexto(t, s, e):
    """La oración que contiene la cita t[s:e] (sin cortar en «et al.», «Fig.», «e.g.»…)."""
    a, b = max(0, s - 600), min(len(t), e + 400)
    ini = a
    for m in FIN_FRASE.finditer(t, a, s):
        ini = m.end()
    m = FIN_FRASE.search(t, e, b)
    return " ".join(t[ini:m.start() + (t[m.start()] == ".") if m else b].split())


def citas_de_texto(texto, archivo):
    sin_com = "\n".join(re.sub(r"(?<!\\)%.*", "", l) for l in texto.splitlines())
    out = []
    for m in CITE.finditer(sin_com):
        linea, ctx = sin_com.count("\n", 0, m.start()) + 1, contexto(sin_com, m.start(), m.end())
        out += [(k.strip(), archivo, linea, ctx) for k in m.group(1).split(",") if k.strip()]
    return out


def leer_tex(ruta):
    archivos = [ruta] if os.path.isfile(ruta) else sorted(glob.glob(os.path.join(ruta, "**", "*.tex"), recursive=True))
    out = []
    for a in archivos:
        with open(a, encoding="utf-8", errors="replace") as f:
            out += citas_de_texto(f.read(), os.path.relpath(a))
    return out


def cruzar(entradas, citas):
    claves = {e["clave"] for e in entradas}
    citadas = {}
    for k, a, l, _ in citas:
        citadas.setdefault(k, f"{a}:{l}")
    todo = "*" in citadas  # \nocite{*}
    citadas.pop("*", None)
    return {"faltan": {k: v for k, v in citadas.items() if k not in claves},
            "sin_citar": [] if todo else sorted(claves - set(citadas)),
            "n_citas": len(citas), "n_claves": len(citadas)}


def obtener_abstract(c):
    if c.get("abstract"):
        return c["abstract"]
    if c.get("doi"):
        a = abstract_oa(get(f"{OA}/works/doi:{q(c['doi'])}?select=abstract_inverted_index"))
        if a:
            return a
    ax = arxiv_id(c.get("arxiv"))
    if ax:
        r = arxiv("id_list=" + q(ax))
        if r and r[0]["abstract"]:
            return r[0]["abstract"]
    return None


def escribir_contextos(ruta, res, citas, hilos):
    usos = defaultdict(list)
    for k, a, l, ctx in citas:
        usos[k].append((a, l, ctx))
    citados = [r for r in res if r["e"]["clave"] in usos]
    with ThreadPoolExecutor(hilos) as ex:
        abstracts = list(ex.map(lambda r: obtener_abstract(r["c"]) if r["c"] else None, citados))
    L = ["# Contexto de cada cita", "",
         "Para cada referencia: qué afirma el texto al citarla y el abstract del paper. Clasificar cada uso "
         "como **respalda**, **respalda en parte**, **no respalda** o **no verificable con el abstract** "
         "(en ese caso, abrir el texto completo).", ""]
    for r, ab in zip(citados, abstracts):
        e, c = r["e"], r["c"]
        L.append(f"## `{e['clave']}` — {r['v']}")
        if c:
            L += [f"**Paper:** {describir_real(c)}  ", f"**Links:** {links(c, e)}", ""]
            L += [f"**Abstract:** {recortar(ab, 2500) if ab else '(no disponible: abrir el paper)'}", ""]
        else:
            L += ["**Paper:** no encontrado (ver informe)", ""]
        L.append("**Citado en:**")
        L += [f"{i}. `{a}:{l}` — {ctx}" for i, (a, l, ctx) in enumerate(usos[e["clave"]], 1)]
        L.append("")
    with open(ruta, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    return len(citados)


# ---------- duplicados, huecos ----------

def duplicados(res):
    grupos = defaultdict(list)
    for r in res:
        c = r["c"]
        if c and r["v"] != "NO ENCONTRADA":
            grupos[(c.get("doi") or "").lower() or c.get("bibcode") or norm(c["title"])].append(r["e"]["clave"])
    repetidas = [k for k, n in Counter(r["e"]["clave"] for r in res).items() if n > 1]
    return {"mismo_paper": [g for g in grupos.values() if len(set(g)) > 1], "clave_repetida": repetidas}


def huecos(res, n):
    """Papers citados por >= 2 de tus referencias que no están en tu bibliografía (OpenAlex)."""
    dois = sorted({r["c"]["doi"].lower() for r in res if r["c"] and r["c"].get("doi")
                   and r["v"] != "NO ENCONTRADA" and "," not in r["c"]["doi"]})
    propios = {r["c"]["oa_id"] for r in res if r["c"] and r["c"].get("oa_id")}
    refs = Counter()
    for i in range(0, len(dois), 50):
        r = get(f"{OA}/works?" + urllib.parse.urlencode({"filter": "doi:" + "|".join(dois[i:i + 50]),
                                                        "per-page": 50, "select": "id,referenced_works"}))
        for w in (r or {}).get("results", []):
            propios.add(w["id"].rsplit("/", 1)[-1])
            refs.update({x.rsplit("/", 1)[-1] for x in w.get("referenced_works") or []})
    top = [(w, k) for w, k in refs.most_common() if w not in propios and k >= 2][:min(n, 50)]
    if not top:
        return []
    cuenta = dict(top)
    r = get(f"{OA}/works?" + urllib.parse.urlencode({"filter": "openalex_id:" + "|".join(cuenta),
                                                    "per-page": 50, "select": OA_SELECT + ",cited_by_count"}))
    out = [(de_openalex(w), cuenta[w["id"].rsplit("/", 1)[-1]], w.get("cited_by_count") or 0)
           for w in (r or {}).get("results", [])]
    return sorted(out, key=lambda x: (-x[1], -x[2]))


# ---------- .bib corregido ----------

SIGLA = re.compile(r"[A-Z].*[A-Z]|[A-Z].*\d|\d.*[A-Z]")
MACROS_AAS = {norm(k): v for v, ks in {
    r"\apj": ["The Astrophysical Journal", "Astrophysical Journal", "ApJ"],
    r"\apjl": ["The Astrophysical Journal Letters", "Astrophysical Journal Letters", "ApJL", "ApJ Letters"],
    r"\apjs": ["The Astrophysical Journal Supplement Series", "Astrophysical Journal Supplement Series",
               "Astrophysical Journal Supplement", "ApJS"],
    r"\aj": ["The Astronomical Journal", "Astronomical Journal", "AJ"],
    r"\aap": ["Astronomy & Astrophysics", "Astronomy and Astrophysics", "A&A"],
    r"\aaps": ["Astronomy & Astrophysics Supplement Series", "Astronomy and Astrophysics Supplement Series", "A&AS"],
    r"\mnras": ["Monthly Notices of the Royal Astronomical Society", "MNRAS"],
    r"\araa": ["Annual Review of Astronomy and Astrophysics", "Annual Review of Astronomy & Astrophysics", "ARA&A"],
    r"\nat": ["Nature"],
    r"\pasp": ["Publications of the Astronomical Society of the Pacific", "PASP"],
    r"\pasj": ["Publications of the Astronomical Society of Japan", "PASJ"],
    r"\icarus": ["Icarus"],
    r"\qjras": ["Quarterly Journal of the Royal Astronomical Society", "QJRAS"],
}.items() for k in ks}


def proteger(titulo):
    """Pone llaves a las siglas (ALMA, PDS, 3D, II) fuera de llaves y de $…$, para que
    plainnat/aasjournals no las pasen a minúsculas. shortcut: no detecta nombres propios (Jupiter)."""
    out, nivel, mat, i = [], 0, False, 0
    while i < len(titulo):
        ch = titulo[i]
        if ch == "\\":
            cmd = re.match(r"\\([a-zA-Z]+|.)", titulo[i:]).group(0)
            out.append(cmd)
            i += len(cmd)
            continue
        if ch == "$":
            mat = not mat
        elif ch == "{":
            nivel += 1
        elif ch == "}":
            nivel -= 1
        elif nivel == 0 and not mat and ch.isalnum() and (i == 0 or not titulo[i - 1].isalnum()):
            w = re.match(r"[A-Za-z0-9]+(?:[-/][A-Za-z0-9]+)*", titulo[i:])
            if w and any(SIGLA.search(p) for p in re.split(r"[-/]", w.group(0))):
                out.append("{" + w.group(0) + "}")
                i += len(w.group(0))
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def parche(t, res, macros=False):
    """Devuelve (texto corregido, resumen). Solo agrega doi/eprint en entradas ✅, protege siglas y,
    con macros, abrevia revistas. Nunca cambia año/volumen/página: eso requiere revisión humana."""
    cambios, resumen = [], Counter()
    for r in res:
        e, c = r["e"], r["c"]
        if "_fin" not in e:
            continue
        pos = e["_pos"]
        if "title" in pos:
            nuevo = proteger(e["title"])
            if nuevo != e["title"]:
                cambios.append((*pos["title"], nuevo))
                resumen["títulos con siglas protegidas"] += 1
        if macros and "journal" in pos:
            mac = MACROS_AAS.get(norm(limpiar(e["journal"])))
            if mac and e["journal"].strip() != mac:
                cambios.append((*pos["journal"], mac))
                resumen["revistas abreviadas con macro AAS"] += 1
        if r["v"] == "OK" and c and not (es_preprint_bib(e) and not c["preprint"]):  # preprint publicado: decide el usuario
            nuevos = []
            if c["doi"] and "doi" not in e:
                nuevos.append(f"doi = {{{c['doi']}}}")
                resumen["DOIs agregados"] += 1
            ax = arxiv_id(c["arxiv"])
            if ax and "eprint" not in e:
                nuevos.append(f"eprint = {{{ax}}}, archivePrefix = {{arXiv}}")
                resumen["eprints de arXiv agregados"] += 1
            if nuevos:
                j = e["_fin"]
                while t[j - 1] in " \t\r\n":
                    j -= 1
                sep = "\n  " if t[j - 1] == "," else ",\n  "
                cambios.append((j, j, sep + ", ".join(nuevos)))
    for s, f, nuevo in sorted(cambios, reverse=True):
        t = t[:s] + nuevo + t[f:]
    return t, resumen


# ---------- informe ----------

def recortar(s, n):
    return (s[:n] + "…") if s and len(s) > n else s


def busqueda(t):
    return (f"[Google Scholar](https://scholar.google.com/scholar?q={q(t)}) · "
            f"[ADS](https://ui.adsabs.harvard.edu/search/q={q(t)})")


def links(c, e):
    if not c:
        return busqueda(limpiar(e.get("title", "")) or e.get("raw", ""))
    ls = []
    if c["doi"]:
        ls.append(f"[DOI](https://doi.org/{c['doi']})")
    if c["bibcode"]:
        ls.append(f"[ADS](https://ui.adsabs.harvard.edu/abs/{q(c['bibcode'])}/abstract)")
    elif c["doi"]:
        ls.append(f"[ADS](https://ui.adsabs.harvard.edu/abs/{c['doi']}/abstract)")
    if c["arxiv"]:
        ls.append(f"[arXiv]({c['arxiv']})")
    if c["pdf"]:
        ls.append(f"[PDF]({c['pdf']})")
    elif c["oa"] and c["oa"] not in (c["arxiv"], f"https://doi.org/{c['doi']}"):
        ls.append(f"[acceso abierto]({c['oa']})")
    if c["isbn"]:
        ls.append(f"[ISBN {c['isbn']}](https://openlibrary.org/isbn/{c['isbn']})")
    elif c["ol"]:
        ls.append(f"[OpenLibrary]({c['ol']})")
    if not (c["doi"] or c["arxiv"] or c["bibcode"]):
        ls.append(busqueda(c["title"]))
    return " · ".join(ls)


def describir_bib(e):
    if e.get("raw"):
        return e["raw"]
    return (f"{limpiar(e.get('author', '?'))} ({e.get('year', '?')}). «{limpiar(e.get('title', '?'))}». "
            f"{limpiar(e.get('journal', e.get('booktitle', e.get('publisher', ''))))} "
            f"{e.get('volume', '')} {e.get('pages', '')}").strip()


def describir_real(c):
    au = list(c["authors"])
    a = ", ".join(au[:3]) + (" et al." if len(au) > 3 else "")
    return f"{a} ({c['year'] or '?'}). «{c['title']}». {c['journal'] or ''} {c['volume'] or ''} {c['page'] or ''}".strip()


def bloque(r):
    e, c = r["e"], r["c"]
    L = [f"### `{e['clave']}` ({e['tipo']})", f"- **El bib dice:** {describir_bib(e)}"]
    if c:
        L.append(f"- **Paper real** ({c['fuente']}): {describir_real(c)}")
    L += [f"- ⚠️ {p}" for p in r["probs"]] + [f"- {n}" for n in r["notas"]]
    L.append(f"- **{'Leer/descargar' if c else 'Buscar a mano'}:** {links(c, e)}")
    return L + [""]


def informe(nombre, res, cruce=None, dups=None, gaps=(), parcheado=None, contextos=None):
    g = {v: [r for r in res if r["v"] == v] for v in ("RETRACTADA", "NO ENCONTRADA", "DISCREPANCIA", "OK")}
    L = [f"# Verificación de bibliografía — `{nombre}`", "",
         f"{len(res)} entradas · ✅ {len(g['OK'])} verificadas · ⚠️ {len(g['DISCREPANCIA'])} con discrepancias · "
         f"❌ {len(g['NO ENCONTRADA'])} no encontradas (posibles citas falsas) · 🚩 {len(g['RETRACTADA'])} retractadas", ""]
    if FALLOS:
        L += [f"> **Aviso:** {len(FALLOS)} consultas fallaron por cupo o error del servidor (p.ej. `{FALLOS[0]}`). "
              "Las ❌ pueden ser falsos negativos y faltar links: repetir más tarde (la caché guarda lo ya "
              "consultado) o con `OPENALEX_API_KEY`.", ""]
    if parcheado:
        ruta, resumen = parcheado
        detalle = ", ".join(f"{n} {k}" for k, n in resumen.items()) or "sin cambios"
        L += [f"> **.bib corregido:** `{ruta}` ({detalle}). Año, volumen y página nunca se cambian solos.", ""]
    if contextos:
        L += [f"> **Contexto de citas:** `{contextos[0]}` ({contextos[1]} referencias citadas en el .tex).", ""]
    titulos = {"RETRACTADA": "## 🚩 Retractadas: no citarlas como evidencia",
               "NO ENCONTRADA": "## ❌ No encontradas: posibles citas falsas",
               "DISCREPANCIA": "## ⚠️ Existe, pero con datos distintos"}
    for v, titulo in titulos.items():
        if g[v]:
            L += [titulo, ""]
            for r in g[v]:
                L += bloque(r)
    if dups and (dups["mismo_paper"] or dups["clave_repetida"]):
        L += ["## 🔁 Duplicados", ""]
        L += [f"- Mismo paper con varias claves: {', '.join(f'`{k}`' for k in grupo)}" for grupo in dups["mismo_paper"]]
        L += [f"- Clave repetida en el .bib: `{k}` (BibTeX usará solo una)" for k in dups["clave_repetida"]]
        L.append("")
    if cruce:
        L += ["## 📄 Cruce con el .tex", "",
              f"{cruce['n_citas']} citas a {cruce['n_claves']} claves distintas.", ""]
        if cruce["faltan"]:
            L += ["**Citadas en el .tex pero ausentes del .bib** (saldrán como `[?]`):", ""]
            L += [f"- `{k}` — primer uso en `{v}`" for k, v in sorted(cruce["faltan"].items())] + [""]
        if cruce["sin_citar"]:
            L += [f"**En el .bib pero nunca citadas** ({len(cruce['sin_citar'])}; no aparecerán en la bibliografía "
                  "salvo con `\\nocite`):", "", ", ".join(f"`{k}`" for k in cruce["sin_citar"]), ""]
    if gaps is None:
        L += ["## 📚 Posibles referencias faltantes", "",
              "No se pudieron calcular: OpenAlex no respondió (cupo diario agotado o caída). "
              "Repetir más tarde o con `OPENALEX_API_KEY`.", ""]
    elif gaps:
        L += ["## 📚 Posibles referencias faltantes", "",
              "Papers que citan varias de tus referencias y que tú no citas. No todos aplican: revisar relevancia.", "",
              "| citado por | paper | citas totales | leer |", "|---|---|---|---|"]
        for c, k, total in gaps:
            a = (list(c["authors"]) or ["?"])[0].split()[-1]
            L.append(f"| {k} de tus refs | {a} ({c['year']}) — {c['title'].replace('|', '/')} | {total} | {links(c, {})} |")
        L.append("")
    if g["OK"]:
        L += ["## ✅ Verificadas", "", "| clave | paper | leer / descargar | notas |", "|---|---|---|---|"]
        for r in g["OK"]:
            e, c = r["e"], r["c"]
            a = (list(c["authors"]) or ["?"])[0].split()[-1]
            notas = "<br>".join(r["notas"]).replace("|", "/")
            L.append(f"| `{e['clave']}` | {a} ({c['year']}) — {c['title'].replace('|', '/')} | {links(c, e)} | {notas} |")
    return "\n".join(L) + "\n"


def a_json(r):
    e = {k: v for k, v in r["e"].items() if not k.startswith("_")}
    return {"clave": e["clave"], "veredicto": r["v"], "problemas": r["probs"], "notas": r["notas"],
            "bib": e, "paper": r["c"], "coincidencia": r["m"]}


# ---------- autoprueba (sin red) ----------

def selftest():
    bib = r"""@article{A, author = {M\"uller, A. and others}, title = {Dust in {PDS} 70 with ALMA},
      journal = "ApJ", volume = 5, pages = {L2--L9}, year = {2021}}
      @string{x = "y"}
      @misc{B,title={Nested {a {b}} c},}
      @book{L, author = {Landau, L. D.}, title = {Fluid Mechanics}, year = {1987}}
      @article{G, author = {{Gaia Collaboration}}, title = {Gaia Early Data Release 3: Summary},
      journal = {Astronomy \& Astrophysics}, volume = {649}, pages = {A1}, year = {2021}}"""
    es = parse_bib(bib)
    assert [e["clave"] for e in es] == ["A", "B", "L", "G"], es
    assert limpiar(es[0]["author"]) == "Muller, A. and others" and es[0]["volume"] == "5", es[0]
    assert es[1]["title"] == "Nested {a {b}} c", es[1]
    assert bib[slice(*es[0]["_pos"]["title"])] == es[0]["title"] and bib[es[0]["_fin"]] == "}"
    assert primer_apellido(es[0]) == "muller" and anio(es[0]) == 2021
    c = cand(title="Dust in PDS 70 with ALMA", authors=["Anna Müller"], year=2021, volume="5", page="L2", journal="ApJ")
    assert veredicto(es[0], comparar(es[0], c), c, None, False) == ("OK", [])
    c2 = {**c, "year": 2015}
    assert veredicto(es[0], comparar(es[0], c2), c2, None, False)[0] == "DISCREPANCIA"
    libro = cand(title="Fluid Mechanics", authors=["L.D Landau"], year=1959)  # otra edición: no es error
    assert veredicto(es[2], comparar(es[2], libro), libro, None, False)[0] == "OK"
    gaia = cand(title="Gaia Early Data Release 3: Summary", authors=["A. G. A. Brown"], year=2021, volume="649", page="A1")
    assert corporativo(es[3]) and veredicto(es[3], comparar(es[3], gaia), gaia, None, False)[0] == "OK"
    t = parse_texto("[1] Müller, A. et al. 2021, ApJ, 5, L2\n[2] Otro, B. 2020, A&A, 600, A1\n[3] Sol, C. 2023, arXiv:2301.01234")
    assert len(t) == 3 and primer_apellido(t[0]) == "muller", t
    assert vol_pag(t[1]) == ("600", "A1") and vol_pag(es[0]) == ("5", "L2")
    assert veredicto(t[0], comparar(t[0], c), c, None, False)[0] == "OK"
    assert es_preprint_bib(t[2]) and not es_preprint_bib(t[0]) and not es_preprint_bib(es[0])
    assert es_preprint_bib({"tipo": "article", "journal": "arXiv e-prints"})
    assert arxiv_id("https://arxiv.org/abs/astro-ph/9910404") == "astro-ph/9910404"
    assert arxiv_id("https://arxiv.org/pdf/1806.11568v2") == "1806.11568"
    # siglas
    assert proteger(r"Dust in PDS 70 with ALMA and {HL} Tau, 3D, $H_\alpha$, VLTI/GRAVITY. II. Jupiter-mass") == \
        r"Dust in {PDS} 70 with {ALMA} and {HL} Tau, {3D}, $H_\alpha$, {VLTI/GRAVITY}. {II}. Jupiter-mass", proteger(
        r"Dust in PDS 70 with ALMA and {HL} Tau, 3D, $H_\alpha$, VLTI/GRAVITY. II. Jupiter-mass")
    assert proteger(r"\textit{ALMA} view") == r"\textit{ALMA} view"
    assert MACROS_AAS[norm(limpiar(r"Astronomy \& Astrophysics"))] == r"\aap"
    # parche: agrega doi/eprint solo en OK, protege siglas, abrevia revista
    res = [resultado(es[0], "OK", [], c=cand(doi="10.1/x", arxiv="https://arxiv.org/abs/2101.00001")),
           resultado(es[3], "DISCREPANCIA", ["x"], c=cand(doi="10.1/y"))]
    nuevo, resumen = parche(bib, res, macros=True)
    p = {e["clave"]: e for e in parse_bib(nuevo)}
    assert p["A"]["doi"] == "10.1/x" and p["A"]["eprint"] == "2101.00001" and p["A"]["title"] == "Dust in {PDS} 70 with {ALMA}"
    assert p["A"]["journal"] == r"\apj" and p["G"]["journal"] == r"\aap" and "doi" not in p["G"], p
    assert p["B"]["title"] == "Nested {a {b}} c" and resumen["DOIs agregados"] == 1
    # .tex
    tex = ("\\section{Intro}\nLos CPD son fríos \\citep[e.g.][]{A,B}. Keppler et al. (2018) lo ven en Fig. 3 \\citet{C}.\n"
           "% \\cite{Comentada}\n\nOtro párrafo \\nocite{D}.")
    cs = citas_de_texto(tex, "x.tex")
    assert [k for k, *_ in cs] == ["A", "B", "C", "D"], cs
    assert cs[0][3].startswith("Los CPD") and cs[2][3].startswith("Keppler et al. (2018) lo ven en Fig. 3"), cs
    cr = cruzar(es, cs)
    assert set(cr["faltan"]) == {"C", "D"} and cr["sin_citar"] == ["G", "L"], cr
    # duplicados
    d = duplicados([resultado({"clave": "X", "tipo": "article"}, "OK", [], c=cand(doi="10.1/Z", title="t")),
                    resultado({"clave": "Y", "tipo": "article"}, "OK", [], c=cand(doi="10.1/z", title="t")),
                    resultado({"clave": "Y", "tipo": "article"}, "NO ENCONTRADA", [])])
    assert d == {"mismo_paper": [["X", "Y"]], "clave_repetida": ["Y"]}, d
    # ADS / OpenLibrary (formato de sus respuestas)
    a = de_ads({"bibcode": "2018A&A...617A..44K", "title": ["Discovery"], "author": ["Keppler, M."], "year": "2018",
                "pub": "Astronomy and Astrophysics", "volume": "617", "page": ["A44"],
                "doi": ["10.1051/0004-6361/201832957"], "identifier": ["arXiv:1806.11568"]})
    assert a["arxiv"] == "https://arxiv.org/abs/1806.11568" and a["page"] == "A44" and a["year"] == 2018
    assert "2018A%26A...617A..44K" in links(a, {})
    o = de_openlibrary({"title": "Fluid Mechanics", "author_name": ["L.D Landau"], "first_publish_year": 1987,
                        "isbn": ["0750627670", "9780750627672"], "key": "/works/OL1W"})
    assert o["isbn"] == "9780750627672" and o["ol"].endswith("/works/OL1W")
    print("selftest ok")


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description="Verifica una bibliografía y entrega links a cada paper.")
    ap.add_argument("archivo", nargs="?", help=".bib o lista de referencias en texto")
    ap.add_argument("--out", help="informe Markdown (por defecto, stdout)")
    ap.add_argument("--json", help="resultados completos en JSON")
    ap.add_argument("--tex", help=".tex o carpeta del manuscrito: cruza las claves \\cite con el .bib")
    ap.add_argument("--contextos", help="con --tex: Markdown con la oración de cada cita y el abstract del paper")
    ap.add_argument("--parche", help="escribe un .bib corregido (DOIs, eprints, siglas protegidas)")
    ap.add_argument("--macros-aas", action="store_true", help="con --parche: revistas como \\apj, \\aap, \\mnras…")
    ap.add_argument("--huecos", type=int, default=0, metavar="N", help="sugiere hasta N referencias faltantes")
    ap.add_argument("--strict", action="store_true", help="código de salida 1 si hay ❌, 🚩 o claves faltantes")
    ap.add_argument("--sin-cache", action="store_true", help=f"no usar la caché ({CACHE}, {TTL // 86400} días)")
    ap.add_argument("--hilos", type=int, default=4)
    ap.add_argument("--selftest", action="store_true", help="pruebas sin red")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.archivo:
        ap.error("falta el archivo")
    if a.contextos and not a.tex:
        ap.error("--contextos necesita --tex")
    USAR_CACHE[0] = not a.sin_cache
    with open(a.archivo, encoding="utf-8", errors="replace") as f:
        t = f.read()
    es_bib = bool(re.search(r"@\w+\s*\{", t))
    if a.parche and not es_bib:
        ap.error("--parche solo funciona con un .bib")
    entradas = parse_bib(t) if es_bib else parse_texto(t)
    fuentes = "OpenAlex/Crossref/arXiv/OpenLibrary" + ("/ADS" if os.environ.get("ADS_API_TOKEN") else "")
    print(f"{len(entradas)} referencias; consultando {fuentes}…", file=sys.stderr)
    hechos, lock = [0], threading.Lock()

    def uno(e):
        try:
            r = verificar(e)
        except Exception as ex:  # una entrada rara no debe tumbar todo el informe
            r = resultado(e, "NO ENCONTRADA", [f"error interno al verificar: {ex!r}"])
        with lock:
            hechos[0] += 1
            if hechos[0] % 10 == 0 or hechos[0] == len(entradas):
                print(f"  {hechos[0]}/{len(entradas)}", file=sys.stderr, flush=True)
        return r

    with ThreadPoolExecutor(a.hilos) as ex:
        res = list(ex.map(uno, entradas))
    citas = leer_tex(a.tex) if a.tex else []
    cruce = cruzar(entradas, citas) if a.tex and es_bib else None
    dups = duplicados(res)
    antes = len(FALLOS)
    gaps = huecos(res, a.huecos) if a.huecos else []
    if a.huecos and not gaps and len(FALLOS) > antes:
        gaps = None  # el informe dice que no se pudo, en vez de callar
    parcheado = contextos = None
    if a.parche:
        nuevo, resumen = parche(t, res, a.macros_aas)
        with open(a.parche, "w", encoding="utf-8") as f:
            f.write(nuevo)
        parcheado = (a.parche, resumen)
    if a.contextos:
        contextos = (a.contextos, escribir_contextos(a.contextos, res, citas, a.hilos))
    md = informe(os.path.basename(a.archivo), res, cruce, dups, gaps, parcheado, contextos)
    n = Counter(r["v"] for r in res)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"informe en {a.out}: {dict(n)}" + (f"; {len(FALLOS)} consultas fallidas" if FALLOS else ""),
              file=sys.stderr)
    else:
        print(md)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({"archivo": a.archivo, "resultados": [a_json(r) for r in res], "duplicados": dups,
                       "cruce_tex": cruce,
                       "huecos": [{"paper": c, "citado_por": k, "citas": tot} for c, k, tot in gaps or []],
                       "fallos": FALLOS}, f, ensure_ascii=False, indent=1)
    if a.strict and (n["NO ENCONTRADA"] or n["RETRACTADA"] or (cruce and cruce["faltan"])):
        sys.exit(1)


if __name__ == "__main__":
    main()
