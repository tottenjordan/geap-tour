"""DOE (Design of Experiments) framework for scaling GEAP experimentation.

Varies agent-configuration factors, fans out one Vertex ``PipelineJob`` per
design point (reusing the eval pipeline as the per-run engine), harvests each
run's ``full_results.json`` from GCS, and reports main effects / cost-quality
trade-offs.

Modules:
  - ``factors``  — declarative factor registry (name, channel, levels)
  - ``design``   — fractional-factorial / full-factorial design generator
  - ``launch``   — fan-out launcher (one submit.py subprocess per design point)
  - ``harvest``  — poll jobs + pull tidy results into a pandas DataFrame
  - ``analyze``  — main effects + cost-quality frontier report
  - ``run_doe``  — orchestrator CLI
"""
