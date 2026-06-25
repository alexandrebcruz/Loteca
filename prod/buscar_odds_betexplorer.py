#!/usr/bin/env python3
"""
Coletor de odds MULTI-CASA do BetExplorer.com — fonte HISTÓRICA para o backtest.

Por quê (≠ buscar_odds_flashscore.py): o Flashscore resolve jogo por um FEED de
agenda que só cobre ~7 dias; concursos antigos da Loteca caem fora desse horizonte
e a via-por-time do Flashscore erra homônimos/seleções (busca PT instável:
BRASIL→clube CRB, ESPANHA→jogador). O BetExplorer (mesma rede Livesport, mesmos
match-ids) é o irmão ARQUIVÍSTICO: tem histórico profundo e — decisivo — uma BUSCA
que devolve cada time JÁ DESAMBIGUADO pelo país e com a UF no nome
(`Santos (Brazil)`, `Atletico-MG (Brazil)`, `Atletico GO (Brazil)`, `Spain
(Europe)`), exatamente o sinal que a Loteca dá em siglaPais/siglaUF. Isso mata o
homônimo que derrubava o Flashscore no backtest.

Como funciona — VIA PRIMÁRIA (agenda do dia, 1 fetch compartilhado):
  A raiz `/br/?year=Y&month=M&day=D` devolve a agenda COMPLETA daquele dia (cada jogo
  num `<ul.table-main__matchInfo data-live=<id> data-dt=...>` cujo link já é a página
  da partida e cujos dois times estão no slug `<home-away>`). Como conhecemos os 14
  confrontos, casar (home,away) DENTRO da lista de um único dia dispensa busca e
  desambiguação (os DOIS lados têm de bater na mesma linha). [_resolver_via_dia]
  CUIDADO: só a RAIZ `/br/?year&month&day` honra a data; `/br/football/?date=` IGNORA
  e mostra sempre hoje. O feed é cacheado por data (uma data serve vários jogos).

Fallback — VIA-POR-TIME (busca desambiguada), usada só quando o dia não casa. Foi
alinhada ao resolvedor de event_id do Sofascore em três camadas:
  1. BUSCA  `gres/ajax/search.php?text=<nome>&sid=0&lang=br` -> candidatos de time
     (HTML). Filtra só `/br/football/team/<slug>/<id>/` (descarta jogadores e outros
     esportes) e extrai o país do sufixo "(País)". A busca já TRADUZ o nome (mandar
     "espanha" devolve "Spain"); por isso pontuo cada candidato contra o nome CRU e
     contra o canônico em INGLÊS (Espanha->Spain), e desempato por país (siglaPais) e
     pela UF no nome (Atletico-MG, Sport-ES). [_buscar_time/_melhor_time]
  2. CAMADA A — casar por TEAM-ID: resolvo AMBOS os times -> team_id e, na página da
     âncora, caso o confronto por ID (`_casar_por_id`) em vez de string — dispensa o
     de-para e mata homônimo, igual ao `modo=id` do Sofascore. String (`_casar_confronto`)
     vira fallback do fallback.
     CAMADA B — histórico FUNDO: a aba "Resumo" do time mostra ~20 jogos; o histórico
     mora em `/results/` (passados, ~1 temporada) e `/fixtures/` (futuros). `_eventos_time`
     carrega `/results/` e, se a data-alvo não estiver coberta, também `/fixtures/`
     (análogo ao paginar last/next do Sofascore). Datas além de ~1 temporada são
     alcançadas pela via-dia primária (parametrizada por data).
  3. PÁGINA DA PARTIDA `/br/football/<pais>/<liga>/<home-away>/<id>/` -> a tabela de
     comparação 1X2 já vem renderizada server-side (`tr[data-bid]`, ~20 casas, cada
     uma com as 3 cotações em `[data-odd]`). [coletar_odds]

CAMADA C — auditoria/resgate por LLM (opcional, `--auditar`): valida o match achado
(consultivo) e, se a via-dia E a via-por-time falharem, RESGATA — o LLM dá o nome
canônico de cada time, eu busco no BetExplorer e VERIFICO o confronto por team-id
antes de confiar (anti-alucinação). `--aplicar-apelido` grava o apelido fundamentado
no apelidos_loteca_betexplorer.json (de-para PRÓPRIO, separado do Sofascore/Flashscore).
Reusa a maquinaria concurso-agnóstica de buscar_eventid_sofascore (_llm_json + contexto).

A probabilidade de consenso sai do MESMO `estimar_prob` do analise_loteca (de-vig por
casa + média), com `invertido` orientando as colunas para a Loteca (1=mandante).

ESCOPO: este coletor é ADITIVO e serve o BACKTEST HISTÓRICO (calcula_backtest_loteca.py
--fonte betexplorer). O pipeline ao vivo / concurso aberto continua no Flashscore
(funciona dentro do horizonte de 7 dias + tem a infra de odds AO VIVO via WebSocket).

Uso (CLI):
    python3 buscar_odds_betexplorer.py 2026-05-31 "SANTOS" "VITORIA/BA" \
        --home-pais BRA --home-uf SP --away-pais BRA --away-uf BA

Uso (import):
    from buscar_odds_betexplorer import buscar_odds_betexplorer
    res = buscar_odds_betexplorer("2026-05-31", "SANTOS", "VITORIA")

Pré-requisitos: nodriver + Chrome/Xvfb. Sem segredos; proxy opcional via HubService.
"""
import os
import re
import sys
import json
import fcntl
import asyncio
import datetime
import argparse
from urllib.parse import quote

import nodriver as uc
from nodriver import cdp

# Matchers e geo reusados do resolvedor do Sofascore (lógica de nomes num lugar só).
# `_canonico` AQUI é o de-para em INGLÊS (Espanha->Spain) — é o certo para o passo de
# BUSCA, cujos resultados vêm em inglês. O confronto (passo 2) é PT-cru.
# A maquinaria de LLM (`_llm_json`, contexto/vocabulário da Loteca) é concurso-
# agnóstica — auditoria/resgate (camada C) a reusa direto.
from buscar_eventid_sofascore import (
    resolver_proxy, _egress_ip, normalize, name_score,
    _melhor_evento, _pista, _uf_no_texto, MATCH_THRESHOLD,
    _canonico as _canonico_en,
    _llm_json, _loteca_contexto, _pista_txt,
)
# Helpers de navegação/avaliação genéricos (origin-agnósticos) já prontos no Flashscore.
from buscar_odds_flashscore import _goto, _unwrap, _eval_json
# A estimativa de probabilidade é a MESMA do pipeline (de-vig + média).
from analise_loteca import estimar_prob, odds_por_casa

ORIGIN = "https://www.betexplorer.com"
SEARCH_EP = ORIGIN + "/gres/ajax/search.php?text={q}&sid=0&lang=br"

# threshold de aceitação do TIME na busca (name_score cru/canônico). Mais baixo que o
# de confronto: a busca traz muitos homônimos e o desempate fino é geo (país/UF).
TIME_THRESHOLD = 0.50

# País (nome em inglês como o BetExplorer mostra) -> ISO alpha-3, p/ casar com a
# siglaPais da Loteca (BRA, ARG…). Cobre o essencial: Brasil (homônimos de clube,
# todos "Brazil") + as nações usuais. Continentes ("Europe", "South America") não
# entram — seleção tem nome único, resolvida por name_score+canônico.
_PAIS_EN_A3 = {
    "brazil": "BRA", "argentina": "ARG", "uruguay": "URU", "paraguay": "PAR",
    "chile": "CHI", "colombia": "COL", "peru": "PER", "ecuador": "EQU",
    "bolivia": "BOL", "venezuela": "VEN", "portugal": "POR", "spain": "ESP",
    "england": "ING", "france": "FRA", "germany": "ALE", "italy": "ITA",
    "netherlands": "HOL", "belgium": "BEL", "mexico": "MEX", "usa": "EUA",
    "japan": "JAP", "south korea": "COR", "morocco": "MAR", "croatia": "CRO",
    "switzerland": "SUI", "scotland": "ESC", "serbia": "SER",
}

