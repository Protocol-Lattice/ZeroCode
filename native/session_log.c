#include "exit.h"

/* The pinned direct backend cannot lower std.fs.appendBytes or pointer-valued
 * C parameters. This bounded scalar bridge owns only the append descriptor;
 * Zero owns journal creation, JSON encoding, redaction, and error reporting. */
#include <errno.h>
#include <fcntl.h>
#include <stddef.h>
#include <sys/stat.h>
#include <unistd.h>

static int log_fd = -1;
static unsigned char log_buffer[32768];
static size_t log_size;
static int log_valid = 1;

void zero_log_reset(void) {
    log_size = 0;
    log_valid = 1;
}

void zero_log_byte(unsigned int value) {
    if (value > 255 || log_size == sizeof(log_buffer)) {
        log_valid = 0;
        return;
    }
    log_buffer[log_size++] = (unsigned char)value;
}

int zero_log_open(void) {
    if (log_fd != -1 || !log_valid || !log_size || log_size >= sizeof(log_buffer)) return 0;
    for (size_t i = 0; i < log_size; i++) if (!log_buffer[i]) return 0;
    log_buffer[log_size] = 0;
    log_fd = open((const char *)log_buffer, O_WRONLY | O_APPEND | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK);
    if (log_fd < 0) return 0;
    struct stat status;
    if (fstat(log_fd, &status) != 0 || !S_ISREG(status.st_mode)
            || status.st_uid != getuid() || status.st_nlink != 1
            || (status.st_mode & 0777) != 0600) {
        close(log_fd);
        log_fd = -1;
        return 0;
    }
    zero_log_reset();
    return 1;
}

int zero_log_flush(void) {
    struct stat status;
    if (log_fd < 0 || !log_valid || !log_size || log_buffer[log_size - 1] != '\n'
            || fstat(log_fd, &status) != 0 || status.st_nlink != 1
            || (status.st_mode & 0777) != 0600) return 0;
    size_t offset = 0;
    while (offset < log_size) {
        ssize_t count = write(log_fd, log_buffer + offset, log_size - offset);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) return 0;
        offset += (size_t)count;
    }
    zero_log_reset();
    return 1;
}

void zero_log_close(void) {
    if (log_fd >= 0) close(log_fd);
    log_fd = -1;
    zero_log_reset();
}
