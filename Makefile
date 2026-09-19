.PHONY: setup build run test clean

setup:
	./scripts/setup-zero.sh

build:
	./scripts/build.sh

run: build
	./dist/zero-coding

test: build
	./dist/zero-coding --self-test
	python3 -m unittest discover -s tests -v

clean:
	rm -f dist/zero-coding