# Continentes sob os quais o BetExplorer agrupa as SELEÇÕES (clubes ficam sob país).
# Sinal forte p/ separar 'Ecuador (South America)' [seleção] de 'Sporting Club Ecuador
# Montanita (Ecuador)' [clube] quando o nome buscado é uma nação.
_CONTINENTES = {"europe", "south america", "north & central america",
                "asia", "africa", "oceania", "world"}

# Lacunas de grafia entre o canônico do Sofascore (de-para inglês) e como o
# BetExplorer escreve a SELEÇÃO. Pequeno e local: a maioria coincide (Spain, Germany,
# Saudi Arabia); só algumas divergem (o Sofascore usa francês/grafia própria).
_NACOES_BX = {
    "cote d ivoire": "Ivory Coast",       # Sofascore: Côte d'Ivoire
}


# De-para PRÓPRIO do BetExplorer (separado do Sofascore/Flashscore, conforme a regra
# "dois de-para separados" do CLAUDE.md). Aqui o resolvedor APRENDE apelidos com a
# auditoria (camada C, `--aplicar-apelido`): a grafia do BetExplorer às vezes diverge
# da do Sofascore (Ivory Coast x Côte d'Ivoire) e clubes têm nome canônico próprio
# (Atletico-MG). Consultado ANTES do canônico-EN do Sofascore. Ausência é inofensiva.
_APELIDOS_BX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "apelidos_loteca_betexplorer.json")


