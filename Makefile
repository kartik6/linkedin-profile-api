.PHONY: install dev test lint fmt run mock e2e docker deploy secrets

install:
	uv venv --python 3.12
	uv pip install -r requirements-dev.txt

test:
	.venv/bin/python -m pytest -q

lint:
	.venv/bin/ruff check app tests scripts

fmt:
	.venv/bin/ruff format app tests scripts
	.venv/bin/ruff check --fix app tests scripts

run:
	.venv/bin/uvicorn app.main:app --reload --port 8080

mock:
	.venv/bin/python scripts/mock_linkedin.py --mode all

e2e:
	.venv/bin/python scripts/e2e.py

docker:
	docker build -t linkedin-profile-api .
	docker run --rm -p 8080:8080 --env-file .env linkedin-profile-api

deploy:
	fly deploy

secrets:
	@echo "fly secrets set LI_AT=... JSESSIONID='ajax:...' API_KEYS=..."
