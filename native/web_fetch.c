#include "exit.h"

/* Read-only web transport. URL bytes arrive on stdin, never through a shell.
 * Private response: status LF, effective URL LF, content type LF,
 * truncated (0|1) LF, then at most 12000 decompressed body bytes.
 * Zero validates UTF-8 and builds the bounded JSON tool result. */
#include <curl/curl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <strings.h>

typedef struct {
    unsigned char body[12000];
    size_t length;
    size_t limit;
    size_t headers;
    int truncated;
} ZeroWebFetch;

static int web_error(const char *message) {
    printf("Error: web_fetch: %s\n", message);
    return 1;
}

static int web_line_safe(const char *text) {
    for (const unsigned char *p = (const unsigned char *)text; *p; p++) {
        if (*p < 32 || *p == 127) return 0;
    }
    return 1;
}

static int web_url_safe(const char *url) {
    const char *host;
    if (!strncasecmp(url, "https://", 8)) host = url + 8;
    else if (!strncasecmp(url, "http://", 7)) host = url + 7;
    else return 0;
    if (!*host || !web_line_safe(url) || strchr(url, ' ')) return 0;
    size_t authority = strcspn(host, "/?#");
    return authority > 0 && !memchr(host, '@', authority);
}

static size_t web_header(char *data, size_t size, size_t count, void *opaque) {
    if (size && count > SIZE_MAX / size) return 0;
    size_t length = size * count;
    ZeroWebFetch *state = opaque;
    if (length > 65536 - state->headers) return 0;
    state->headers += length;
    /* Discard intermediate redirect/proxy response bodies. */
    if (length >= 5 && !memcmp(data, "HTTP/", 5)) {
        state->length = 0;
        state->truncated = 0;
    }
    return length;
}

static size_t web_body(char *data, size_t size, size_t count, void *opaque) {
    if (size && count > SIZE_MAX / size) return 0;
    size_t length = size * count;
    ZeroWebFetch *state = opaque;
    size_t remaining = state->limit - state->length;
    size_t copied = length < remaining ? length : remaining;
    memcpy(state->body + state->length, data, copied);
    state->length += copied;
    if (copied < length) {
        state->truncated = 1;
        return 0; /* Intentional stop, including for decompression expansion. */
    }
    return length;
}

static int web_text_type(const char *type) {
    size_t n = strcspn(type, "; ");
    if (!n || !strncasecmp(type, "text/", 5)) return 1;
    const char *types[] = {"application/json", "application/xml",
        "application/javascript", "application/x-javascript",
        "application/x-www-form-urlencoded"};
    for (size_t i = 0; i < sizeof(types) / sizeof(types[0]); i++) {
        if (strlen(types[i]) == n && !strncasecmp(type, types[i], n)) return 1;
    }
    return n > 5 && !strncasecmp(type, "application/", 12)
        && (!strncasecmp(type + n - 5, "+json", 5)
            || !strncasecmp(type + n - 4, "+xml", 4));
}

int zero_web_fetch(unsigned int expected, unsigned int limit, unsigned int timeout) {
    if (!expected || expected > 2048 || limit < 4 || limit > 12000
        || timeout < 1 || timeout > 60) return web_error("invalid request limits.");
    char url[2049] = {0};
    if (fread(url, 1, expected, stdin) != expected || memchr(url, 0, expected)
        || !web_url_safe(url)) {
        return web_error("use an HTTP or HTTPS URL without credentials or whitespace.");
    }
    if (curl_global_init(CURL_GLOBAL_DEFAULT) != CURLE_OK) {
        return web_error("could not initialize libcurl.");
    }
    CURL *curl = curl_easy_init();
    if (!curl) {
        curl_global_cleanup();
        return web_error("could not create the HTTP request.");
    }
    ZeroWebFetch state = {.limit = limit};
    CURLcode setup = CURLE_OK;
#define WEB_SET(option, value) do { if (setup == CURLE_OK) setup = curl_easy_setopt(curl, option, value); } while (0)
    WEB_SET(CURLOPT_URL, url);
    WEB_SET(CURLOPT_HTTPGET, 1L);
    WEB_SET(CURLOPT_USERAGENT, "ZeroCode/0.1 web_fetch");
    WEB_SET(CURLOPT_DISALLOW_USERNAME_IN_URL, 1L);
    WEB_SET(CURLOPT_NETRC, (long)CURL_NETRC_IGNORED);
    WEB_SET(CURLOPT_SSL_VERIFYPEER, 1L);
    WEB_SET(CURLOPT_SSL_VERIFYHOST, 2L);
    WEB_SET(CURLOPT_FOLLOWLOCATION, 1L);
    WEB_SET(CURLOPT_MAXREDIRS, 5L);
#if LIBCURL_VERSION_NUM >= 0x075500
    WEB_SET(CURLOPT_PROTOCOLS_STR, "http,https");
    WEB_SET(CURLOPT_REDIR_PROTOCOLS_STR, "http,https");
#else
    WEB_SET(CURLOPT_PROTOCOLS, (long)(CURLPROTO_HTTP | CURLPROTO_HTTPS));
    WEB_SET(CURLOPT_REDIR_PROTOCOLS, (long)(CURLPROTO_HTTP | CURLPROTO_HTTPS));
#endif
    WEB_SET(CURLOPT_CONNECTTIMEOUT, 10L);
    WEB_SET(CURLOPT_TIMEOUT, (long)timeout);
    WEB_SET(CURLOPT_NOSIGNAL, 1L);
    WEB_SET(CURLOPT_ACCEPT_ENCODING, "");
    WEB_SET(CURLOPT_HEADERFUNCTION, web_header);
    WEB_SET(CURLOPT_HEADERDATA, &state);
    WEB_SET(CURLOPT_WRITEFUNCTION, web_body);
    WEB_SET(CURLOPT_WRITEDATA, &state);
#undef WEB_SET
    CURLcode result = setup == CURLE_OK ? curl_easy_perform(curl) : setup;
    long status = 0;
    char *effective = NULL;
    char *type = NULL;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);
    curl_easy_getinfo(curl, CURLINFO_EFFECTIVE_URL, &effective);
    curl_easy_getinfo(curl, CURLINFO_CONTENT_TYPE, &type);
    if (!type) type = "";
    int code = 1;
    if (result != CURLE_OK && !(result == CURLE_WRITE_ERROR && state.truncated)) {
        web_error(curl_easy_strerror(result));
    } else if (status < 200 || status >= 300) {
        printf("Error: web_fetch: HTTP %ld.\n", status);
    } else if (!effective || strlen(effective) > 2048 || !web_url_safe(effective)
        || strlen(type) > 256 || !web_line_safe(type)) {
        web_error("response URL or content type is invalid or too large.");
    } else if (!web_text_type(type)) {
        web_error("unsupported content type; fetch a text, HTML, JSON or XML resource.");
    } else if (printf("%ld\n%s\n%s\n%d\n", status, effective, type, state.truncated) > 0
        && fwrite(state.body, 1, state.length, stdout) == state.length && !fflush(stdout)) {
        code = 0;
    }
    curl_easy_cleanup(curl);
    curl_global_cleanup();
    return code;
}
