.PHONY: setup build run test install uninstall clean

PREFIX ?= $(HOME)/.local
INSTALL_FLAGS ?=

setup:
	./scripts/setup-zero.sh

build:
	./scripts/build.sh

run: build
	./dist/zero-coding

test: build
	./dist/zero-coding --self-test
	python3 -m unittest discover -s tests -v

install:
	sh ./install.sh --prefix "$(PREFIX)" $(INSTALL_FLAGS)

uninstall:
	sh ./install.sh --prefix "$(PREFIX)" --uninstall

clean:
	rm -f dist/zero-coding
