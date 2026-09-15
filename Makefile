.PHONY: test test-regression test-smoke test-incremental test-eures test-spike-kuntarekry test-backend test-backend-pg test-web audit-db repair-urls backup-db backup-private backup-all restore-db docker-config browserbase-check transit-distance-check help

# Backups live outside the repository. `profile/` originals and `.env` are
# ignored/private, so a database dump alone is not a recovery set.
BACKUP_DIR ?= $(HOME)/job-search-agent-backups

help:
	@echo "Targets:"
	@echo "  test-regression  Full API regression gate (stdlib, needs network)"
	@echo "  test-smoke       Quick curl smoke (05_apis_testi.sh)"
	@echo "  test-incremental Incremental strategy probes (09_incremental_testi.py)"
	@echo "  test-eures       EURES-only smoke (10_eures_testi.sh)"
	@echo "  test-spike-kuntarekry  Kuntarekry/Valtiolle harvest spikes (11_*, SPIKE_ORG_SAMPLE=50)"
	@echo "  test-backend     Backend pytest suite"
	@echo "  test-backend-pg  Backend suite incl. PostgreSQL tests (needs TEST_DATABASE_URL)"
	@echo "  test-web         Web typecheck and build"
	@echo "  audit-db         Run database consistency audit in the API container"
	@echo "  repair-urls      Backfill broken Duunitori/TMT announcement URLs in the DB"
	@echo "  backup-db        Write a PostgreSQL custom-format backup to BACKUP_DIR"
	@echo "  backup-private   Archive ignored profile originals and .env to BACKUP_DIR"
	@echo "  backup-all       backup-db + backup-private"
	@echo "  restore-db       Restore BACKUP=<file.dump> into PostgreSQL"
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
	cd backend && python3 -m pytest --timeout=10

# Real PostgreSQL semantics matter for SQL/state changes. Point TEST_DATABASE_URL
# at a disposable, already-migrated database; never a production one.
test-backend-pg:
	test -n "$(TEST_DATABASE_URL)"
	cd backend && TEST_DATABASE_URL="$(TEST_DATABASE_URL)" python3 -m pytest --timeout=20

test-web:
	cd web && npm ci && npm run typecheck && npm run build

audit-db:
	docker compose exec api python -m app.audit

repair-urls:
	docker compose exec api python -m app.repair_urls

# Credentials come from the database container itself, not hardcoded Make
# variables, so a non-default POSTGRES_USER/POSTGRES_DB still backs up the
# right database. The dump is written to a temporary file and only renamed
# after a successful, checksummed dump, so a failed run leaves no file that
# looks like a valid archive.
backup-db:
	@mkdir -p "$(BACKUP_DIR)"
	@stamp=$$(date -u +%Y%m%dT%H%M%SZ); \
	tmp="$(BACKUP_DIR)/.jobsearchagent-$$stamp.dump.tmp"; \
	final="$(BACKUP_DIR)/jobsearchagent-$$stamp.dump"; \
	user=$$(docker compose exec -T db sh -lc 'printf %s "$$POSTGRES_USER"'); \
	db=$$(docker compose exec -T db sh -lc 'printf %s "$$POSTGRES_DB"'); \
	test -n "$$user" -a -n "$$db" || { echo "could not read POSTGRES_USER/POSTGRES_DB from db container" >&2; exit 1; }; \
	trap 'rm -f "$$tmp"' EXIT; \
	docker compose exec -T db pg_dump -U "$$user" -d "$$db" --format=custom --no-owner --no-acl > "$$tmp" && \
	mv "$$tmp" "$$final" && \
	chmod 600 "$$final" && \
	sha256sum "$$final" > "$$final.sha256" && \
	echo "wrote $$final"

# profile/raw, profile/extracted and .env are not in Git. They are part of the
# recovery set and are archived separately with owner-only permissions.
backup-private:
	@mkdir -p "$(BACKUP_DIR)"
	@stamp=$$(date -u +%Y%m%dT%H%M%SZ); \
	tmp="$(BACKUP_DIR)/.private-$$stamp.tar.gz.tmp"; \
	final="$(BACKUP_DIR)/private-$$stamp.tar.gz"; \
	trap 'rm -f "$$tmp"' EXIT; \
	tar -czf "$$tmp" --exclude='__pycache__' profile .env 2>/dev/null || { echo "no profile/.env to archive" >&2; exit 1; }; \
	mv "$$tmp" "$$final" && \
	chmod 600 "$$final" && \
	sha256sum "$$final" > "$$final.sha256" && \
	echo "wrote $$final"

backup-all: backup-db backup-private

restore-db:
	test -n "$(BACKUP)"
	test -f "$(BACKUP)"
	user=$$(docker compose exec -T db sh -lc 'printf %s "$$POSTGRES_USER"'); \
	db=$$(docker compose exec -T db sh -lc 'printf %s "$$POSTGRES_DB"'); \
	test -n "$$user" -a -n "$$db" || { echo "could not read POSTGRES_USER/POSTGRES_DB from db container" >&2; exit 1; }; \
	cat "$(BACKUP)" | docker compose exec -T db pg_restore -U "$$user" -d "$$db" --clean --if-exists --no-owner --no-acl

docker-config:
	docker compose config --no-interpolate

browserbase-check:
	set -a && . ./.env && set +a && cd backend && python3 -m app.browserbase_check

transit-distance-check:
	set -a && . ./.env && set +a && cd backend && python3 -m app.transit_distance_check
