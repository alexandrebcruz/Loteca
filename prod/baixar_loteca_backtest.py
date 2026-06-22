#!/usr/bin/env python3
"""
Baixa o histórico de concursos da Loteca direto da API oficial da Caixa,
preservando a resposta JSON crua (byte a byte) de cada concurso.

Saída (apenas os RAW — sem consolidados):
  data/raw/loteca-NNNN.json   -> resposta crua de cada concurso (1 arquivo cada)

Antes de baixar, VERIFICA todos os raw já presentes (JSON válido + número
batendo com o nome do arquivo) e baixa só os que faltam ou estão corrompidos —
então rodar de novo é barato e idempotente.

Fonte oficial:
  https://servicebus2.caixa.gov.br/portaldeloterias/api/loteca/{numero}
"""
import os
import ssl
import sys
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "https://servicebus2.caixa.gov.br/portaldeloterias/api/loteca"
AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)
RAW_DIR = os.path.join(RAIZ, "data", "raw")   # mesma pasta que os outros scripts leem
WORKERS = 6
RETRIES = 5
TIMEOUT = 30

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def fetch(numero):
    """Retorna os bytes crus da resposta da API para um concurso."""
    url = BASE if numero is None else f"{BASE}/{numero}"
    last_err = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, context=_ctx, timeout=TIMEOUT) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"concurso {numero}: falhou apos {RETRIES} tentativas: {last_err}")


def descobrir_ultimo():
    return int(json.loads(fetch(None))["numero"])


def caminho(numero):
    return os.path.join(RAW_DIR, f"loteca-{numero:04d}.json")


def arquivo_valido(numero):
    """True se o raw do concurso já existe, é JSON válido e tem o número certo."""
    p = caminho(numero)
    if not (os.path.exists(p) and os.path.getsize(p) > 0):
        return False
    try:
        with open(p, "rb") as f:
            return int(json.loads(f.read()).get("numero", -1)) == numero
    except Exception:  # noqa: BLE001 — arquivo truncado/corrompido conta como ausente
        return False


def escanear_existentes(ultimo):
    """Verifica todos os raw de 1..ultimo já baixados. -> set dos válidos."""
    return {n for n in range(1, ultimo + 1) if arquivo_valido(n)}


def baixar_um(numero):
    """Baixa, valida e grava os BYTES CRUS de um concurso."""
    try:
        raw = fetch(numero)
        j = json.loads(raw)                       # valida JSON + concurso esperado
        if int(j.get("numero", -1)) != numero:
            return numero, "erro", (f"numero divergente: esperado {numero}, "
                                    f"veio {j.get('numero')}")
        with open(caminho(numero), "wb") as f:
            f.write(raw)                           # grava cru, sem reserializar
        return numero, "ok", None
    except Exception as e:  # noqa: BLE001
        return numero, "erro", str(e)


def main():
    os.makedirs(RAW_DIR, exist_ok=True)
    ultimo = descobrir_ultimo()
    print(f"Ultimo concurso oficial: {ultimo}")

    print("Verificando os raw ja baixados...", flush=True)
    existentes = escanear_existentes(ultimo)
    faltantes = [n for n in range(1, ultimo + 1) if n not in existentes]
    print(f"  {len(existentes)} ja validos | {len(faltantes)} a baixar", flush=True)

    if not faltantes:
        print("\nNada a baixar — todos os concursos ja estao em data/raw.")
        return

    falhas, feitos, total = [], 0, len(faltantes)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(baixar_um, n): n for n in faltantes}
        for fut in as_completed(futs):
            numero, status, err = fut.result()
            feitos += 1
            if status == "erro":
                falhas.append({"numero": numero, "erro": err})
            if feitos % 50 == 0 or feitos == total:
                print(f"  {feitos}/{total} baixados | {len(falhas)} falhas",
                      flush=True)

    print(f"\nConcluido. Arquivos crus em {RAW_DIR}/loteca-NNNN.json")
    if falhas:
        print(f"\n  ATENCAO: {len(falhas)} falhas. Rode de novo p/ tentar as faltantes.")
        for fl in falhas[:20]:
            print(f"    - concurso {fl['numero']}: {fl['erro']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
