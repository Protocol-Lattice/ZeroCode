#include "exit.h"

/* Transport only: Zero owns SSE parsing, provider state and tool execution.
 * Private worker protocol: HTTP status LF, JSON|SSE LF, then response bytes.
 * Credentials travel on stdin, never in argv or a temporary file. */
#include <curl/curl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <stdint.h>
#include <limits.h>
#include <sys/types.h>
#include <sys/resource.h>
#include <unistd.h>

/* A nested HTTP worker belongs to its model worker's group, so cancelling the
 * model worker also reaches the transport. Root HTTP workers keep their group. */
static pid_t zero_expected_parent;
static int zero_join_attempted;

/* Shared with the application watchdog so it cannot cancel a valid request
 * before libcurl's deadline. The watchdog adds time for worker cleanup. */
int zero_http_timeout_seconds(void) {
    return 360;
}

/* The pinned Zero backend uses explicit fixed buffers on the stack. Reserve
 * enough per-process address space for two 1000 KB JSON-escaped edit fragments;
 * the HTTP-only worker does not enter the application or need this stack. */
int zero_prepare_stack(void) {
    struct rlimit limit;
    rlim_t needed = 64 * 1024 * 1024;
    if (getrlimit(RLIMIT_STACK, &limit)) return 0;
    if (limit.rlim_cur == RLIM_INFINITY || limit.rlim_cur >= needed) return 1;
    if (limit.rlim_max != RLIM_INFINITY && limit.rlim_max < needed) {
        /* Darwin reserves guard pages below its nominal 64 MiB hard limit. */
        if (limit.rlim_max < 63 * 1024 * 1024) return 0;
        needed = limit.rlim_max;
    }
    limit.rlim_cur = needed;
    return setrlimit(RLIMIT_STACK, &limit) == 0;
}

static int zero_stream_parent_alive(void) {
    return !zero_join_attempted || (zero_expected_parent > 1
        && getppid() == zero_expected_parent
        && getpgrp() == zero_expected_parent
        && getpgid(zero_expected_parent) == zero_expected_parent);
}

int zero_worker_pid(void) {
    return (int)getpid();
}

int zero_join_worker_group(unsigned int expected_parent) {
    zero_join_attempted = 1;
    zero_expected_parent = 0;
    if (expected_parent <= 1 || expected_parent > INT_MAX) return 0;
    pid_t parent = (pid_t)expected_parent;
    if (getppid() != parent || getpgid(parent) != parent) return 0;
    if (setpgid(0, parent) != 0) return 0;
    zero_expected_parent = parent;
    /* The parent can exit between the first identity check and setpgid. */
    return zero_stream_parent_alive();
}

static int zero_stream_progress(void *opaque, curl_off_t download_total,
        curl_off_t downloaded, curl_off_t upload_total, curl_off_t uploaded) {
    (void)opaque;
    (void)download_total;
    (void)downloaded;
    (void)upload_total;
    (void)uploaded;
    return !zero_stream_parent_alive();
}

typedef struct {
    long status;
    size_t received;
    int sse;
    int emitted;
} ZeroStreamTransport;

static int zero_stream_prefix(ZeroStreamTransport *state) {
    if (state->emitted) return 1;
    state->emitted = 1;
    return fprintf(stdout, "%ld\n%s\n", state->status, state->sse ? "SSE" : "JSON") > 0
        && fflush(stdout) == 0;
}

static size_t zero_stream_header(char *data, size_t size, size_t count, void *opaque) {
    if (size && count > SIZE_MAX / size) return 0;
    size_t length = size * count;
    ZeroStreamTransport *state = opaque;
    if (length >= 5 && !memcmp(data, "HTTP/", 5)) {
        char line[128];
        size_t copied = length < sizeof(line) - 1 ? length : sizeof(line) - 1;
        memcpy(line, data, copied);
        line[copied] = 0;
        char *space = strchr(line, ' ');
        if (!space) return 0;
        state->status = strtol(space + 1, NULL, 10);
        state->sse = 0;
    } else if (length > 13 && !strncasecmp(data, "Content-Type:", 13)) {
        const char *value = data + 13;
        const char *end = data + length;
        while (value < end && (*value == ' ' || *value == '\t')) value++;
        const char *type = "text/event-stream";
        size_t n = strlen(type);
        state->sse = (size_t)(end - value) >= n && !strncasecmp(value, type, n)
            && (value + n == end || value[n] == ';' || value[n] == '\r' || value[n] == '\n');
    }
    return length;
}

