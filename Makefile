PY=python3
run:
	PYTHONPATH=src $(PY) -m newswire.cli
test:
	PYTHONPATH=src $(PY) -m pytest -q
demo:
	PYTHONPATH=src $(PY) -m newswire.cli --fixture fixtures/scenario.json --db newswire-demo.db --reset
clean:
	rm -rf .pytest_cache **/__pycache__ *.db
