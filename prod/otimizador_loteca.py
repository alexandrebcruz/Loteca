#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
otimizador_loteca.py — Otimizador exato de apostas da Loteca.

Recebe o número de um concurso, lê as probabilidades 1X2 estimadas em
data/analise/<concurso>/ (geradas por analise_loteca.py) e calcula, por
programação dinâmica, as apostas ÓTIMAS: para cada orçamento, o conjunto de
marcações (simples/duplo/triplo por jogo) que MAXIMIZA a probabilidade de
14 acertos. Salva um relatório otimizacao.html (página responsiva e interativa)
na própria pasta de análise.

Modelo
------
Cada jogo j tem probabilidades (p1, pX, p2), já na orientação da Loteca
(casa->coluna 1, empate->X, fora->coluna 2). Marcar m resultados num jogo
cobre os m mais prováveis; a "cobertura" c[j] é a soma das probs marcadas:
    1 marca (simples) -> top1
    2 marcas (duplo)  -> top1+top2
    3 marcas (triplo) -> 1.0
O custo de um bilhete é (Π_j marcas[j]) combinações x R$ PRECO. As métricas:
    P(14)        = Π_j c[j]
    P(13 exato)  = Σ_j (1-c[j]) · Π_{i≠j} c[i]
    P(13+)       = P(14) + P(13 exato)

Otimização (exata, não heurística)
----------------------------------
DP sobre o número de combinações (= produto das marcas). Estado: combos
alcançáveis (valores 2^a·3^b ≤ teto). Para cada jogo escolhe-se o nível de
marcação que maximiza a cobertura acumulada (produto). Ao final, para cada
custo alcançável temos o MÁXIMO P(14) e o bilhete que o atinge — a fronteira
de Pareto custo×P(14). Isso substitui a heurística gulosa por ordenação de
ganho, que não garante otimalidade sob orçamento fixo.

Uso
---
    python3 otimizador_loteca.py 1257
    python3 otimizador_loteca.py 1257 --max-custo 500 --objetivo p14
    python3 otimizador_loteca.py 1257 --destaque 48
