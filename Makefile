.PHONY: install dev test run doctor clean

install:
	pipx install . || pip install .

dev:
	pip install -e ".[dev,subscription]"

test:
	PYTHONPATH=src python -m unittest discover -s tests -v

run:
	vibe-research

doctor:
	vibe-research doctor

clean:
	rm -rf build dist *.egg-info src/*.egg-info .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} +
