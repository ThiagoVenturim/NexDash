# nexdash

Dashboards e relatórios a partir de perguntas em linguagem natural, expostos por MCP e REST.

English: [README.md](README.md).

*(Chamado askdash no início do desenvolvimento.)*

O nexdash responde perguntas por uma camada semântica: cubos descritos em YAML, um
plano estruturado validado pelo Pydantic e SQL parametrizado compilado por uma
allowlist. O modelo de linguagem nunca escreve SQL livre. Os modelos podem ser open
source (Ollama, vLLM) ou pagos, escolhidos pelos tiers do `nexcoder-core`.

Status: desenvolvimento inicial.

## Camada semântica

O modelo escolhe um cubo e preenche um plano tipado; quem escreve o SQL é o servidor.
Isso evita SQL injection e consultas inventadas:

1. **Cubos (YAML)**: entidades analíticas com tabela ou view de origem, dimensões,
   métricas com agregação, joins permitidos e filtros permitidos.
2. **Plano de consulta (Pydantic)**: cubo, métricas, dimensões, filtros, intervalo de
   datas, ordenação e limite.
3. **Compilador com allowlist**: gera SQL parametrizado com SQLAlchemy e recusa qualquer
   dimensão, métrica, join, filtro ou operador fora do cubo. Valores de filtro são
   sempre parâmetros.
4. **Executor plugável**: SQLite por padrão, ou PostgreSQL, MySQL, SQL Server e ODBC
   via `DB_URL`.
5. **Relatórios gerenciados (`ReportStore`)**: rascunho, preview, diff, publicação,
   rollback e descarte, com códigos de aprovação humana de uso único e curta duração.

```python
from nexdash import CubeRegistry, PlanFilter, PlanOrderBy, QueryPlan, TimeRange, get_executor

registry = CubeRegistry.from_directory("examples/cubes")
plan = QueryPlan(
    cube="orders",
    dimensions=["status"],
    metrics=["order_count", "total_revenue"],
    filters=[PlanFilter(field="status", operator="eq", value="completed")],
    time_range=TimeRange(field="order_date", start="2026-01-01", end="2026-12-31"),
    order_by=[PlanOrderBy(field="total_revenue", direction="desc")],
    limit=10,
)
executor = get_executor()  # SQLite por padrão, ou o banco em DB_URL
result = executor.execute_plan(plan, registry.get("orders"), registry=registry)
```

Referência completa em [docs/SEMANTIC_LAYER.md](docs/SEMANTIC_LAYER.md) e as
verificações entre a resposta do modelo e o banco em
[docs/SQL_GUARDS.md](docs/SQL_GUARDS.md).

## MCP, REST e interface web

As três interfaces usam o mesmo compilador: servidor MCP (stdio com `nexdash --mcp`,
ou `POST /mcp`), API REST (`/api/cubes`, `/api/plan`, `/api/ask`) e uma interface web
mínima em `/`. O acesso HTTP exige o token em `NEXDASH_API_TOKEN` (ambiente ou `.env`).
`--demo` carrega os cubos sintéticos `sales`, `support` e `stock`.

```bash
export NEXDASH_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
nexdash --demo --host 127.0.0.1 --port 8000   # depois abra http://127.0.0.1:8000/
```

Veja [docs/INTERFACES.md](docs/INTERFACES.md).

## Banco plugável e containers

- **Banco**: configure `DB_URL` (`postgresql+psycopg://...`, `mysql+pymysql://...`,
  `mssql+pyodbc://...`). Drivers e extras em [docs/DATABASE.md](docs/DATABASE.md).
- **Containers**: `Dockerfile` multi-arquitetura e `docker-compose.yml` (app e
  PostgreSQL opcional).
- **Kubernetes**: manifestos em `k8s/`.
- **Scripts**: `scripts/run.ps1` (Windows PowerShell) e `scripts/run.sh` (Linux/macOS).

## Dashboards ao vivo

Agentes escrevem o HTML; o servidor coleta os dados das fontes configuradas (`sql` com
plano semântico ou `SELECT` protegido, `http`, `csv`, `sheets`), guarda segredos
criptografados com Fernet (referenciados como `{{env.NOME}}`) e injeta os dados na
página como `window.NEXDASH_DATA`, sob uma Content-Security-Policy com
`connect-src 'none'`. Há proteção contra SSRF em cada redirecionamento, atualização
agendada (`never`, `hourly`, `daily`, `weekly`) que mantém o último dado bom quando uma
fonte falha, validade de 30 dias com renovação e tokens de edição (`ed_...`) mostrados
uma vez e guardados só como hash SHA-256. Veja
[docs/LIVE_DASHBOARDS.md](docs/LIVE_DASHBOARDS.md).

## Conectores

`nexdash.connectors` lê itens de trabalho do GitHub, Azure DevOps, Redmine, Jira ou de
qualquer API REST JSON, normaliza tudo numa tabela `work_items` e consulta pelo cubo
`work_items`. Credenciais vêm só do ambiente ou do `.env`. Veja
[docs/CONNECTORS.md](docs/CONNECTORS.md).

## Modelos de relatório e motor compartilhado

`nexdash.report_models` traz três relatórios parametrizados (produtividade de
desenvolvimento, postura de segurança e métricas de suporte), cada um com cubo e
template de dashboard. Outros projetos consomem esses relatórios pelo nexdash, via MCP
(`nexdash_run_report`) ou REST (`/api/reports/run`), com token e escopos próprios por
projeto. Veja [docs/REPORT_MODELS.md](docs/REPORT_MODELS.md) e
[docs/REPORT_ENGINE.md](docs/REPORT_ENGINE.md).

## Documentação

| Página | Conteúdo |
| --- | --- |
| [SEMANTIC_LAYER.md](docs/SEMANTIC_LAYER.md) | Camada semântica vs text-to-SQL, YAML dos cubos, planos, perguntas em linguagem natural |
| [SQL_GUARDS.md](docs/SQL_GUARDS.md) | Verificações sobre a saída do modelo, guarda de SQL somente leitura, limites conhecidos |
| [LIVE_DASHBOARDS.md](docs/LIVE_DASHBOARDS.md) | Fontes, segredos, SSRF, sandbox CSP, ciclo de vida e tokens |
| [INTERFACES.md](docs/INTERFACES.md) | MCP, REST e interface web |
| [DATABASE.md](docs/DATABASE.md) | URLs de banco, drivers, containers |
| [CONNECTORS.md](docs/CONNECTORS.md) | Conectores GitHub, Azure DevOps, Redmine, Jira e REST |
| [REPORT_MODELS.md](docs/REPORT_MODELS.md) | Modelos de relatório prontos |
| [REPORT_ENGINE.md](docs/REPORT_ENGINE.md) | Motor de relatórios para outros projetos |
| [COMPARISON.md](docs/COMPARISON.md) | Comparação com ferramentas parecidas |
| [REFERENCIAS.md](docs/REFERENCIAS.md) | Pesquisa de projetos parecidos (pt-BR) |

As páginas técnicas estão em inglês. Os exemplos em Python das três primeiras rodam
offline em `tests/test_docs.py`.

## Desenvolvimento

```bash
python -m venv .venv
.venv/Scripts/pip install -e ../fabrica-code/packages/core -e .[dev]
git config core.hooksPath .githooks
.venv/Scripts/python -m pytest
.venv/Scripts/ruff check .
python scripts/check_no_emoji.py $(git ls-files)
```

## Origem

O nexdash nasceu de conceitos de dashboards analíticos explorados no ApoIA e no
lochome, rearquitetados como um motor de consulta semântica independente e open source.

## Licença

MIT
