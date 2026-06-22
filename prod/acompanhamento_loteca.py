#!/usr/bin/env python3
"""acompanhamento_loteca.py — acompanha um concurso JÁ analisado, ao vivo.

Recebe (1) o nome de uma pasta de análise (ex.: `1257_202606212000`, gerada pelo
pipeline em data/analise/) e (2) o VALOR em R$ do bilhete que foi efetivamente
jogado. Produz UM arquivo HTML de acompanhamento em
`data/analise/<pasta>/acompanhamento/<R$>_<AAAAMMDDHHMM>.html` (fuso BR).

O que faz, reaproveitando o que a análise já descobriu:

  1. Lê o `analise.json` da pasta — dele aproveita, de cada jogo, o `mid` do
     Flashscore (já resolvido), o `invertido` (orientação Flashscore→Loteca), a
     probabilidade 1X2 ORIGINAL e o palpite.
  2. Reconstrói o BILHETE correspondente ao valor jogado: roda o mesmo otimizador
     (sobre os preços congelados em `precos.json`) e escolhe, na fronteira, a
     aposta daquele preço — daí saem as MARCAÇÕES por jogo (1 / X / 2; simples,
     duplo ou triplo). É sobre ESSAS marcações que todas as contas são feitas.
  3. Visita a PÁGINA de cada jogo no Flashscore (sem usar o feed): de lá lê o
     PLACAR, o STATUS ("Encerrado"/minuto/"Adiado"…) e o horário de início.
     Classifica cada jogo como agendado / ao vivo / finalizado (término estimado
     em início + 120 min — o Flashscore não publica a hora exata de término).
  4. Para os jogos que ainda faltam (ou estão acontecendo) raspa as odds 1X2
     ATUAIS (multi-casa) reusando `coletar_odds` e reestima a prob de consenso
     (`estimar_prob`). Para os já finalizados, o resultado 1/X/2 vem do placar.
  5. Calcula as probabilidades atualizadas DO BILHETE JOGADO — P(14), P(13 exato)
     e P(14 ou 13) — pela fórmula Poisson-binomial do otimizador, onde a cobertura
     de cada jogo é a soma das probabilidades das colunas marcadas (jogo decidido
     entra com 1 se o resultado caiu numa coluna marcada, senão 0).
  6. Reconstrói a EVOLUÇÃO dessas 3 probabilidades ao longo do tempo, na ordem em
     que os jogos foram terminando, e monta um HTML (inspirado no otimizacao.html).

Saída: SÓ o HTML. Nenhum arquivo residual de scraping é deixado para trás.

Uso:
    python3 acompanhamento_loteca.py 1257_202606212000 64
    python3 acompanhamento_loteca.py 1257_202606212000 48 --proxy fixo --country BR
"""
import os
import re
import sys
import json
import asyncio
import argparse
import datetime as dt

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)
ANALISE_DIR = os.path.join(RAIZ, "data", "analise")
BR_TZ = dt.timezone(dt.timedelta(hours=-3))  # Brasil (UTC-3, sem horário de verão)
FIM_MIN = 120          # término do tempo regulamentar estimado em início + 120 min


def _fim_apostas_utc(s):
    """Converte o `fim_apostas` da análise ('DD/MM/YYYY HHh', fuso BR) em datetime
    UTC. Retorna None se ausente ou em formato inesperado."""
    if not s:
        return None
    try:
        d, h = s.split()
        dia, mes, ano = (int(x) for x in d.split("/"))
        hora = int(h.rstrip("hH"))
        loc = dt.datetime(ano, mes, dia, hora, tzinfo=BR_TZ)
        return loc.astimezone(dt.timezone.utc).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None

# Reuso do prod/ (fonte única). O coletor do Flashscore já tem todo o miolo de
# browser/consent/odds; a análise tem o estimador de consenso 1X2; o otimizador
# reconstrói o bilhete a partir do valor jogado.
sys.path.insert(0, AQUI)
import nodriver as uc                                            # noqa: E402
from nodriver import cdp                                         # noqa: E402
from buscar_odds_flashscore import (                             # noqa: E402
    resolver_proxy, _egress_ip, _consent, _goto, coletar_odds,
    _canonical_from_mid, _esperar, ORIGIN,
)
from analise_loteca import estimar_prob                          # noqa: E402

PALP2KEY = {"1": "casa", "X": "empate", "2": "fora"}


# --------------------------------------------------------------------------- #
# IO / utilidades
# --------------------------------------------------------------------------- #
def _log(msg):
    print(f"\033[1m[acompanhamento]\033[0m {msg}", file=sys.stderr, flush=True)


