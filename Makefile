.PHONY: test test-regression test-smoke test-incremental test-eures test-spike-kuntarekry test-backend test-web audit-db backup-db restore-db docker-config browserbase-check transit-distance-check help

help:
	@echo "Targets:"
	@echo "  test-regression  Full API regression gate (stdlib, needs network)"
	@echo "  test-smoke       Quick curl smoke (05_apis_testi.sh)"
	@echo "  test-incremental Incremental strategy probes (09_incremental_testi.py)"
	@echo "  test-eures       EURES-only smoke (10_eures_testi.sh)"
	@echo "  test-spike-kuntarekry  Kuntarekry/Valtiolle harvest spikes (11_*, SPIKE_ORG_SAMPLE=50)"
	@echo "  test-backend     Backend pytest suite"
	@echo "  test-web         Web typecheck and build"
	@echo "  audit-db         Run database consistency audit in the API container"
	@echo "  backup-db        Write PostgreSQL custom-format backup to backups/"
	@echo "  restore-db       Restore BACKUP=backups/file.dump into PostgreSQL"
	@echo "  docker-config    Validate compose.yaml"
	@echo "  browserbase-check  Verify BROWSERBASE_API_KEY (loads .env)"
	@echo "  transit-distance-check  Verify GOOGLE_MAPS_API_KEY transit routing (loads .env)"
	@echo "  test             Alias for test-regression"

test: test-regression

test-regression:
	python3 scrape-test/regression_test.py

test-smoke:
	bash scrape-test/05_apis_testi.sh

test-incremental:
	python3 scrape-test/09_incremental_testi.py

test-eures:
	bash scrape-test/10_eures_testi.sh

test-spike-kuntarekry:
	SPIKE_ORG_SAMPLE=50 python3 scrape-test/11_kuntarekry_valtiolle_spike.py

test-backend:
	cd backend && python3 -m pytest

test-web:
	cd web && npm ci && npm run typecheck && npm run build

audit-db:
	docker compose exec api python -m app.audit

backup-db:
	mkdir -p backups
	docker compose exec -T db pg_dump -U jobsearchagent -d jobsearchagent --format=custom --no-owner --no-acl > backups/jobsearchagent-$$(date -u +%Y%m%dT%H%M%SZ).dump

restore-db:
	test -n "$(BACKUP)"
	test -f "$(BACKUP)"
	cat "$(BACKUP)" | docker compose exec -T db pg_restore -U jobsearchagent -d jobsearchagent --clean --if-exists --no-owner --no-acl

docker-config:
	docker compose config --no-interpolate

browserbase-check:
	set -a && . ./.env && set +a && cd backend && python3 -m app.browserbase_check

transit-distance-check:
	set -a && . ./.env && set +a && cd backend && python3 -m app.transit_distance_check
