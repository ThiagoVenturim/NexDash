# Referencias e diferencial do askdash

Projetos parecidos (open source e privados), o diferencial do askdash e o que melhorar com base neles. Pesquisa de 2026-11-08; verifique cada projeto antes de usar.

## Parecidos

### Open source
- **Wren AI** (Canner): texto-para-SQL e texto-para-grafico com camada semantica (MDL que restringe toda consulta do LLM); dashboards e relatorios; Apache 2.0. O mais proximo.
- **Cube**: camada semantica universal que conecta fontes a ferramentas de BI; Apache 2.0 (semantica, nao dashboard completo).
- **Lightdash**: BI open source em cima de projetos dbt (governado, versionado).
- **Drizby / drizzle-cube**: BI em cima de camada semantica em TypeScript.
- **Synmetrix**, **OrionBelt**, **OpenChatBI**: camada semantica / chat sobre dados.
- **Metabase / Superset**: BI open source classico (sem camada semantica nativa forte).

### Privados / fechados
- **ThoughtSpot**, **Power BI Copilot**, **Tableau Pulse/Ask Data**, **Looker** (LookML): perguntas em linguagem natural e semantica, fechados.

## Nosso diferencial
- **O LLM nunca escreve SQL livre**: cubos em YAML, plano validado por Pydantic e SQL parametrizado por allowlist. Seguranca por construcao (sem SQL injection, sem query fora do permitido).
- **Leve e embutivel**: Python, exposto por MCP e REST, para ser o **motor de relatorios compartilhado** do portfolio (faccoder e Support Engine consomem), nao um BI monolitico.
- **Escolha de modelo aberto/pago** via faccoder-core (mesma base do resto).
- **Conectores voltados ao nosso uso**: GitHub, Azure DevOps, Redmine, Jira, suporte (issues #9-#12), para relatorios de produtividade e seguranca, nao so tabelas de banco.

## O que melhorar aprendendo com eles
- **MDL rica como a do Wren AI**: evoluir os cubos para metricas derivadas, relacoes e regras de acesso mais expressivas.
- **Mais bancos** (Wren/Cube suportam muitos): alem de SQLite, Postgres/BigQuery/DuckDB.
- **Padrao semantico** (Cube/dbt/Lightdash): considerar compatibilidade com dbt ou com a spec de semantic layer para pegar carona no ecossistema.
- **Texto-para-grafico** (Wren AI): nao so dados, mas sugerir o grafico certo (reusar a skill graficos-com-regras).
- **UI de exploracao** leve (Metabase/Superset): uma interface simples alem de MCP/REST.

## Sources
- https://awesome.ecosyste.ms/projects/github.com%2Fcanner%2Fwrenai
- https://holistics.io/bi-tools/semantic-layer
- https://openalternative.co/tags/bi
- https://project-awesome.org/r/Canner-WrenAI
