#!/usr/bin/env python3
"""pipeline_loteca.py — orquestra a análise completa de um concurso da Loteca.

Roda, em sequência, três etapas que hoje são chamadas à mão:

  1. analise_loteca.py    -> sobe o browser, coleta odds multi-casa e estima as
                             probabilidades 1X2 (grava data/analise/<pasta>/analise.json).
  2. otimizador_loteca.py -> lê esse analise.json e gera o relatório
                             otimizacao.html na MESMA pasta.
  3. audita_apelidos_loteca_flashscore.py -> audita o de-para Flashscore
                             (saúde do dicionário de apelidos). É independente do
                             concurso; o relatório JSON é gravado na pasta do run.

Pasta de saída (sob data/analise): por padrão `<concurso>_<AAAAMMDDHHMM>` — o
número do concurso + o instante de execução do pipeline. As etapas 1 e 2
compartilham essa MESMA pasta. Use --saida p/ um nome fixo.

O concurso é descoberto uma única vez (o próximo ABERTO na programação da Caixa)
e repassado às etapas 1/2 via --concurso-json, evitando uma segunda consulta.

Exemplos:
    python3 pipeline_loteca.py                          # próximo aberto, IP da máquina
    python3 pipeline_loteca.py --proxy fixo --country BR   # IP sujo -> casas BR
    python3 pipeline_loteca.py --auditar                # liga a auditoria por LLM na etapa 1
    python3 pipeline_loteca.py --saida 1257_manual      # nome de pasta fixo
    python3 pipeline_loteca.py --concurso-json prog.json   # usa uma programação já salva
    python3 pipeline_loteca.py --pular-auditoria        # só análise + otimizador
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)
ANALISE_DIR = os.path.join(RAIZ, "data", "analise")
PY = sys.executable or "python3"


def _log(msg):
    print(f"\n\033[1m[pipeline] {msg}\033[0m", file=sys.stderr, flush=True)


def _rodar(cmd, etapa, stdout_path=None):
    """Roda um subprocesso na pasta prod. Se stdout_path, captura o stdout nele.
    Devolve o returncode (não levanta) — quem chama decide se aborta."""
    _log(f"{etapa}: {' '.join(str(c) for c in cmd)}")
    if stdout_path:
        with open(stdout_path, "w", encoding="utf-8") as f:
            p = subprocess.run(cmd, cwd=AQUI, stdout=f)
    else:
        p = subprocess.run(cmd, cwd=AQUI)
    return p.returncode


def main():
    ap = argparse.ArgumentParser(
        description="Pipeline Loteca: análise -> otimizador -> auditoria de apelidos.")
    # --- nome da pasta / concurso ---
    ap.add_argument("--saida", default=None,
                    help="nome fixo da subpasta sob data/analise "
                         "(default: <concurso>_<AAAAMMDDHHMM>)")
    ap.add_argument("--concurso-json", default=None, dest="concurso_json",
                    help="usa uma programação/concurso já salvo em vez de "
                         "consultar a Caixa")
    # --- passthrough p/ a etapa 1 (analise_loteca) ---
    ap.add_argument("--proxy", nargs="?", choices=["none", "rotativo", "fixo"],
                    const="fixo", default=None,
                    help="proxy p/ análise e auditoria (sem o flag: IP da máquina)")
    ap.add_argument("--country", default="BR",
                    help="ISO-2 do país p/ --proxy fixo (default: BR)")
    ap.add_argument("--modo", choices=["fuzzy", "id"], default=None,
                    help="modo de resolução de times na análise (default do script)")
    ap.add_argument("--janela-dias", type=int, default=None, dest="janela_dias",
                    help="janela (dias) p/ tolerar jogo em data próxima")
    ap.add_argument("--auditar", action="store_true",
                    help="liga a auditoria por LLM em CADA jogo na etapa 1 (lento/caro)")
    ap.add_argument("--refazer", action="store_true",
                    help="ignora o checkpoint da análise e refaz todos os jogos")
    # --- passthrough p/ a etapa 2 (otimizador) ---
    ap.add_argument("--max-custo", type=float, default=None, dest="max_custo",
                    help="teto de custo em R$ para a fronteira do otimizador")
    ap.add_argument("--destaque", type=float, default=None,
                    help="custo em R$ do bilhete em destaque no relatório")
    ap.add_argument("--checar-precos", action="store_true", dest="checar_precos",
                    help="rebusca os preços oficiais na rede (default: usa cache)")
    # --- etapa 3 (auditoria) ---
    ap.add_argument("--pular-auditoria", action="store_true", dest="pular_audit",
                    help="não roda a etapa 3 (auditoria de apelidos)")
    ap.add_argument("--via-time", action="store_true", dest="via_time",
                    help="auditoria: usa também o fallback por página de equipe")
    ap.add_argument("--quiet", action="store_true",
                    help="silencia os logs/progresso das etapas")
    a = ap.parse_args()

    # 1) descobre o concurso UMA vez e congela a programação p/ as etapas 1/2.
    sys.path.insert(0, AQUI)
    from analise_loteca import obter_concurso  # noqa: E402
    try:
        concurso, aberto = obter_concurso(a.concurso_json)
    except Exception as e:  # noqa: BLE001
        print(f"[erro] não foi possível obter o concurso: {e}", file=sys.stderr)
        sys.exit(1)
    num = concurso.get("nuConcurso")
    if not num:
        print("[erro] concurso sem nuConcurso.", file=sys.stderr)
        sys.exit(1)

    ts = dt.datetime.now().strftime("%Y%m%d%H%M")
    pasta = a.saida or f"{num}_{ts}"
    cdir = os.path.join(ANALISE_DIR, pasta)
    os.makedirs(cdir, exist_ok=True)

    # programação congelada -> repassada às etapas 1/2 (evita 2ª consulta à Caixa)
    prog_path = os.path.join(cdir, "_programacao_pipeline.json")
    with open(prog_path, "w", encoding="utf-8") as f:
        json.dump(concurso, f, ensure_ascii=False, indent=2)

    estado = "ABERTO" if aberto else "FECHADO (mais à frente)"
    _log(f"concurso {num} ({estado}) -> pasta data/analise/{pasta}/")

    # ----------------------------------------------------------------- etapa 1
    cmd1 = [PY, "analise_loteca.py", "--saida", pasta,
            "--concurso-json", prog_path]
    if a.proxy:
        cmd1 += ["--proxy", a.proxy, "--country", a.country]
    if a.modo:
        cmd1 += ["--modo", a.modo]
    if a.janela_dias is not None:
        cmd1 += ["--janela-dias", str(a.janela_dias)]
    if a.auditar:
        cmd1 += ["--auditar"]
    if a.refazer:
        cmd1 += ["--refazer"]
    if a.quiet:
        cmd1 += ["--quiet"]
    if _rodar(cmd1, "1/3 análise") != 0:
        print("[erro] etapa 1 (análise) falhou; abortando.", file=sys.stderr)
        sys.exit(1)

    # ----------------------------------------------------------------- etapa 2
    cmd2 = [PY, "otimizador_loteca.py", str(num), "--saida", pasta]
    if not a.checar_precos:
        cmd2 += ["--sem-checar-precos"]
    if a.max_custo is not None:
        cmd2 += ["--max-custo", str(a.max_custo)]
    if a.destaque is not None:
        cmd2 += ["--destaque", str(a.destaque)]
    if a.quiet:
        cmd2 += ["--quiet"]
    if _rodar(cmd2, "2/3 otimizador") != 0:
        print("[erro] etapa 2 (otimizador) falhou; abortando.", file=sys.stderr)
        sys.exit(1)
    html_path = os.path.join(cdir, "otimizacao.html")

    # ----------------------------------------------------------------- etapa 3
    audit_path = None
    if not a.pular_audit:
        audit_path = os.path.join(cdir, "auditoria_apelidos.json")
        cmd3 = [PY, "audita_apelidos_loteca_flashscore.py", "--etapa", "12", "--json"]
        if a.proxy:
            cmd3 += ["--proxy", a.proxy, "--country", a.country]
        if a.via_time:
            cmd3 += ["--via-time"]
        rc = _rodar(cmd3, "3/3 auditoria apelidos", stdout_path=audit_path)
        if rc != 0:
            print(f"[aviso] etapa 3 (auditoria) retornou {rc}; "
                  f"veja {audit_path}.", file=sys.stderr)

    # ----------------------------------------------------------------- resumo
    _log("concluído.")
    print(json.dumps({
        "concurso": num,
        "aberto": aberto,
        "pasta": cdir,
        "analise_json": os.path.join(cdir, "analise.json"),
        "otimizacao_html": html_path,
        "auditoria_json": audit_path,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
