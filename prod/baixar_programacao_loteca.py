#!/usr/bin/env python3
"""
Consulta a PRÓXIMA PROGRAMAÇÃO da Loteca na API oficial da Caixa e IMPRIME, em JSON
no stdout, APENAS o concurso ainda ABERTO a apostas cuja data-limite está mais
PRÓXIMA de fechar (o menor prazo de apostas ainda no futuro). Não grava nada.

Diferente do `baixar_loteca_backtest.py` (que baixa o HISTÓRICO já apurado em data/raw/), aqui
a fonte é o endpoint de PROGRAMAÇÃO, que lista os concursos AINDA NÃO sorteados —
`/loteca/{numero}` devolve HTTP 500 p/ esses, então não dá p/ usar o caminho normal.

Fonte oficial:
  https://servicebus2.caixa.gov.br/portaldeloterias/api/loteca/programacao
  -> lista de concursos futuros; cada item tem janela de apostas, datas dos jogos e
     `listaJogos` (14 jogos com times, campeonato, UF e país).

O prazo de apostas é `dataFimApostas` (dd/mm/aaaa) + `horarioFimApostas` (hora cheia),
interpretado no fuso do Brasil (UTC-3, sem horário de verão desde 2019). Entre os
concursos com prazo ainda no futuro, escolhe o de MENOR prazo (o que fecha primeiro).
Se nenhum estiver aberto, cai no de maior prazo (o "próximo" disponível) e avisa no
stderr.

Saída: o objeto JSON do concurso escolhido (os dados como vieram da API). Erros e
avisos vão p/ stderr.
"""
import sys
import ssl
import json
import time
import urllib.request
import datetime as dt

URL = "https://servicebus2.caixa.gov.br/portaldeloterias/api/loteca/programacao"
RETRIES = 5
TIMEOUT = 30
BR_TZ = dt.timezone(dt.timedelta(hours=-3))   # Brasil (UTC-3, sem horário de verão)

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def fetch(url=URL):
    """Retorna os BYTES CRUS da resposta da API (com retries)."""
    last_err = None
    for tentativa in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, context=_ctx, timeout=TIMEOUT) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.5 * (tentativa + 1))
    raise RuntimeError(f"programação: falhou após {RETRIES} tentativas: {last_err}")


def _prazo_apostas(concurso):
    """Datetime (no fuso BR) do fim das apostas do concurso. None se não der p/ ler."""
    data = (concurso.get("dataFimApostas") or "").strip()
    hora = str(concurso.get("horarioFimApostas") or "").strip() or "0"
    try:
        d = dt.datetime.strptime(data, "%d/%m/%Y").date()
        return dt.datetime(d.year, d.month, d.day, int(float(hora)), tzinfo=BR_TZ)
    except (ValueError, TypeError):
        return None


def escolher_proximo_aberto(programacao, agora=None):
    """Entre os concursos da programação, devolve o ABERTO (prazo de apostas ainda no
    futuro) com a data-limite mais próxima. Sem nenhum aberto, devolve o de maior
    prazo. -> (concurso, aberto: bool)."""
    agora = agora or dt.datetime.now(BR_TZ)
    com_prazo = [(c, _prazo_apostas(c)) for c in programacao]
    com_prazo = [(c, p) for c, p in com_prazo if p is not None]
    if not com_prazo:
        raise RuntimeError("nenhum concurso com prazo de apostas legível na resposta.")
    abertos = [(c, p) for c, p in com_prazo if p >= agora]
    if abertos:
        c, _ = min(abertos, key=lambda cp: cp[1])
        return c, True
    c, _ = max(com_prazo, key=lambda cp: cp[1])   # nada aberto -> o mais à frente
    return c, False


def main():
    raw = fetch()
    programacao = json.loads(raw)
    if not isinstance(programacao, list) or not programacao:
        raise RuntimeError(f"resposta inesperada da API: {raw[:200]!r}")

    concurso, aberto = escolher_proximo_aberto(programacao)
    prazo = _prazo_apostas(concurso)
    if aberto:
        print(f"[info] concurso {concurso.get('nuConcurso')} aberto; apostas até "
              f"{prazo:%d/%m/%Y %H}h (BR).", file=sys.stderr)
    else:
        print(f"[aviso] nenhum concurso aberto a apostas; retornando o mais à frente "
              f"({concurso.get('nuConcurso')}, prazo {prazo:%d/%m/%Y %H}h BR).",
              file=sys.stderr)

    json.dump(concurso, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"ERRO: {e}", file=sys.stderr)
        sys.exit(1)
