# Sandbox backup and restore

Stop the bot before running these commands. The operation is rejected if the
process lock cannot be acquired.

```sh
anima backup /secure/location/guild-1.zip --sandbox guild:1
anima restore /secure/location/guild-1.zip --sandbox guild:1
```

A backup stores all regular files in the selected sandbox (memory, inventory,
logs, processed-event markers, plugin state, and execution history) in a ZIP
archive with a SHA-256 manifest. It excludes fixed persona resources, voice
models, music audio, `.env`, and process-wide state.
The output must be outside the state root. Existing ZIP files are not
overwritten, and new archives are created with permissions `0600`.
Archives may contain secrets and must not be committed to Git. They are not
encrypted, so protect the storage location separately.

Restore only targets a sandbox directory that does not yet exist and has the
same sandbox ID as the archive. Existing directories are not automatically
deleted or moved. Path traversal, duplicate or unknown entries, hash mismatches,
and version or ID mismatches are rejected. All files are verified and staged in
a temporary directory before the restored directory is made available by rename.

Restoring also brings back old promises and unprocessed events; inspect the
contents before starting the bot. OpenAI Vector Stores and Discord posts are
not rolled back. Only restore trusted archives you created yourself: a limit on
the total extracted size has not yet been implemented.
