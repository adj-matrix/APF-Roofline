"""Extensions on top of the frozen base model (tpch.model, params 8402a6f5657e), one subpackage each.

Dependencies point one way: tpch.ext.<name> -> base (read-only use of tpch.model and the infrastructure);
no extension imports another; the base model code and model/params.json are not modified.
Layout per extension <name>: code src/tpch/ext/<name>/, tests tests/test_ext_<name>.py, scripts
queries/ext/<name>/, docs docs/ext/<name>.md, parameters / predictions model/ext/<name>/.
"""
