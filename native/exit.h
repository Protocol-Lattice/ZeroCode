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
void zero_learning_reset(void);
void zero_learning_byte(unsigned int value);
unsigned int zero_learning_at(unsigned int index);
int zero_learning_open(unsigned int scope);
int zero_learning_select(unsigned int scope);
int zero_learning_load(void);
int zero_learning_commit(unsigned int revision);
int zero_learning_experience_open(void);
int zero_learning_experience_write(void);
void zero_learning_experience_close(void);
void zero_learning_close(void);


#endif
