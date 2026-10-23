# POSIX task runner. Windows users: see tasks.ps1 (same targets).
PY ?= .venv/bin/python

.PHONY: setup lint test run day demo schedule unschedule dbt docs clean up down

setup:          ## create the venv and install the local stack
	python3 -m venv .venv && $(PY) -m pip install -r requirements.txt

lint:
	$(PY) -m ruff check .

test:
	$(PY) -m pytest -q

run:            ## full end-to-end run (generates source data on first run)
	$(PY) -m pipeline.run

day:            ## append one day of source activity, then run incrementally
	$(PY) -m pipeline.run --append-day

demo:           ## build + 3 daily runs incl. one injected gate failure and its recovery (ops page history)
	$(PY) tools/demo_history.py

schedule:       ## cron: weekdays 06:00 local, before the 07:00 SLO deadline
	( crontab -l 2>/dev/null | grep -v 'vsc-data-platform-daily'; \
	  echo "0 6 * * 1-5 cd $(CURDIR) && $(PY) -m pipeline.run --append-day  # vsc-data-platform-daily" ) | crontab -

unschedule:
	crontab -l 2>/dev/null | grep -v 'vsc-data-platform-daily' | crontab -

dbt:
	$(PY) -m pipeline.run --only dbt

docs:
	$(PY) -m dbt.cli.main docs generate --project-dir dbt --profiles-dir dbt

clean:
	rm -rf data site dbt/target dbt/logs

up:             ## server stack (needs Docker; untested)
	docker compose --profile airflow --profile bi up -d

down:
	docker compose --profile airflow --profile bi --profile source down