def _carregar_apelidos_bx(path=_APELIDOS_BX_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return {normalize(k): v for k, v in (d.get("times") or {}).items()}


_APELIDOS_BX = _carregar_apelidos_bx()


def _canonico_bx(nome):
    """De-para Loteca -> nome no BetExplorer (inglês). Ordem: (1) de-para PRÓPRIO do
    BetExplorer (aprendido pela auditoria), depois (2) o canônico do Sofascore
    (Costa do Marfim -> Côte d'Ivoire) com o ajuste de grafia do BetExplorer
    (-> Ivory Coast). Para clubes sem entrada devolve o nome cru."""
    base = normalize((nome or "").split("/", 1)[0].strip())
    if base in _APELIDOS_BX:
        return _APELIDOS_BX[base]
    en = _canonico_en(nome)
    return _NACOES_BX.get(normalize(en), en)


# --------------------------------------------------------------------------- #
# Consent (BetExplorer mostra o banner de cookies por texto, sem id estável)
# --------------------------------------------------------------------------- #
async def _consent(tab):
    try:
        await tab.evaluate(
            "(()=>{const b=[...document.querySelectorAll('button,a')]"
            ".find(x=>/aceitar|accept|concordo|agree|consent/i.test(x.innerText||''));"
            "if(b)b.click();})()")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Passo 1: BUSCA do time (gres/ajax/search.php) -> candidato desambiguado
# --------------------------------------------------------------------------- #
async def _buscar_time_uma(tab, termo):
    """Uma consulta ao endpoint de busca. -> [{nome, pais, id, slug, href}] (futebol).

    O endpoint devolve um fragmento HTML; parseio in-page injetando num <div>.
    Filtra `href ~ /br/football/team/<slug>/<id>/` (descarta /player/ e outros
    esportes) e extrai o país do sufixo "(País)" do título."""
    url = SEARCH_EP.format(q=quote((termo or "").strip()))
    body = _unwrap(await tab.evaluate(
        "(async()=>{try{const r=await fetch(%r);return await r.text();}"
        "catch(e){return '';}})()" % url, await_promise=True)) or ""
    if not body:
        return []
    return await _parse_busca(tab, body)


async def _buscar_time(tab, nome):
    """Busca um time pelo nome CRU e também pelo canônico em INGLÊS (Costa do
    Marfim->Côte d'Ivoire, Holanda->Netherlands), mesclando os candidatos. A busca do
    BetExplorer indexa seleções pelo nome inglês, então o termo PT cru às vezes nem
    traz a seleção (e traz homônimo de clube). -> [{nome, pais, id, slug, href}]."""
    termos = [nome]
    can = _canonico_bx(nome)
    if normalize(can) != normalize(nome):
        termos.append(can)
    out, vistos = [], set()
    for t in termos:
        for c in await _buscar_time_uma(tab, t):
            if c.get("id") and c["id"] not in vistos:
                vistos.add(c["id"])
                out.append(c)
    return out


async def _parse_busca(tab, body):
    """Parseia o HTML da busca (injeção in-page). -> lista de candidatos de time."""
    return await _eval_json(tab, r"""JSON.stringify((()=>{
        const d=document.createElement('div'); d.innerHTML=%s;
        const out=[];
        d.querySelectorAll('a.list-events__item__title, a').forEach(a=>{
          const href=a.getAttribute('href')||'';
          const m=href.match(/^\/[a-z]{2}\/football\/team\/([^\/]+)\/([A-Za-z0-9]{6,})\/$/);
          if(!m) return;
          const txt=(a.innerText||a.textContent||'').replace(/\s+/g,' ').trim();
          const pm=txt.match(/\(([^)]+)\)\s*$/);
          out.push({nome:txt.replace(/\s*\([^)]*\)\s*$/,'').trim(),
                    pais:pm?pm[1].trim():null, slug:m[1], id:m[2], href:href});
        });
        return out;
    })())""" % json.dumps(body)) or []


def _pais_match(pais_nome, pista):
    """True se o país (nome inglês do BetExplorer) bate com a siglaPais da Loteca."""
    if not pais_nome or not pista:
        return False
    a3 = _PAIS_EN_A3.get(normalize(pais_nome))
    return bool(a3 and a3 == (pista.get("alpha3") or "").upper())


def _melhor_time(cands, nome, pista):
    """Escolhe o time. Score: se há TRADUÇÃO (seleção mapeada: Holanda->Netherlands),
    o nome INGLÊS é a verdade — pontua por ele e rebaixa o cru (senão o homônimo de
    clube 'Holanda (Brazil)' empata em 1.0 com a seleção e ganha pela ordem). Sem
    tradução (clube), pontua pelo cru. Desempate geográfico: país da Loteca + UF no
    nome. -> (cand, base_score) ou (None, 0)."""
    can = _canonico_bx(nome)
    tem_traducao = normalize(can) != normalize(nome)
    melhor, melhor_rank, melhor_base = None, -1.0, 0.0
    for c in cands:
        cont = normalize(c.get("pais") or "") in _CONTINENTES
        if tem_traducao:
            # nação mapeada: o nome inglês é a verdade; seleção mora sob CONTINENTE.
            # Não dou bônus de país-de-clube aqui (senão um clube homônimo cujo país
            # bate, ex. 'Sporting Club Ecuador', supera a seleção).
            base = name_score(can, c["nome"])
            bonus = 0.15 if cont else 0.0
        else:
            base = name_score(nome, c["nome"])
            bonus = 0.0
            if pista:
                if _pais_match(c.get("pais"), pista):
                    bonus += 0.12
                if _uf_no_texto(normalize(c["nome"]), pista.get("uf")):
                    bonus += 0.10
        rank = base + bonus
        if rank > melhor_rank:
            melhor, melhor_rank, melhor_base = c, rank, base
    if not melhor or melhor_base < TIME_THRESHOLD:
        return None, (melhor_base if melhor else 0.0)
    return melhor, melhor_base


# --------------------------------------------------------------------------- #
# Passo 2: PÁGINA DO TIME -> jogos (mandante, visitante PT, data, link da partida)
# --------------------------------------------------------------------------- #
def _data_br(s):
    """'31.05.2026' -> date. None se ilegível."""
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", s or "")
    if not m:
        return None
    try:
        return datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


async def _scrape_pagina_time(tab, url, self_id=None):
    """Raspa os jogos de UMA página do time (resumo, /results/ ou /fixtures/).
    -> [{...,_home_id,_away_id,_match_href,_date}].

    Cada linha de jogo tem duas células de participante (link /team/<slug>/<id>/ do
    adversário e <strong> do próprio time), em ordem mandante->visitante; uma célula
    de data DD.MM.AAAA; e o link "detalhes" da partida (`/football/<pais>/<liga>/
    <home-away>/<id>/`, ≠ /team/). Linhas sem os dois participantes + link de partida
    são puladas. Captura o team-id do adversário (camada A — casar por ID): a célula
    do próprio time NÃO tem link (`<strong>`), então seu id é preenchido com `self_id`
    (o id do time-âncora já resolvido)."""
    await _goto(tab, url)
    # a página do time já vem renderizada; pequena espera de segurança
    await asyncio.sleep(2.0)
    rows = await _eval_json(tab, r"""JSON.stringify((()=>{
        const out=[];
        document.querySelectorAll('table.table-main tr').forEach(tr=>{
          const cells=[];
          tr.querySelectorAll('td').forEach(td=>{
            const a=td.querySelector('a[href*="/team/"]');
            const s=td.querySelector('strong');
            if(a){const m=(a.getAttribute('href')||'')
                    .match(/\/team\/[^\/]+\/([A-Za-z0-9]{6,})\//);
                  cells.push({name:(a.innerText||'').trim(),
                              id:m?m[1]:null, self:false});}
            else if(s) cells.push({name:(s.innerText||'').trim(),
                                   id:null, self:true});
          });
          if(cells.length<2) return;
          const mA=[...tr.querySelectorAll('a')].find(a=>{
            const h=a.getAttribute('href')||'';
            return /^\/[a-z]{2}\/football\/[^\/]+\/[^\/]+\/[^\/]+\/[A-Za-z0-9]{6,}\/$/.test(h)
                   && !/\/team\//.test(h);
          });
          if(!mA) return;
          const txt=(tr.innerText||'').replace(/\s+/g,' ').trim();
          const dm=txt.match(/\d{2}\.\d{2}\.\d{4}/);
          out.push({home:cells[0].name, away:cells[1].name,
                    home_id:cells[0].id, away_id:cells[1].id,
                    home_self:cells[0].self, away_self:cells[1].self,
                    data:dm?dm[0]:null, match_href:mA.getAttribute('href')});
        });
        return out;
    })())""") or []
    # adapta pro formato que _melhor_evento espera + anexa data e team-ids.
    eventos = []
    for r in rows:
        d = _data_br(r.get("data"))
        ts = int(datetime.datetime(d.year, d.month, d.day).timestamp()) if d else None
        hi = r.get("home_id") or (self_id if r.get("home_self") else None)
        ai = r.get("away_id") or (self_id if r.get("away_self") else None)
        eventos.append({
            "id": r.get("match_href"),
            "homeTeam": {"name": r.get("home", ""), "country": {}},
            "awayTeam": {"name": r.get("away", ""), "country": {}},
            "tournament": {"name": ""},
            "startTimestamp": ts,
            "_match_href": r.get("match_href"),
            "_date": d,
            "_home_id": hi,
            "_away_id": ai,
        })
    return eventos


async def _eventos_time(tab, href, self_id=None, data=None, janela_dias=3):
    """Camada B — histórico FUNDO do time. A aba "Resumo" (href base) mostra só ~20
    jogos (recentes + próximos); o histórico real mora nas sub-abas `/results/`
    (passados, ~1 temporada) e `/fixtures/` (futuros). Análogo ao `_eventos_do_time`
    do Sofascore (que pagina last/next até cobrir a data): carrega `/results/` e, só
    se a data-alvo ficar além do que ele cobre (ou for futura), também `/fixtures/`,
    mesclando e dedupando por match_href. -> lista de eventos. (Limite: o BetExplorer
    mostra ~1 temporada por sub-aba; datas mais antigas que isso são alcançadas pela
    via-dia primária, que é parametrizada por data.)"""
    base = ORIGIN + (href if href.startswith("/") else "/" + href).rstrip("/")
    alvo = None
    try:
        alvo = datetime.date.fromisoformat(data) if data else None
    except (TypeError, ValueError):
        pass

    eventos, vistos = [], set()

    def _merge(novos):
        for ev in novos:
            mh = ev.get("_match_href")
            if mh and mh not in vistos:
                vistos.add(mh)
                eventos.append(ev)

    _merge(await _scrape_pagina_time(tab, base + "/results/", self_id))
    # carrega os FUTUROS só se a data-alvo for além do que /results/ cobre (ou não
    # houver alvo) — poupa uma navegação no caso comum (backtest = data passada).
    datas = [e["_date"] for e in eventos if e.get("_date")]
    cobre_alvo = bool(alvo and datas and min(datas) <= alvo <= max(datas))
    if not cobre_alvo:
        _merge(await _scrape_pagina_time(tab, base + "/fixtures/", self_id))
    return eventos


def _melhor_evento_bi(eventos, home, away, ph, pa):
    """_melhor_evento RAW-FIRST + fallback canônico em INGLÊS. A página do time traz
    CLUBES no nome cru (Santos, Gremio — iguais ao da Loteca) mas SELEÇÕES em inglês
    (Spain, Saudi Arabia); então tento o nome cru e, se melhorar, o canônico-EN
    (Espanha->Spain). -> (ev, score, inv)."""
    ev, sc, inv = _melhor_evento(eventos, home, away, ph, pa)
    hc, ac = _canonico_bx(home), _canonico_bx(away)
    if (normalize(hc), normalize(ac)) != (normalize(home), normalize(away)):
        ev2, sc2, inv2 = _melhor_evento(eventos, hc, ac, ph, pa)
        if ev2 and sc2 > sc:
            ev, sc, inv = ev2, sc2, inv2
    return ev, sc, inv


def _casar_confronto(eventos, home, away, ph, pa, data, janela_dias):
    """Casa o confronto na lista de jogos do time, preferindo a data na janela.
    Reusa _melhor_evento (name_score + invertido + geo). -> (ev, score, inv) ou (None,..)."""
    if not eventos:
        return None, 0.0, False
    alvo = None
    try:
        alvo = datetime.date.fromisoformat(data)
    except (TypeError, ValueError):
        pass
    # subconjunto na janela de datas (se houver alvo); senão, tudo.
    subset = eventos
    if alvo:
        perto = [e for e in eventos if e.get("_date")
                 and abs((e["_date"] - alvo).days) <= janela_dias]
        if perto:
            subset = perto
    ev, score, inv = _melhor_evento_bi(subset, home, away, ph, pa)
    if (not ev or score < MATCH_THRESHOLD) and subset is not eventos:
        ev, score, inv = _melhor_evento_bi(eventos, home, away, ph, pa)  # alarga
    if not ev or score < MATCH_THRESHOLD:
        return None, score, inv
    return ev, score, inv


def _casar_por_id(eventos, home_id, away_id, data, janela_dias):
    """Camada A — casa o confronto por TEAM-ID (não por string), análogo ao
    `_match_por_id` do Sofascore: aceita a linha cujos dois team-ids batem com
    (home_id, away_id) em qualquer ordem, preferindo a data mais próxima na janela.
    `invertido` sai da ORIENTAÇÃO do BetExplorer (mandante=BX-home). Casar por ID
    dispensa o de-para de nomes e mata homônimo. -> (ev, invertido) ou (None, False)."""
    if not home_id or not away_id:
        return None, False
    alvo = None
    try:
        alvo = datetime.date.fromisoformat(data)
    except (TypeError, ValueError):
        pass
    cands = []
    for ev in eventos:
        hi, ai = ev.get("_home_id"), ev.get("_away_id")
        if hi and ai and {hi, ai} == {home_id, away_id}:
            inv = (hi == away_id)         # mandante do BX é o time-away da Loteca
            d = ev.get("_date")
            dist = abs((d - alvo).days) if (d and alvo) else 0
            cands.append((dist, ev, inv))
    if not cands:
        return None, False
    cands.sort(key=lambda c: c[0])
    dist, ev, inv = cands[0]
    if alvo and dist > janela_dias:       # confronto certo, mas fora da janela
        return None, False
    return ev, inv


# camada B½: só ancora-por-data-única se o lado-âncora resolveu com score ALTO
# (acima de TIME_THRESHOLD=0.50, que é frouxo). Evita disparar quando os dois
# lados são fracos — aí a unicidade-na-data poderia casar o jogo errado.
ANCORA_DATA_MIN = 0.80


def _casar_por_data_unica(eventos, data, anc_id, anc_lado):
    """Camada B½ — ÂNCORA + DATA ÚNICA. Quando A (id) e B (string) falham porque o
    ADVERSÁRIO vem brutalmente abreviado no programa (continentais: 'UNION S.FE',
    'UNIV CAT EQUADOR'), mas UM lado já é âncora confiável: na página de jogos da
    âncora ela joga ≤1×/dia, então se há EXATAMENTE UM jogo na data EXATA (±0) da
    Loteca, esse jogo É o confronto — sem depender do nome do adversário. Casa só na
    data exata + unicidade (um clube não joga 2× no mesmo dia) -> seguro contra
    falso-positivo. `invertido` sai da orientação do BX vs o lado (home/away) que a
    âncora ocupa na Loteca. -> (ev, invertido) ou (None, False)."""
    if not anc_id or not data:
        return None, False
    try:
        alvo = datetime.date.fromisoformat(data)
    except (TypeError, ValueError):
        return None, False
    namo = [e for e in eventos if e.get("_date") == alvo          # data EXATA (±0)
            and anc_id in {e.get("_home_id"), e.get("_away_id")}]  # âncora presente
    if len(namo) != 1:                       # 0 ou ambíguo -> não arrisca
        return None, False
    ev = namo[0]
    bx_home = (anc_id == ev.get("_home_id"))
    inv = (anc_lado == "home") ^ bx_home     # ver _casar_por_id p/ a semântica de `inv`
    return ev, inv


async def _resolver_jogo(tab, home, away, data, ph=None, pa=None, janela_dias=3,
                         verbose=True):
    """Resolve o jogo -> dict {match_href, home, away, invertido, score, metodo}.
    Levanta se não achar.

    Análogo ao `_buscar_por_id` do Sofascore: resolve AMBOS os times -> team_id (via
    busca desambiguada) e casa o confronto por ID na página da âncora (maior score);
    se a busca não der os dois ids, ou o id não casar, cai no match por string
    (`_casar_confronto`). Tenta a âncora de maior confiança e, se falhar, a outra."""
    times = {}
    for lado, nome, p in (("home", home, ph), ("away", away, pa)):
        cand, sc = _melhor_time(await _buscar_time(tab, nome), nome, p)
        if cand:
            cand = dict(cand, _score=sc, _lado=lado)
            times[lado] = cand
            if verbose:
                print(f"[bx] '{nome}' -> {cand['nome']} ({cand.get('pais')}) "
                      f"[{cand['id']}]", file=sys.stderr)
        elif verbose:
            print(f"[bx] busca não desambiguou '{nome}' (melhor name_score={sc:.2f})",
                  file=sys.stderr)

    home_id = (times.get("home") or {}).get("id")
    away_id = (times.get("away") or {}).get("id")
    ordem = sorted(times.values(), key=lambda t: -t["_score"])

    melhor = None
    for anc in ordem:
        eventos = await _eventos_time(tab, anc["href"], self_id=anc["id"],
                                      data=data, janela_dias=janela_dias)
        # 1) camada A: casar por team-ID (mais robusto; dispensa de-para).
        ev, inv = _casar_por_id(eventos, home_id, away_id, data, janela_dias)
        metodo = "busca+id"
        if not ev:
            # 2) fallback: casar por string (name_score + invertido + geo).
            ev, score, inv = _casar_confronto(eventos, home, away, ph, pa,
                                               data, janela_dias)
            metodo = "busca+time"
            # 2½) camada B½: adversário abreviado (continental) furou A e B, mas a
            # âncora é forte e há UM só jogo dela na data exata -> esse é o jogo.
            if not ev and anc.get("_score", 0.0) >= ANCORA_DATA_MIN:
                ev2, inv2 = _casar_por_data_unica(eventos, data, anc.get("id"),
                                                  anc.get("_lado"))
                if ev2:
                    ev, inv, score = ev2, inv2, round(anc["_score"], 3)
                    metodo = "busca+ancora-data"
                    if verbose:
                        print(f"[bx] âncora+data-única: {ev['homeTeam']['name']} x "
                              f"{ev['awayTeam']['name']} ({ev.get('_date')}) "
                              f"via '{anc['nome']}'", file=sys.stderr)
        else:
            score = 1.0
        if ev:
            melhor = {
                "match_href": ev["_match_href"],
                "home": ev["homeTeam"]["name"], "away": ev["awayTeam"]["name"],
                "data_exibida": ev["_date"].isoformat() if ev.get("_date") else None,
                "invertido": inv, "score": round(score, 3),
                "mid": _mid_da_href(ev["_match_href"]), "metodo": metodo,
            }
            break
    if not melhor:
        raise RuntimeError(
            f"não resolvi '{home}' x '{away}' ({data}) no BetExplorer "
            f"(busca/confronto abaixo do threshold).")
    return melhor


# --------------------------------------------------------------------------- #
# Passo 3: PÁGINA DA PARTIDA -> tabela 1X2 multi-casa (server-side)
# --------------------------------------------------------------------------- #
async def coletar_odds(tab, match_href):
    """Abre a partida e raspa a comparação 1X2 de todas as casas. -> dict no formato
    de buscar_odds_flashscore.coletar_odds (odds_por_casa[].odds_1x2={casa,empate,fora},
    na orientação do BetExplorer: casa=mandante, fora=visitante)."""
    url = ORIGIN + (match_href if match_href.startswith("/") else "/" + match_href)
    # Navegação com RETRY: o load da página da partida às vezes cai em
    # `chrome-error://chromewebdata/` (erro de rede transitório) — sem retry, o
    # jogo saía com n_casas=0 mesmo tendo mercado 1X2. Re-navega até render a
    # tabela `tr[data-bid]`; detecta o chrome-error cedo p/ não esperar 12s à toa.
    rendered = False
    for tentativa in range(3):
        await _goto(tab, url)
        for _ in range(12):
            try:
                loc = str(_unwrap(await tab.evaluate("location.href")))
                n = int(_unwrap(await tab.evaluate(
                    "document.querySelectorAll('tr[data-bid]').length")))
            except Exception:
                loc, n = "", 0
            if n > 0:
                rendered = True
                break
            if loc.startswith("chrome-error"):
                break          # nav falhou — re-navega já, sem esperar o resto
            await asyncio.sleep(1.0)
        if rendered:
            break
        await asyncio.sleep(2.0 * (tentativa + 1))   # backoff antes de re-tentar

    dados = await _eval_json(tab, r"""JSON.stringify((()=>{
        const casas=[];
        document.querySelectorAll('tr[data-bid]').forEach(tr=>{
          // id ESTÁVEL da casa = data-bookie-id (o data-bid da <tr> é id da linha/oferta);
          // NOME da casa = texto do <a> (ex.: "1xBet.br"); data-bookmaker/title vêm vazios.
          const bid=(tr.getAttribute('data-bookie-id')||tr.getAttribute('data-bid')||'').trim();
          const a=tr.querySelector('a');
          const nome=(a?(a.innerText||a.textContent||'').trim():'')
                || tr.getAttribute('data-bookmaker')
                || (a?(a.getAttribute('title')||'').trim():'')
                || ('bk'+bid);
          const odds=[...tr.querySelectorAll('[data-odd-current],[data-odd]')]
            .map(e=>e.getAttribute('data-odd-current')||e.getAttribute('data-odd'))
            .filter(v=>v && /^\d+(\.\d+)?$/.test(v));
          if(odds.length>=3) casas.push({casa:nome, bookmaker_id:bid, odds:odds.slice(0,3)});
        });
        const seen={}, out=[];
        casas.forEach(c=>{ if(!seen[c.bookmaker_id]){seen[c.bookmaker_id]=1; out.push(c);} });
        const jogo=(document.querySelector('h1, .list-breadcrumb__item--active')||{}).innerText||'';
        return {casas:out, jogo:jogo.replace(/\s+/g,' ').trim(), url:location.href};
    })())""") or {}

    casas = []
    for c in (dados.get("casas") or []):
        o = c.get("odds") or []
        casas.append({
            "casa": c.get("casa"),
            "bookmaker_id": c.get("bookmaker_id"),
            "odds_1x2": {
                "casa": float(o[0]) if len(o) > 0 else None,
                "empate": float(o[1]) if len(o) > 1 else None,
                "fora": float(o[2]) if len(o) > 2 else None,
            },
        })
    if not casas:
        raise RuntimeError(f"nenhuma casa raspada em {dados.get('url') or url} "
                           f"(partida sem mercado 1X2?).")
    return {
        "url": dados.get("url") or url,
        "jogo_label": dados.get("jogo"),
        "n_casas": len(casas),
        "casas_disponiveis": [c["casa"] for c in casas],
        "odds_por_casa": casas,
        "fonte": "betexplorer.com",
    }


# --------------------------------------------------------------------------- #
# Via PRIMÁRIA: agenda do DIA inteiro num fetch só (`/br/?year=&month=&day=`)
# --------------------------------------------------------------------------- #
# Diferente do `/br/football/?date=` (que IGNORA a data e mostra sempre hoje), a
# RAIZ `/br/?year=Y&month=M&day=D` devolve a agenda completa daquele dia: cada jogo
# é um `<ul class="table-main__matchInfo" data-live=<id> data-dt=...>` cujo link já
# é a PÁGINA DA PARTIDA (`/br/football/<pais>/<liga>/<home-away>/<id>/`, a mesma que
# `coletar_odds` consome) e cujos dois times estão no slug `<home-away>`. Como
# conhecemos os 14 confrontos, casar (home,away) DENTRO da lista de um único dia
# dispensa a busca por time e a desambiguação de homônimo/seleção: os DOIS lados têm
# de bater na MESMA linha. Fica como via primária; a via-por-time é o fallback.
DIA_EP = ORIGIN + "/br/?year={y}&month={m}&day={d}"
_CACHE_DIA = {}  # (ano,mes,dia) -> [jogos]; datas de backtest são passadas (estáveis)


def _slug_da_href(href):
    """'/br/football/brazil/serie-a-betano/santos-vitoria/C8XJVmQE/' -> 'santos-vitoria'."""
    partes = [p for p in (href or "").split("/") if p]
    return partes[-2] if len(partes) >= 2 else ""


def _mid_da_href(href):
    """Último segmento da href da partida = o MATCH-ID da Livesport (mesmo `mid` do
    Flashscore — ambos Livesport; ver buscar_odds_flashscore docstring). Propagá-lo
    deixa o BetExplorer compatível com a ponte de odds AO VIVO (WebSocket do
    Flashscore, que indexa por esse mid). -> str|None."""
    partes = [p for p in (href or "").split("/") if p]
    return partes[-1] if partes else None


def _data_do_dt(dt):
    """data-dt '31,5,2026,1,00' -> date(2026,5,31). None se ilegível."""
    try:
        d, m, y = (dt or "").split(",")[:3]
        return datetime.date(int(y), int(m), int(d))
    except Exception:  # noqa: BLE001
        return None


async def _feed_dia(tab, ano, mes, dia):
    """Baixa a agenda completa de UM dia e devolve [{href, slug, data, id, score}].
    Cacheado por (ano,mes,dia) — no backtest a mesma data serve vários jogos."""
    chave = (int(ano), int(mes), int(dia))
    if chave in _CACHE_DIA:
        return _CACHE_DIA[chave]
    await _goto(tab, DIA_EP.format(y=ano, m=mes, d=dia))
    for _ in range(12):  # espera ativa pelos <ul> de jogo
        try:
            n = int(_unwrap(await tab.evaluate(
                "document.querySelectorAll('ul.table-main__matchInfo[data-dt]').length")))
        except Exception:  # noqa: BLE001
            n = 0
        if n > 0:
            break
        await asyncio.sleep(1.0)
    bruto = await _eval_json(tab, r"""JSON.stringify((()=>{
        const out=[];
        document.querySelectorAll('ul.table-main__matchInfo[data-dt]').forEach(ul=>{
          const dt=ul.getAttribute('data-dt');
          const now=ul.getAttribute('data-dt-now');
          const id=ul.getAttribute('data-live');
          let href='';
          ul.querySelectorAll('a[href]').forEach(a=>{
            const h=a.getAttribute('href')||'';
            if(/^\/[a-z]{2}\/football\/[^\/]+\/[^\/]+\/[^\/]+\/[A-Za-z0-9]{6,}\/?$/.test(h)
               && !/\/team\//.test(h)) href=h;
          });
          const sc=(ul.querySelector('[data-live-cell="score"]')||{}).textContent||'';
          // status do jogo: célula data-live-cell="time" (FIN / minuto / hora / ADI…)
          const st=(ul.querySelector('[data-live-cell="time"], .table-main__matchStatus')
                    ||{}).textContent||'';
          if(href) out.push({dt, now, id, href,
                             score:sc.replace(/\s+/g,'').trim(),
                             status:st.replace(/\s+/g,' ').trim()});
        });
        return out;
    })())""") or []
    jogos = []
    for b in bruto:
        href = b.get("href") or ""
        jogos.append({"href": href, "slug": _slug_da_href(href),
                      "data": _data_do_dt(b.get("dt")), "id": b.get("id"),
                      "score": b.get("score"), "status": b.get("status"),
                      "dt_raw": b.get("dt"), "dt_now": b.get("now")})
    _CACHE_DIA[chave] = jogos
    return jogos


async def estado_por_mid(tab, mid, data_iso, janela_dias=3):
    """Placar + status + horário de UM jogo (por mid) lidos do FEED-DO-DIA do
    BetExplorer — mesma rede Livesport, então placar/status ticam ao re-buscar.
    Reusa `_feed_dia` (cacheado por data). -> dict ou None:
      {placar:'4-1'|None, status_raw:'FIN'|'67'|'21:00'|…, dt_raw, dt_now}.
    O acompanhamento mapeia `status_raw` p/ agendado/ao_vivo/finalizado/sorteio e
    converte dt_raw->UTC usando dt_now (relógio do servidor) p/ achar o fuso."""
    try:
        alvo = datetime.date.fromisoformat(data_iso)
    except (TypeError, ValueError):
        alvo = None
    deltas = [0]
    for k in range(1, max(janela_dias, 0) + 1):
        deltas += [-k, k]
    base = alvo or datetime.date.today()
    for dd in deltas:
        d = base + datetime.timedelta(days=dd)
        for jg in await _feed_dia(tab, d.year, d.month, d.day):
            if jg.get("id") == mid:
                sc = (jg.get("score") or "").strip()
                m = re.search(r"(\d+)\D+(\d+)", sc)
                return {"placar": (f"{m.group(1)}-{m.group(2)}" if m else None),
                        "status_raw": jg.get("status"), "dt_raw": jg.get("dt_raw"),
                        "dt_now": jg.get("dt_now"), "mid": mid}
    return None


def _match_dia(jogos, home, away):
    """Acha (home,away) na lista de um dia. Para cada slug `a-b-c`, testa todo corte
    em 2 lados e as duas orientações; aceita o melhor cujos DOIS lados batem (min dos
    dois name_scores). Casa o nome CRU e o canônico-EN (Espanha->Spain). -> melhor dict
    {match_href, invertido, score, slug, data} (já filtrado por MATCH_THRESHOLD) ou None."""
    alvo_h = [home] + ([_canonico_bx(home)]
                       if normalize(_canonico_bx(home)) != normalize(home) else [])
    alvo_a = [away] + ([_canonico_bx(away)]
                       if normalize(_canonico_bx(away)) != normalize(away) else [])
    melhor = None
    for jg in jogos:
        partes = (jg.get("slug") or "").split("-")
        if len(partes) < 2:
            continue
        for i in range(1, len(partes)):
            esq, dir_ = " ".join(partes[:i]), " ".join(partes[i:])
            s_h_esq = max(name_score(h, esq) for h in alvo_h)
            s_a_dir = max(name_score(a, dir_) for a in alvo_a)
            s_h_dir = max(name_score(h, dir_) for h in alvo_h)
            s_a_esq = max(name_score(a, esq) for a in alvo_a)
            s_norm, s_inv = min(s_h_esq, s_a_dir), min(s_h_dir, s_a_esq)
            s, inv = ((s_norm, False) if s_norm >= s_inv else (s_inv, True))
            if melhor is None or s > melhor["score"]:
                melhor = {"match_href": jg["href"], "invertido": inv, "score": s,
                          "slug": jg["slug"], "data": jg["data"]}
    if melhor and melhor["score"] >= MATCH_THRESHOLD:
        return melhor
    return None


async def _resolver_via_dia(tab, home, away, data_iso, janela_dias=3, verbose=True):
    """Resolve o confronto pela agenda do DIA (e vizinhos ±janela se preciso).
    -> {match_href, home, away, data_exibida, invertido, score, metodo} ou None."""
    try:
        alvo = datetime.date.fromisoformat(data_iso)
    except Exception:  # noqa: BLE001
        return None
    # tenta o dia exato; depois alarga simetricamente até a janela.
    deltas = [0]
    for k in range(1, max(janela_dias, 0) + 1):
        deltas += [-k, k]
    acumulado = []
    for dd in deltas:
        d = alvo + datetime.timedelta(days=dd)
        acumulado += await _feed_dia(tab, d.year, d.month, d.day)
        m = _match_dia(acumulado, home, away)
        if m:
            esq, dir_ = "", ""
            partes = (m["slug"] or "").split("-")
            # apenas p/ rótulo: parte mais provável de cada lado (não crítico)
            esq, dir_ = partes[0], partes[-1]
            he, aw = (dir_, esq) if m["invertido"] else (esq, dir_)
            return {"match_href": m["match_href"], "home": he, "away": aw,
                    "data_exibida": (m["data"].isoformat() if m["data"] else None),
                    "invertido": m["invertido"], "score": round(m["score"], 3),
                    "mid": _mid_da_href(m["match_href"]), "metodo": "dia"}
    return None


# --------------------------------------------------------------------------- #
# Camada C: auditoria + resgate por LLM (Claude via Hub), análogo ao Sofascore.
# Reusa a maquinaria concurso-agnóstica (contexto/vocabulário da Loteca, _llm_json).
#   (1) valida o match achado (consultivo, não altera) — pega falso-positivo;
#   (2) resgata o NÃO-achado: o LLM dá o nome canônico de cada time, EU busco no
#       BetExplorer e VERIFICO o confronto por TEAM-ID antes de confiar (anti-
#       alucinação — idêntico ao gate do Sofascore, só que por id do BetExplorer).
# --------------------------------------------------------------------------- #
def _aplicar_apelido_bx(de, para, path=_APELIDOS_BX_PATH):
    """Acrescenta {de: para} ao apelidos_loteca_betexplorer.json. -> 'adicionado' |
    'ja-existe' | 'erro: ...'. Read-modify-write sob lock EXCLUSIVO (fcntl) + escrita
    atômica (tmp + os.replace), como o `_aplicar_apelido` do Sofascore. Reflete também
    em memória (`_APELIDOS_BX`) p/ este processo."""
    try:
        lock_f = open(path + ".lock", "w")
    except Exception as e:  # noqa: BLE001
        return f"erro: não abri o lock ({e})"
    try:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}
        except Exception as e:  # noqa: BLE001
            return f"erro: não li o apelidos_betexplorer.json ({e})"
        times = data.setdefault("times", {})
        if normalize(de) in {normalize(k) for k in times}:
            return "ja-existe"
        times[de] = para
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001
            return f"erro: não gravei o apelidos_betexplorer.json ({e})"
        _APELIDOS_BX[normalize(de)] = para
        return "adicionado"
    finally:
        fcntl.flock(lock_f, fcntl.LOCK_UN)
        lock_f.close()


