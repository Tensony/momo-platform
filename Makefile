.PHONY: install test cover lint proofs build deploy tf-fmt scan load
PY ?= python3
TEST_DATABASE_URL ?= postgresql://postgres@localhost/momo_test

install:       ## install dev dependencies
	$(PY) -m pip install -r requirements-dev.txt

test:          ## unit + integration tests (integration needs TEST_DATABASE_URL)
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(PY) -m pytest

cover:         ## tests with the >=60% coverage gate
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(PY) -m pytest --cov --cov-report=term-missing --cov-report=xml

lint:
	ruff check src tests scripts

scan:          ## secret scan (same as CI)
	gitleaks detect --config .gitleaks.toml --redact --verbose

proofs:        ## run the three correctness proofs locally against TEST_DATABASE_URL
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(PY) scripts/proofs/run_proofs.py --local

build:         ## build Lambda artifacts
	scripts/build_lambdas.sh

tf-fmt:
	terraform -chdir=infra/terraform fmt -recursive

deploy:        ## make deploy EMAIL=you@example.com
	scripts/deploy.sh $(EMAIL)

load:          ## k6 load test against a deployed API: make load URL=https://... TOKEN=...
	k6 run -e BASE_URL=$(URL) -e TOKEN=$(TOKEN) load/k6-autoscale.js
