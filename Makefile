.PHONY: setup pull-public pull-qcew pull-bls-laus pull-permits pull-demographics pull-supply normalize index report publish test lint format

# CBSA codes for the QCEW pull. Override at the CLI:
#   make pull-qcew METROS="19100 12420"
# Defaults are the union of msa_code values currently in agents/configs/*.yaml.
METROS ?= 12420 13140 13820 19100 26620 33260 41700 43300

setup:
	uv venv && uv pip install -r requirements.txt && pre-commit install
	uv run playwright install chromium --with-deps

pull-qcew:
	@for msa in $(METROS); do \
		echo ">>> QCEW $$msa"; \
		uv run python etl/ingest_qcew.py $$msa || echo "[warn] QCEW failed for $$msa, continuing"; \
	done

pull-bls-laus:
	@for msa in $(METROS); do \
		echo ">>> BLS LAUS $$msa"; \
		uv run python etl/ingest_bls.py $$msa \
			--output data/public/processed/macro/bls_laus_$$msa.parquet \
			|| echo "[warn] BLS LAUS failed for $$msa, continuing"; \
	done

pull-permits:
	@for msa in $(METROS); do \
		echo ">>> Census BPS permits $$msa"; \
		uv run python etl/ingest_census_permits.py $$msa \
			|| echo "[warn] Permits failed for $$msa, continuing"; \
	done

pull-public: pull-qcew pull-bls-laus pull-permits

pull-demographics:
	@if [ -z "$(CONFIG)" ]; then echo "Usage: make pull-demographics CONFIG=agents/configs/<property>.yaml"; exit 1; fi
	uv run python etl/ingest_demographics.py --config $(CONFIG)
	uv run python etl/render_demographics.py --config $(CONFIG) --html

pull-supply:
	@if [ -z "$(CONFIG)" ]; then echo "Usage: make pull-supply CONFIG=agents/configs/<property>.yaml"; exit 1; fi
	uv run python etl/ingest_supply.py --config $(CONFIG)
	uv run python etl/render_supply.py --config $(CONFIG) --html

normalize:
	uv run python etl/transform_normalize.py

index:
	uv run python retrieval/indexer.py

report:
	uv run python agents/runners/run_local.py --config agents/configs/austin_tx.yaml

publish:
	git tag -a "report-$(shell date +%Y%m%d)" -m "Published reports" && git push --tags

lint:
	uv run ruff check .

format:
	uv run ruff check --fix . && uv run ruff format .

test:
	uv run pytest
