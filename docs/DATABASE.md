# Pluggable Database Configuration (Bring Your Own Database)

NexDash uses SQLite by default with zero setup required. In production or enterprise
deployments, you can point NexDash to an external relational database via SQLAlchemy
and ODBC connection strings.

## Database URL Configuration

Configure the database connection using the `DB_URL` environment variable or place it
inside a `.env` file in your application root directory.

Secrets must remain exclusively in `.env` and must never be committed to git repositories.

### Supported Databases and URL Formats

| Engine | Connection URL Example | Optional Package Extra |
| :--- | :--- | :--- |
| **SQLite (Default)** | `sqlite:///data/nexdash.db` or `sqlite:///:memory:` | Included (standard library) |
| **PostgreSQL** | `postgresql+psycopg://username:password@hostname:5432/database` | `pip install "nexdash[postgres]"` |
| **MySQL / MariaDB** | `mysql+pymysql://username:password@hostname:3306/database` | `pip install "nexdash[mysql]"` |
| **SQL Server (ODBC)** | `mssql+pyodbc://username:password@hostname:1433/database?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes` | `pip install "nexdash[mssql]"` |

Install all database drivers at once with:

```bash
pip install "nexdash[all-dbs]"
```

## Security and Parameterized Execution

NexDash enforces strict query isolation across all supported databases:

1. **Allowlists**: Analytical dimensions, metrics, and joins must be declared in YAML cubes.
2. **Parameterized Binding**: Filter values are bound using SQLAlchemy named parameters (`:param_name`). Free-form SQL concatenation is rejected.
3. **SELECT-Only Guard**: Dashboards and API endpoints enforce read-only `SELECT` statements and reject DDL/DML, stored procedures, or multi-statement commands.

## Running with Containers

### Docker Compose

The included `docker-compose.yml` starts NexDash alongside an optional PostgreSQL service:

```bash
# Start with default SQLite storage:
docker compose up --build

# Start with PostgreSQL:
DB_URL=postgresql+psycopg://nexdash:nexdash_secret@db:5432/nexdash docker compose up --build
```

### Kubernetes

Example manifests are provided in `k8s/`:

- `k8s/configmap.yaml`: Port and environment variables.
- `k8s/secret.yaml`: Secure database credentials (`DB_URL`).
- `k8s/deployment.yaml`: Deployment with non-root security context and probes.
- `k8s/service.yaml`: ClusterIP service.
- `k8s/kustomization.yaml`: Kustomize package bundle.

Apply manifests to your cluster:

```bash
kubectl apply -k k8s/
```

## Cross-Platform Execution Scripts

NexDash includes cross-platform launch scripts:

- **Windows (PowerShell)**:
  ```powershell
  .\scripts\run.ps1                     # Run server locally
  .\scripts\run.ps1 -Mode docker        # Run with Docker Compose
  .\scripts\run.ps1 -Mode test          # Run test suite
  ```

- **Linux and macOS (Bash)**:
  ```bash
  ./scripts/run.sh                      # Run server locally
  ./scripts/run.sh docker               # Run with Docker Compose
  ./scripts/run.sh test                 # Run test suite
  ```