async def _candidatos_time_bx(tab, nome, top=6):
    """Times reais que a busca do BetExplorer devolve p/ um nome — fatos p/ o LLM ver
    a grafia. -> [{nome, pais}]."""
    out = []
    for c in await _buscar_time(tab, nome):
        out.append({"nome": c.get("nome"), "pais": c.get("pais")})
        if len(out) >= top:
            break
    return out


async def _auditar_match_bx(reg, jg, ph, pa, model):
    """Fase 1: o LLM julga se o jogo casado é o certo. Consultivo (não altera).
    -> dict {veredito, confianca, motivo, alias_sugerido}."""
    rs = reg.get("resolvido") or {}
    prompt = (
        "Confira se o jogo casado pelo sistema e REALMENTE o jogo da Loteca.\n\n"
        + _loteca_contexto(jg["home"], jg["away"]) +
        f"LOTECA: '{jg['home']}' x '{jg['away']}' em {jg['data']} "
        f"(mandante: {_pista_txt(ph)}; visitante: {_pista_txt(pa)}).\n"
        f"CASOU COM (BetExplorer): '{rs.get('home')}' x '{rs.get('away')}' em "
        f"{rs.get('data_exibida')}, metodo={rs.get('metodo')}, "
        f"score={rs.get('score')}, invertido={rs.get('invertido')}.\n\n"
        "Considere: time errado/homonimo (ex.: Atletico-PR x Atletico Madrid), "
        "pais ou divisao incompativel, data distante. Diferenca de grafia/idioma/"
        "sigla e NORMAL e nao e erro.\n"
        'Responda JSON: {"veredito":"ok"|"suspeito","confianca":0..1,'
        '"motivo":"...","alias_sugerido":{"de":"<nome loteca>",'
        '"para":"<nome betexplorer>"}|null}.'
    )
    try:
        out = await _llm_json(prompt, model, max_tokens=400)
        out["_ok"] = True
        print(f"[auditoria] veredito={out.get('veredito')} "
              f"({out.get('confianca')}): {out.get('motivo')}", file=sys.stderr)
        return out
    except Exception as e:  # noqa: BLE001
        print(f"[auditoria] falhou: {e}", file=sys.stderr)
        return {"_ok": False, "erro": str(e)[:200]}


