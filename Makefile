.PHONY: install test selftest bench lint stats secrets run web

install:
	python -m pip install -e ".[dev]"
	cd frontend && npm install

test:
	cd backend && python -m pytest -q

selftest:
	cd backend && python selftest.py

bench:
	cd backend && python mrbench.py

lint:
	ruff check backend

stats:
	python scripts/repo_stats.py

secrets:
	python scripts/check_secrets.py

run:
	cd backend && uvicorn app:app --host 127.0.0.1 --port 8000

web:
	cd frontend && npm run dev
