# Contributing

Fork the repository, create a focused branch, and keep each commit independently tested.

Before submitting a change:

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
python -m coverage erase
PYTHONPATH=src python -m coverage run --source=src/anima -m unittest discover -s tests
python -m coverage report --show-missing
git diff --check
```

New SDK-free classes should have 100% line coverage. Add an architecture test when a new
layer or dependency restriction is introduced. Do not commit credentials, persona data,
runtime state, generated media, or copyrighted datasets.