async def _resgatar_llm_bx(tab, jg, ph, pa, janela_dias, erro_fb, model,
                           verbose=True):
    """Fase 2: jogo NÃO achado. O LLM dá o NOME CANONICO de cada time (resolve apelido
    que a busca nao indexa); EU busco no BetExplorer, resolvo os team-ids e VERIFICO o
    confronto por ID na pagina do time antes de confiar. -> ev dict (metodo
    'llm-resgate' + apelido_sugerido) ou levanta (anti-alucinacao)."""
    home_in, away_in = jg["home"], jg["away"]
    cand_h = await _candidatos_time_bx(tab, home_in)
    cand_a = await _candidatos_time_bx(tab, away_in)

    def _fmt(c):
        return ("; ".join(f"{x['nome']} ({x['pais']})" for x in c)
                or "(a busca nao trouxe nada)")
    prompt = (
        "O jogo da Loteca abaixo NAO foi encontrado no BetExplorer. Diga, para cada "
        "time, o NOME CANONICO exato pelo qual ele aparece no BetExplorer (em INGLES "
        "para selecoes/paises), para eu buscar. Use seu conhecimento de futebol "
        "(apelidos GALO=Atletico Mineiro, TIMAO=Corinthians; siglas; selecao em "
        "ingles) e o contexto (UF/pais, o adversario). A lista que minha busca ja "
        "retornou serve so p/ voce ver a grafia do BetExplorer.\n\n"
        + _loteca_contexto(home_in, away_in) +
        f"LOTECA: '{home_in}' x '{away_in}' em {jg['data']} "
        f"(mandante: {_pista_txt(ph)}; visitante: {_pista_txt(pa)}).\n"
        f"Busca p/ '{home_in}' retornou: {_fmt(cand_h)}\n"
        f"Busca p/ '{away_in}' retornou: {_fmt(cand_a)}\n\n"
        'Responda JSON: {"home_busca":"<nome p/ buscar>",'
        '"away_busca":"<nome p/ buscar>","motivo":"...",'
        '"alias_sugerido":{"de":"<nome loteca>","para":"<nome betexplorer>"}|null}.'
    )
    escolha = await _llm_json(prompt, model, max_tokens=400)
    hq, aq = escolha.get("home_busca"), escolha.get("away_busca")
    if verbose:
        print(f"[resgate] LLM sugere buscar '{hq}' x '{aq}': "
              f"{escolha.get('motivo')}", file=sys.stderr)
    if not hq or not aq:
        raise RuntimeError(f"{erro_fb or 'não achei o jogo'}; LLM não sugeriu nomes.")

    # resolve os nomes sugeridos -> team-ids reais (busca + desempate geo)
    th, _sh = _melhor_time(await _buscar_time(tab, hq), hq, ph)
    ta, _sa = _melhor_time(await _buscar_time(tab, aq), aq, pa)
    if not th or not ta:
        raise RuntimeError(f"{erro_fb or 'não achei o jogo'}; nomes do LLM "
                           f"('{hq}', '{aq}') não acharam time no BetExplorer.")

    # VERIFICA por ID: existe confronto entre esses ids na janela? (gate anti-alucinação)
    anc, outro = (th, ta) if _sh >= _sa else (ta, th)
    eventos = await _eventos_time(tab, anc["href"], self_id=anc["id"],
                                  data=jg["data"], janela_dias=janela_dias)
    ev, inv = _casar_por_id(eventos, th["id"], ta["id"], jg["data"], janela_dias)
    if not ev:
        raise RuntimeError(
            f"{erro_fb or 'não achei o jogo'}; resgate do LLM "
            f"('{th['nome']}' x '{ta['nome']}') NÃO tem confronto verificavel em "
            f"±{janela_dias}d de {jg['data']} — descartado (anti-alucinacao).")
    res = {
        "match_href": ev["_match_href"],
        "home": ev["homeTeam"]["name"], "away": ev["awayTeam"]["name"],
        "data_exibida": ev["_date"].isoformat() if ev.get("_date") else None,
        "invertido": inv, "score": 1.0, "metodo": "llm-resgate",
        "mid": _mid_da_href(ev["_match_href"]),
        "apelido_sugerido": escolha.get("alias_sugerido"),
    }
    if verbose:
        print(f"[resgate] VERIFICADO: {res['home']} x {res['away']} "
              f"({res['data_exibida']}). Apelido: {res.get('apelido_sugerido')}",
              file=sys.stderr)
    return res


