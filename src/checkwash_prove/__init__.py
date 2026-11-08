"""checkwash-prove (experimental): show, by running tests, what protection a change dropped.

A companion to checkwash, kept apart from it: checkwash reads a diff and never runs your code; this package runs your
tests. See README.md and THREATMODEL.md. The engine is in engine.py; mutops_v05m2.py is the frozen mutation-operator
catalogue v0.5-m2 (muJava's method-level operators mapped to Python, plus PY-ASRS), vendored byte for byte and checked
by hash.
"""

__version__ = "0.1.0.dev0"
