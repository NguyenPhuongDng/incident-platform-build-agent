.PHONY: install dev check seed smoke clean

PY=.venv/bin/python

install:
	python3.12 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt

dev:
	./run.sh

check:
	$(PY) scripts/check_qwen.py

seed:
	$(PY) scripts/seed.py

smoke:
	$(PY) scripts/smoke_test.py

clean:
	rm -rf data/demo.db data/chroma
