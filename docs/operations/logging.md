# Runtime Logging

`tools/scripts/lib/runtime_logging.sh` is the shared shell library for
repository-local operator logging.

Purpose:

- centralize stdout and stderr capture for shell entrypoints
- write timestamped log lines under `runtime/`
- emit structured runtime events through `runtime_log_event`
- keep log rotation and cleanup behavior consistent across callers

Expected usage:

- source `tools/scripts/lib/runtime_logging.sh` from another shell script
- call `runtime_log_init <repo_root> <tool_name> <language>` early
- call `runtime_log_install_exit_trap` so file descriptors, FIFOs, and temp
  directories are cleaned up on exit

Current storage behavior:

- root log path defaults to `runtime/log-output/index-actions.log`
- rotated archives default to `runtime/backups/logs/`
- rotation is size-based and controlled by `INDEX_LOG_ROTATE_MAX_BYTES` and
  `INDEX_LOG_ROTATE_KEEP`
- all output stays repository-local; this library does not publish, upload, or
  transmit logs

Run-origin behavior:

- if `INDEX_RUN_ORIGIN` is already set, that value is used
- otherwise the library inspects parent processes and classifies the run as
  `manual`, `cron`, or `scheduled`

Safety notes:

- this is a shell library, not an operator CLI
- it does not add network access
- it expects callers to provide a repository-local root path

Sync note:

- when changing log paths, rotation defaults, event field names, or cleanup
  behavior in `runtime_logging.sh`, update this paired document
