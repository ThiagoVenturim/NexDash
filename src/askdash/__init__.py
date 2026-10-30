"""askdash: dashboards and reports from natural-language questions.

Questions are answered through a semantic layer of YAML cubes: the model picks
cubes and fills a structured plan, and askdash compiles that plan into
parameterized SQL. The model never writes free SQL.
"""

__version__ = "0.1.0"