def _salvar_texto(path, texto):
    """Escrita atômica (tmp + os.replace), padrão do projeto. Cria a pasta."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(texto)
    os.replace(tmp, path)


def _carregar_analise(pasta):
    cdir = os.path.join(ANALISE_DIR, pasta)
    apath = os.path.join(cdir, "analise.json")
    if not os.path.isfile(apath):
        raise SystemExit(f"[erro] não achei {apath}. Passe o nome de uma pasta "
                         f"de análise existente sob data/analise/.")
    with open(apath, encoding="utf-8") as f:
        return cdir, json.load(f)


def _resultado_loteca(placar, invertido):
    """Converte o placar da página (ordem Flashscore) no resultado 1/X/2 NA ORDEM
    DA LOTECA. `invertido` troca 1<->2 (Flashscore mandante = Loteca visitante)."""
    if not placar or "-" not in placar:
        return None
    try:
        gh, ga = (int(x) for x in placar.split("-", 1))
    except ValueError:
        return None
    base = "1" if gh > ga else ("2" if gh < ga else "X")
    if invertido and base in ("1", "2"):
        base = "1" if base == "2" else "2"
    return base


def _cob_marcas(prob, marcas):
    """Cobertura = soma das probabilidades das colunas MARCADAS. None se não há
    prob legível para nenhuma coluna marcada."""
    if not prob or not marcas:
        return None
    s, achou = 0.0, False
    for k in marcas:
        v = prob.get(PALP2KEY.get(k))
        if v is not None:
            s += v
            achou = True
    return s if achou else None


# --------------------------------------------------------------------------- #
# Probabilidades combinadas do bilhete — Poisson-binomial (mesma fórmula do
# otimizador.metricas_bilhete, aqui sobre as coberturas correntes do bilhete).
# --------------------------------------------------------------------------- #
def _metricas(cobs):
    cobs = [c for c in cobs if c is not None]
    p14 = 1.0
    for c in cobs:
        p14 *= c
    p13 = 0.0
    for k in range(len(cobs)):
        termo = 1.0 - cobs[k]
        for i, c in enumerate(cobs):
            if i != k:
                termo *= c
        p13 += termo
    return {"p14": p14, "p13": p13, "p13mais": p14 + p13}


# --------------------------------------------------------------------------- #
# Bilhete jogado: reconstrução a partir do valor em R$ (reusa o otimizador)
# --------------------------------------------------------------------------- #
def _carregar_precos(cdir):
    """Preços/regras: prefere o snapshot congelado na pasta (precos.json); se não
    houver, resolve sem tocar a rede (cache fresco ou fallback embutido)."""
    p = os.path.join(cdir, "precos.json")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    from precos_loteca import obter_precos
    return obter_precos(checar=False, verbose=False)


def _pick_bilhete(bilhetes, combos_alvo):
    """Da fronteira (lista [(combos, cov, niveis)] asc por combos) escolhe o
    bilhete do preço pedido: combos exato, senão o MAIOR que cabe no valor."""
    exato = [b for b in bilhetes if b[0] == combos_alvo]
    if exato:
        return exato[0]
    cabe = [b for b in bilhetes if b[0] <= combos_alvo]
    return cabe[-1] if cabe else bilhetes[0]


def _reconstruir_bilhete(pasta, cdir, analise, valor, verbose=True):
    """Roda o otimizador sobre a mesma análise/preços e devolve
    (marcas_por_seq, info) do bilhete que corresponde ao `valor` em R$.

    marcas_por_seq: {seq -> ["1","X"]} (colunas marcadas, ordem natural 1,X,2).
    info: custo/combos/d/t/validade + métricas iniciais (probs da análise).
    Devolve (None, None) se a reconstrução falhar (cai no palpite seco)."""
    try:
        import otimizador_loteca as opt
        opt.SAIDA_OVERRIDE = pasta
        opt.aplicar_precos(_carregar_precos(cdir))
        meta, jogos_opt = opt.carregar_jogos(analise.get("concurso"))
        dp = opt.otimizar(jogos_opt, opt.MAX_COMBOS_LIMITE)
        bilhetes = opt.todos_bilhetes(dp)
        if not bilhetes:
            return None, None
        combos_alvo = max(opt.MIN_COMBOS, int(valor // opt.PRECO))
        combos, _cov, niveis = _pick_bilhete(bilhetes, combos_alvo)
        marc = opt.marcacoes_bilhete(jogos_opt, niveis)
        mt = opt.metricas_bilhete(jogos_opt, niveis)
        val = opt.validar_bilhete(niveis)
        marcas_por_seq = {m["seq"]: m["resultados"] for m in marc}
        info = {
            "valor_input": valor,
            "custo": val["custo"], "combos": combos, "d": val["d"], "t": val["t"],
            "valido": val["valido"], "motivo": val["motivo"],
            "p14_ini": mt["p14"], "p13_ini": mt["p13"], "p13mais_ini": mt["p13mais"],
        }
        if verbose:
            _log(f"bilhete R$ {val['custo']:.2f} · {combos} apostas · "
                 f"{val['d']} duplo(s) · {val['t']} triplo(s) "
                 f"({'válido' if val['valido'] else 'fora da tabela'})")
        return marcas_por_seq, info
    except Exception as e:                                       # noqa: BLE001
        if verbose:
            _log(f"não consegui reconstruir o bilhete ({str(e)[:140]}); "
                 f"caindo no palpite seco.")
        return None, None


# --------------------------------------------------------------------------- #
# Página do jogo: placar + status + início (direto, sem o feed)
# --------------------------------------------------------------------------- #
_JS_ESTADO = r"""JSON.stringify((()=>{
  const q=s=>document.querySelector(s);
  const txt=el=>el?((el.innerText||el.textContent||'')+'').trim():'';
  const start=txt(q('.duelParticipant__startTime'))||txt(q('[class*="duelParticipant__startTime"]'));
  const sw=q('.detailScore__wrapper')||q('[class*="detailScore__wrapper"]');
  const score=sw?((sw.innerText||'')+'').replace(/\s+/g,''):'';
  const stEls=['.fixedHeaderDuel__detailStatus','.detailScore__status',
               '[class*="detailStatus"]','[class*="eventStatus"]'];
  let status='';
  for(const s of stEls){const e=q(s); if(e){status=txt(e); if(status) break;}}
  return {start, score, status, tzoff:new Date().getTimezoneOffset()};
})())"""

_FIN_KW = ("encerr", "final", "após pen", "apos pen", "após pror", "apos pror",
           "após prog", "apos prog", "pênaltis", "penaltis", "aet", "a.e.t")
_PARADO_KW = ("adiado", "cancel", "interromp", "abandon", "suspens",
              "walkover", "w.o", "wo ", "perda de mando")
_VIVO_KW = ("interv", "parte", "prorrog", "penalt", "ao vivo", "live")


def _placar_de(score):
    """De um texto tipo '1-3' devolve '1-3' normalizado, ou None se não houver
    dois números (jogo sem placar mostra '-')."""
    m = re.search(r"(\d+)\D+(\d+)", score or "")
    return f"{m.group(1)}-{m.group(2)}" if m else None


def _start_utc(start_txt, data_exibida, tzoff_min):
    """Converte o horário exibido na página (no fuso do browser) em datetime UTC
    naive. Data: do próprio texto (DD.MM.AAAA) ou, na falta, de `data_exibida`."""
    if not start_txt:
        return None
    ano_fb = None
    if data_exibida:
        m0 = re.match(r"(\d{4})-(\d{2})-(\d{2})", data_exibida)
        ano_fb = int(m0.group(1)) if m0 else None
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})?\D*?(\d{1,2}):(\d{2})", start_txt)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        y = int(y) if y else (ano_fb or dt.datetime.utcnow().year)
        hh, mm = int(m.group(4)), int(m.group(5))
    else:
        mt = re.search(r"(\d{1,2}):(\d{2})", start_txt)
        if not (mt and data_exibida and ano_fb):
            return None
        y, mo, d = ano_fb, int(data_exibida[5:7]), int(data_exibida[8:10])
        hh, mm = int(mt.group(1)), int(mt.group(2))
    try:
        local = dt.datetime(y, mo, d, hh, mm)
    except ValueError:
        return None
    # getTimezoneOffset(): UTC = local + offset(min)
    return local + dt.timedelta(minutes=int(tzoff_min or 0))


def _classificar(status, placar, dt_utc, agora):
    """Estado do jogo a partir do status textual da página (autoritativo) com
    fallback no horário.
    -> 'finalizado'|'ao_vivo'|'agendado'|'sorteio'|'indefinido'.
    'sorteio' = não ocorreu/interrompido sem fim (na Loteca vira sorteio,
    resultado equiprovável 1X2)."""
    s = (status or "").lower()
    has = lambda *ks: any(k in s for k in ks)
    if has(*_FIN_KW):
        return "finalizado" if placar else "indefinido"
    if has(*_PARADO_KW):
        return "sorteio"     # não ocorreu / interrompido sem fim → entra como sorteio
    if has(*_VIVO_KW) or re.search(r"\d+\s*['′\+]", status or ""):
        return "ao_vivo"
    # sem status legível: decide pelo horário (+ placar)
    if dt_utc is None:
        return "agendado"
    if agora < dt_utc:
        return "agendado"
    if agora < dt_utc + dt.timedelta(minutes=FIM_MIN):
        return "ao_vivo"
    return "finalizado" if placar else "indefinido"


async def _ler_pagina(tab, data_exibida, agora):
    """Lê estado/placar/início da página do jogo já aberta. -> dict."""
    await _esperar(tab, '[class*="duelParticipant"]', tentativas=6, intervalo=1.5)
    raw = await tab.evaluate(_JS_ESTADO)
    val = getattr(raw, "value", raw)
    try:
        d = json.loads(val)
    except Exception:                                           # noqa: BLE001
        d = {}
    placar = _placar_de(d.get("score"))
    dt_utc = _start_utc(d.get("start"), data_exibida, d.get("tzoff"))
    estado = _classificar(d.get("status"), placar, dt_utc, agora)
    return {"placar": placar, "status": d.get("status"),
            "dt_utc": dt_utc, "estado": estado}


def _fmt_br(dt_utc):
    """datetime naive (UTC) -> 'DD/MM HH:MM' no fuso BR."""
    if not dt_utc:
        return None
    br = dt_utc.replace(tzinfo=dt.timezone.utc).astimezone(BR_TZ)
    return br.strftime("%d/%m %H:%M")


# --------------------------------------------------------------------------- #
# Coleta principal (visita a página de cada jogo)
# --------------------------------------------------------------------------- #
async def _coletar(analise, marcas_por_seq, proxy, country, verbose):
    jogos_src = analise.get("jogos") or []

    pr = resolver_proxy(proxy, country)
    args = ["--no-sandbox"]
    if pr:
        host, port, user, pw = pr
        args.append(f"--proxy-server=http://{host}:{port}")
        if verbose:
            _log(f"proxy {proxy}/{(country or '').upper()} via {host}:{port}")

    browser = await uc.start(browser_args=args, sandbox=False)
    try:
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
                _log(f"IP de saída: {await _egress_ip(tab)}")

        # home + consent (uma vez)
        await _goto(tab, ORIGIN + "/")
        await asyncio.sleep(3)
        await _consent(tab)
        await asyncio.sleep(1)

        jogos = []
        for j in jogos_src:
            res = j.get("resolvido") or {}
            lot = j.get("loteca") or {}
            mid = res.get("mid")
            inv = bool(res.get("invertido"))
            palp = j.get("palpite")
            data_exib = res.get("data_exibida") or lot.get("data")
            prob_orig = j.get("prob_1x2") or {}
            marcas = marcas_por_seq.get(j.get("seq")) if marcas_por_seq else None
            if not marcas:
                marcas = [palp] if palp in PALP2KEY else ["1", "X", "2"]

            reg = {
                "seq": j.get("seq"),
                "home": lot.get("home"), "away": lot.get("away"),
                "palpite": palp, "marcas": marcas,
                "mid": mid, "invertido": inv,
                "prob_orig": prob_orig or None,
                "prob": None, "placar": None, "resultado": None,
                "estado": "agendado", "inicio": None, "fim_est": None,
                "_dt_utc": None, "coberto": None, "erro": None,
            }

            if not mid:
                reg["erro"] = "sem mid na análise"
                jogos.append(reg)
                continue

            agora = dt.datetime.utcnow()
            try:
                url = await _canonical_from_mid(tab, mid)
                pag = await _ler_pagina(tab, data_exib, agora)
            except Exception as e:                              # noqa: BLE001
                reg["erro"] = f"página: {str(e)[:120]}"
                jogos.append(reg)
                continue

            dt_utc = pag["dt_utc"]
            reg["estado"] = pag["estado"]
            reg["placar"] = pag["placar"]
            reg["_dt_utc"] = dt_utc
            reg["inicio"] = _fmt_br(dt_utc)
            reg["fim_est"] = _fmt_br(dt_utc + dt.timedelta(minutes=FIM_MIN)) if dt_utc else None

            if pag["estado"] == "finalizado":
                resultado = _resultado_loteca(pag["placar"], inv)
                if resultado is None:
                    reg["estado"] = "indefinido"          # encerrado sem placar legível
                else:
                    reg["resultado"] = resultado
                    reg["coberto"] = resultado in marcas

            # odds ATUAIS só para jogos ainda não decididos (sorteio não tem odds)
            if reg["resultado"] is None and reg["estado"] != "sorteio":
                try:
                    odds = await coletar_odds(tab, url)
                    reg["prob"] = estimar_prob(odds, invertido=inv)
                except Exception as e:                          # noqa: BLE001
                    reg["erro"] = str(e)[:160]

            if verbose:
                if reg["resultado"] is not None:
                    _log(f"  [{reg['seq']:2}] {reg['home']} x {reg['away']} -> "
                         f"FIM {reg['placar']} ({reg['resultado']}) "
                         f"{'coberto' if reg['coberto'] else 'FUROU'}")
                elif reg["estado"] == "sorteio":
                    cb = (len(reg["marcas"]) / 3.0) if reg["marcas"] else 0.0
                    _log(f"  [{reg['seq']:2}] {reg['home']} x {reg['away']} -> "
                         f"SORTEIO (equiprovável 1X2; cobertura {cb*100:.1f}%)")
                else:
                    p = reg["prob"] or {}
                    _log(f"  [{reg['seq']:2}] {reg['home']} x {reg['away']} "
                         f"[{reg['estado']}] -> 1={p.get('casa')} X={p.get('empate')} "
                         f"2={p.get('fora')}" + (f"  ⚠ {reg['erro']}" if reg["erro"] else ""))
            jogos.append(reg)
        return jogos
    finally:
        try:
            browser.stop()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Montagem dos dados (KPIs atuais + timeline)
# --------------------------------------------------------------------------- #
def _cob_atual(r):
    """Cobertura do bilhete naquele jogo AGORA: sorteio (jogo não realizado/
    interrompido) → nº de colunas marcadas ÷ 3 (resultado equiprovável 1X2);
    decidido → 1/0 (o resultado caiu numa coluna marcada?); senão a soma das
    probs ATUAIS das colunas marcadas (ou as originais como fallback)."""
    if r.get("estado") == "sorteio":
        m = r.get("marcas") or []
        return (len(m) / 3.0) if m else None
    if r.get("coberto") is not None:
        return 1.0 if r["coberto"] else 0.0
    c = _cob_marcas(r.get("prob"), r["marcas"])
    if c is None:
        c = _cob_marcas(r.get("prob_orig"), r["marcas"])
    return c


def _montar_dados(pasta, analise, jogos, bilhete):
    def _locked(r):
        # jogo já "travado": decidido em campo OU resolvido por sorteio.
        return r.get("resultado") is not None or r.get("estado") == "sorteio"

    def _cob_lock(r):
        if r.get("estado") == "sorteio":
            m = r.get("marcas") or []
            return (len(m) / 3.0) if m else None
        return 1.0 if r.get("coberto") else 0.0

    cobs = [_cob_atual(r) for r in jogos]
    atual = _metricas(cobs)

    decididos = [r for r in jogos if _locked(r)]
    decididos.sort(key=lambda r: r["_dt_utc"] or dt.datetime.max)

    # cob "pré-jogo": jogos travados entram com a prob ORIGINAL nas colunas
    # marcadas (não temos odds ao vivo do passado); pendentes ficam na cob atual.
    cob_base = []
    for r in jogos:
        if _locked(r):
            cob_base.append(_cob_marcas(r.get("prob_orig"), r["marcas"]))
        else:
            cob_base.append(_cob_atual(r))
    inicial = _metricas(cob_base)

    timeline = []
    # Âncora do ponto "início": a data-limite da aposta (instante em que o bilhete
    # nasceu com a probabilidade inicial). Fallback: kickoff do 1º jogo decidido.
    ancora = _fim_apostas_utc(analise.get("fim_apostas"))
    if ancora is None:
        ancora = decididos[0]["_dt_utc"] if decididos else None
    rot_ini = _fmt_br(ancora) if ancora else "início"
    timeline.append({
        "ts": int(ancora.replace(tzinfo=dt.timezone.utc).timestamp()) if ancora else 0,
        "t": rot_ini, "jogo": None, "resultado": None, "coberto": None,
        "sorteio": False, **inicial,
    })
    cob = list(cob_base)
    idx = {id(r): i for i, r in enumerate(jogos)}
    for r in decididos:
        cob[idx[id(r)]] = _cob_lock(r)
        end = (r["_dt_utc"] + dt.timedelta(minutes=FIM_MIN)) if r["_dt_utc"] else None
        is_sort = r.get("estado") == "sorteio"
        timeline.append({
            "ts": int(end.replace(tzinfo=dt.timezone.utc).timestamp()) if end else 0,
            "t": r["fim_est"] or "—",
            "jogo": f'{r["home"]} x {r["away"]}',
            "resultado": r["resultado"],
            "coberto": (None if is_sort else bool(r["coberto"])),
            "sorteio": is_sort,
            **_metricas(cob),
        })

    resumo = {
        "total": len(jogos),
        "finalizados": sum(1 for r in jogos if r["estado"] == "finalizado"),
        "sorteios": sum(1 for r in jogos if r["estado"] == "sorteio"),
        "ao_vivo": sum(1 for r in jogos if r["estado"] == "ao_vivo"),
        "pendentes": sum(1 for r in jogos if r["estado"] in ("agendado", "indefinido")),
        "cobertos": sum(1 for r in jogos if r.get("coberto") is True),
        "furos": sum(1 for r in jogos if r.get("coberto") is False),
    }

    agora_br = dt.datetime.now(BR_TZ)
    jogos_out = []
    for r in jogos:
        o = {k: v for k, v in r.items() if not k.startswith("_")}
        o["cob"] = _cob_atual(r)
        o["ts_ini"] = (int(r["_dt_utc"].replace(tzinfo=dt.timezone.utc).timestamp())
                       if r.get("_dt_utc") else None)
        jogos_out.append(o)

    return {
        "concurso": analise.get("concurso"),
        "pasta": pasta,
        "gerado_em": agora_br.strftime("%d/%m/%Y %H:%M"),
        "fonte": analise.get("fonte_odds") or "flashscore.com.br",
        "fim_apostas": analise.get("fim_apostas"),
        "bilhete": bilhete,
        "resumo": resumo,
        "inicial": inicial,
        "atual": atual,
        "jogos": jogos_out,
        "timeline": timeline,
    }


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
_HTML = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Loteca — acompanhamento</title>
<style>
  :root{
    --bg:#0f1419; --card:#ffffff; --ink:#1a2330; --muted:#5d6b7a;
    --line:#e3e8ee; --accent:#0a7d3b; --accent2:#0b66c3; --warn:#b8860b;
    --red:#c0392b;
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
  .sub-h{font-size:.98rem;margin:1.4em 0 .6em;padding-top:1.1em;
    border-top:1px solid var(--line)}
  .sub{color:var(--muted);font-size:.86rem;margin:-.3em 0 .8em}
  .tabela-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
  table{border-collapse:collapse;width:100%;font-size:.92rem}
  th,td{padding:7px 9px;text-align:center;white-space:nowrap}
  th{background:#f4f7fa;color:var(--muted);font-weight:600;font-size:.8rem;
    text-transform:uppercase;letter-spacing:.02em;border-bottom:2px solid var(--line)}
  td{border-bottom:1px solid var(--line)}
  td.conf,th.conf{text-align:left;white-space:normal;min-width:160px}
  tr:last-child td{border-bottom:none}
  .badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:.74rem;
    font-weight:700}
  .badge.fin{background:#eef2f6;color:var(--muted)}
  .badge.ok{background:#e4f5ea;color:var(--accent)}
  .badge.no{background:#fbe6e3;color:var(--red)}
  .badge.live{background:#fbf1dc;color:var(--warn)}
  .badge.ag{background:#eaf1fb;color:var(--accent2)}
  .badge.sort{background:#efe7fb;color:#6b3fb0}
  .prob{font-variant-numeric:tabular-nums;font-weight:600;border-radius:5px;
    padding:3px 6px;display:inline-block;min-width:46px}
  .fav{outline:2px solid var(--accent);outline-offset:-2px;font-weight:800}
  .placar{font-weight:800;font-variant-numeric:tabular-nums}
  .mk{font-weight:800;letter-spacing:.06em}
  .res-ok{color:var(--accent);font-weight:800}
  .res-no{color:var(--red);font-weight:800}
  /* KPIs */
  .resumo{display:flex;flex-wrap:wrap;gap:10px;margin:6px 0 2px}
  .kpi{flex:1 1 150px;background:#f4f7fa;border:1px solid var(--line);border-radius:10px;
    padding:12px 14px}
  .kpi .lab{color:var(--muted);font-size:.74rem;text-transform:uppercase;letter-spacing:.03em}
  .kpi .val{font-size:1.5rem;font-weight:800;font-variant-numeric:tabular-nums;margin-top:2px}
  .kpi .sub2{color:var(--muted);font-size:.74rem;margin-top:1px}
  .kpi.destaque{background:#e4f5ea;border-color:#bfe6cd}
  .kpi.destaque .val{color:var(--accent)}
  .delta-up{color:var(--accent);font-weight:700}
  .delta-dn{color:var(--red);font-weight:700}
  /* placar de acertos/erros (destaque) */
  .placar-box{display:flex;gap:10px;margin:4px 0 14px}
  .pl-item{flex:1 1 0;border-radius:12px;padding:14px 10px;text-align:center;
    border:1px solid var(--line)}
  .pl-item .pl-num{font-size:2rem;font-weight:800;font-variant-numeric:tabular-nums;line-height:1}
  .pl-item .pl-lab{font-size:.78rem;text-transform:uppercase;letter-spacing:.04em;margin-top:5px}
  .pl-item.ok{background:#e4f5ea;border-color:#bfe6cd}
  .pl-item.ok .pl-num{color:var(--accent)}
  .pl-item.no{background:#fbe6e3;border-color:#f0c8c2}
  .pl-item.no .pl-num{color:var(--red)}
  .pl-item.sort{background:#efe7fb;border-color:#d9c9f0}
  .pl-item.sort .pl-num{color:#6b3fb0}
  /* botões de ordenação dos jogos */
  .ordena{display:flex;flex-wrap:wrap;gap:8px;margin:2px 0 12px}
  .ordena button{cursor:pointer;border:1px solid var(--line);background:#f4f7fa;
    color:var(--muted);border-radius:9px;padding:8px 12px;font-size:.84rem;
    font-weight:700;transition:.12s}
  .ordena button.on{background:var(--accent2);color:#fff;border-color:var(--accent2)}
  /* gráfico */
  .metricas{display:flex;flex-wrap:wrap;gap:8px;margin:2px 0 12px}
  .metricas button{flex:1 1 auto;min-width:120px;cursor:pointer;border:1px solid var(--line);
    background:#f4f7fa;color:var(--muted);border-radius:9px;padding:9px 10px;font-size:.86rem;
    font-weight:700;transition:.12s}
  .metricas button.on{background:var(--accent2);color:#fff;border-color:var(--accent2)}
  svg.chart{width:100%;height:auto;display:block;touch-action:manipulation}
  svg.chart text{fill:var(--muted);font-size:11px}
  svg.chart .grid{stroke:#eef2f6}
  svg.chart .axis{stroke:#c9d3dd}
  svg.chart .line{fill:none;stroke:var(--accent2);stroke-width:2.5}
  svg.chart .area{fill:rgba(11,102,195,.08);stroke:none}
  svg.chart .pt{fill:#fff;stroke:var(--accent2);stroke-width:2;cursor:pointer}
  svg.chart .pt.sel{fill:var(--accent);stroke:var(--accent)}
  .dica{color:var(--muted);font-size:.82rem;margin:.5em 0 0;text-align:center}
  .info-tab{table-layout:fixed;width:100%}
  .info-tab td{text-align:left;border:none;padding:5px 8px;white-space:normal}
  .info-tab td.k{color:var(--muted);width:42%;font-size:.86rem}
  .info-tab td.v{font-weight:600}
  .rodape{color:var(--muted);font-size:.8rem;margin-top:22px}
  @media (max-width:560px){
    header h1{font-size:1.15rem}
    .kpi{flex:1 1 45%}
    th,td{padding:6px 6px;font-size:.86rem}
  }
</style>
</head>
<body>
<main>
  <header>
    <h1 id="titulo">Loteca — acompanhamento</h1>
    <p id="subtitulo"></p>
  </header>
  <section id="info" class="card"></section>
  <section id="jogos-sec" class="card"></section>
  <section id="kpi-sec" class="card"></section>
  <section id="grafico-sec" class="card"></section>
  <p class="rodape">Probabilidades de <b>consenso de mercado</b> (de-vig + média
  entre casas) do <b>bilhete efetivamente jogado</b>: a cobertura de cada jogo é a
  soma das probabilidades das colunas marcadas (simples/duplo/triplo). Jogos
  decididos entram com 1 (o resultado caiu numa coluna marcada) ou 0 (furou). O
  término de cada jogo é <b>estimado</b> em início + 120&nbsp;min (o Flashscore não
  publica a hora exata). Jogos não realizados ou interrompidos sem chegar ao fim
  entram na Loteca como <b>sorteio</b> — resultado equiprovável entre 1, X e 2
  (a cobertura vira o nº de colunas marcadas ÷ 3). Na evolução temporal, os jogos
  ainda pendentes são mantidos na sua cobertura atual (não há histórico de odds
  para reconstruir).</p>
</main>
<script id="dados" type="application/json">__DADOS_JSON__</script>
<script>
"use strict";
const D = JSON.parse(document.getElementById('dados').textContent);
const KEY = {'1':'casa','X':'empate','2':'fora'};
const intBR = v => Number(v).toLocaleString('pt-BR');
const fmtBRL = v => 'R$ ' + Number(v).toLocaleString('pt-BR',{minimumFractionDigits:2,maximumFractionDigits:2});
function pct(p,dec){ if(p==null) return '—'; return (p*100).toFixed(dec==null?1:dec)+'%'; }
function umEm(p){ if(!p||p<=0) return '—'; return '1 em '+intBR(Math.round(1/p)); }
function esc(s){ return (s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function grad(p){
  if(p==null) return 'transparent';
  const t=Math.max(0,Math.min(1,p));
  const r=Math.round(255+(22-255)*t), g=Math.round(255+(150-255)*t), b=Math.round(255+(60-255)*t);
  return 'rgb('+r+','+g+','+b+')';
}
function tipoAposta(m){ const n=(m||[]).length; return n>=3?'triplo':n===2?'duplo':'simples'; }

/* cabeçalho */
document.getElementById('titulo').textContent = 'Loteca '+D.concurso+' — acompanhamento';
document.getElementById('subtitulo').innerHTML =
  'gerado em '+esc(D.gerado_em)+'<br>fonte '+esc(D.fonte)+
  '<br>análise de apostas: '+esc(D.pasta);

/* card info + bilhete */
(function(){
  const r=D.resumo, b=D.bilhete;
  let bil='';
  if(b){
    bil='<tr><td class="k">Bilhete jogado</td><td class="v">'+fmtBRL(b.custo)+
      ' &nbsp;<span style="color:var(--muted);font-weight:400">('+intBR(b.combos)+
      ' apostas · '+b.d+' duplo(s) · '+b.t+' triplo(s)'+
      (b.valido?'':' · ⚠ fora da tabela')+')</span></td></tr>';
    if(Math.abs((b.valor_input||b.custo)-b.custo)>0.005)
      bil+='<tr><td class="k">Valor informado</td><td class="v">'+fmtBRL(b.valor_input)+
        ' <span style="color:var(--muted);font-weight:400">→ usei a aposta vendável de '+
        fmtBRL(b.custo)+'</span></td></tr>';
  }
  const box='<div class="placar-box">'+
    '<div class="pl-item ok"><div class="pl-num">'+r.cobertos+'</div><div class="pl-lab">acertos</div></div>'+
    '<div class="pl-item no"><div class="pl-num">'+r.furos+'</div><div class="pl-lab">erros</div></div>'+
    (r.sorteios?'<div class="pl-item sort"><div class="pl-num">'+r.sorteios+'</div><div class="pl-lab">sorteio</div></div>':'')+
    '</div>';
  document.getElementById('info').innerHTML =
    '<h2>Situação</h2>'+box+
    '<table class="info-tab"><tbody>'+
    bil+
    '<tr><td class="k">Jogos finalizados</td><td class="v">'+r.finalizados+' de '+r.total+'</td></tr>'+
    (r.sorteios?'<tr><td class="k">Definidos por sorteio</td><td class="v">'+r.sorteios+'</td></tr>':'')+
    '<tr><td class="k">Ao vivo agora</td><td class="v">'+r.ao_vivo+'</td></tr>'+
    '<tr><td class="k">Ainda por jogar</td><td class="v">'+r.pendentes+'</td></tr>'+
    (D.fim_apostas?'<tr><td class="k">Apostas até</td><td class="v">'+esc(D.fim_apostas)+'</td></tr>':'')+
    '</tbody></table>';
})();

/* tabela dos jogos (com ordenação) */
(function(){
  let ordem='loteca';

  function badgeFor(j){
    if(j.estado==='finalizado'){ const ok=j.coberto; return '<span class="badge '+(ok?'ok':'no')+'">'+(ok?'✓ coberto':'✗ furou')+'</span>'; }
    if(j.estado==='sorteio'){ return '<span class="badge sort">⚄ sorteio</span>'; }
    if(j.estado==='ao_vivo'){ return '<span class="badge live">● ao vivo</span>'; }
    if(j.estado==='indefinido'){ return '<span class="badge fin">encerrado</span>'; }
    return '<span class="badge ag">agendado</span>';
  }

  function cellsFor(j){
    const marcas=j.marcas||[];
    if(j.estado==='sorteio'){
      // jogo vira sorteio: resultado equiprovável (1/3 em cada coluna)
      return ['1','X','2'].map(k=>{
        const fav=marcas.includes(k)?' fav':'';
        return '<td><span class="prob'+fav+'" style="background:'+grad(1/3)+'">'+pct(1/3)+'</span></td>';
      }).join('');
    }
    if(j.resultado!=null){
      // jogo decidido: ● na coluna do resultado (verde se coberta, vermelho se não)
      return ['1','X','2'].map(k=>{
        const isRes=(k===j.resultado), marked=marcas.includes(k);
        if(isRes){ const col=j.coberto?'var(--accent)':'var(--red)';
          return '<td><span class="prob'+(marked?' fav':'')+'" style="background:'+grad(1)+';color:'+col+'">●</span></td>'; }
        return '<td>'+(marked?'<span class="prob fav" style="color:var(--muted)">·</span>':'·')+'</td>';
      }).join('');
    }
    const p=j.prob||j.prob_orig||{};
    return ['1','X','2'].map(k=>{
      const v=p[KEY[k]]; const fav=marcas.includes(k)?' fav':'';
      return '<td><span class="prob'+fav+'" style="background:'+grad(v)+'">'+pct(v)+'</span></td>';
    }).join('');
  }

  function rowFor(j){
    const marcas=j.marcas||[];
    const placar=(j.placar!=null && (j.resultado!=null || j.estado==='ao_vivo'))
      ? '<span class="placar">'+esc(j.placar)+'</span>' : '—';
    return '<tr>'+
      '<td class="conf">'+esc(j.home)+' <span style="color:var(--muted)">x</span> '+esc(j.away)+
        (j.erro?' <span title="'+esc(j.erro)+'" style="color:var(--warn)">⚠</span>':'')+'</td>'+
      '<td>'+(j.inicio?esc(j.inicio):'—')+'</td>'+
      '<td>'+badgeFor(j)+'</td>'+
      '<td><span class="mk">'+esc(marcas.join(' '))+'</span><br><span style="color:var(--muted);font-size:.74rem">'+tipoAposta(marcas)+'</span></td>'+
      '<td>'+placar+'</td>'+
      '<td>'+pct(j.cob)+'</td>'+
      cellsFor(j)+
    '</tr>';
  }

  function ordenar(arr){
    const a=arr.slice();
    if(ordem==='horario'){
      a.sort((x,y)=>{ const tx=x.ts_ini==null?Infinity:x.ts_ini, ty=y.ts_ini==null?Infinity:y.ts_ini; return (tx-ty)||(x.seq-y.seq); });
    } else if(ordem==='cobertura'){
      a.sort((x,y)=>{ const cx=x.cob==null?-1:x.cob, cy=y.cob==null?-1:y.cob; return (cy-cx)||(x.seq-y.seq); });
    } else {
      a.sort((x,y)=>x.seq-y.seq);
    }
    return a;
  }

  function render(){
    document.getElementById('jogos-body').innerHTML = ordenar(D.jogos).map(rowFor).join('');
    [...document.querySelectorAll('#ord-btns button')].forEach(b=>b.className=(b.dataset.o===ordem?'on':''));
  }

  document.getElementById('jogos-sec').innerHTML =
    '<h2>Jogos</h2><p class="sub">Coluna(s) <b>marcada(s) no bilhete</b> destacada(s). '+
    'Pendentes: prob. 1X2 <b>atualizada</b> e cobertura corrente. Decididos: placar e '+
    '● na coluna do resultado (verde = coberto, vermelho = furou). '+
    '<b>Sorteio</b>: jogo não realizado/interrompido — resultado equiprovável 1X2.</p>'+
    '<div class="ordena" id="ord-btns">'+
      '<button data-o="loteca">Ordem da Loteca</button>'+
      '<button data-o="horario">Data e hora</button>'+
      '<button data-o="cobertura">Cobertura</button>'+
    '</div>'+
    '<div class="tabela-wrap"><table><thead><tr>'+
    '<th class="conf">Confronto</th><th>Data/Hora</th><th>Situação</th><th>Aposta</th><th>Placar</th>'+
    '<th>Cob.</th><th>1</th><th>X</th><th>2</th></tr></thead><tbody id="jogos-body"></tbody></table></div>';
  [...document.querySelectorAll('#ord-btns button')].forEach(b=>b.onclick=()=>{ ordem=b.dataset.o; render(); });
  render();
})();

/* card KPIs atuais (com o delta vs. início) */
(function(){
  const a=D.atual, ini=D.inicial||{};
  function delta(now,was){
    if(now==null||was==null) return '';
    const d=now-was; if(Math.abs(d)<1e-9) return ' <span style="color:var(--muted)">=</span>';
    const cls=d>0?'delta-up':'delta-dn'; const sig=d>0?'▲':'▼';
    return ' <span class="'+cls+'">'+sig+' '+(Math.abs(d)*100).toFixed(1)+'pp</span>';
  }
  function kpi(lab,val,sub,dest){
    return '<div class="kpi'+(dest?' destaque':'')+'"><div class="lab">'+lab+'</div>'+
      '<div class="val">'+val+'</div><div class="sub2">'+sub+'</div></div>';
  }
  document.getElementById('kpi-sec').innerHTML =
    '<h2>Probabilidade atual do bilhete</h2>'+
    '<p class="sub">Do bilhete jogado'+(D.bilhete?' ('+fmtBRL(D.bilhete.custo)+')':'')+
    ', com os jogos já decididos fixados. <b>pp</b> = variação vs. o início.</p>'+
    '<div class="resumo">'+
      kpi('P(14 ou 13)', pct(a.p13mais), umEm(a.p13mais)+delta(a.p13mais,ini.p13mais), true)+
      kpi('P(14)', pct(a.p14), umEm(a.p14)+delta(a.p14,ini.p14))+
      kpi('P(13 exato)', pct(a.p13), umEm(a.p13)+delta(a.p13,ini.p13))+
    '</div>';
})();

/* gráfico temporal + tabela de evolução */
(function(){
  const METR=[{k:'p13mais',rot:'P(14 ou 13)'},{k:'p14',rot:'P(14)'},{k:'p13',rot:'P(13 exato)'}];
  let metr='p13mais';
  const tl=D.timeline;

  const g=document.getElementById('grafico-sec');
  if(!tl || tl.length<2){
    g.innerHTML='<h2>Evolução temporal</h2><p class="sub">Ainda não há jogos '+
      'finalizados — a evolução aparece conforme os resultados saem.</p>'+
      '<div id="evol-sec"></div>';
  } else {
    g.innerHTML='<h2>Evolução temporal</h2>'+
      '<p class="sub">Como a probabilidade foi mudando conforme os jogos terminaram '+
      '(término estimado em início + 120 min).</p>'+
      '<div class="metricas" id="metr"></div><div id="chart"></div>'+
      '<p class="dica" id="dica">Toque num ponto para ver o valor.</p>'+
      '<div id="evol-sec"></div>';
    const mc=document.getElementById('metr');
    METR.forEach(m=>{ const b=document.createElement('button'); b.textContent=m.rot;
      b.dataset.k=m.k; if(m.k===metr)b.className='on';
      b.onclick=()=>{metr=m.k; [...mc.children].forEach(x=>x.className=x.dataset.k===metr?'on':''); draw();};
      mc.appendChild(b); });
    draw();
  }

  function draw(){
    const W=920,H=320,P={l:54,r:18,t:16,b:46};
    const ys=tl.map(p=>p[metr]);
    const ymax=Math.max(...ys, 1e-9)*1.12, ymin=0;
    const px=i=> P.l + (W-P.l-P.r) * (tl.length<2?0.5:i/(tl.length-1));
    const py=v=> H-P.b - (H-P.t-P.b) * ((v-ymin)/(ymax-ymin||1));
    let grid='', ax='';
    for(let gi=0;gi<=4;gi++){ const v=ymin+(ymax-ymin)*gi/4, y=py(v);
      grid+='<line class="grid" x1="'+P.l+'" y1="'+y+'" x2="'+(W-P.r)+'" y2="'+y+'"/>';
      ax+='<text x="'+(P.l-8)+'" y="'+(y+4)+'" text-anchor="end">'+(v*100).toFixed(1)+'%</text>'; }
    let xlab='';
    tl.forEach((p,i)=>{ if(tl.length>8 && i%2 && i!==tl.length-1) return;
      xlab+='<text x="'+px(i)+'" y="'+(H-P.b+18)+'" text-anchor="middle">'+esc(p.t)+'</text>'; });
    const dpts=tl.map((p,i)=>(i?'L':'M')+px(i)+' '+py(p[metr])).join(' ');
    const area=(tl.length>1)?('M'+px(0)+' '+py(0)+' '+tl.map((p,i)=>'L'+px(i)+' '+py(p[metr])).join(' ')+' L'+px(tl.length-1)+' '+py(0)+' Z'):'';
    let pts='';
    tl.forEach((p,i)=>{ pts+='<circle class="pt" data-i="'+i+'" cx="'+px(i)+'" cy="'+py(p[metr])+'" r="5"/>'; });
    document.getElementById('chart').innerHTML=
      '<svg class="chart" viewBox="0 0 '+W+' '+H+'">'+grid+
      '<line class="axis" x1="'+P.l+'" y1="'+P.t+'" x2="'+P.l+'" y2="'+(H-P.b)+'"/>'+
      '<line class="axis" x1="'+P.l+'" y1="'+(H-P.b)+'" x2="'+(W-P.r)+'" y2="'+(H-P.b)+'"/>'+
      (area?'<path class="area" d="'+area+'"/>':'')+
      '<path class="line" d="'+dpts+'"/>'+ax+xlab+pts+'</svg>';
    document.querySelectorAll('#chart .pt').forEach(c=>c.onclick=()=>{
      const i=+c.dataset.i, p=tl[i];
      document.querySelectorAll('#chart .pt').forEach(x=>x.classList.remove('sel'));
      c.classList.add('sel');
      const ctx = p.jogo ? (p.sorteio ? (esc(p.jogo)+' → sorteio (equiprovável 1X2)')
        : (esc(p.jogo)+' → '+p.resultado+' ('+(p.coberto?'coberto':'furou')+')')) : 'situação inicial';
      document.getElementById('dica').innerHTML='<b>'+esc(p.t)+'</b> · '+ctx+' · '+
        METR.find(m=>m.k===metr).rot+' = <b>'+pct(p[metr])+'</b> ('+umEm(p[metr])+')';
    });
  }

  // tabela de evolução
  let rows='';
  tl.forEach((p)=>{
    let ev='—';
    if(p.jogo){
      ev = p.sorteio
        ? (esc(p.jogo)+' <span class="badge sort">⚄ sorteio</span>')
        : (esc(p.jogo)+' <span class="mk">'+p.resultado+'</span> '+
           (p.coberto?'<span class="res-ok">✓</span>':'<span class="res-no">✗</span>'));
    }
    rows+='<tr><td>'+esc(p.t)+'</td><td class="conf">'+ev+'</td>'+
      '<td>'+pct(p.p13mais)+'</td><td>'+pct(p.p14)+'</td><td>'+pct(p.p13)+'</td></tr>';
  });
  document.getElementById('evol-sec').innerHTML=
    '<h3 class="sub-h">Evolução (tabela)</h3><div class="tabela-wrap"><table><thead><tr>'+
    '<th>Quando</th><th class="conf">Jogo decidido</th><th>P(14 ou 13)</th><th>P(14)</th>'+
    '<th>P(13)</th></tr></thead><tbody>'+rows+'</tbody></table></div>';
})();
</script>
</body>
</html>
"""