def _sugestao_apelido_bx(reg, jg):
    """Deriva (de, para) FUNDAMENTADO p/ gravar no apelidos_betexplorer.json: 'de' tem
    de ser um dos nomes da Loteca consultados e 'para' é o nome REAL do BetExplorer
    daquele lado (NÃO o texto livre do LLM — anti-alucinação no valor). -> (de,para) ou None."""
    rs = reg.get("resolvido") or {}
    inv = bool(rs.get("invertido"))
    hs, asof = rs.get("home"), rs.get("away")
    para_home = asof if inv else hs        # nome BetExplorer do mandante da Loteca
    para_away = hs if inv else asof        # nome BetExplorer do visitante da Loteca

    sug = reg.get("apelido_sugerido")      # resgate (fase 2) tem prioridade
    if not sug:
        aud = reg.get("auditoria") or {}
        if aud.get("veredito") == "ok":
            sug = aud.get("alias_sugerido")
    if not isinstance(sug, dict):
        return None
    de = (sug.get("de") or "").strip()
    if not de:
        return None
    nd = normalize(de)
    if nd == normalize(jg["home"]):
        para = para_home
    elif nd == normalize(jg["away"]):
        para = para_away
    else:
        return None
    if not para or normalize(de) == normalize(para):
        return None
    return de, para


