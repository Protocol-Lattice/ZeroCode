#define _GNU_SOURCE
#define _DARWIN_C_SOURCE
#include "exit.h"

/* Scalar transport, like session_log.c. Policy, JSON, replay and decisions live
 * in Zero's canonical program graph. This bridge supplies durable transactions
 * and private experience journals; it never runs a proposed command. */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

#define LEARNING_CAP (2 * 1024 * 1024)
static unsigned char transfer[LEARNING_CAP + 1];
static size_t transfer_size;
static int transfer_valid = 1;
typedef struct {
    int dir;
    unsigned char before[LEARNING_CAP + 1];
    size_t size;
    int loaded;
} LearningStore;
static LearningStore stores[2] = {{.dir = -1}, {.dir = -1}};
static unsigned selected;
static int experience_fd = -1;
static char experience_name[96];
static unsigned serial;

void zero_learning_reset(void) { transfer_size = 0; transfer_valid = 1; }
void zero_learning_byte(unsigned int value) {
    if (value > 255 || transfer_size >= LEARNING_CAP) { transfer_valid = 0; return; }
    transfer[transfer_size++] = (unsigned char)value;
}
unsigned int zero_learning_at(unsigned int index) {
    return index < transfer_size ? transfer[index] : 0;
}
static int private_dir(int parent, const char *name) {
    if (mkdirat(parent, name, 0700) && errno != EEXIST) return -1;
    int fd = openat(parent, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    struct stat st;
    if (fd < 0) return -1;
    if (fstat(fd, &st) || st.st_uid != getuid() || (st.st_mode & 0022)) { close(fd); return -1; }
    return fd;
}
static int regular(int fd) {
    struct stat st;
    return fd >= 0 && !fstat(fd, &st) && S_ISREG(st.st_mode) && st.st_uid == getuid()
        && st.st_nlink == 1 && !(st.st_mode & 0077) && st.st_size <= LEARNING_CAP;
}
static int read_graph(int dir, unsigned char *out, size_t *size) {
    int fd = openat(dir, "graph.json", O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
    *size = 0;
    if (fd < 0) return errno == ENOENT;
    if (!regular(fd)) { close(fd); return 0; }
    while (*size < LEARNING_CAP) {
        ssize_t n = read(fd, out + *size, LEARNING_CAP - *size);
        if (n < 0 && errno == EINTR) continue;
        if (n < 0) { close(fd); return 0; }
        if (!n) { close(fd); return 1; }
        *size += (size_t)n;
    }
    unsigned char extra;
    int ok = read(fd, &extra, 1) == 0;
    close(fd);
    return ok;
}
static int write_all(int fd, const unsigned char *data, size_t size) {
    while (size) {
        ssize_t n = write(fd, data, size);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return 0;
        data += n; size -= (size_t)n;
    }
    return 1;
}
int zero_learning_open(unsigned int scope) {
    if (scope > 1) return 0;
    selected = scope;
    if (stores[scope].dir >= 0) return 1;
    if (!transfer_valid || !transfer_size || transfer_size >= 4096
            || memchr(transfer, 0, transfer_size)) return 0;
    transfer[transfer_size] = 0;
    int root = open((char *)transfer, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (root < 0) return 0;
    int state = private_dir(root, scope ? "zero-code-learning" : ".zero-agent");
    close(root);
    if (state < 0) return 0;
    int dir = private_dir(state, "learning");
    close(state);
    if (dir < 0) return 0;
    stores[scope].dir = dir;
    return 1;
}
int zero_learning_select(unsigned int scope) {
    if (scope > 1 || stores[scope].dir < 0) return 0;
    selected = scope; return 1;
}
int zero_learning_load(void) {
    LearningStore *s = &stores[selected];
    s->loaded = 0;
    if (s->dir < 0 || !read_graph(s->dir, s->before, &s->size)) return -1;
    memcpy(transfer, s->before, s->size);
    transfer_size = s->size; transfer_valid = 1; s->loaded = 1;
    return (int)transfer_size;
}
/* 1 committed, 2 conflict, 0 failed. A version is durable before HEAD moves.
 * flock is released by the OS on crash. No stale lock directories or truncation.
 * Orphan snapshots after a crash are harmless and replaced under the lock. */
int zero_learning_commit(unsigned int revision) {
    LearningStore *s = &stores[selected];
    if (!s->loaded || s->dir < 0 || !transfer_valid || !transfer_size || !revision) return 0;
    int lock = openat(s->dir, "lock", O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK, 0600);
    if (!regular(lock) || flock(lock, LOCK_EX | LOCK_NB)) { if (lock >= 0) close(lock); return 0; }
    unsigned char *current = malloc(LEARNING_CAP + 1);
    size_t size = 0;
    int result = 0;
    if (!current || !read_graph(s->dir, current, &size)) goto done;
    if (size != s->size || memcmp(current, s->before, size)) { result = 2; goto done; }
    int versions = private_dir(s->dir, "versions");
    if (versions < 0) goto done;
    char temp[96], version[64];
    snprintf(temp, sizeof(temp), "pending-%ld-%u", (long)getpid(), ++serial);
    snprintf(version, sizeof(version), "%u.json", revision);
    int fd = openat(s->dir, temp, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (fd < 0) { close(versions); goto done; }
    int ok = write_all(fd, transfer, transfer_size) && !fsync(fd);
    close(fd);
    if (ok) {
        /* Snapshot and current are separate inodes; no writable hard links. */
        char vtemp[96];
        snprintf(vtemp, sizeof(vtemp), "pending-%ld-%u", (long)getpid(), ++serial);
        fd = openat(versions, vtemp, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
        ok = fd >= 0 && write_all(fd, transfer, transfer_size) && !fsync(fd);
        if (fd >= 0) close(fd);
        if (ok) ok = !renameat(versions, vtemp, versions, version) && !fsync(versions);
        unlinkat(versions, vtemp, 0);
    }
    if (ok) ok = !renameat(s->dir, temp, s->dir, "graph.json");
    if (ok) { (void)fsync(s->dir); result = 1; s->loaded = 0; }
    unlinkat(s->dir, temp, 0);
    close(versions);
done:
    free(current); flock(lock, LOCK_UN); close(lock); return result;
}
int zero_learning_experience_open(void) {
    if (experience_fd >= 0 || stores[0].dir < 0) return 0;
    int dir = private_dir(stores[0].dir, "experiences");
    if (dir < 0) return 0;
    /* Random identity is unique across projects and PID reuse. Neither wall
     * clock adjustments nor concurrent sessions can alias triggering evidence. */
    for (unsigned attempt = 0; attempt < 1024; ++attempt) {
        unsigned char identity[16];
        int random = open("/dev/urandom", O_RDONLY | O_CLOEXEC);
        if (random < 0) break;
        ssize_t got;
        do { got = read(random, identity, sizeof(identity)); } while (got < 0 && errno == EINTR);
        close(random);
        if (got != sizeof(identity)) break;
        memcpy(experience_name, "e-", 2);
        for (unsigned i = 0; i < sizeof(identity); ++i)
            snprintf(experience_name + 2 + i * 2, 3, "%02x", identity[i]);
        memcpy(experience_name + 34, ".jsonl", 7);
        experience_fd = openat(dir, experience_name, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
        if (experience_fd >= 0 || errno != EEXIST) break;
    }
    close(dir);
    if (experience_fd < 0) return 0;
    zero_learning_reset();
    transfer_size = strlen(experience_name);
    memcpy(transfer, experience_name, transfer_size);
    return (int)transfer_size;
}
int zero_learning_experience_write(void) {
    return experience_fd >= 0 && transfer_valid && transfer_size && transfer[transfer_size - 1] == '\n'
        && write_all(experience_fd, transfer, transfer_size) && !fdatasync(experience_fd);
}
void zero_learning_experience_close(void) {
    if (experience_fd >= 0) close(experience_fd);
    experience_fd = -1;
}
void zero_learning_close(void) {
    zero_learning_experience_close();
    for (unsigned i = 0; i < 2; ++i) {
        if (stores[i].dir >= 0) close(stores[i].dir);
        stores[i].dir = -1; stores[i].loaded = 0;
    }
    zero_learning_reset();
}