def gerar_html(dados):
    blob = json.dumps(dados, ensure_ascii=False)
    return _HTML.replace("__DADOS_JSON__", blob)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description="Acompanhamento ao vivo de um concurso já analisado da Loteca.")
    ap.add_argument("pasta", help="nome da pasta de análise sob data/analise/ "
                                   "(ex.: 1257_202606212000)")
    ap.add_argument("valor", type=float,
                    help="valor em R$ do bilhete jogado (mapeia p/ a aposta da "
                         "fronteira do otimizador — define as marcações por jogo)")
    ap.add_argument("--proxy", nargs="?", choices=["none", "rotativo", "fixo"],
                    const="fixo", default=None,
                    help="proxy p/ a coleta (sem o flag: IP da máquina)")
    ap.add_argument("--country", default="BR", help="ISO-2 p/ --proxy fixo (default BR)")
    ap.add_argument("--quiet", action="store_true", help="silencia o progresso")
    a = ap.parse_args()
    verbose = not a.quiet

    cdir, analise = _carregar_analise(a.pasta)
    if verbose:
        _log(f"concurso {analise.get('concurso')} · {len(analise.get('jogos') or [])} jogos · "
             f"pasta {a.pasta} · bilhete R$ {a.valor:.2f}")

    marcas_por_seq, bilhete = _reconstruir_bilhete(a.pasta, cdir, analise, a.valor, verbose)

    jogos = asyncio.run(_coletar(analise, marcas_por_seq, a.proxy, a.country, verbose))
    dados = _montar_dados(a.pasta, analise, jogos, bilhete)

    ts = dt.datetime.now(BR_TZ).strftime("%Y%m%d%H%M")
    vfmt = ("%g" % a.valor)
    out = os.path.join(cdir, "acompanhamento", f"{vfmt}_{ts}.html")
    _salvar_texto(out, gerar_html(dados))

    a_ = dados["atual"]
    _log(f"OK -> {out}")
    print(json.dumps({
        "html": out,
        "bilhete": bilhete,
        "resumo": dados["resumo"],
        "atual": {"p14": a_["p14"], "p13": a_["p13"], "p13mais": a_["p13mais"]},
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
