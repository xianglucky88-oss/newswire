PY=python3
run:
	PYTHONPATH=src $(PY) -m newswire.cli
test:
	@$(PY) -c "import pytest" 2>/dev/null && PYTHONPATH=src $(PY) -m pytest -q || PYTHONPATH=src $(PY) tests/run_tests.py
clean:
	rm -rf .pytest_cache **/__pycache__ *.db
