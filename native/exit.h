#ifndef ZERO_CODING_EXIT_H
#define ZERO_CODING_EXIT_H

/* The hosted Zero entry point returns Void. Forward its explicit status to libc. */
void exit(int status);
int zero_worker_pid(void);
int zero_join_worker_group(unsigned int expected_parent);
int zero_http_stream(unsigned int expected);
int zero_http_timeout_seconds(void);
int zero_prepare_stack(void);
void zero_log_reset(void);
void zero_log_byte(unsigned int value);
int zero_log_open(void);
int zero_log_flush(void);
void zero_log_close(void);


#endif
