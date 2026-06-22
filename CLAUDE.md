# CLAUDE.md

Orientações para o Claude Code trabalhar neste repositório. Para a descrição
funcional completa do projeto, ver o [README.md](README.md).

## O que é

Pipeline da **Loteca** (loteria de 14 jogos da Caixa): coleta odds 1X2
multi-casa → estima probabilidade de consenso → otimiza bilhetes por programação
dinâmica (maximiza P(14)) → relatório HTML. Mais a manutenção/auditoria dos
dicionários de nomes (de-para Loteca → casa de odds).

## Estrutura

- **`prod/`** — código canônico. **Toda mudança de runtime vai aqui.** Os módulos
  se importam entre si por nome (mesmo diretório), então rode os scripts a partir
  de `prod/` ou com `prod/` no path.
- **`dev/`** — ferramentas fora de produção e experimentos. Dependem de `prod/`
  via `sys.path.insert(..., "..", "prod")` e importam `buscar_eventid_sofascore`.
  Não quebre esse path relativo ao mexer em `dev/`.
- **`data/`** — saídas/cache. `data/raw/` (histórico cru da Caixa, re-baixável com
  `baixar_loteca_backtest.py`) e o cache de preços **NÃO** são versionados. A
  exceção é **`data/analise/<concurso>/` — esse SIM é versionado** (checkpoints +
  relatórios HTML das análises; `.gitignore` faz `data/*` + `!data/analise/`).

## Como rodar

```bash
python3 prod/pipeline_loteca.py            # pipeline completo do concurso aberto
python3 prod/baixar_loteca_backtest.py     # (1x) popula data/raw com o histórico
```

Pré-requisito de rede: automação de browser precisa de **Chrome + Xvfb**.
`--proxy` e a auditoria por LLM precisam de `HUB_SERVICE_URL`/`HUB_API_KEY` no
ambiente; sem elas, use `--sem-auditar`/`--pular-auditoria` e sem `--proxy`.

## Convenções e cuidados (importante)

- **Segredos**: nunca embuta credenciais. Tudo vem do ambiente (`os.environ`,
  `HUB_API_KEY`/`HUB_SERVICE_URL`) ou da API do Hub em runtime. O repositório é
  **público** — não comite tokens, nem arquivos de `data/`.
- **Edição dos `apelidos_*.json`**: sempre via **lock + backup + escrita atômica**
  (já implementado em `aplicar()` dos auditores e na gravação `--aplicar-apelido`).
  Não reescreva esses JSONs com `open(...,'w')` direto.
- **Escrita de arquivos em geral**: use o padrão atômico do projeto (tmp +
  `os.replace`) para checkpoints e saídas, para nunca deixar arquivo truncado.
- **WSL/drvfs**: este projeto vive em `/mnt/d` (drvfs). **Não use `perl -i`** para
  editar arquivos in-place — nesse FS ele DELETA o arquivo. Use `sed -i`.
- **nodriver**: subir com `uc.start(browser_args=['--no-sandbox'], sandbox=False)`
  e **headful** (Xvfb cuida do display). "Event loop is closed" no encerramento é
  ruído cosmético. Para proxy `user:pass`, responda o auth via CDP
  (`fetch.continue_with_auth`) e continue TODA request (`fetch.continue_request`),
  senão a página trava.
- **Dois de-para SEPARADOS**: Sofascore em INGLÊS (Alemanha→Germany), Flashscore
  em PT raw-first (Alemanha→Alemanha). Não unifique — a tabela em inglês quebra o
  match no Flashscore.
- **Odds AO VIVO (intragame) ≠ odds pré-jogo**: a página HTML de comparação
  (`coletar_odds`) é a *closing line* congelada. As odds que ticam in-play vêm de
  um GraphQL separado (`global.ds.lsapp.eu/odds/pq_graphql`, `findOddsByEventId`,
  sem `x-fsign`) — exposto em `buscar_odds_live_flashscore()`. É um canal
  **aditivo**: só o `acompanhamento_loteca.py` consome, e só para jogos `ao_vivo`;
  **não toca** `coletar_odds`, o pipeline, nem as probabilidades/cobertura do
  bilhete (que seguem as odds pré-jogo). Endpoint e mapeamento 1X2 no README.
- **Saída do pipeline**: pasta `data/analise/<concurso>_<AAAAMMDDHHMM>/`. O
  override de pasta nas etapas 1/2 é o flag `--saida` (seta `SAIDA_OVERRIDE`, que
  faz `_dir_concurso` ignorar o número do concurso).

## Git

- Branch principal: `main`. Commite/pushe só quando solicitado.
- `.venv/`, `data/raw/` e o cache de preços ficam fora do versionamento; **`data/analise/`
  é versionado** (ver `.gitignore`).
