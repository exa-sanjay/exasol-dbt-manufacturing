.PHONY: demo up seed seed-uci seed-uci-live dbt-run dbt-test docs clean help ai-setup ai-agent orchestrate

PYTHON := python3
DBT    := $(or $(DBT_CMD),dbt)

help:
	@echo "Exasol + DBT Manufacturing OEE Demo  (with Self-Healing Factory AI)"
	@echo ""
	@echo "  Core pipeline:"
	@echo "  make demo      Full end-to-end run (up + seed + dbt)"
	@echo "  make up        Start PostgreSQL, Exasol, and Ollama containers"
	@echo "  make seed      Seed data into both databases"
	@echo "  make dbt-run   Run all DBT models"
	@echo "  make dbt-test  Run all DBT tests"
	@echo "  make docs      Generate and serve DBT docs (http://localhost:8080)"
	@echo "  make clean        Stop containers and remove volumes"
	@echo ""
	@echo "  Real sensor data (UCI AI4I 2020 Predictive Maintenance dataset):"
	@echo "  make seed-uci     Download UCI dataset + replace synthetic IoT data (batch)"
	@echo "  make seed-uci-live Stream one reading per machine every 5 min (real-time mode)"
	@echo ""
	@echo "  AI layer (run after make demo or seed-uci):"
	@echo "  make ai-setup  Create AI_SCHEMA tables + seed failure pattern vectors"
	@echo "                 + pull Ollama model qwen2.5:0.5b (~400MB, once only)"
	@echo "  make ai-agent  Run the Factory AI Agent (detect → retrieve → reason → recommend)"
	@echo ""
	@echo "  Orchestration (auto-trigger pipeline on new IoT data):"
	@echo "  make orchestrate  Start Dagster UI (http://localhost:3000)"
	@echo "                    Then enable sensor 'new_sensor_data_sensor' in the UI"

demo: up seed dbt-run dbt-test docs

up:
	@echo "==> Starting containers..."
	docker compose up -d
	@echo "==> Waiting for PostgreSQL..."
	@until docker compose exec -T postgres pg_isready -U erp_user -d manufacturing_erp; do sleep 2; done
	@echo "==> Waiting for Exasol (may take ~90s on first start)..."
	@until docker compose exec -T exasol python3 -c \
		"import pyexasol; c=pyexasol.connect(dsn='localhost:8563',user='sys',password='exasol'); print('ready')" \
		2>/dev/null; do sleep 5; done
	@echo "==> Both databases ready."

seed:
	@echo "==> Setting up Exasol schemas, Virtual Schema, and IoT data..."
	$(PYTHON) scripts/setup_exasol.py
	@echo "==> Seed complete."

dbt-run:
	@echo "==> Running DBT models..."
	cd dbt_project && $(DBT) deps && $(DBT) run

dbt-test:
	@echo "==> Running DBT tests..."
	cd dbt_project && $(DBT) test

docs:
	@echo "==> Generating DBT docs..."
	cd dbt_project && $(DBT) docs generate && $(DBT) docs serve --port 8081

seed-uci:
	@echo "==> Seeding IoT data from UCI AI4I 2020 Predictive Maintenance Dataset ..."
	$(PYTHON) scripts/seed_iot_from_uci.py
	@echo "==> Rebuilding dbt models with new data ..."
	cd dbt_project && $(DBT) run

seed-uci-live:
	@echo "==> Live streaming UCI data into Exasol (Ctrl+C to stop) ..."
	$(PYTHON) scripts/seed_iot_from_uci.py --live

ai-setup:
	@echo "==> Creating AI_SCHEMA tables and seeding failure pattern vectors ..."
	$(PYTHON) scripts/setup_ai_tables.py
	@echo "==> Pulling Ollama model qwen2.5:0.5b (~400MB, first run only) ..."
	$(PYTHON) scripts/pull_ollama_model.py

ai-agent:
	@echo "==> Running Self-Healing Factory AI Agent ..."
	$(PYTHON) scripts/factory_ai_agent.py

orchestrate:
	@echo "==> Starting Dagster UI at http://localhost:3000 ..."
	@echo "==> Enable the sensor 'new_sensor_data_sensor' in the UI to start auto-triggering."
	dagster dev

clean:
	docker compose down -v
	@echo "==> Containers and volumes removed."