"""

import os
import sys
import json
import argparse

from precos_loteca import obter_precos

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)
ANALISE_DIR = os.path.join(RAIZ, "data", "analise")

# Preços/regras da Loteca — resolvidos da fonte oficial em tempo de execução
# (precos_loteca.obter_precos). Estes são apenas DEFAULTS de partida; são
# sobrescritos por aplicar_precos() antes de qualquer cálculo.
PRECO = 2.00            # R$ por combinação
MIN_COMBOS = 2          # aposta mínima = 1 duplo (seco não é vendável)
MAX_COMBOS_LIMITE = 864  # maior bilhete oficial (5 duplos+3 triplos)
TABELA_DT = {}          # (D,T) -> {"combos","valor"} da tabela oficial de aposta


def aplicar_precos(p):
    """Sobrescreve as constantes globais com os preços resolvidos da fonte."""
    global PRECO, MIN_COMBOS, MAX_COMBOS_LIMITE, TABELA_DT
    PRECO = float(p["preco_unitario"])
    MIN_COMBOS = int(p["min_combos"])
    MAX_COMBOS_LIMITE = int(p["max_combos"])
    TABELA_DT = {(r["d"], r["t"]): r for r in (p.get("tabela_dt") or [])}


def validar_bilhete(niveis):
    """Confere que um bilhete (marcas por jogo) é uma aposta VENDÁVEL da tabela
    oficial e devolve seu custo verificado.

    Retorna {"d","t","combos","custo","valido","motivo"}. `valido` só é True se
    o par (D,T) consta na tabela oficial de valor da aposta, está dentro de
    [min, max] combinações e o custo `combos × unitário` bate com o valor
    publicado. Quando há tabela, o custo devolvido é o VALOR PUBLICADO (fonte de
    verdade); sem tabela (fallback ausente), recai na fórmula.
    """
    d = sum(1 for m in niveis if m == 2)
    t = sum(1 for m in niveis if m == 3)
    combos = 1
    for m in niveis:
        combos *= m
    custo = round(combos * PRECO, 2)
    motivos = []
    if combos < MIN_COMBOS:
        motivos.append(f"{combos} combinações < mínimo vendável ({MIN_COMBOS})")
    if combos > MAX_COMBOS_LIMITE:
        motivos.append(f"{combos} combinações > teto oficial ({MAX_COMBOS_LIMITE})")
    row = TABELA_DT.get((d, t))
    if TABELA_DT and row is None:
        motivos.append(f"par (D={d},T={t}) ausente na tabela oficial")
    elif row is not None:
        if row["combos"] != combos:
            motivos.append(f"combos {combos} divergem da tabela ({row['combos']})")
        if row.get("valor"):
            if abs(row["valor"] - custo) > 0.005:
                motivos.append(
                    f"custo R$ {custo:.2f} != tabela R$ {row['valor']:.2f}")
            else:
                custo = row["valor"]  # valor publicado é a fonte de verdade
    return {"d": d, "t": t, "combos": combos, "custo": custo,
            "valido": not motivos, "motivo": "; ".join(motivos)}


# ----------------------------------------------------------------------------
# IO
# ----------------------------------------------------------------------------
# Nome da subpasta sob data/analise. None = usa o número do concurso (default
# histórico). O pipeline injeta aqui um nome custom (ex.: "1257_202606221830").
SAIDA_OVERRIDE = None


def _dir_concurso(numero):
    nome = SAIDA_OVERRIDE if SAIDA_OVERRIDE else str(numero)
    return os.path.join(ANALISE_DIR, nome)


def _carregar_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _salvar_texto(path, texto):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(texto)
    os.replace(tmp, path)


def carregar_jogos(numero):
    """Lê analise.json (preferido) ou reconstrói de jogo-NN.json.

    Retorna (meta, jogos) onde jogos é lista de dicts:
        {seq, home, away, data, p:{"1":..,"X":..,"2":..}, palpite, erro}
    """
    cdir = _dir_concurso(numero)
    if not os.path.isdir(cdir):
        raise SystemExit(f"[erro] pasta não encontrada: {cdir}\n"
                         f"       rode antes: python3 analise_loteca.py "
                         f"(ou verifique o número do concurso)")

    meta = {"concurso": numero, "fim_apostas": None, "fonte_odds": None,
            "jogos_total": None, "jogos_resolvidos": None}
    brutos = []
    analise = _carregar_json(os.path.join(cdir, "analise.json"))
    if analise and analise.get("jogos"):
        meta["concurso"] = analise.get("concurso", numero)
        meta["fim_apostas"] = analise.get("fim_apostas")
        meta["fonte_odds"] = analise.get("fonte_odds")
        meta["jogos_total"] = analise.get("jogos_total")
        meta["jogos_resolvidos"] = analise.get("jogos_resolvidos")
        brutos = analise["jogos"]
    else:
        for nome in sorted(os.listdir(cdir)):
            if nome.startswith("jogo-") and nome.endswith(".json"):
                j = _carregar_json(os.path.join(cdir, nome))
                if j:
                    brutos.append(j)
        brutos.sort(key=lambda j: j.get("seq", 0))
        if not brutos:
            raise SystemExit(f"[erro] nenhum jogo em {cdir}")

    jogos = []
    for j in brutos:
        lot = j.get("loteca") or {}
        pr = j.get("prob_1x2") or {}
        casa, emp, fora = pr.get("casa"), pr.get("empate"), pr.get("fora")
        if None in (casa, emp, fora):
            p = None  # jogo sem odds -> tratado como triplo forçado
        else:
            p = {"1": float(casa), "X": float(emp), "2": float(fora)}
        ncasas = j.get("n_casas")
        if ncasas is None:
            ncasas = pr.get("n_casas_validas")
        jogos.append({
            "seq": j.get("seq"),
            "home": lot.get("home"),
            "away": lot.get("away"),
            "data": lot.get("data"),
            "p": p,
            "n_casas": ncasas,
            "palpite": j.get("palpite"),
            "erro": j.get("erro"),
        })
    return meta, jogos


# ----------------------------------------------------------------------------
# Núcleo de otimização
# ----------------------------------------------------------------------------
def _coberturas(p):
    """Para um jogo, devolve lista [(marcas, cobertura, resultados_ordenados)].

    Nível 1=top1, 2=top1+top2, 3=1.0 (triplo). resultados são as letras
    "1"/"X"/"2" ordenadas por probabilidade decrescente.
    """
    if p is None:
        # sem odds: só faz sentido o triplo (cobre tudo); marcamos como forçado
        return [(3, 1.0, ["1", "X", "2"])], ["1", "X", "2"]
    ordenados = sorted(("1", "X", "2"), key=lambda k: p[k], reverse=True)
    acc = 0.0
    niveis = []
    for m in (1, 2, 3):
        acc = sum(p[k] for k in ordenados[:m])
        cob = 1.0 if m == 3 else acc
        niveis.append((m, cob, ordenados[:m]))
    return niveis, ordenados


def otimizar(jogos, max_combos):
    """DP exata: para cada nº de combos alcançável (≤ max_combos), o bilhete
    de MÁXIMA cobertura-produto (= máximo P14).

    Retorna dict combos -> {"cov": P14, "niveis": [marcas por jogo]}.
    """
    opts = [_coberturas(j["p"])[0] for j in jogos]
    # dp: combos -> (cov_produto, [marcas por jogo até aqui])
    dp = {1: (1.0, [])}
    for niveis in opts:
        novo = {}
        for combos, (cov, marcas) in dp.items():
            for m, c, _res in niveis:
                nc = combos * m
                if nc > max_combos:
                    continue
                ncov = cov * c
                cur = novo.get(nc)
                if cur is None or ncov > cur[0]:
                    novo[nc] = (ncov, marcas + [m])
        dp = novo
    return {combos: {"cov": cov, "niveis": marcas}
            for combos, (cov, marcas) in dp.items()}


def metricas_bilhete(jogos, niveis):
    """Calcula P14, P13 exato, P13+ e a cobertura por jogo de um bilhete."""
    cobs = []
    for j, m in zip(jogos, niveis):
        nivs, _ord = _coberturas(j["p"])
        cob = next(c for mm, c, _r in nivs if mm == m)
        cobs.append(cob)
    p14 = 1.0
    for c in cobs:
        p14 *= c
    p13 = 0.0
    for k in range(len(cobs)):
        termo = (1.0 - cobs[k])
        for i, c in enumerate(cobs):
            if i != k:
                termo *= c
        p13 += termo
    return {"p14": p14, "p13": p13, "p13mais": p14 + p13, "cobs": cobs}


def marcacoes_bilhete(jogos, niveis):
    """Descreve as marcações por jogo: lista de dicts com resultados marcados."""
    out = []
    for j, m in zip(jogos, niveis):
        nivs, ordenados = _coberturas(j["p"])
        res = next(r for mm, c, r in nivs if mm == m)
        # mantém ordem natural 1,X,2 para exibição
        marcados = [k for k in ("1", "X", "2") if k in res]
        out.append({"seq": j["seq"], "marcas": m, "resultados": marcados})
    return out


def todos_bilhetes(dp):
    """Para CADA par (D,T) alcançável — i.e., cada nº de combinações vendável,
    pois combos = 2^D·3^T é fatoração única — devolve a MELHOR aposta (máx P14).

    O DP já guarda, por combos, o bilhete de máxima cobertura; aqui só
    materializamos um por par, ordenados por combos (≈ por preço). Diferente da
    fronteira de Pareto, NÃO removemos os pares dominados: queremos os 38 pares
    publicados pela Caixa, cada um com sua aposta ótima. A eficiência (envelope
    de Pareto) é destacada na página. Retorna [(combos, cov, niveis)]."""
    out = []
    for combos, info in sorted(dp.items()):  # por combos asc
        if combos < MIN_COMBOS:  # seco (1 combo) não é aposta vendável
            continue
        out.append((combos, info["cov"], info["niveis"]))
    return out



_HTML_TEMPLATE = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Loteca — bilhetes otimizados</title>
<style>
  :root{
    --bg:#0f1419; --card:#ffffff; --ink:#1a2330; --muted:#5d6b7a;
    --line:#e3e8ee; --accent:#0a7d3b; --accent2:#0b66c3; --warn:#b8860b;
  }
  *{box-sizing:border-box}
  body{margin:0;background:#eef2f6;color:var(--ink);
    font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    -webkit-text-size-adjust:100%;}
  main{max-width:980px;margin:0 auto;padding:16px 14px 60px}
  header h1{font-size:1.35rem;margin:.2em 0 .1em}
  header p{color:var(--muted);margin:.1em 0 0}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;
    padding:16px 16px 18px;margin:16px 0;box-shadow:0 1px 3px rgba(20,30,50,.06)}
  .card h2{font-size:1.08rem;margin:.1em 0 .7em}
  .sub{color:var(--muted);font-size:.86rem;margin:-.3em 0 .8em}
  .tabela-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
  table{border-collapse:collapse;width:100%;font-size:.92rem}
  th,td{padding:7px 9px;text-align:center;white-space:nowrap}
  th{background:#f4f7fa;color:var(--muted);font-weight:600;font-size:.8rem;
    text-transform:uppercase;letter-spacing:.02em;border-bottom:2px solid var(--line)}
  td{border-bottom:1px solid var(--line)}
  td.conf,th.conf{text-align:left;white-space:normal;min-width:150px}
  tr:last-child td{border-bottom:none}
  .info-tab{table-layout:fixed;width:100%}
  .info-tab td{text-align:left;border:none;padding:6px 8px;white-space:normal;
    word-break:break-word;overflow-wrap:anywhere;vertical-align:top}
  .info-tab td.k{color:var(--muted);width:40%;font-size:.86rem}
  .info-tab td.v{font-weight:600}
  .badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:.78rem;
    font-weight:600}
  .badge.ok{background:#e4f5ea;color:var(--accent)}
  .badge.fb{background:#fbf1dc;color:var(--warn)}
  .prob{font-variant-numeric:tabular-nums;font-weight:600;border-radius:5px}
  .fav{font-weight:800}
  /* gráfico */
  .modos{display:flex;gap:6px;background:#eef2f6;border:1px solid var(--line);
    border-radius:11px;padding:4px;margin:0 0 14px}
  .modos button{flex:1 1 0;cursor:pointer;border:none;background:transparent;
    color:var(--muted);border-radius:8px;padding:9px 10px;font-size:.9rem;
    font-weight:700;transition:.12s}
  .modos button.on{background:#fff;color:var(--accent);box-shadow:0 1px 3px rgba(16,24,40,.12)}
  #grafico-sec h3{margin:18px 0 2px;font-size:1rem}
  .metricas{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 12px}
  .metricas button{flex:1 1 auto;min-width:110px;cursor:pointer;border:1px solid var(--line);
    background:#f4f7fa;color:var(--muted);border-radius:9px;padding:9px 10px;font-size:.86rem;
    font-weight:600;transition:.12s}
  .metricas button.on{background:var(--accent2);color:#fff;border-color:var(--accent2)}
  svg.chart{width:100%;height:auto;display:block;touch-action:manipulation}
  svg.chart text{fill:var(--muted);font-size:12px}
  svg.chart .grid{stroke:#eef2f6}
  svg.chart .axis{stroke:#c9d3dd}
  svg.chart .line{fill:none;stroke:var(--accent2);stroke-width:2.5}
  svg.chart .pt{fill:#fff;cursor:pointer}
  svg.chart .pt.dom{stroke:#b3c0cc;stroke-width:1.5}
  svg.chart .pt.eff{stroke:var(--accent2);stroke-width:2}
  svg.chart .pt.sel{fill:var(--accent);stroke:var(--accent);stroke-width:2.5}
  svg.chart .hit{cursor:pointer}
  .dica{color:var(--muted);font-size:.82rem;margin:.5em 0 0;text-align:center}
  /* tabela preço × prob */
  .precos-tab tbody tr{cursor:pointer}
  .precos-tab tbody tr:hover{background:#f0f6ff}
  .precos-tab tbody tr.sel{background:#e4f5ea;outline:2px solid var(--accent);
    outline-offset:-2px}
  .precos-tab td{font-variant-numeric:tabular-nums}
  .precos-tab td.preco{font-weight:700}
  /* detalhe */
  #detalhe .topo{display:flex;flex-wrap:wrap;align-items:baseline;gap:10px;
    justify-content:space-between}
  .resumo{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0 14px}
  .kpi{flex:1 1 130px;background:#f4f7fa;border:1px solid var(--line);border-radius:10px;
    padding:10px 12px}
  .kpi .lab{color:var(--muted);font-size:.74rem;text-transform:uppercase;letter-spacing:.03em}
  .kpi .val{font-size:1.18rem;font-weight:800;font-variant-numeric:tabular-nums;margin-top:2px}
  .kpi .sub2{color:var(--muted);font-size:.72rem}
  .kpi.destaque{background:#e4f5ea;border-color:#bfe6cd}
  .kpi.destaque .val{color:var(--accent)}
  .det-tab .cel{position:relative;display:block;border-radius:6px;padding:7px 4px;
    font-variant-numeric:tabular-nums;font-weight:600}
  .det-tab .cel.marcado{box-shadow:inset 0 0 0 3px var(--accent);font-weight:800}
  .det-tab .cel.marcado::after{content:"✓";position:absolute;top:-7px;right:-3px;
    background:var(--accent);color:#fff;font-size:10px;line-height:1;padding:2px 3px;
    border-radius:6px}
  .det-tab td.mk{padding:5px 6px}
  .btns{display:flex;gap:10px;flex-wrap:wrap;margin-top:6px}
  .btn{cursor:pointer;border:none;border-radius:9px;padding:11px 16px;font-size:.92rem;
    font-weight:700;color:#fff;background:var(--accent)}
  .btn.sec{background:#f4f7fa;color:var(--ink);border:1px solid var(--line)}
  .selo{font-size:.84rem;font-weight:600}
  .selo.ok{color:var(--accent)} .selo.no{color:#c0392b}
  .rodape{color:var(--muted);font-size:.8rem;margin-top:22px}
  .leg{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:.8rem;margin-top:8px}
  .leg .bar{height:12px;width:120px;border-radius:3px;
    background:linear-gradient(90deg,#fff,rgb(22,150,60))}
  @media (max-width:560px){
    header h1{font-size:1.15rem}
    .kpi{flex:1 1 45%}
    th,td{padding:6px 6px;font-size:.86rem}
  }
  @media print{
    body{background:#fff}
    main{max-width:none;padding:0}
    .nao-imprimir{display:none!important}
    .card{box-shadow:none;border:1px solid #ccc;break-inside:avoid}
    .btns{display:none!important}
    .det-tab .cel.marcado{box-shadow:inset 0 0 0 2px #0a7d3b;
      -webkit-print-color-adjust:exact;print-color-adjust:exact}
    .prob,.cel,.kpi.destaque{-webkit-print-color-adjust:exact;print-color-adjust:exact}
  }
</style>
</head>
<body>
<main>
  <header>
    <h1 id="titulo">Loteca</h1>
    <p id="subtitulo"></p>
  </header>
  <section id="info" class="card nao-imprimir"></section>
  <section id="jogos-sec" class="card nao-imprimir"></section>
  <section id="grafico-sec" class="card nao-imprimir"></section>
  <section id="detalhe" class="card"></section>
  <p class="rodape nao-imprimir">Probabilidades de <b>consenso de mercado</b>
  (de-vig + média entre casas). Não há aposta de valor esperado positivo na
  Loteca — &ldquo;ótimo&rdquo; = maior chance de prêmio por real gasto. O mercado
  tende a subestimar zebras, então o P(14) real é provavelmente um pouco menor.
  Confira o volante na lotérica.</p>
</main>

<script id="dados" type="application/json">__DADOS_JSON__</script>
<script>
"use strict";
const D = JSON.parse(document.getElementById('dados').textContent);
const RES = ['1','X','2'];

const fmtBRL  = v => 'R$ ' + Number(v).toLocaleString('pt-BR',{minimumFractionDigits:2,maximumFractionDigits:2});
const fmtBRL0 = v => 'R$ ' + Number(v).toLocaleString('pt-BR',{maximumFractionDigits:0});
const intBR   = v => Number(v).toLocaleString('pt-BR');
function pct(p,dec){ if(p==null) return '—'; return (p*100).toFixed(dec==null?2:dec)+'%'; }
function umEm(p){ if(!p||p<=0) return '—'; return '1 em '+intBR(Math.round(1/p)); }
function esc(s){ return (s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function grad(p){
  if(p==null) return {bg:'transparent',fg:'#9aa6b2'};
  const r=Math.round(255+(22-255)*p), g=Math.round(255+(150-255)*p), b=Math.round(255+(60-255)*p);
  const lum=0.299*r+0.587*g+0.114*b;
  return {bg:'rgb('+r+','+g+','+b+')', fg: lum<150?'#fff':'#16451f'};
}

/* ---------- cabeçalho + info ---------- */
function buildHeader(){
  const m=D.meta;
  document.getElementById('titulo').textContent = 'Loteca — concurso '+(m.concurso??'?');
  const part=[];
  if(m.fim_apostas) part.push('Apostas até '+esc(m.fim_apostas));
  if(m.fonte_odds)  part.push('Odds: '+esc(m.fonte_odds));
  document.getElementById('subtitulo').textContent = part.join('  ·  ');
}
function buildInfo(){
  const m=D.meta, r=D.regras;
  let teto='';
  if(r.tabela_validada){
    teto = '<tr><td class="k">Teto verificado</td><td class="v">'+
      (r.linhas_tabela??'?')+' pares (D,T) conferidos ponto a ponto contra 2^D·3^T '+
      '(máx. '+(r.max_duplos??'?')+' duplos ou '+(r.max_triplos??'?')+' triplos)</td></tr>';
  }
  document.getElementById('info').innerHTML =
    '<h2>Concurso</h2><table class="info-tab"><tbody>'+
    '<tr><td class="k">Concurso</td><td class="v">'+(m.concurso??'—')+'</td></tr>'+
    '<tr><td class="k">Apostas até</td><td class="v">'+esc(m.fim_apostas||'—')+'</td></tr>'+
    '<tr><td class="k">Jogos</td><td class="v">'+
      (m.jogos_resolvidos!=null?(m.jogos_resolvidos+' de '+(m.jogos_total??14)+' com odds')
        :(m.jogos_total??14)+' jogos')+'</td></tr>'+
    '<tr><td class="k">Fonte das odds</td><td class="v">'+esc(m.fonte_odds||'—')+'</td></tr>'+
    '<tr><td class="k">Preço por combinação</td><td class="v">'+fmtBRL(r.preco)+'</td></tr>'+
    '<tr><td class="k">Aposta mínima</td><td class="v">'+fmtBRL(r.min_valor)+
      ' ('+r.min_combos+' combinações — ao menos 1 duplo)</td></tr>'+
    '<tr><td class="k">Maior bilhete oficial</td><td class="v">'+fmtBRL(r.max_valor)+
      ' ('+intBR(r.max_combos)+' apostas)</td></tr>'+
    teto+
    '</tbody></table>';
}

/* ---------- tabela de jogos ---------- */
function buildJogos(){
  let body='';
  D.jogos.forEach(j=>{
    const p=j.p;
    let cels;
    if(!p){
      cels = '<td>—</td><td>—</td><td>—</td>';
    }else{
      const fav = RES.reduce((a,k)=> p[k]>p[a]?k:a, '1');
      cels = RES.map(k=>{
        const g=grad(p[k]);
        return '<td class="prob'+(k===fav?' fav':'')+'" style="background:'+g.bg+';color:'+g.fg+'">'+
          pct(p[k],1)+'</td>';
      }).join('');
    }
    body += '<tr>'+
      '<td class="conf">'+esc(j.home)+' <span style="color:var(--muted)">×</span> '+esc(j.away)+'</td>'+
      cels+
      '<td>'+esc(j.data||'—')+'</td>'+
      '<td>'+(j.n_casas!=null?j.n_casas:'—')+'</td>'+
      '</tr>';
  });
  document.getElementById('jogos-sec').innerHTML =
    '<h2>Jogos do concurso</h2>'+
    '<p class="sub">Probabilidades 1X2 médias do mercado, na ordem da Loteca. '+
    '<b>Bets</b> = nº de casas de aposta coletadas por jogo.</p>'+
    '<div class="tabela-wrap"><table>'+
    '<thead><tr><th class="conf">Confronto</th>'+
    '<th>P(1)</th><th>P(X)</th><th>P(2)</th><th>Data</th><th>Bets</th></tr></thead>'+
    '<tbody>'+body+'</tbody></table></div>'+
    '<div class="leg"><span class="bar"></span> menor → maior probabilidade</div>';
}

/* ---------- gráfico custo × probabilidade ---------- */
const METRICAS = [
  {k:'p13mais', rot:'P(14 ou 13)'},
  {k:'p14',     rot:'P(14)'},
  {k:'p13',     rot:'P(13)'},
];
let metric = 'p13mais';
let modo = 'prob';            // 'prob' | 'alav'
let selIdx = D.destaque_idx||0;

/* ---------- alavancagem ----------
   Razão entre a probabilidade real (consenso de mercado) e a probabilidade do
   MESMO evento (14 acertos, 13 exatos, ou 13+) se todos os 3^14 resultados fossem
   equiprováveis. Base equiprovável de um bilhete:
     • 14 acertos: combos/3^14   (combos = nº de combinações cobertas = Π marcas)
     • 13 exatos : [Σ_j (3-m_j)·Π_{i≠j} m_i]/3^14 = combos·Σ_j(3/m_j - 1)/3^14
                   = combos·(2·simples + 0.5·duplos)/3^14
     • 13 ou 14  : soma das duas. */
const N3 = 4782969; // 3^14
function baseEqui(b, mk){
  const simples = 14 - b.d - b.t;
  const c13 = b.combos*(2*simples + 0.5*b.d); // nº de resultados com 13 exatos
  if(mk==='p14') return b.combos/N3;
  if(mk==='p13') return c13/N3;
  return (b.combos + c13)/N3;                  // p13mais
}
function alav(b, mk){ const base=baseEqui(b,mk); return base>0 ? b[mk]/base : null; }
function fmtAlav(a){ if(a==null) return '—';
  return '×'+(a>=100 ? intBR(Math.round(a)) : a.toLocaleString('pt-BR',{minimumFractionDigits:1,maximumFractionDigits:1})); }
// valor plotado/exibido conforme o modo atual, para uma métrica mk
function valY(b, mk){ return modo==='alav' ? (alav(b,mk)||0) : b[mk]; }
// texto da célula (tabela) para a métrica mk
function showVal(b, mk){ return modo==='alav' ? fmtAlav(alav(b,mk)) : pct(b[mk],1); }

const MODOS = [
  {k:'prob', rot:'Probabilidade'},
  {k:'alav', rot:'Alavancagem'},
];
function buildChartSec(){
  const btns = METRICAS.map(m=>
    '<button data-metric="'+m.k+'"'+(m.k===metric?' class="on"':'')+'>'+m.rot+'</button>').join('');
  const mbtns = MODOS.map(m=>
    '<button data-modo="'+m.k+'"'+(m.k===modo?' class="on"':'')+'>'+m.rot+'</button>').join('');
  document.getElementById('grafico-sec').innerHTML =
    '<div class="modos">'+mbtns+'</div>'+
    '<h2>Custo × probabilidade</h2>'+
    '<p class="sub">Um ponto por par <b>(duplos, triplos)</b> vendável — a aposta que '+
    '<b>maximiza P(14)</b> naquele preço. Pontos <b>cheios</b> = eficientes em '+
    'probabilidade (melhor prob. por aquele custo), <b>vazados</b> = dominados — os '+
    'mesmos pontos são realçados nos dois modos. <b>Alavancagem</b> = prob. real ÷ prob. '+
    'se todos os resultados fossem equiprováveis (quão melhor que o acaso). '+
    'Toque num ponto para ver o bilhete.</p>'+
    '<div class="metricas">'+btns+'</div>'+
    '<div id="chart-box"></div>'+
    '<p class="dica" id="dica-eixos"></p>'+
    '<div id="tabela-precos"></div>';
  document.querySelectorAll('.metricas button').forEach(b=>{
    b.addEventListener('click',()=>{
      metric=b.dataset.metric;
      document.querySelectorAll('.metricas button').forEach(x=>x.classList.toggle('on',x===b));
      drawChart();
    });
  });
  document.querySelectorAll('.modos button').forEach(b=>{
    b.addEventListener('click',()=>{
      modo=b.dataset.modo;
      document.querySelectorAll('.modos button').forEach(x=>x.classList.toggle('on',x===b));
      drawChart(); buildTabelaPrecos();
    });
  });
  drawChart(); buildTabelaPrecos();
}

function drawChart(){
  const B=D.bilhetes;
  const W=820,H=420,mL=72,mR=22,mT=20,mB=54;
  const xs=B.map(b=>b.custo), ys=B.map(b=>valY(b,metric));
  const xmin=Math.min(...xs), xmax=Math.max(...xs);
  let ymax=Math.max(...ys); if(!isFinite(ymax)||ymax<=0) ymax=1; ymax*=1.1;
  const X=v=> mL + (xmax===xmin?0.5:(v-xmin)/(xmax-xmin))*(W-mL-mR);
  const Y=v=> H-mB - (v/ymax)*(H-mT-mB);
  const dec = ymax<0.005?4 : ymax<0.05?3 : 2;
  const fmtYlab = yv => modo==='alav' ? ('×'+intBR(Math.round(yv))) : ((yv*100).toFixed(dec)+'%');

  let grid='',xlab='',ylab='';
  for(let i=0;i<=5;i++){
    const yv=ymax*i/5, y=Y(yv);
    grid += '<line class="grid" x1="'+mL+'" y1="'+y+'" x2="'+(W-mR)+'" y2="'+y+'"/>';
    ylab += '<text x="'+(mL-8)+'" y="'+(y+4)+'" text-anchor="end">'+fmtYlab(yv)+'</text>';
  }
  for(let i=0;i<=5;i++){
    const xv=xmin+(xmax-xmin)*i/5, x=X(xv);
    xlab += '<text x="'+x+'" y="'+(H-mB+20)+'" text-anchor="middle">'+fmtBRL0(xv)+'</text>';
  }
  const axis='<line class="axis" x1="'+mL+'" y1="'+mT+'" x2="'+mL+'" y2="'+(H-mB)+'"/>'+
             '<line class="axis" x1="'+mL+'" y1="'+(H-mB)+'" x2="'+(W-mR)+'" y2="'+(H-mB)+'"/>';
  // Eficiência é SEMPRE definida pela PROBABILIDADE (mesma fronteira nos dois modos):
  // um ponto é eficiente se nenhum bilhete igual/mais barato tem prob igual/maior
  // (B já vem ordenado por combos = custo asc). No modo alavancagem reaproveitamos
  // exatamente esses mesmos pontos, plotados no eixo de leverage.
  let melhor=-Infinity; const eficiente=new Set();
  B.forEach((b,i)=>{ if(b[metric]>melhor+1e-15){ melhor=b[metric]; eficiente.add(i); } });
  const poly = B.map((b,i)=>eficiente.has(i)?(X(b.custo)+','+Y(valY(b,metric))):null)
                .filter(Boolean).join(' ');
  let pts='',hit='';
  B.forEach((b,i)=>{
    const cx=X(b.custo), cy=Y(valY(b,metric));
    const cls = i===selIdx ? 'pt sel' : (eficiente.has(i) ? 'pt eff' : 'pt dom');
    const r   = i===selIdx ? 7 : (eficiente.has(i) ? 5 : 3.5);
    pts += '<circle class="'+cls+'" data-i="'+i+'" cx="'+cx+'" cy="'+cy+'" r="'+r+'"></circle>';
    hit += '<circle class="hit" data-i="'+i+'" cx="'+cx+'" cy="'+cy+'" r="14" fill="transparent"></circle>';
  });
  document.getElementById('chart-box').innerHTML =
    '<svg class="chart" viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="xMidYMid meet">'+
    grid+axis+ylab+xlab+
    '<polyline class="line" points="'+poly+'"/>'+pts+hit+'</svg>';
  document.querySelectorAll('#chart-box circle[data-i]').forEach(c=>{
    c.addEventListener('click',()=>selecionar(+c.dataset.i));
  });
  document.getElementById('dica-eixos').textContent =
    'Eixo X: custo do bilhete · Eixo Y: '+(modo==='alav'?'alavancagem (× sobre o acaso)':'probabilidade selecionada');
}

/* ---------- tabela preço × probabilidade ---------- */
function buildTabelaPrecos(){
  let rows='';
  D.bilhetes.forEach((b,i)=>{
    rows += '<tr data-i="'+i+'"'+(i===selIdx?' class="sel"':'')+'>'+
      '<td class="preco">'+fmtBRL(b.custo)+'</td>'+
      '<td>'+showVal(b,'p13mais')+'</td>'+
      '<td>'+showVal(b,'p14')+'</td>'+
      '<td>'+showVal(b,'p13')+'</td></tr>';
  });
  const L = modo==='alav' ? 'A' : 'P';
  document.getElementById('tabela-precos').innerHTML =
    '<h3>Tabela — '+(modo==='alav'?'alavancagem':'probabilidade')+' por preço</h3>'+
    '<p class="sub">As '+D.bilhetes.length+' apostas vendáveis (uma por par '+
    'duplos/triplos), ordenadas por preço.'+(modo==='alav'?' <b>A</b> = alavancagem.':'')+
    ' <b>Clique numa linha</b> para ver o bilhete abaixo — equivale a clicar no ponto do gráfico.</p>'+
    '<div class="tabela-wrap"><table class="precos-tab">'+
    '<thead><tr><th>Preço</th><th>'+L+'(14 ou 13)</th><th>'+L+'(14)</th>'+
    '<th>'+L+'(13)</th></tr></thead>'+
    '<tbody>'+rows+'</tbody></table></div>';
  document.querySelectorAll('.precos-tab tbody tr').forEach(tr=>{
    tr.addEventListener('click',()=>selecionar(+tr.dataset.i));
  });
}
function marcarLinha(i){
  document.querySelectorAll('.precos-tab tbody tr').forEach(tr=>
    tr.classList.toggle('sel', +tr.dataset.i===i));
}

/* ---------- detalhe do bilhete ---------- */
function selecionar(i){
  selIdx=i; drawChart(); marcarLinha(i); buildDetalhe(i);
}
function buildDetalhe(i){
  const b=D.bilhetes[i];
  const selo = b.valido
    ? '<span class="selo ok">✅ aposta válida na tabela oficial</span>'
    : '<span class="selo no">⚠️ '+esc(b.motivo)+'</span>';

  let rows='';
  D.jogos.forEach((j,k)=>{
    const marks=(b.marcas[k]||[]);
    const p=j.p;
    const cels = RES.map(res=>{
      const val = p? p[res] : null;
      const g=grad(val);
      const mk=marks.includes(res)?' marcado':'';
      return '<td class="mk"><span class="cel'+mk+'" style="background:'+g.bg+';color:'+g.fg+'">'+
        (p?pct(val,1):'—')+'</span></td>';
    }).join('');
    rows += '<tr>'+
      '<td class="conf">'+esc(j.home)+' <span style="color:var(--muted)">×</span> '+esc(j.away)+'</td>'+
      cels+'</tr>';
  });

  document.getElementById('detalhe').innerHTML =
    '<div class="topo"><h2 style="margin:0">Bilhete — '+intBR(b.combos)+' apostas · '+fmtBRL0(b.custo)+'</h2>'+
      selo+'</div>'+
    '<div class="resumo">'+
      kpi('P(14 ou 13)', pct(b.p13mais,1), umEm(b.p13mais)+' · alav. '+fmtAlav(alav(b,'p13mais')), true)+
      kpi('P(14)', pct(b.p14,1), umEm(b.p14)+' · alav. '+fmtAlav(alav(b,'p14')))+
      kpi('P(13 exato)', pct(b.p13,1), umEm(b.p13)+' · alav. '+fmtAlav(alav(b,'p13')))+
      kpi('Valor do bilhete', fmtBRL(b.custo), b.d+' duplo(s) · '+b.t+' triplo(s)')+
    '</div>'+
    '<p class="sub">Marque na Loteca os resultados destacados (✓) de cada jogo. '+
    'Células coloridas pela probabilidade do mercado.</p>'+
    '<div class="tabela-wrap"><table class="det-tab">'+
    '<thead><tr><th class="conf">Confronto</th><th>1</th><th>X</th><th>2</th></tr></thead>'+
    '<tbody>'+rows+'</tbody></table></div>'+
    '<div class="btns">'+
      '<button class="btn" onclick="window.print()">⬇ Exportar bilhete (PDF)</button>'+
    '</div>';
}
function kpi(lab,val,sub,destaque){
  return '<div class="kpi'+(destaque?' destaque':'')+'"><div class="lab">'+lab+'</div>'+
    '<div class="val">'+val+'</div><div class="sub2">'+sub+'</div></div>';
}

/* ---------- init ---------- */
buildHeader(); buildInfo(); buildJogos(); buildChartSec(); buildDetalhe(selIdx);
</script>
</body>
</html>
"""


