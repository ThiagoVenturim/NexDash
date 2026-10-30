# askdash

Dashboards and reports from natural-language questions, exposed over MCP and REST.

askdash answers questions through a semantic layer: cubes described in YAML, a
structured plan validated by Pydantic, and parameterized SQL compiled through an
allowlist. The language model never writes free SQL. Models can be open source
(Ollama, vLLM) or paid, selected through `faccoder-core` tiers.

Status: early development.

## Development

```
python -m venv .venv
.venv/Scripts/pip install -e ../fabrica-code/packages/core -e .[dev]
git config core.hooksPath .githooks
.venv/Scripts/python -m pytest
.venv/Scripts/ruff check .
python scripts/check_no_emoji.py $(git ls-files)
```

## License

MIT