# --------------------------------------------------------------------------- #
# Browser (nodriver) + proxy — espelha _abrir_browser do analise_loteca
# --------------------------------------------------------------------------- #
async def abrir_browser(proxy, country, verbose=True):
    """Sobe o Chrome com proxy opcional e devolve (browser, tab) com o consent aceito."""
    pr = resolver_proxy(proxy, country)
    args = ["--no-sandbox"]
    if pr:
        host, port, _u, _p = pr
        args.append(f"--proxy-server=http://{host}:{port}")
        if verbose:
            print(f"[proxy] {proxy}/{(country or '').upper()} via {host}:{port}",
                  file=sys.stderr)
    browser = await uc.start(browser_args=args, sandbox=False)
    tab = await browser.get("about:blank")
    if pr:
        host, port, user, pw = pr

        async def on_auth(ev):
            await tab.send(cdp.fetch.continue_with_auth(
                request_id=ev.request_id,
                auth_challenge_response=cdp.fetch.AuthChallengeResponse(
                    response="ProvideCredentials", username=user, password=pw)))

        async def on_req(ev):
            try:
                await tab.send(cdp.fetch.continue_request(request_id=ev.request_id))
            except Exception:
                pass

        tab.add_handler(cdp.fetch.AuthRequired, on_auth)
        tab.add_handler(cdp.fetch.RequestPaused, on_req)
        await tab.send(cdp.fetch.enable(handle_auth_requests=True))
        if verbose:
            print(f"[proxy] IP de saída: {await _egress_ip(tab)}", file=sys.stderr)

    await _goto(tab, ORIGIN + "/br/")
    await asyncio.sleep(3)
    await _consent(tab)
    await asyncio.sleep(1)
    return browser, tab