static size_t zero_stream_body(char *data, size_t size, size_t count, void *opaque) {
    if (size && count > SIZE_MAX / size) return 0;
    size_t length = size * count;
    ZeroStreamTransport *state = opaque;
    if (length > 64 * 1024 * 1024 - state->received) return 0;
    state->received += length;
    if (!zero_stream_prefix(state)) return 0;
    if (fwrite(data, 1, length, stdout) != length || fflush(stdout)) return 0;
    return length;
}

int zero_http_stream(unsigned int expected) {
    if (!zero_stream_parent_alive() || !expected || expected > 1310720) return 1;
    char *request = calloc((size_t)expected + 1, 1);
    if (!request) return 1;
    if (fread(request, 1, expected, stdin) != expected || memchr(request, 0, expected)) {
        free(request);
        return 1;
    }
    char *line = strchr(request, '\n');
    if (!line || strncmp(request, "POST ", 5)) { free(request); return 1; }
    *line = 0;
    if (line > request && line[-1] == '\r') line[-1] = 0;
    char *url = request + 5;
    if (strncmp(url, "https://", 8) && strncmp(url, "http://127.0.0.1:", 17)
        && strncmp(url, "http://localhost:", 17)) { free(request); return 1; }
    struct curl_slist *headers = NULL;
    char *cursor = line + 1;
    char *body = NULL;
    while (cursor < request + expected) {
        char *end = strchr(cursor, '\n');
        if (!end) break;
        *end = 0;
        if (end > cursor && end[-1] == '\r') end[-1] = 0;
        if (!*cursor) { body = end + 1; break; }
        if (!strchr(cursor, ':')) break;
        struct curl_slist *next = curl_slist_append(headers, cursor);
        if (!next) break;
        headers = next;
        cursor = end + 1;
    }
    if (!body || curl_global_init(CURL_GLOBAL_DEFAULT) != CURLE_OK) {
        curl_slist_free_all(headers);
        free(request);
        return 1;
    }
    CURL *curl = curl_easy_init();
    if (!curl) { curl_slist_free_all(headers); free(request); curl_global_cleanup(); return 1; }
    ZeroStreamTransport state = {0};
    CURLcode setup = CURLE_OK;
#define ZERO_CURL_SET(option, value) do { if (setup == CURLE_OK) setup = curl_easy_setopt(curl, option, value); } while (0)
    ZERO_CURL_SET(CURLOPT_URL, url);
    ZERO_CURL_SET(CURLOPT_POST, 1L);
    ZERO_CURL_SET(CURLOPT_POSTFIELDS, body);
    ZERO_CURL_SET(CURLOPT_POSTFIELDSIZE_LARGE, (curl_off_t)(request + expected - body));
    ZERO_CURL_SET(CURLOPT_HTTPHEADER, headers);
    ZERO_CURL_SET(CURLOPT_HEADERFUNCTION, zero_stream_header);
    ZERO_CURL_SET(CURLOPT_HEADERDATA, &state);
    ZERO_CURL_SET(CURLOPT_WRITEFUNCTION, zero_stream_body);
    ZERO_CURL_SET(CURLOPT_WRITEDATA, &state);
    ZERO_CURL_SET(CURLOPT_ACCEPT_ENCODING, "");
    ZERO_CURL_SET(CURLOPT_CONNECTTIMEOUT, 15L);
    ZERO_CURL_SET(CURLOPT_TIMEOUT, (long)zero_http_timeout_seconds());
    ZERO_CURL_SET(CURLOPT_NOSIGNAL, 1L);
    ZERO_CURL_SET(CURLOPT_FOLLOWLOCATION, 0L);
    ZERO_CURL_SET(CURLOPT_NOPROGRESS, 0L);
    ZERO_CURL_SET(CURLOPT_XFERINFOFUNCTION, zero_stream_progress);
#undef ZERO_CURL_SET
    CURLcode result = !zero_stream_parent_alive() ? CURLE_ABORTED_BY_CALLBACK
        : setup == CURLE_OK ? curl_easy_perform(curl) : setup;
    if (result == CURLE_OK && !zero_stream_prefix(&state)) result = CURLE_WRITE_ERROR;
    curl_easy_cleanup(curl);
    curl_slist_free_all(headers);
    memset(request, 0, expected);
    free(request);
    curl_global_cleanup();
    return result == CURLE_OK ? 0 : 1;
}