def _payload_jogos(jogos):
    out = []
    for j in jogos:
        p = j["p"]
        out.append({
            "seq": j["seq"],
            "home": j["home"],
            "away": j["away"],
            "data": j["data"],
            "n_casas": j.get("n_casas"),
            "p": ({"1": p["1"], "X": p["X"], "2": p["2"]} if p else None),
        })
    return out


def _payload_bilhetes(jogos, fronteira):
    out = []
    for combos, _cov, niveis in fronteira:
        mt = metricas_bilhete(jogos, niveis)
        v = validar_bilhete(niveis)
        marc = marcacoes_bilhete(jogos, niveis)  # alinhado à ordem de jogos
        out.append({
            "combos": combos,
            "custo": v["custo"],
            "d": v["d"],
            "t": v["t"],
            "valido": v["valido"],
            "motivo": v["motivo"],
            "p14": mt["p14"],
            "p13": mt["p13"],
            "p13mais": mt["p13mais"],
            "marcas": [m["resultados"] for m in marc],
        })
    return out


def _idx_destaque(bilhetes, destaque_combos):
    """Índice do bilhete cujo combos == destaque (senão o maior ≤ destaque)."""
    if not bilhetes:
        return 0
    exato = [i for i, b in enumerate(bilhetes) if b["combos"] == destaque_combos]
    if exato:
        return exato[0]
    cabe = [i for i, b in enumerate(bilhetes) if b["combos"] <= destaque_combos]
    return cabe[-1] if cabe else 0


