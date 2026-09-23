/* The host owns native resources and stack-backed App storage for its lifetime.
 * Only the event-loop callback changes, between calls with no Zero frames from
 * the previous tick active. Keep old images mapped: App can retain their strings.
 */
#define _POSIX_C_SOURCE 200809L
#include <dlfcn.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

struct ZeroLiveApp;
extern int zero_live_tick_v1(struct ZeroLiveApp *app);
typedef int (*tick_fn)(struct ZeroLiveApp *);
static tick_fn active_tick;
static tick_fn pending_tick;
static void *images[32];
static size_t image_count;
static char input[8192], directory[4096], version[64];
static size_t input_size;
static int input_overflow;
static int changed;
static unsigned char output[8192];
static size_t output_size;
static int output_failed;

void zero_live_reset(void) {
    input_size = 0;
    input_overflow = 0;
}

void zero_live_byte(unsigned int value) {
    if (value == 0 || input_size + 1 >= sizeof input) input_overflow = 1;
    else input[input_size++] = (char)value;
}

int zero_live_field(unsigned int field) {
    input[input_size] = 0;
    char *target = field == 0 ? directory : version;
    size_t capacity = field == 0 ? sizeof directory : sizeof version;
    int ok = !input_overflow && input_size > 0 && input_size < capacity;
    if (ok) memcpy(target, input, input_size + 1);
    else target[0] = 0;
    zero_live_reset();
    return ok;
}

int zero_live_stage(void) {
    if (pending_tick || image_count >= 32 || directory[0] != '/' || !version[0]) return 0;
    char path[8192];
    if (snprintf(path, sizeof path, "%s/zero-live.so", directory) >= (int)sizeof path) return 0;
    void *image = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (!image) return 0;
    void *symbol = dlsym(image, "zero_live_tick_v1");
    if (!symbol) { dlclose(image); return 0; }
    memcpy(&pending_tick, &symbol, sizeof pending_tick);
    images[image_count++] = image;
    return 1;
}

int zero_live_pending(void) { return pending_tick != NULL; }

static int activation_failed(struct ZeroLiveApp *app) { (void)app; return -1; }

tick_fn zero_live_target(void) {
    if (pending_tick) {
        // The launcher and all subsequently spawned workers observe the active
        // generation. Updating this only here preserves the current tick's ID.
        if (setenv("ZERO_LEARNING_PROGRAM_VERSION", version, 1) ||
            setenv("ZERO_LEARNING_PROGRAM_GRAPH_DIR", directory, 1)) return activation_failed;
        active_tick = pending_tick;
        pending_tick = NULL;
        changed = 1;
    }
    return active_tick ? active_tick : zero_live_tick_v1;
}

int zero_live_changed(void) { int result = changed; changed = 0; return result; }
unsigned int zero_live_at(unsigned int field, unsigned int index) {
    const char *value = field == 0 ? directory : version;
    size_t capacity = field == 0 ? sizeof directory : sizeof version;
    return index < capacity ? (unsigned char)value[index] : 0;
}

/* Zero reserves callee-saved registers for argc/argv. A normal C callback may
 * temporarily use them for C locals. Return from C before tail-entering Zero,
 * restoring that implicit context as well as the explicit App argument. */
#ifdef __APPLE__
#define SYM(name) "_" name
#else
#define SYM(name) name
#endif
#if defined(__aarch64__) || defined(__arm64__)
__asm__(".text\n.p2align 2\n.globl " SYM("zero_live_dispatch") "\n"
        SYM("zero_live_dispatch") ":\n"
        "stp x29, x30, [sp, #-32]!\nmov x29, sp\nstr x0, [sp, #16]\n"
        "bl " SYM("zero_live_target") "\nmov x16, x0\nldr x0, [sp, #16]\n"
        "ldp x29, x30, [sp], #32\nbr x16\n");
#elif defined(__x86_64__)
__asm__(".text\n.p2align 4\n.globl " SYM("zero_live_dispatch") "\n"
        SYM("zero_live_dispatch") ":\n"
        "pushq %rbp\nmovq %rsp, %rbp\nsubq $16, %rsp\nmovq %rdi, -8(%rbp)\n"
        "call " SYM("zero_live_target") "\nmovq %rax, %r11\nmovq -8(%rbp), %rdi\n"
        "leave\njmp *%r11\n");
#else
#error "Live evolution requires a supported Zero host: ARM64 or x86_64"
#endif

int zero_output_flush(void) {
    size_t sent = 0;
    while (sent < output_size && !output_failed) {
        ssize_t count = write(STDOUT_FILENO, output + sent, output_size - sent);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) output_failed = 1;
        else sent += (size_t)count;
    }
    output_size = 0;
    return !output_failed;
}

void zero_output_byte(unsigned int value) {
    if (output_size == sizeof output) zero_output_flush();
    output[output_size++] = (unsigned char)value;
}
