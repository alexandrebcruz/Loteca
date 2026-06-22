# Loteca — coleta de odds + otimização de apostas

Pipeline para a **Loteca** (loteria esportiva da Caixa, 14 jogos): a partir do
concurso aberto, coleta as **odds 1X2 multi-casa** de cada jogo, estima a
**probabilidade de consenso** do mercado e calcula, por programação dinâmica, os
bilhetes que **maximizam P(14 acertos)** para cada orçamento — gerando um
relatório HTML interativo. Inclui a manutenção dos dicionários de nomes
(de-para Loteca → casa de odds) e sua auditoria por "verdade de campo".

A automação de navegador usa **Chrome headful + [`nodriver`](https://github.com/ultrafunkamsterdam/nodriver)**
(opcionalmente atrás de proxy). Foi desenvolvida em ambiente com Chrome + Xvfb +
proxy via um serviço interno ("HubService"), mas o núcleo roda em qualquer
máquina com Chrome e Python.

## Organização das pastas

- **`prod/`** — código canônico (o que roda em produção). Ver
  [Scripts de produção](#scripts-de-produção).
- **`dev/`** — ferramentas fora de produção e experimentos (não fazem parte do
  runtime). Acham o `prod/` via `../prod` no `sys.path`:
  - `dev/sofascore_odds.py` — coleta de odds do Sofascore (legado/standalone).
  - `dev/proxy_relay.py`, `dev/_teste_*.py` — experimentos de proxy/nodriver.
  - `dev/varredura_nomes.py`, `dev/verificar_flagrados.py`, `dev/comparar_modos.py`
    — análise/manutenção de nomes (varrem concursos p/ achar divergências e
    comparar estratégias de resolução).
- **`data/`** — saídas e cache. O histórico cru da Caixa
  (`data/raw/loteca-NNNN.json`) e o cache de preços (`data/loteca_precos.json`)
  **não são versionados** (regeneráveis). Já os **checkpoints + relatórios de cada
  análise em `data/analise/<concurso>/` SÃO versionados** (`.gitignore`:
  `data/*` + `!data/analise/`).

## Fluxo de ponta a ponta

```
baixar_programacao_loteca  →  concurso aberto + 14 jogos
        │
analise_loteca  →  p/ cada jogo: buscar_odds_flashscore (odds 1X2 multi-casa)
        │                         → estima prob. de consenso  → analise.json
otimizador_loteca  →  DP exata sobre os 14 jogos  → bilhetes ótimos + otimizacao.html
        │                         (preços/regras oficiais via precos_loteca)
audita_apelidos_*  →  saúde dos dicionários de apelidos (verdade de campo)
```

O `pipeline_loteca.py` orquestra as três etapas principais numa tacada só.

## Início rápido

```bash
# pipeline completo do concurso aberto (IP da máquina), gera o HTML e audita os apelidos:
python3 prod/pipeline_loteca.py

# pré-requisito de dados (1x): baixa o histórico cru de concursos da Caixa
python3 prod/baixar_loteca_backtest.py

# IP "sujo" → force casas BR via proxy fixo:
python3 prod/pipeline_loteca.py --proxy fixo --country BR
```

Saída do pipeline em `data/analise/<concurso>_<AAAAMMDDHHMM>/`:
`analise.json` (probabilidades), `otimizacao.html` (relatório) e
`auditoria_apelidos.json`.

## Dependências

- Python: `nodriver`, `requests`.
- Chrome + Xvfb (display virtual p/ rodar headful sem tela).
- Para `--proxy` e a auditoria por LLM (default ligada nos resolvers): variáveis
  de ambiente `HUB_SERVICE_URL` e `HUB_API_KEY` (serviço interno). **Sem elas**,
  rode com `--sem-auditar` / `--pular-auditoria` e sem `--proxy` — nenhum segredo
  é embutido no código; tudo vem do ambiente em runtime.

---

## Scripts de produção

### Orquestração

- **`pipeline_loteca.py`** — orquestra as três etapas: (1) `analise_loteca.py` →
  (2) `otimizador_loteca.py` → (3) `audita_apelidos_loteca_flashscore.py`.
  Descobre o concurso aberto **uma vez** e congela a programação
  (`--concurso-json`) p/ as etapas 1/2 não consultarem a Caixa duas vezes. Grava
  tudo em **`data/analise/<concurso>_<AAAAMMDDHHMM>/`** (concurso + instante de
  execução; `--saida` fixa um nome). Etapas 1 e 2 compartilham a pasta; a etapa 3
  é independente do concurso (audita o dicionário global) mas seu relatório JSON
  vai p/ a pasta do run. Aborta se a análise/otimizador falham; a auditoria só
  **avisa**. Flags repassadas: `--proxy/--country`, `--modo`, `--janela-dias`,
  `--auditar`, `--refazer`, `--max-custo`, `--destaque`, `--checar-precos`,
  `--via-time`, `--pular-auditoria`, `--quiet`.

- **`analise_loteca.py`** — orquestrador ponta-a-ponta de um concurso. Junta o
  `baixar_programacao_loteca.py` (pega o concurso aberto mais próximo de fechar)
  ao `buscar_odds_flashscore.py` (odds 1X2 multi-casa dos 14 jogos) e estima a
  **probabilidade 1X2** de cada jogo. Reusa **uma só sessão de Chrome** para os 14
  jogos (consent uma vez), em vez de subir 14 navegadores.
  - **Probabilidade de consenso** (`estimar_prob`): de cada casa tira o overround
    (normaliza `1/odd` p/ somar 1, removendo o vig) e faz a média entre as casas.
    É probabilidade **de mercado**, não um modelo próprio. Respeita as colunas da
    Loteca (1 = `nomeEquipeUm`, 2 = `nomeEquipeDois`); se o casamento vier
    `invertido`, troca casa↔fora.
  - **Checkpoint / retomada** em `data/analise/<concurso>/`: grava
    `programacao.json` antes de coletar, um `jogo-NN.json` (NN = `nuSequencial`)
    por jogo coletado, e o `analise.json` agregado no fim (escrita atômica).
    Rodar de novo **retoma** do disco; `--refazer` ignora o cache. Um jogo que
    falha não derruba o concurso (vira registro com `erro`, prob `null`).
  - Flags: `--modo {fuzzy,id}`, `--janela-dias`, `--auditar` (LLM por jogo,
    desligada por padrão no batch), `--llm-model`, `--proxy/--country`,
    `--concurso-json`, `--saida` (subpasta custom sob `data/analise`,
    default = número do concurso), `--quiet`.

- **`acompanhamento_loteca.py`** — gera **um HTML ao vivo** de um concurso já
  analisado: `python3 prod/acompanhamento_loteca.py <pasta> <valor_R$>` (ex.:
  `1257_202606212000 108`). Reaproveita o `analise.json` da pasta (mid, prob 1X2,
  invertido) e **reconstrói o bilhete jogado** rodando o mesmo `otimizador_loteca`
  sobre o valor em R$ informado. Lê placar/status/horário **direto na página de
  cada jogo** (não usa o feed), recoleta odds só dos pendentes e **reestima
  P(14)/P(13)/P(13+)** (Poisson-binomial) com a evolução temporal. Jogo não
  realizado ou interrompido sem chegar ao fim vira **sorteio** (resultado
  equiprovável 1X2 → cobertura = nº de colunas marcadas ÷ 3). Salva em
  `data/analise/<pasta>/acompanhamento/<R$>_<AAAAMMDDHHMM>.html`, sem resíduos de
  scraping.
  - **Odds ao vivo (complementar):** para cada jogo no estado `ao_vivo`, chama
    automaticamente `buscar_odds_live_flashscore` (acima) e, havendo casas
    precificando in-play, mostra no HTML — sob cada coluna 1/X/2, na cobertura do
    jogo, nos cards P(14)/P(13)/P(13+) e numa linha extra na "Evolução (tabela)" —
    a leitura ao vivo com a variação (▲/▼ em pp) vs. a prob pré-jogo. **Não há flag
    para isso**: o comando é o mesmo e o ao vivo só "liga" quando há jogo
    acontecendo no momento da execução. É **puramente informativo** — NÃO entra na
    cobertura nem nas probabilidades do bilhete (que seguem as odds pré-jogo). Se a
    coleta live falhar é best-effort: registra o aviso e não derruba o HTML.

### Coleta de dados

- **`baixar_loteca_backtest.py`** — baixa o histórico de concursos da API oficial
  da Caixa em `data/raw/loteca-NNNN.json` (RAW crus). Verifica os já baixados
  (JSON válido + número certo) e busca só os faltantes/corrompidos — idempotente.
  É a fonte que o resolver e a auditoria consomem (âncora histórica de nomes).

- **`baixar_programacao_loteca.py`** — consulta a próxima programação (concursos
  ainda abertos a aposta, com os 14 jogos) em `/loteca/programacao` (os futuros
  dão HTTP 500 em `/loteca/{n}`, por isso é outro caminho). Não grava nada:
  imprime no stdout **apenas o concurso ainda ABERTO com a data-limite mais
  próxima de fechar** (menor `dataFimApostas`+`horarioFimApostas` no futuro, fuso
  BR UTC-3). Sem nenhum aberto, cai no de maior prazo e avisa no stderr.

- **`buscar_odds_flashscore.py`** — coletor multi-casa de odds **1X2** no
  **Flashscore.com.br** (backend Livesport, o mesmo do BetExplorer). Cobertura de
  casas MUITO maior que o Sofascore: ~**24 casas BR** num jogo (vs. ~2). Sobe
  Chrome headful, **aceita o consent** (sem isso a página vem vazia). Interface
  idêntica à do `buscar_eventid_sofascore.py` (posicionais `data home away` +
  flags). Dois modos:
  - **`--modo fuzzy`** (default): `agenda → lista-do-time → LLM`. (1) **agenda da
    data** via feed da Livesport (`feed/f_1_{offset}_0_pt-br_1`, header
    `x-fsign: SW9D1eZo`), casa `home`+`away` (apelidos + geo + invertido),
    preguiçosa por fuso (±`--janela-dias`), abre `/jogo/{mid}/`; (2) fallback
    determinístico pela via por time; (3) auditoria/resgate por LLM.
    `metodo` = `agenda` / `lista-do-time` / `llm-resgate`.
  - **`--modo id`**: pula a agenda e resolve a página do time
    (`/equipe/SLUG/ID/`), casando por adversário + data.
  - Devolve `odds_por_casa[]` com `odds_1x2:{casa,empate,fora}`. **Reusa o miolo
    do `buscar_eventid_sofascore.py`** (`name_score`, `_melhor_evento`, `_pista`,
    helpers de LLM). Apelidos **raw-first**: casa primeiro com nomes crus (PT,
    igual à Loteca) e só cai no nome canônico abaixo do threshold; usa o
    `apelidos_loteca_flashscore.json` (PT). O match-id do Flashscore **≠** o
    `event_id` do Sofascore — por isso resolve por nome/data.
  - **Odds AO VIVO (intragame)** — `buscar_odds_live_flashscore(tab, mid,
    match_url=...)`. As odds que **ticam com a bola rolando NÃO vêm da página HTML
    de comparação** (`coletar_odds`, que mostra a *closing line* pré-jogo e fica
    congelada). Vêm de um **GraphQL limpo** (JSON, sem `x-fsign`):

    ```
    GET https://global.ds.lsapp.eu/odds/pq_graphql?_hash=oce&eventId=<mid>&projectId=401&geoIpCode=BR&geoIpSubdivisionCode=BRSP
    ```

    em `data.findOddsByEventId`. Cada item de `odds` é um mercado por **casa ×
    tipo × tempo**: filtre `bettingType="HOME_DRAW_AWAY"` (=1X2) +
    `bettingScope="FULL_TIME"`. Cada mercado traz `value` (cotação corrente),
    `opening` (abertura), `active` e a flag **`hasLiveBettingOffers`** — o sinal
    real de que a casa está precificando in-play. Mapeamento dos 3 itens:
    **empate = `eventParticipantId` nulo**; mandante/visitante por id (o 1º id no
    path da URL canônica `/jogo/futebol/<slug>-<homeId>/<slug>-<awayId>/` é o
    mandante — robusto mesmo se o feed inverter a ordem). `settings.bookmakers` dá
    o de-para `bookmakerId → nome`. **Cobertura é fina**: na maioria dos jogos só
    um punhado de casas oferece live (o consenso de ~24 casas pré-jogo NÃO existe
    ao vivo). A função recebe uma `tab` já aberta (consent resolvido pelo
    chamador), faz o fetch in-page e devolve **no MESMO formato de `coletar_odds`**
    (`odds_por_casa[]` / `odds_1x2` em orientação Flashscore) — então
    `estimar_prob(..., invertido=...)` funciona direto, sem adaptação. Por padrão
    (`so_live=True`) só retorna casas com `hasLiveBettingOffers=True`. **É um canal
    SEPARADO e aditivo**: `coletar_odds` (usada na análise/pipeline) não foi
    tocada, e a coleta live só é acionada pelo acompanhamento (ver abaixo).

- **`buscar_eventid_sofascore.py`** — resolve o `EVENT_ID` do **Sofascore** de um
  jogo (data + nomes). Sobe Chrome headful, captura o token de sessão e resolve
  por: (1) **agenda** da data (casa nomes com `apelidos_loteca_sofascore.json`,
  preguiçosa por fuso ±`--janela-dias`); (2) **fallback** pela lista de confrontos
  do time; (3) **auditoria por LLM** (ligada por padrão; `--sem-auditar` desliga)
  que valida o match e resgata o não-achado (apelido → nome canônico → resolve →
  verifica confronto). Detalhes do anti-bot na seção [Apêndice: Sofascore](#apêndice-anti-bot-do-sofascore).

- **`buscar_odds.py`** — recebe o JSON do `buscar_eventid_sofascore` (ou um
  `--event-id`) e raspa as odds daquele jogo no Sofascore (mesma manha do token).
  Multi-casa e **multi-mercado** (1X2, dupla chance, DNB, BTTS, over/under,
  handicap asiático, cartões, escanteios…): o Sofascore tem profundidade (muitos
  mercados por casa); o Flashscore tem largura (muitas casas, só 1X2).

### Otimização e preços

- **`otimizador_loteca.py`** — otimizador **exato** de apostas. Lê
  `data/analise/<concurso>/` e calcula, por **programação dinâmica**, o bilhete
  que **maximiza P(14)** para cada orçamento (substitui a heurística gulosa, que
  não garante ótimo sob custo fixo). Modelo: marcar `m` resultados num jogo cobre
  os `m` mais prováveis (simples=top1, duplo=top1+top2, triplo=1); custo =
  (Π marcas) × preço unitário.
  - DP sobre o nº de combinações (`2^D·3^T ≤ teto`): como `combos = 2^D·3^T` é
    fatoração única, `todos_bilhetes(dp)` materializa a melhor aposta de **cada**
    par (D,T) alcançável (os 38 vendáveis), não só a fronteira de Pareto. Métricas
    por bilhete: `P(14)`, `P(13 exato)`, `P(13+)`.
  - **`validar_bilhete`** confere cada bilhete contra a tabela oficial (par (D,T)
    consta? dentro de [min,max] combos?) e devolve o custo verificado.
  - Salva **`otimizacao.html`**: página responsiva, interativa e **auto-contida**
    (sem CDN; SVG desenhado por JS, dados embutidos). Tabela de jogos com probs
    1X2 (gradiente verde) e nº de casas; card "Custo × probabilidade" com toggle
    Probabilidade/Alavancagem; gráfico custo×prob (um ponto por par D,T,
    eficientes cheios) e tabela sincronizada; clicar abre o detalhe do bilhete
    (grade de marcações + resumo + Exportar PDF).
  - Flags: `--max-custo`, `--destaque`, `--sem-checar-precos`, `--forcar-precos`,
    `--saida`, `--quiet`.

- **`precos_loteca.py`** — busca os **preços/regras oficiais da Loteca** na landing
  da Caixa (HTML estático) em vez de cravar no código. Cruza dois âncoras
  redundantes — a frase "aposta mínima R$ 4,00 … um duplo" (⇒ unitário R$ 2/comb)
  e a **tabela de valor da aposta** (colunas Duplos | Triplos | Nº de Apostas |
  Valor), conferindo `2^D·3^T == Nº de Apostas` e `valor == combinações ×
  unitário` (38 pares; teto = 5 duplos + 3 triplos = 864 apostas = R$ 1.728,00 —
  729 é só o máximo de 6 triplos). Cacheia em `data/loteca_precos.json` (TTL 24h)
  e cai num fallback embutido se a rede/parse falhar. CLI:
  `python3 precos_loteca.py [--forcar|--offline|--tabela]`.

### Dicionários de nomes e auditoria

- **`apelidos_loteca_sofascore.json`** — de-para Loteca → **Sofascore** (seleções
  em INGLÊS: Alemanha→Germany), códigos de país (alpha-3 → alpha-2) e termos de UF
  p/ desempate. Carregado relativo a `prod/`.
- **`apelidos_loteca_flashscore.json`** — de-para dedicado ao Flashscore (PT:
  Alemanha→Alemanha). **Separado** do Sofascore porque a tabela em inglês
  quebraria o match no Flashscore. Começa enxuto; a camada de resgate por LLM o
  popula (`--aplicar-apelido`).
- **`audita_apelidos_loteca_sofascore.py`** — auditoria do de-para Sofascore.
  Etapa 1 (offline): apaga órfãos/duplicatas, sinaliza typos. Etapa 2 (rede):
  valida pela **verdade de campo** — ancora no adversário, lê o outro lado do
  confronto sem confiar no apelido sob teste (Sofascore: resolve o adversário por
  ID e pagina o histórico, robusto a homônimos e a datas antigas). Dry-run por
  padrão; `--apply` grava sob **lock + backup + escrita atômica**.
- **`audita_apelidos_loteca_flashscore.py`** — equivalente para o de-para
  Flashscore (reusa a camada de dados do auditor Sofascore; Etapa 2 com motor
  próprio). Verdade de campo via **feed do dia** da Livesport. **Limitação:** o
  feed só cobre datas RECENTES (o Flashscore não expõe histórico profundo por
  time), então aparições antigas caem em `nao-verificavel` — o valor real é
  validar entradas de concursos recentes. Dry-run por padrão; `--apply` sob lock
  + backup.

> **Âncora histórica da Loteca (anti-homônimo):** as auditorias por LLM recebem,
> por consulta, um fato determinístico — quantas vezes cada time já apareceu no
> histórico de concursos (`data/raw/*.json`). Assim o LLM sabe que, p.ex.,
> "ATHLETIC CLUB" é o clube brasileiro da Série B, não o Athletic Bilbao. Sem os
> raw da Caixa o contexto fica vazio (inofensivo).

---

## Apêndice: anti-bot do Sofascore

A API de odds do Sofascore é protegida — útil tanto para o
`buscar_eventid_sofascore.py`/`buscar_odds.py` quanto para o legado
`dev/sofascore_odds.py`.

### Endpoint de odds
```
GET https://www.sofascore.com/api/v1/event/<EVENT_ID>/odds/1/all
```
Resposta: `{"markets":[{ "marketName", "marketGroup" (ex.: "1X2"),
"choices":[{ "name", "fractionalValue", ... }], ... }]}`.

### Header `X-Requested-With` (token de sessão) — CRÍTICO
- **sem** o header → `403 {"reason":"challenge"}`
- valor **errado** → `403 {"reason":"Forbidden"}`
- valor **correto** → `200` + JSON das odds

O valor é um token curto (ex.: `064db5`) que o JS do Sofascore gera. É
**reutilizável dentro da MESMA sessão** (capture de qualquer `/api/v1/...` e reuse
p/ qualquer `event_id`), mas **muda a cada carregamento** — capture dinâmico, não
hardcode.

### Geo: odds só em países permitidos
O Sofascore só monta o widget de odds (e serve a API) onde oferece apostas. Num
IP sem cobertura a página nem chama `/odds/1/all` (só `/odds/providers/<cc>/web`).
**Use IP do Brasil**. `TimeoutError` esperando `/odds/1/all` quase sempre = país
do IP, não o código nem o jogo.

### Descoberta de eventos
- URL: `…/match/<slug>/<customId>#id:<EVENT_ID>` (o ID após `#id:`).
- Ao vivo: `GET /api/v1/sport/football/events/live`.
- Agendados: `GET /api/v1/sport/football/scheduled-events/<AAAA-MM-DD>`
  (filtre `status.type == "notstarted"`). Chame pela página (in-page fetch) p/
  herdar token+cookies+proxy.

### Proxy (quando o IP precisa parecer BR)
Navegue com um helper `goto()` (o `tab.get()` do nodriver trava com o Fetch de
proxy ativo); auth de proxy `user:pass` via CDP (`continue_with_auth` +
`continue_request` em toda request); mantenha `User-Agent`/timezone/
`Accept-Language` coerentes com o país do IP. As credenciais de proxy vêm do
serviço interno (`HUB_API_KEY`/`HUB_SERVICE_URL`), nunca embutidas no código.