def montar_html(meta, jogos, fronteira, destaque_combos):
    """Gera a página HTML responsiva (auto-contida, sem dependências externas)."""
    pr = meta.get("precos") or {}
    fonte = pr.get("fonte", "fallback")
    quando = pr.get("atualizado_em")
    if "caixa.gov.br" in str(fonte):
        origem = "fonte oficial Caixa" + (f" · {quando}" if quando else "")
        oficial = True
    else:
        origem = "fallback embutido (fonte oficial indisponível)"
        oficial = False

    bilhetes = _payload_bilhetes(jogos, fronteira)
    dados = {
        "meta": {
            "concurso": meta.get("concurso"),
            "fim_apostas": meta.get("fim_apostas"),
            "fonte_odds": meta.get("fonte_odds"),
            "jogos_total": meta.get("jogos_total") or len(jogos),
            "jogos_resolvidos": meta.get("jogos_resolvidos"),
        },
        "regras": {
            "preco": PRECO,
            "min_combos": MIN_COMBOS,
            "max_combos": MAX_COMBOS_LIMITE,
            "min_valor": round(MIN_COMBOS * PRECO, 2),
            "max_valor": round(MAX_COMBOS_LIMITE * PRECO, 2),
            "origem": origem,
            "oficial": oficial,
            "tabela_validada": bool(pr.get("tabela_validada")),
            "linhas_tabela": pr.get("linhas_tabela"),
            "max_duplos": pr.get("max_duplos"),
            "max_triplos": pr.get("max_triplos"),
        },
        "jogos": _payload_jogos(jogos),
        "bilhetes": bilhetes,
        "destaque_idx": _idx_destaque(bilhetes, destaque_combos),
    }
    blob = json.dumps(dados, ensure_ascii=False)
    return _HTML_TEMPLATE.replace("__DADOS_JSON__", blob)



