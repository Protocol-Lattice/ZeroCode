"""Native activation boundaries, shared state, and failed-load recovery."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LiveHostTests(unittest.TestCase):
    def test_activation_keeps_host_resources_and_old_strings_after_failed_load(self):
        with tempfile.TemporaryDirectory(prefix="zero live host ") as temporary:
            root = Path(temporary)
            header = 'struct ZeroLiveApp { int calls; const char *remembered; };\n'
            host = root / "host.c"
            host.write_text('''#define _POSIX_C_SOURCE 200809L
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#include "exit.h"
''' + header + '''
static int services;
int host_service(void) { return ++services; }
int zero_live_tick_v1(struct ZeroLiveApp *app) {
    ++app->calls;
    return host_service();
}
static void field(const char *value, unsigned int key) {
    zero_live_reset();
    while (*value) zero_live_byte((unsigned char)*value++);
    assert(zero_live_field(key));
}
static int stage(const char *directory, const char *version) {
    field(directory, 0); field(version, 1);
    return zero_live_stage();
}
int main(int argc, char **argv) {
    assert(argc == 4);
    struct ZeroLiveApp app = {0};
    assert(zero_live_dispatch(&app) == 1 && app.calls == 1);
    assert(stage(argv[1], "first"));
    assert(zero_live_pending() && app.calls == 1 && services == 1);
    assert(zero_live_dispatch(&app) == 2 && app.calls == 3);
    assert(!zero_live_pending() && zero_live_changed());
    assert(!zero_live_changed());
    assert(!strcmp(getenv("ZERO_LEARNING_PROGRAM_VERSION"), "first"));
    assert(!strcmp(getenv("ZERO_LEARNING_PROGRAM_GRAPH_DIR"), argv[1]));
    assert(!strcmp(app.remembered, "first module string"));
    assert(!stage(argv[3], "missing"));
    assert(!zero_live_pending() && !zero_live_changed());
    assert(zero_live_dispatch(&app) == 3 && app.calls == 5);
    assert(!strcmp(getenv("ZERO_LEARNING_PROGRAM_VERSION"), "first"));
    assert(stage(argv[2], "second"));
    assert(zero_live_dispatch(&app) == 4 && app.calls == 8);
    assert(zero_live_changed());
    assert(!strcmp(getenv("ZERO_LEARNING_PROGRAM_VERSION"), "second"));
    assert(!strcmp(app.remembered, "first module string"));
    return 0;
}
''')

            def run(arguments):
                result = subprocess.run(arguments, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            modules = []
            for name, increment, remember in (("first", 2, 'app->remembered = "first module string";'),
                                               ("second", 3, "")):
                directory = root / name
                directory.mkdir()
                source = directory / "module.c"
                source.write_text(header + 'extern int host_service(void);\n'
                                  'int zero_live_tick_v1(struct ZeroLiveApp *app) {\n'
                                  f'app->calls += {increment}; {remember}\nreturn host_service();\n}}\n')
                flags = (["-dynamiclib", "-Wl,-undefined,dynamic_lookup"] if sys.platform == "darwin"
                         else ["-shared", "-Wl,-Bsymbolic"])
                run(["cc", "-fPIC", *flags, str(source), "-o", str(directory / "zero-live.so")])
                modules.append(str(directory))
            export = "-Wl,-export_dynamic" if sys.platform == "darwin" else "-rdynamic"
            run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", export,
                 "-I", str(ROOT / "native"), str(host), str(ROOT / "native/live_program.c"),
                 "-ldl", "-o", str(root / "host")])
            run([str(root / "host"), *modules, str(root / "missing")])


if __name__ == "__main__":
    unittest.main()