# --------------------------------------------------------------------------- #
# API de alto nível: analisa UM jogo (mesma forma de _analisar_jogo do analise_loteca)
# --------------------------------------------------------------------------- #
async def analisar_jogo(tab, jg, janela_dias=3, verbose=True, auditar=False,
                        llm_model="claude-sonnet-4-5", aplicar_apelido=False):
    """Resolve UM jogo no BetExplorer e estima a probabilidade 1X2. -> reg dict.
    Nunca levanta: em falha devolve o registro com `erro` (prob=None), p/ o concurso
    sair inteiro. Forma idêntica ao _analisar_jogo do analise_loteca (drop-in).

    Camada C (auditar=True): valida o match achado (consultivo) e, se a via-dia E a
    via-por-time falharem, RESGATA por LLM (com verificação por team-id). `aplicar_apelido`
    grava o apelido fundamentado no apelidos_loteca_betexplorer.json (implica auditar)."""
    if aplicar_apelido:
        auditar = True                # aplicar exige a sugestão produzida pela auditoria
    reg = {"seq": jg["seq"], "loteca": {"home": jg["home"], "away": jg["away"],
                                        "data": jg["data"]},
           "campeonato": jg.get("campeonato"), "resolvido": None, "prob_1x2": None,
           "palpite": None, "n_casas": 0, "erro": None}
    if not jg["data"]:
        reg["erro"] = "data do jogo ilegível na programação"
        return reg
    ph = _pista(jg.get("home_uf"), jg.get("home_pais"))
    pa = _pista(jg.get("away_uf"), jg.get("away_pais"))
    try:
        # Via PRIMÁRIA: agenda do dia (1 fetch compartilhado, sem busca/homônimo).
        ev = await _resolver_via_dia(tab, jg["home"], jg["away"], jg["data"],
                                     janela_dias=janela_dias, verbose=verbose)
        # Fallback: via-por-time (busca desambiguada + casar por id) se o dia não casa.
        if not ev:
            try:
                ev = await _resolver_jogo(tab, jg["home"], jg["away"], jg["data"],
                                          ph=ph, pa=pa, janela_dias=janela_dias,
                                          verbose=verbose)
            except Exception as e_res:  # noqa: BLE001
                if not auditar:
                    raise
                if verbose:
                    print(f"[bx] não resolvido ({str(e_res)[:120]}); resgate LLM…",
                          file=sys.stderr)
                ev = await _resgatar_llm_bx(tab, jg, ph, pa, janela_dias,
                                            str(e_res), llm_model, verbose)
        odds = await coletar_odds(tab, ev["match_href"])
        prob = estimar_prob(odds, invertido=bool(ev.get("invertido")))
        reg["resolvido"] = {"home": ev.get("home"), "away": ev.get("away"),
                            "data_exibida": ev.get("data_exibida"),
                            "match_href": ev.get("match_href"),
                            "mid": ev.get("mid") or _mid_da_href(ev.get("match_href")),
                            "metodo": ev.get("metodo"), "score": ev.get("score"),
                            "invertido": ev.get("invertido")}
        if ev.get("apelido_sugerido"):
            reg["apelido_sugerido"] = ev["apelido_sugerido"]
        reg["n_casas"] = odds.get("n_casas", 0)
        reg["prob_1x2"] = prob
        reg["odds_casas"] = odds_por_casa(odds, invertido=bool(ev.get("invertido")))
        reg["palpite"] = (max((("1", prob["casa"]), ("X", prob["empate"]),
                               ("2", prob["fora"])), key=lambda kv: kv[1])[0]
                          if prob else None)
        if prob is None:
            reg["erro"] = "jogo resolvido, mas sem odds 1X2 válidas"
        # auditoria CONSULTIVA do match achado (o resgate já é verificado por id).
        if auditar and ev.get("metodo") != "llm-resgate":
            reg["auditoria"] = await _auditar_match_bx(reg, jg, ph, pa, llm_model)
        # grava o apelido fundamentado, se pedido (só o que bate com um lado real).
        if aplicar_apelido:
            sug = _sugestao_apelido_bx(reg, jg)
            if sug:
                de, para = sug
                st = _aplicar_apelido_bx(de, para)
                reg["apelido_aplicado"] = {"de": de, "para": para, "status": st}
                if verbose:
                    print(f"[apelido] {st}: '{de}' -> '{para}'", file=sys.stderr)
            elif verbose:
                print("[apelido] nenhuma sugestão aplicável.", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 — um jogo falho não derruba o concurso
        reg["erro"] = str(e)[:300]
    if verbose:
        if reg["prob_1x2"]:
            p = reg["prob_1x2"]
            print(f"[{jg['seq']:>2}] {jg['home']} x {jg['away']} -> "
                  f"1={p['casa']:.0%} X={p['empate']:.0%} 2={p['fora']:.0%} "
                  f"({reg['n_casas']} casas) palpite={reg['palpite']}", file=sys.stderr)
        else:
            print(f"[{jg['seq']:>2}] {jg['home']} x {jg['away']} -> FALHOU: "
                  f"{reg['erro']}", file=sys.stderr)
    return reg


# --------------------------------------------------------------------------- #
# CLI standalone (1 jogo) — p/ depurar a resolução/coleta
# --------------------------------------------------------------------------- #
async def _run_cli(data, home, away, home_uf, away_uf, home_pais, away_pais,
                   proxy, country, janela_dias, verbose, auditar, llm_model,
                   aplicar_apelido):
    browser, tab = await abrir_browser(proxy, country, verbose)
    try:
        jg = {"seq": 0, "home": home, "away": away, "data": data,
              "home_uf": home_uf, "away_uf": away_uf,
              "home_pais": home_pais, "away_pais": away_pais, "campeonato": None}
        return await analisar_jogo(tab, jg, janela_dias=janela_dias,
                                   verbose=verbose, auditar=auditar,
                                   llm_model=llm_model,
                                   aplicar_apelido=aplicar_apelido)
    finally:
        try:
            browser.stop()
        except Exception:
            pass


def buscar_odds_betexplorer(data, home, away, home_uf=None, away_uf=None,
                            home_pais=None, away_pais=None, proxy="none",
                            country="BR", janela_dias=3, verbose=True,
                            auditar=False, llm_model="claude-sonnet-4-5",
                            aplicar_apelido=False):
    """API síncrona: resolve + coleta + estima p/ UM jogo. -> reg dict (ver analisar_jogo)."""
    return asyncio.run(_run_cli(data, home, away, home_uf, away_uf, home_pais,
                                away_pais, proxy, country, janela_dias, verbose,
                                auditar, llm_model, aplicar_apelido))


def main():
    ap = argparse.ArgumentParser(
        description="Resolve um jogo no BetExplorer (busca desambiguada -> página do "
                    "time -> partida) e estima a probabilidade 1X2 multi-casa.")
    ap.add_argument("data", help="data do jogo, AAAA-MM-DD")
    ap.add_argument("home", help="time mandante (nome da Loteca)")
    ap.add_argument("away", help="time visitante (nome da Loteca)")
    ap.add_argument("--home-uf", dest="home_uf", default=None)
    ap.add_argument("--away-uf", dest="away_uf", default=None)
    ap.add_argument("--home-pais", dest="home_pais", default=None)
    ap.add_argument("--away-pais", dest="away_pais", default=None)
    ap.add_argument("--janela-dias", type=int, default=3, dest="janela_dias")
    ap.add_argument("--auditar", action="store_true",
                    help="liga a auditoria/resgate por LLM (camada C; precisa de "
                         "HUB_SERVICE_URL/HUB_API_KEY)")
    ap.add_argument("--llm-model", default="claude-sonnet-4-5", dest="llm_model",
                    help="modelo do Hub p/ a auditoria (default: claude-sonnet-4-5)")
    ap.add_argument("--aplicar-apelido", action="store_true", dest="aplicar_apelido",
                    help="grava no apelidos_loteca_betexplorer.json o apelido sugerido "
                         "(implica --auditar; só grava o fundamentado)")
    ap.add_argument("--proxy", nargs="?", choices=["none", "rotativo", "fixo"],
                    const="fixo", default="none")
    ap.add_argument("--country", default="BR")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    res = buscar_odds_betexplorer(a.data, a.home, a.away, home_uf=a.home_uf,
                                  away_uf=a.away_uf, home_pais=a.home_pais,
                                  away_pais=a.away_pais, proxy=a.proxy,
                                  country=a.country, janela_dias=a.janela_dias,
                                  verbose=not a.quiet, auditar=a.auditar,
                                  llm_model=a.llm_model,
                                  aplicar_apelido=a.aplicar_apelido)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
