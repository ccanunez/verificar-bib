"""Prueba de regresión con red: corre el verificador sobre los fixtures y compara veredictos.

Uso: python3 tests/regresion.py   (1-3 min la primera vez; luego usa la caché)
"""
import json
import os
import subprocess
import sys
import tempfile

AQUI = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(AQUI, "..", "scripts", "verificar_bib.py")
ESPERADO = {
    "falsas.bib": {"Inventada": "NO ENCONTRADA", "Hibrida": "DISCREPANCIA", "AnioMal": "DISCREPANCIA",
                   "DoiRoto": "DISCREPANCIA", "Retractada": "RETRACTADA", "Preprint": "OK", "Real": "OK",
                   "Libro": "OK", "Corporativo": "OK"},
    "refs.txt": {"ref1": "OK", "ref2": "OK", "ref3": "NO ENCONTRADA"},
}


def correr(archivo, *extra):
    with tempfile.TemporaryDirectory() as d:
        js = os.path.join(d, "r.json")
        subprocess.run([sys.executable, SCRIPT, os.path.join(AQUI, archivo), "--out", os.path.join(d, "r.md"),
                        "--json", js, *extra], check=True, cwd=d)
        with open(js, encoding="utf-8") as f:
            return json.load(f)


def main():
    fallas = []
    for archivo, esperado in ESPERADO.items():
        extra = ("--tex", os.path.join(AQUI, "manuscrito.tex")) if archivo.endswith(".bib") else ()
        r = correr(archivo, *extra)
        if r["fallos"]:
            print(f"aviso: {len(r['fallos'])} consultas fallaron (cupo); los resultados pueden variar")
        obtenido = {x["clave"]: x for x in r["resultados"]}
        for clave, v in esperado.items():
            if obtenido[clave]["veredicto"] != v:
                fallas.append(f"{archivo}:{clave}: esperado {v}, obtenido {obtenido[clave]['veredicto']} "
                              f"{obtenido[clave]['problemas']}")
        if archivo.endswith(".bib"):
            if not any("ya está publicado" in n for n in obtenido["Preprint"]["notas"]):
                fallas.append("Preprint: falta la nota de versión publicada")
            if "año" not in " ".join(obtenido["AnioMal"]["problemas"]):
                fallas.append("AnioMal: no detectó el año")
            if "no existe" not in " ".join(obtenido["DoiRoto"]["problemas"]):
                fallas.append("DoiRoto: no detectó el DOI inexistente")
            if set(r["cruce_tex"]["faltan"]) != {"NoExiste"}:
                fallas.append(f"cruce .tex: {r['cruce_tex']['faltan']}")
    print("\n".join(fallas) or "regresión ok")
    sys.exit(1 if fallas else 0)


if __name__ == "__main__":
    main()
