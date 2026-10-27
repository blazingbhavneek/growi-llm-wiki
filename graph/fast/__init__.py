"""Fast policy for ``sync --fast``. Its behaviour lives here, never in the shared engine.

The engine calls the hooks on ``common.policy.Policy`` (defaults = standard production
behaviour); ``graph/fast/policy.py`` overrides them with the code in ``wiki.py`` and
``linker.py``. Standard sync never imports this package.
"""