# ----------------------------------------------------------------------------
# Orquestração
# ----------------------------------------------------------------------------
def otimizar_loteca(numero, max_custo=None, destaque=48.0, salvar=True,
                    checar_precos=True, forcar_precos=False, verbose=True,
                    saida=None):
    global SAIDA_OVERRIDE
    if saida:
        SAIDA_OVERRIDE = saida
    # 1) resolve preços/regras na fonte oficial (cache + fallback embutido)
    precos = obter_precos(checar=checar_precos, forcar=forcar_precos,
                          verbose=verbose)
    aplicar_precos(precos)

    meta, jogos = carregar_jogos(numero)
    meta["precos"] = precos
    # teto oficial da Caixa limita qualquer orçamento pedido
    if max_custo is None:
        max_custo = MAX_COMBOS_LIMITE * PRECO
    max_combos = min(MAX_COMBOS_LIMITE, max(MIN_COMBOS, int(max_custo // PRECO)))
    dp = otimizar(jogos, max_combos)
    bilhetes = todos_bilhetes(dp)  # a melhor aposta de cada par (D,T) alcançável

    # validação explícita: cada bilhete tem de ser uma aposta vendável da tabela
    # oficial, com custo conferido contra o valor publicado.
    validacoes = [validar_bilhete(nv) for _c, _cov, nv in bilhetes]
    invalidos = [v for v in validacoes if not v["valido"]]

    destaque_combos = max(MIN_COMBOS, int(destaque // PRECO))
    html = montar_html(meta, jogos, bilhetes, destaque_combos)

    out_path = os.path.join(_dir_concurso(meta["concurso"]), "otimizacao.html")
    if salvar:
        _salvar_texto(out_path, html)
        if verbose:
            print(f"[ok] relatório salvo em {out_path}")
            print(f"[ok] {len(bilhetes)} apostas (uma por par D,T alcançável) "
                  f"(teto R$ {max_custo:.0f} = {max_combos} combos)")
            if invalidos:
                print(f"[ALERTA] {len(invalidos)} bilhete(s) fora da tabela "
                      f"oficial: {invalidos[0]['motivo']}")
            elif TABELA_DT:
                print(f"[ok] {len(bilhetes)} apostas validadas contra a "
                      f"tabela oficial de valor da aposta")
    return {"meta": meta, "bilhetes": bilhetes, "html": html, "path": out_path,
            "validacoes": validacoes}


def main():
    ap = argparse.ArgumentParser(description="Otimizador exato de apostas da Loteca.")
    ap.add_argument("concurso", type=int, help="número do concurso")
    ap.add_argument("--max-custo", type=float, default=None,
                    help="teto de custo em R$ para a fronteira "
                         "(default = maior bilhete oficial)")
    ap.add_argument("--destaque", type=float, default=48.0,
                    help="custo em R$ do bilhete detalhado em destaque (default 48)")
    ap.add_argument("--sem-checar-precos", action="store_true",
                    help="não toca a rede; usa cache fresco ou fallback embutido")
    ap.add_argument("--forcar-precos", action="store_true",
                    help="ignora o cache e rebusca o preço na fonte oficial")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--saida", default=None,
                    help="nome da subpasta sob data/analise p/ ler/gravar "
                         "(default: o número do concurso)")
    a = ap.parse_args()
    otimizar_loteca(a.concurso, max_custo=a.max_custo, destaque=a.destaque,
                    checar_precos=not a.sem_checar_precos,
                    forcar_precos=a.forcar_precos, salvar=True,
                    verbose=not a.quiet, saida=a.saida)


if __name__ == "__main__":
    main()
