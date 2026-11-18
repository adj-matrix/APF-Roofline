"""Performance model of single-query PostgreSQL execution on this machine.

    T = C / f + W        (cost.py)

Layers, each depending only on the ones above it:
  pg.py         PostgreSQL 18 executor arithmetic (hash-join sizing, spilled-tuple bytes); no parameters
  plan.py       EXPLAIN trees: target plan (estimates only) vs calibration plan (actual rows)
  workload.py   what the query must do at the target SF: pages, spilled tuples, temp bytes, batches
  params.py     parameters with provenance (bench / train / assumption)
  registry.py   which measurements are training data and which are tests (calibration may not read tests)
  data.py       measured runs and benchmark results as observations
  calibrate.py  parameters from benchmarks and training runs  -> model/params.json
  cost.py       workload + parameters -> time, with every term
  evaluate.py   replay of registered conditions, term by term

CLI: ./tpch model calibrate | predict --query Q --sf N [--set k=v] | evaluate

common.py and queries/q*/model.py are the stage-1 models (SF1/SF10 reports); they are kept so that
those reports can be rebuilt, and are not used here.
"""
