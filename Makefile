PYTHON ?= python3

.PHONY: build test install status uninstall

build:
	$(PYTHON) fullscreen_fix.py build

test:
	$(PYTHON) -m unittest discover -s tests -v

install:
	$(PYTHON) fullscreen_fix.py install

status:
	$(PYTHON) fullscreen_fix.py status

uninstall:
	$(PYTHON) fullscreen_fix.py uninstall
