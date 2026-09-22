#define _GNU_SOURCE
#define _DARWIN_C_SOURCE
#include "exit.h"

/* Transport and durable set union only. Zero owns admission, failure classes,
 * tool guards and model context. No received text can become code or a command.
 * Every process serves the same protocol and can exchange with any other peer. */
#include <arpa/inet.h>
#include <curl/curl.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/file.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define ROW 103
#define MAX_LESSONS 4096
#define WIRE_CAP (ROW * MAX_LESSONS)
#define MAX_PEERS 16
static char transfer[8192], root[4096], name[65] = "local", bind_ip[64] = "127.0.0.1";
static char seeds[8192], token[257], origin[33], urls[MAX_PEERS][512];
static size_t transfer_size;
static int transfer_ok = 1, directory = -1, listener = -1, active, started_server, started_sync;
static unsigned port, url_count, failures, exchanges;
static time_t seen[MAX_PEERS];
static char lessons[WIRE_CAP];
static size_t lesson_size;
static pthread_t server_thread, sync_thread;
static pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;
static atomic_int stopping;
static atomic_uint dirty, attempted;

void zero_peer_reset(void) { transfer_size = 0; transfer_ok = 1; }
void zero_peer_byte(unsigned int byte) {
    if (byte > 255 || transfer_size >= sizeof(transfer) - 1) { transfer_ok = 0; return; }
    transfer[transfer_size++] = (char)byte;
}
int zero_peer_field(unsigned int key) {
    char *out = NULL; size_t cap = 0;
    if (active || !transfer_ok || memchr(transfer, 0, transfer_size)) return 0;
    if (key == 0) { out = root; cap = sizeof(root); }
    if (key == 1) { out = name; cap = sizeof(name); }
    if (key == 2) { out = bind_ip; cap = sizeof(bind_ip); }
    if (key == 3) { out = seeds; cap = sizeof(seeds); }
    if (!out || transfer_size >= cap) return 0;
    memcpy(out, transfer, transfer_size); out[transfer_size] = 0;
    return 1;
}
static int hex(const char *s, size_t n) {
    for (size_t i = 0; i < n; ++i)
        if (!((s[i] >= '0' && s[i] <= '9') || (s[i] >= 'a' && s[i] <= 'f'))) return 0;
    return 1;
}
static int valid_rows(const char *data, size_t n) {
    if (n > WIRE_CAP || n % ROW) return 0;
    for (size_t i = 0; i < n; i += ROW) {
        const char *p = data + i;
        if (memcmp(p, "Z1 ", 3) || !hex(p + 3, 32) || p[35] != ' '
                || p[36] < '1' || p[36] > '6' || p[37] != ' '
                || !hex(p + 38, 64) || p[102] != '\n') return 0;
    }
    return 1;
}
static int compare_row(const void *a, const void *b) { return memcmp(a, b, ROW); }
static int private_dir(int parent, const char *entry) {
    if (mkdirat(parent, entry, 0700) && errno != EEXIST) return -1;
    int fd = openat(parent, entry, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    struct stat st;
    if (fd < 0) return -1;
    if (fstat(fd, &st) || st.st_uid != getuid() || (st.st_mode & 0077)) { close(fd); return -1; }
    return fd;
}
static int regular(int fd, size_t cap) {
    struct stat st;
    return fd >= 0 && !fstat(fd, &st) && S_ISREG(st.st_mode) && st.st_uid == getuid()
        && st.st_nlink == 1 && !(st.st_mode & 0077) && st.st_size >= 0 && (size_t)st.st_size <= cap;
}
static int write_all(int fd, const char *p, size_t n) {
    while (n) {
        ssize_t k = write(fd, p, n);
        if (k < 0 && errno == EINTR) continue;
        if (k <= 0) return 0;
        p += k; n -= (size_t)k;
    }
    return 1;
}
static int read_all(int fd, char *p, size_t cap, size_t *size) {
    *size = 0;
    while (*size < cap) {
        ssize_t n = read(fd, p + *size, cap - *size);
        if (n < 0 && errno == EINTR) continue;
        if (n < 0) return 0;
        if (!n) return 1;
        *size += (size_t)n;
    }
    char extra;
    return read(fd, &extra, 1) == 0;
}
/* Called under the process mutex. A separate lock also serializes other local
 * peers. Persist first, publish in memory second; a failed write admits nothing. */
static int merge(const char *data, size_t size) {
    if (directory < 0 || !valid_rows(data, size)) return 0;
    int lock = openat(directory, "lock", O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK, 0600);
    if (!regular(lock, 0) || flock(lock, LOCK_EX)) { if (lock >= 0) close(lock); return 0; }
    char *all = malloc(2 * WIRE_CAP);
    size_t old_size = 0, total = 0;
    int ok = 0, fd = -1;
    if (!all) goto done;
    fd = openat(directory, "lessons.v1", O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK);
    if (fd >= 0) {
        if (!regular(fd, WIRE_CAP) || !read_all(fd, all, WIRE_CAP, &old_size) || !valid_rows(all, old_size)) goto done;
        close(fd); fd = -1;
    } else if (errno != ENOENT) goto done;
    memcpy(all + old_size, data, size);
    qsort(all, (old_size + size) / ROW, ROW, compare_row);
    for (size_t i = 0; i < old_size + size; i += ROW) {
        if (total && !memcmp(all + total - ROW, all + i, ROW)) continue;
        if (total >= WIRE_CAP) goto done;
        memmove(all + total, all + i, ROW); total += ROW;
    }
    if (total != old_size) {
        char temporary[80];
        snprintf(temporary, sizeof(temporary), ".pending-%ld", (long)getpid());
        /* A stale file from a crashed process is never followed or truncated. */
        fd = openat(directory, temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
        if (fd < 0) goto done;
        int saved = write_all(fd, all, total) && !fsync(fd);
        close(fd); fd = -1;
        if (saved) saved = !renameat(directory, temporary, directory, "lessons.v1") && !fsync(directory);
        if (!saved) { unlinkat(directory, temporary, 0); goto done; }
    }
    memcpy(lessons, all, total); lesson_size = total; ok = 1;
done:
    if (fd >= 0) close(fd);
    free(all); (void)flock(lock, LOCK_UN); close(lock);
    return ok;
}
static void note_failure(void) {
    pthread_mutex_lock(&mutex); if (failures < 1000000) ++failures; pthread_mutex_unlock(&mutex);
}
static int identity(int ephemeral) {
    char file[90], bytes[16]; size_t n = 0;
    snprintf(file, sizeof(file), "%s.id", name);
    int fd = ephemeral ? -1 : openat(directory, file, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK);
    if (fd >= 0) {
        int ok = regular(fd, 32) && read_all(fd, origin, 32, &n) && n == 32 && hex(origin, 32);
        close(fd); origin[32] = 0; return ok;
    }
    if (!ephemeral && errno != ENOENT) return 0;
    fd = open("/dev/urandom", O_RDONLY | O_CLOEXEC);
    if (fd < 0) return 0;
    ssize_t count = read(fd, bytes, sizeof(bytes)); close(fd);
    if (count != sizeof(bytes)) return 0;
    const char *digits = "0123456789abcdef";
    for (size_t i = 0; i < 16; ++i) {
        origin[2*i] = digits[(unsigned char)bytes[i] >> 4];
        origin[2*i+1] = digits[(unsigned char)bytes[i] & 15];
    }
    origin[32] = 0;
    if (ephemeral) return 1;
    fd = openat(directory, file, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (fd < 0) return 0;
    int ok = write_all(fd, origin, 32) && !fsync(fd); close(fd);
    return ok && !fsync(directory);
}
static int authorized(const char *value) {
    const char *prefix = "Bearer ";
    if (strncmp(value, prefix, 7) || strlen(value + 7) != strlen(token)) return 0;
    unsigned diff = 0;
    for (size_t i = 0; i < strlen(token); ++i) diff |= (unsigned char)value[i+7] ^ (unsigned char)token[i];
    return diff == 0;
}
static int send_all(int fd, const char *p, size_t n) {
    while (n && !atomic_load(&stopping)) {
#ifdef MSG_NOSIGNAL
        ssize_t k = send(fd, p, n, MSG_NOSIGNAL);
#else
        ssize_t k = send(fd, p, n, 0);
#endif
        if (k < 0 && errno == EINTR) continue;
        if (k <= 0) return 0;
        p += k; n -= (size_t)k;
    }
    return n == 0;
}
static void reply(int fd, int status, const char *body, size_t size) {
    char header[256];
    int n = snprintf(header, sizeof(header), "HTTP/1.1 %d %s\r\nContent-Type: application/x-zero-lessons\r\nContent-Length: %zu\r\nConnection: close\r\n\r\n", status, status == 200 ? "OK" : "Rejected", size);
    if (n > 0 && send_all(fd, header, (size_t)n)) (void)send_all(fd, body, size);
}
static void serve(int fd) {
    char header[4096]; size_t used = 0;
    /* Read headers without consuming the body. Bound idle/slow clients. */
    time_t deadline = time(NULL) + 3;
    while (used + 1 < sizeof(header) && time(NULL) <= deadline && !atomic_load(&stopping)) {
        if (recv(fd, header + used, 1, 0) != 1) return;
        ++used; header[used] = 0;
        if (used >= 4 && !memcmp(header + used - 4, "\r\n\r\n", 4)) break;
    }
    if (used < 4 || memcmp(header + used - 4, "\r\n\r\n", 4)) { reply(fd, 400, "", 0); return; }
    char *line_end = strstr(header, "\r\n");
    if (!line_end) return;
    *line_end = 0;
    int get = !strcmp(header, "GET /v1/lessons HTTP/1.1");
    int post = !strcmp(header, "POST /v1/lessons HTTP/1.1");
    if (!get && !post) { reply(fd, 404, "", 0); return; }
    size_t length = 0; int auth = 0, lengths = 0, bad = 0;
    char *line = line_end + 2;
    while (*line) {
        char *end = strstr(line, "\r\n"); if (!end) { bad = 1; break; } *end = 0;
        if (!strncasecmp(line, "Authorization:", 14)) {
            const char *v = line + 14; while (*v == ' ') ++v;
            if (auth || !authorized(v)) bad = 1; else auth = 1;
        } else if (!strncasecmp(line, "Content-Length:", 15)) {
            char *v = line + 15; while (*v == ' ') ++v;
            if (++lengths > 1 || !*v) bad = 1;
            for (; *v; ++v) {
                if (*v < '0' || *v > '9' || length > WIRE_CAP) { bad = 1; break; }
                length = length * 10 + (size_t)(*v - '0');
            }
        } else if (!strncasecmp(line, "Transfer-Encoding:", 18)) bad = 1;
        line = end + 2;
    }
    if (!auth) { reply(fd, 401, "", 0); return; }
    if (bad || length > WIRE_CAP || (post && !lengths) || (get && length)) { reply(fd, 400, "", 0); return; }
    char *data = malloc(WIRE_CAP + 1); if (!data) { reply(fd, 503, "", 0); return; }
    used = 0;
    while (used < length && time(NULL) <= deadline && !atomic_load(&stopping)) {
        ssize_t n = recv(fd, data + used, length - used, 0);
        if (n <= 0) break;
        used += (size_t)n;
    }
    if (used != length || !valid_rows(data, length)) { reply(fd, 400, "", 0); free(data); return; }
    pthread_mutex_lock(&mutex);
    int ok = merge(data, length);
    size_t size = lesson_size;
    if (ok) { memcpy(data, lessons, size); ++exchanges; } else ++failures;
    pthread_mutex_unlock(&mutex);
    reply(fd, ok ? 200 : 503, data, ok ? size : 0); free(data);
}
static void *server_main(void *unused) {
    (void)unused;
    while (!atomic_load(&stopping)) {
        struct pollfd pfd = {.fd = listener, .events = POLLIN};
        if (poll(&pfd, 1, 200) <= 0) continue;
        int fd = accept(listener, NULL, NULL); if (fd < 0) continue;
        (void)fcntl(fd, F_SETFD, FD_CLOEXEC);
        struct timeval timeout = {.tv_sec = 1};
        (void)setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
        (void)setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
#ifdef SO_NOSIGPIPE
        int one = 1; (void)setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof(one));
#endif
        serve(fd); close(fd);
    }
    return NULL;
}
typedef struct { char *bytes; size_t size; } Response;
static size_t receive_body(char *data, size_t width, size_t count, void *opaque) {
    Response *r = opaque;
    if (width && count > (WIRE_CAP - r->size) / width) return 0;
    size_t n = width * count;
    memcpy(r->bytes + r->size, data, n); r->size += n; return n;
}
static int progress(void *unused, curl_off_t a, curl_off_t b, curl_off_t c, curl_off_t d) {
    (void)unused; (void)a; (void)b; (void)c; (void)d; return atomic_load(&stopping);
}
static void exchange(unsigned index) {
    char *sent = malloc(WIRE_CAP), *received = malloc(WIRE_CAP);
    CURL *curl = curl_easy_init();
    if (!sent || !received || !curl) { free(sent); free(received); if (curl) curl_easy_cleanup(curl); note_failure(); return; }
    pthread_mutex_lock(&mutex);
    int loaded = merge("", 0);
    size_t size = lesson_size; memcpy(sent, lessons, size);
    pthread_mutex_unlock(&mutex);
    char auth[300]; snprintf(auth, sizeof(auth), "Authorization: Bearer %s", token);
    struct curl_slist *headers = curl_slist_append(NULL, auth);
    headers = curl_slist_append(headers, "Content-Type: application/x-zero-lessons");
    headers = curl_slist_append(headers, "Expect:");
    Response response = {.bytes = received};
    curl_easy_setopt(curl, CURLOPT_URL, urls[index]);
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, sent);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, (long)size);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, receive_body);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, 750L);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, 2000L);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 0L);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(curl, CURLOPT_NOPROGRESS, 0L);
    curl_easy_setopt(curl, CURLOPT_XFERINFOFUNCTION, progress);
    CURLcode result = loaded ? curl_easy_perform(curl) : CURLE_READ_ERROR;
    long status = 0; curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);
    pthread_mutex_lock(&mutex);
    if (result == CURLE_OK && status == 200 && merge(received, response.size)) { seen[index] = time(NULL); ++exchanges; }
    else if (!atomic_load(&stopping)) ++failures;
    pthread_mutex_unlock(&mutex);
    curl_slist_free_all(headers); curl_easy_cleanup(curl); free(sent); free(received);
}
static void *sync_main(void *unused) {
    (void)unused;
    while (!atomic_load(&stopping)) {
        unsigned revision = atomic_load(&dirty);
        for (unsigned i = 0; i < url_count && !atomic_load(&stopping); ++i) exchange(i);
        atomic_store(&attempted, revision);
        for (int i = 0; i < 20 && !atomic_load(&stopping); ++i) {
            if (atomic_load(&dirty) != revision) break;
            struct timespec delay = {.tv_nsec = 100000000}; nanosleep(&delay, NULL);
        }
    }
    return NULL;
}
static int parse_urls(void) {
    char copy[sizeof(seeds)]; memcpy(copy, seeds, sizeof(copy));
    char *save = NULL;
    for (char *p = strtok_r(copy, ",", &save); p; p = strtok_r(NULL, ",", &save)) {
        if (url_count == MAX_PEERS || strlen(p) > 470) return 0;
        CURLU *url = curl_url(); if (!url) return 0;
        char *scheme = NULL, *host = NULL, *path = NULL, *user = NULL, *query = NULL, *fragment = NULL;
        int ok = !curl_url_set(url, CURLUPART_URL, p, 0)
            && !curl_url_get(url, CURLUPART_SCHEME, &scheme, 0)
            && !curl_url_get(url, CURLUPART_HOST, &host, 0)
            && !curl_url_get(url, CURLUPART_PATH, &path, 0);
        if (ok) ok = (!strcmp(scheme, "https") || (!strcmp(scheme, "http")
            && (!strcmp(host, "127.0.0.1") || !strcmp(host, "localhost") || !strcmp(host, "[::1]")))) && !strcmp(path, "/");
        if (!curl_url_get(url, CURLUPART_USER, &user, 0) || !curl_url_get(url, CURLUPART_QUERY, &query, 0)
                || !curl_url_get(url, CURLUPART_FRAGMENT, &fragment, 0)) ok = 0;
        curl_free(scheme); curl_free(host); curl_free(path); curl_free(user); curl_free(query); curl_free(fragment); curl_url_cleanup(url);
        if (!ok) return 0;
        size_t n = strlen(p); while (n && p[n-1] == '/') --n;
        snprintf(urls[url_count++], sizeof(urls[0]), "%.*s/v1/lessons", (int)n, p);
    }
    return 1;
}
int zero_peer_start(unsigned int requested_port, unsigned int ephemeral) {
    if (active || requested_port > 65535 || !root[0] || !name[0]) return 0;
    for (size_t i = 0; name[i]; ++i)
        if (!((name[i] >= 'a' && name[i] <= 'z') || (name[i] >= 'A' && name[i] <= 'Z')
                || (name[i] >= '0' && name[i] <= '9') || name[i] == '-' || name[i] == '_')) return 0;
    const char *key = getenv("ZERO_PEER_TOKEN");
    if (!key || strlen(key) < 32 || strlen(key) > 256) return 0;
    for (size_t i = 0; key[i]; ++i) if ((unsigned char)key[i] < 33 || (unsigned char)key[i] > 126) return 0;
    strcpy(token, key);
    if (curl_global_init(CURL_GLOBAL_DEFAULT) != CURLE_OK) return 0;
    url_count = 0; if (!parse_urls()) goto fail;
    int fd = open(root, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) goto fail;
    int state = private_dir(fd, ".zero-agent"); close(fd);
    if (state < 0) goto fail;
    directory = private_dir(state, "peers"); close(state);
    if (directory < 0 || !identity(ephemeral != 0) || !merge("", 0)) goto fail;
    listener = socket(AF_INET, SOCK_STREAM, 0);
    if (listener < 0) goto fail;
    (void)fcntl(listener, F_SETFD, FD_CLOEXEC);
    int one = 1; (void)setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in address = {.sin_family = AF_INET, .sin_port = htons((unsigned short)requested_port)};
    if (inet_pton(AF_INET, bind_ip, &address.sin_addr) != 1 || bind(listener, (struct sockaddr *)&address, sizeof(address)) || listen(listener, 16)) goto fail;
    socklen_t length = sizeof(address);
    if (getsockname(listener, (struct sockaddr *)&address, &length)) goto fail;
    port = ntohs(address.sin_port); atomic_store(&stopping, 0); active = 1;
    if (pthread_create(&server_thread, NULL, server_main, NULL)) goto fail;
    started_server = 1;
    if (pthread_create(&sync_thread, NULL, sync_main, NULL)) goto fail;
    started_sync = 1; return 1;
fail:
    zero_peer_stop(); return 0;
}
void zero_peer_stop(void) {
    /* Give a short-lived headless peer a bounded final gossip opportunity.
     * Offline neighbors recover the durable set on the next connection. */
    if (active && started_sync && url_count) {
        for (int i = 0; i < 125 && atomic_load(&attempted) < atomic_load(&dirty); ++i) {
            struct timespec delay = {.tv_nsec = 20000000}; nanosleep(&delay, NULL);
        }
    }
    atomic_store(&stopping, 1);
    if (started_server) pthread_join(server_thread, NULL);
    if (started_sync) pthread_join(sync_thread, NULL);
    started_server = started_sync = 0;
    if (listener >= 0) close(listener);
    if (directory >= 0) close(directory);
    listener = directory = -1; active = 0; port = 0;
    memset(token, 0, sizeof(token));
}
unsigned int zero_peer_status(unsigned int field) {
    pthread_mutex_lock(&mutex);
    unsigned result = 0;
    if (field == 0) result = active;
    if (field == 1) result = (unsigned)(lesson_size / ROW);
    if (field == 2) result = url_count;
    if (field == 3) for (unsigned i = 0; i < url_count; ++i) if (seen[i] && time(NULL) - seen[i] < 45) ++result;
    if (field == 4) result = port;
    if (field == 5) result = failures;
    if (field == 6) result = exchanges;
    pthread_mutex_unlock(&mutex); return result;
}
unsigned int zero_peer_origin(unsigned int index) { return index < 32 ? (unsigned char)origin[index] : 0; }
int zero_peer_has(unsigned int rule) {
    int result = 0; pthread_mutex_lock(&mutex);
    if (active) for (size_t i = 0; i < lesson_size; i += ROW) if (lessons[i + 36] == (char)('0' + rule)) { result = 1; break; }
    pthread_mutex_unlock(&mutex); return result;
}
int zero_peer_record(unsigned int rule) {
    if (!active || rule < 1 || rule > 6 || !transfer_ok || transfer_size != 64 || !hex(transfer, 64)) return 0;
    char row[ROW + 1]; snprintf(row, sizeof(row), "Z1 %s %u %.*s\n", origin, rule, 64, transfer);
    pthread_mutex_lock(&mutex); int ok = merge(row, ROW); if (!ok) ++failures; pthread_mutex_unlock(&mutex);
    if (ok) atomic_fetch_add(&dirty, 1);
    return ok;
}
