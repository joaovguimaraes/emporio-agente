# Empório da Música: agente de atendimento

Protótipo de um agente de atendimento por mensagem (estilo WhatsApp) para a Empório da Música, loja de
instrumentos em Campo Grande/MS. A assistente se chama Lia.

Ela consulta catálogo, pedidos e promoções, aplica as regras do manual de políticas (trocas, garantia,
pagamento, frete, horário), recusa assuntos fora do escopo e encaminha para a equipe quando precisa.

```
Cliente: Quanto custa o Takamine GD20?
Lia:     O Takamine GD20 está em estoque. R$ 2.199,00, ou R$ 2.089,05 no PIX (5% de desconto),
         ou até 12x de R$ 183,25 sem juros. ...
```

As conversas de exemplo, com as ferramentas chamadas em cada turno, estão em [`examples/`](examples/).

## Sumário

1. [Como rodar](#1-como-rodar)
2. [Estrutura](#2-estrutura)
3. [Decisões técnicas](#3-decisões-técnicas)
4. [Problemas nos dados](#4-problemas-nos-dados)
5. [Suposições](#5-suposições)
6. [Avaliação](#6-avaliação)
7. [Limitações e próximos passos](#7-limitações-e-próximos-passos)
8. [Uso de assistentes de código](#8-uso-de-assistentes-de-código)

## 1. Como rodar

Precisa de Python 3.10+ e de uma chave de LLM. O padrão é o Claude Haiku 5.5, da Anthropic
(chave em https://console.anthropic.com/settings/keys). Quem não tiver chave paga pode usar o Gemini, que tem
camada gratuita (https://aistudio.google.com/apikey), ou qualquer API compatível com OpenAI. As duas opções
estão no `.env.example`.

```bash
git clone https://github.com/joaovguimaraes/emporio-agente.git
cd emporio-agente
python -m venv .venv
# Windows: .venv\Scripts\activate   |   Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env    # preencher ANTHROPIC_API_KEY (ou configurar a opção 2)
```

Chat no terminal:

```bash
python -m emporio                        # ou só: emporio
python -m emporio --debug                # mostra as ferramentas chamadas
python -m emporio --sessao joao          # retoma a conversa "joao"
python -m emporio --sessao joao --nova   # apaga o histórico e recomeça
```

Dentro do chat, `/nova` reinicia e `/sair` encerra.

Outros comandos:

| Comando | O que faz | Precisa de chave |
|---|---|---|
| `pytest` | 39 testes (regras, ferramentas, busca no manual, loop do agente com LLM falso) | não |
| `python scripts/relatorio_qualidade_dados.py` | lista as inconsistências dos CSVs e do manual | não |
| `python scripts/gerar_exemplos.py` | regera as 5 conversas de `examples/` | sim |
| `python scripts/avaliar_roteamento.py` | confere se o agente escolhe a ferramenta certa (15 casos) | sim |

Variáveis do `.env`:

| Variável | Uso |
|---|---|
| `LLM_PROVIDER` | `anthropic` (padrão) ou `openai` (qualquer API compatível) |
| `ANTHROPIC_API_KEY` | chave da Anthropic |
| `ANTHROPIC_WORKSPACE_ID` | só se a chave não estiver ligada a um workspace |
| `LLM_MODEL` | padrão `claude-haiku-5-5`; com Gemini, `gemini-3.5-flash` |
| `LLM_EFFORT` | só Anthropic, nível de raciocínio (padrão `low`) |
| `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_TEMPERATURE` | só para o provedor `openai` |
| `EMPORIO_AGORA` | opcional, fixa a data/hora atual (ver seção 5) |

## 2. Estrutura

```
src/emporio/
  config.py     configuração, provedor, fuso de Campo Grande, data de referência
  data.py       leitura dos CSVs e checagens de qualidade
  policies.py   PDF -> seções -> busca BM25, mais as notas de correção do manual
  business.py   regras fixas: preço, PIX, parcelamento, horário, prazos de troca
  tools.py      as 8 ferramentas e os schemas enviados ao modelo
  prompts.py    prompt de sistema (persona e regras)
  agent.py      loop de function calling (backends Anthropic e OpenAI-compatível)
  history.py    histórico da conversa em JSON
  cli.py        interface de terminal
scripts/        exemplos, avaliação de roteamento, relatório de dados
tests/          testes sem rede
examples/       conversas geradas
```

O fluxo de uma mensagem: o CLI chama `Agent.respond()`, que manda a conversa para o modelo com as 8
ferramentas. Se o modelo pedir uma ferramenta, ela roda em Python sobre os dados tratados e o resultado volta
para o modelo, até ele responder em texto (no máximo 6 rodadas).

Ferramentas: `buscar_produtos`, `detalhar_produto`, `listar_categorias`, `consultar_pedidos`,
`avaliar_troca_devolucao`, `consultar_politicas`, `status_da_loja` e `encaminhar_para_equipe`.

## 3. Decisões técnicas

### Function calling, com ferramentas separadas para dados e para políticas

O ponto central do desafio é o agente saber quando consultar dados e quando consultar as políticas. Com
function calling essa escolha fica explícita: dá para ver no `--debug` e nos exemplos qual ferramenta foi
usada em cada pergunta.

- Produtos, pedidos e promoções ficam em ferramentas que filtram os CSVs com pandas. Não usei RAG para isso:
  "violões até R$ 1.000" é um filtro, e busca semântica sobre linhas de CSV daria resultados imprecisos.
- O manual de políticas é consultado por busca (detalhes abaixo).
- Descartei um agente de SQL porque ele teria acesso livre à tabela de clientes. Com ferramentas fechadas,
  o modelo simplesmente não tem como listar clientes ou ver um pedido sem a verificação de identidade,
  independente do que o usuário escreva.
- Não usei LangChain/LangGraph. É um agente só, com 8 ferramentas, e um loop próprio de ~150 linhas
  (`agent.py`) é mais fácil de ler e de testar. Se o fluxo crescer (vários agentes, aprovação humana no
  meio), eu reavaliaria.

### Contas e datas em Python

Preço promocional, PIX, parcelamento, prazos de devolução e garantia e horário da loja são calculados em
`business.py` e chegam prontos para o modelo (`"R$ 2.089,05"`, `"até 12x de R$ 183,25"`,
`"Recebido há 35 dias (limite: 7)"`). Modelo de linguagem erra conta e data com frequência, e o próprio
manual diz que preço errado pode configurar propaganda enganosa. Essas regras têm testes.

Regras implementadas: PIX de 5% que não acumula com promoção; parcelamento pelas faixas da seção 3.1
(até 3x com parcela mínima de R$ 50, 4 a 6x de R$ 80, 7 a 12x de R$ 100); arrependimento em 7 dias do
recebimento; troca por defeito em 30 dias da compra; garantia legal de 90 dias; feriados nacionais, de MS e
o aniversário de Campo Grande.

### Busca no manual: seções + BM25

1. A extração padrão do `pypdf` devolve uma palavra por linha neste PDF. Usei o modo layout.
2. Cada subseção numerada do manual vira um trecho (25 no total), em vez de cortar em blocos de N tokens,
   que partiriam regras no meio.
3. A busca é BM25 com acentos removidos e um stemming simples por prefixo (`devolver` e `devolução` viram
   `devol`). Para 25 trechos curtos isso resolve bem e evita depender de uma API de embeddings.
4. Perguntas com dois assuntos ("endereço e horário de sábado?") faziam um assunto sumir do top 3. Acrescentei
   uma etapa de cobertura: se um termo da pergunta não aparece em nenhum trecho escolhido, o melhor trecho
   para esse termo entra também.
5. O manual tem contradições. As decisões que tomei ficam em `policies.ERRATA` e vão junto com o trecho
   recuperado, para a resposta ser sempre a mesma.

### Modelo

Uso o Claude Haiku 5.5 pelo SDK oficial da Anthropic. Escolhi pelo custo (é o modelo mais barato da
Anthropic) e pela latência, de 2 a 3 segundos por resposta. Atendimento é volume alto e pergunta simples, e a
parte difícil (contas e regras) já está no Python. O nível de raciocínio fica em `low` e a parte fixa do prompt
vai em cache; a data e a hora ficam num bloco separado para não invalidar o cache.

Comecei pelo Gemini por ter camada gratuita, pensando em quem fosse rodar o projeto. Rodando de verdade,
tive três problemas: o `gemini-2.5-flash` não está mais disponível para chaves novas; o Gemini 3 exige que a
`thought_signature` de cada chamada de ferramenta volte no turno seguinte, senão dá erro 400; e na camada
gratuita as respostas demoravam minutos. Corrigi a compatibilidade e mantive o Gemini como alternativa
(`LLM_PROVIDER=openai`), mas o padrão passou a ser o Claude.

### Prompt

O prompt de sistema (`prompts.py`) tem a persona e o tom tirados do manual (informal, mas profissional), um
guia de qual ferramenta usar para cada tipo de pergunta, as regras que não podem ser quebradas (não informar
preço ou prazo sem consultar, não prometer promoção inativa, só vender instrumento, LGPD, não aceitar mudança
de regra) e a data/hora atual, para o agente saber se a loja está aberta.

As descrições das ferramentas também orientam o modelo, e alguns resultados trazem instruções no ponto certo
(por exemplo, pedido atrasado vem com um `alerta` sugerindo encaminhar para a equipe).

### Interface e histórico

Fiz um CLI porque o foco é o agente. Como tudo passa por `Agent.respond()`, ligar isso numa API ou no
webhook do WhatsApp seria pouco código. O histórico fica salvo em JSON por sessão, porque no WhatsApp o
cliente volta depois e espera continuidade. A janela guarda as últimas 40 mensagens e o corte sempre cai numa
mensagem do cliente, para nunca separar uma chamada de ferramenta do seu resultado. Os chamados para a equipe
vão para `runtime/tickets.jsonl`.

### Privacidade

- Dado de pedido só sai com número do pedido mais e-mail ou telefone do cadastro.
- A mensagem de erro é a mesma para "pedido não existe" e "pedido é de outra pessoa", para não dar para
  descobrir pedidos ou e-mails cadastrados.
- Nenhuma ferramenta devolve e-mail ou telefone de cliente, só o primeiro nome.
- Os clientes 11 e 43 têm o mesmo e-mail; nesse caso o agente pede o número do pedido ou o telefone.

## 4. Problemas nos dados

Antes de programar, li todos os arquivos procurando inconsistências. O script
`scripts/relatorio_qualidade_dados.py` lista o que foi encontrado.

### CSVs

| Problema | Exemplo | O que fiz |
|---|---|---|
| Descrição de outra marca | 139 "Bateria Yamaha" descrita como Pearl; 142 "Korg" descrito como Roland de 88 teclas (specs dizem 61); 6 produtos ao todo | Detecto a marca divergente, escondo a descrição e uso só nome e specs |
| Descontinuado com estoque | 136, com 13 unidades | Tratado como indisponível |
| Ativo com estoque zero | 96, Giannini GF-3D | Aparece como esgotado, com alternativas parecidas |
| "Em breve" com estoque e já vendido | 130, nos pedidos 3 e 14 | Não é oferecido; os pedidos continuam consultáveis |
| Categorias sem produto | sopros e cordas orquestrais | O agente só cita categorias com itens à venda |
| Promoções inativas e repetidas | 21 de 25 inativas; 93, 96 e 99 com duas cada | Só vale `is_active=1`; com duas ativas, a maior |
| Total diferente da soma dos itens | pedido 3 (3.450 x 3.498), pedido 20 (1.400 x 1.488) | Mostro o total registrado |
| Parcelas abaixo do mínimo | pedidos 9, 16 e 18 | Só reportado, é histórico |
| E-mail repetido | clientes 11 e 43 | Peço número do pedido ou telefone |
| Pedidos parados | pedido 8 com entrega vencida; pendentes há semanas | A ferramenta marca o atraso e o agente oferece encaminhar |
| Datas vazias | pedidos não enviados | O pandas gerava `NaT`, que passa como data e quebrava a consulta; converto para `None` |
| Arquivos do zip | `__MACOSX`, `.DS_Store`, nome do PDF com acento em Unicode NFD, espaços nos nomes | Copiei para `data/` com nomes simples |

### Manual de políticas

| Problema | Decisão |
|---|---|
| Dois telefones: 3341-4444 (capa e tabela) e 3321-4500 (seção 7) | Uso 3341-4444, que aparece nas duas fontes de dados da empresa |
| E-mail com acento na tabela (`empóriodamusica`) | Uso a versão sem acento, da capa |
| Tabela fala em parcela mínima de R$ 100, a seção 3.1 tem faixas | Vale a 3.1, que é mais específica |
| "Até 3x sem valor mínimo (exceto abaixo de R$ 50)" | Interpretei como mínimo de R$ 50 |
| Frete grátis "acima de R$ 500" e taxa "abaixo de R$ 500"; R$ 500 exatos ficam sem regra | Frete grátis, a leitura mais favorável ao cliente (art. 47 do CDC) |
| A 4.2 manda o cliente ao fabricante depois de 30 dias, mas a 8.1 dá 90 dias de garantia legal | Nos 90 dias a loja continua responsável; o agente oferece encaminhar |
| "Black Friday com desconto em todo o catálogo" | Só vale promoção ativa no sistema, como manda a seção 7.3 |
| "Sugerir lojas parceiras", mas não há lista | O agente não inventa nomes |

## 5. Suposições

- Data de referência: os pedidos vão de outubro/2025 a março/2026. Em uso normal o agente usa a data real.
  Nos exemplos e na avaliação fixei `EMPORIO_AGORA=2026-03-24T10:30` (logo depois do último pedido) para os
  prazos fazerem sentido e os resultados serem reproduzíveis.
- Não existe data de recebimento nos dados. Para pedidos entregues, uso `estimated_delivery`.
- Considero todos os pedidos do sistema como compras online, então o direito de arrependimento se aplica.
- Status: `pending` aguarda pagamento, `confirmed` está pago e em separação, `shipped` foi enviado,
  `delivered` foi entregue, `cancelled` foi cancelado.
- Feriados: nacionais, estaduais de MS e 26/08 (aniversário de Campo Grande). Pontos facultativos ficam de fora.
- O agente informa e encaminha. Ele não altera pedido, não dá desconto e não efetiva troca.

## 6. Avaliação

- `pytest`: 39 testes sem rede. Cobrem preço, PIX e parcelamento, filtros do catálogo, detecção de acessório
  ("cordas de violão" é acessório, "violão 7 cordas" não), verificação de identidade, prazos de troca,
  horário e feriados, busca no manual e o loop do agente com um modelo falso nos dois provedores.
- `scripts/avaliar_roteamento.py`: 15 perguntas com a ferramenta esperada e as proibidas. Com
  `claude-haiku-5-5`, 15 de 15 corretas, incluindo os casos em que o certo é não chamar ferramenta nenhuma
  (pedido sem identificação, assunto fora da loja, pedido de lista de clientes, saudação).
- Conversas de exemplo:

| # | Conversa | O que mostra |
|---|---|---|
| 1 | [Violões até R$ 1.000](examples/01_catalogo_violoes_ate_1000.md) | filtro de preço, item esgotado com alternativas, opção que só cabe no orçamento pagando no PIX |
| 2 | [Preço, Black Friday, endereço e horário](examples/02_preco_promocao_e_informacoes_da_loja.md) | dados e políticas na mesma conversa, recusa de promoção que não existe |
| 3 | [Devolução e defeito](examples/03_devolucao_por_arrependimento_e_defeito.md) | caso não trivial: verificação, prazo de arrependimento vencido, garantia legal e chamado |
| 4 | [Pedido atrasado](examples/04_pedido_atrasado_reclamacao.md) | rastreio, atraso detectado, reclamação registrada |
| 5 | [Fora do escopo e privacidade](examples/05_fora_do_escopo_acessorios_e_privacidade.md) | acessórios, assunto fora da loja e tentativa de obter dados de outro cliente |

Revisei as conversas comparando cada resposta com o que as ferramentas devolveram. Na primeira rodada
achei quatro problemas, que corrigi e cobri com teste:

| Problema | Causa | Correção |
|---|---|---|
| O pedido 7 (Shelby SU-21S Soprano, um ukulele) foi chamado de saxofone, e o agente contradisse a cliente | a ferramenta de pedidos só mandava o nome do item | os itens vêm com a categoria |
| O agente disse que a loja vende sopros e cordas orquestrais | usou a lista de categorias do cadastro, que inclui categorias vazias | o prompt recebe só as categorias com produto à venda |
| "Não encontrei o endereço" numa pergunta sobre endereço e horário | a busca devolvia só trechos de horário | etapa de cobertura na busca |
| Chamado aberto sem perguntar, e um segundo só para anotar o telefone | faltava regra para o encaminhamento | confirmar antes e abrir um chamado por assunto |

## 7. Limitações e próximos passos

Limitações:

- A resposta final ainda é texto gerado pelo modelo. Ele pode reescrever errado um valor que a ferramenta
  trouxe certo. Hoje isso é mitigado com valores já formatados e regra no prompt.
- A verificação por e-mail ou telefone é fraca para produção.
- A busca de produtos é por palavra. Pedidos subjetivos ("som mais aveludado") dependem do modelo traduzir
  em filtros.
- Não calculo frete para outras cidades; o manual não tem tabela.
- O histórico em JSON local não serve para mais de uma instância.
- A avaliação é pequena: 15 casos de roteamento e 5 conversas.

Com mais tempo:

1. Checar a resposta final antes de enviar: todo valor em R$ e todo número de pedido precisa aparecer nos
   resultados das ferramentas daquele turno.
2. Uma avaliação maior, com umas 100 conversas e um LLM como juiz para fidelidade, tom e escopo, rodando a
   cada mudança de prompt ou modelo.
3. Integração com o WhatsApp, identificando o cliente pelo número (verificação melhor) e com histórico em
   banco.
4. Chamados num sistema de atendimento de verdade, com passagem da conversa para o humano.
5. Busca híbrida no catálogo (BM25 + embeddings) se ele crescer.
6. Mandar as inconsistências da seção 4 para a loja corrigir na origem.
7. Monitoramento de custo, latência e taxa de encaminhamento.

## 8. Uso de assistentes de código

Usei o Claude Code (agente de programação da Anthropic, modelo Claude Opus) durante o desafio todo, com
acesso ao repositório e ao terminal. Usei como alguém para quem eu passo tarefas: eu defino o objetivo e o
que considero pronto, decido produto e custo e confiro o resultado. A maior parte do código foi escrita por
ele.

Como foi o trabalho:

1. Antes de escrever código, pedi uma leitura completa do enunciado, do manual e dos CSVs procurando
   pegadinhas. Isso virou a seção 4 e influenciou a arquitetura.
2. Discutimos as alternativas (RAG, agente de SQL, function calling, frameworks) e escolhi ferramentas
   fechadas com as contas em Python, porque o maior risco aqui é inventar preço ou vazar dado de cliente.
3. Fui montando por partes (dados, manual, regras, ferramentas, agente), testando cada uma com os dados
   reais e commitando. O histórico do Git mostra essa ordem.
4. Comecei com o Gemini pela camada gratuita. Quando rodei de verdade e vi os problemas descritos na seção 3,
   troquei para o Claude e pedi o modelo mais barato.
5. Não publiquei os exemplos direto: comparei as conversas com os dados e corrigi o que estava errado
   (seção 6).

Erros do assistente que apareceram no caminho:

| Erro | Como apareceu |
|---|---|
| Datas vazias viravam `NaT` e quebravam a consulta de pedidos | teste da ferramenta com pedidos não enviados |
| Um commit entrou com teste falhando (um `\| tail` escondia o código de saída do `pytest`) | conferindo a saída; depois disso, só commit com tudo passando |
| Arquivos de build (`egg-info`) commitados | `git status` antes do commit seguinte |
| A `thought_signature` do Gemini 3 era descartada (erro 400 no segundo turno) | teste com a API real |
| Ukulele chamado de saxofone, categorias vazias citadas, endereço não encontrado, chamado duplicado | revisão das conversas (seção 6) |

O assistente acelerou bastante o código, os testes e a documentação. Ficou comigo decidir o que priorizar,
qual arquitetura usar, qual modelo e quando o resultado estava bom o suficiente.
