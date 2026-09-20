.PHONY: setup build run test benchmark-learning install uninstall clean

PREFIX ?= $(HOME)/.local
INSTALL_FLAGS ?=
ARGS ?=

setup:
	./scripts/setup-zero.sh

build:
	./scripts/build.sh

run: build
	./dist/zero-code $(ARGS)

test: build
	./dist/zero-code --self-test
	python3 -m unittest discover -s tests -v

benchmark-learning: build
	python3 scripts/learning_benchmark.py --tasks 100 --holdout 40 --require-improvement --output .zero/learning-benchmark.json

install:
	sh ./install.sh --prefix "$(PREFIX)" $(INSTALL_FLAGS)

uninstall:
	sh ./install.sh --prefix "$(PREFIX)" --uninstall

clean:
	rm -f dist/zero-code
