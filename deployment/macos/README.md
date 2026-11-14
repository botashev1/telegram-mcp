# macOS attachment deployment

The local runtime remains isolated under `_telegram_read`. Its root-owned launcher fixes `TG_DOWNLOAD_DIR` to `/Library/TelegramReadOnly/downloads`. The sandbox permits writes only within that directory; direct network access, writes elsewhere and reads under `/Users` remain denied. Calls without a download directory retain the original fully read-only filesystem profile, including owner setup. The model cannot choose a download directory through MCP.

The cache is owned by the service with mode `0700`. The designated local owner receives a read/traverse ACL; downloaded files inherit read access without write or execute access. Account credentials and the permitted-chat policy stay in their existing protected locations. Downloading does not grant permission to execute files.

## Apply the reviewed update

This updater is for an existing protected installation. Review the attachment commit and this script first. Stop the Telegram MCP server so no process under `_telegram_read` is running. From the repository root, run in a local terminal:

```sh
sudo ./deployment/macos/update-attachments.sh install YOUR_LOCAL_SHORT_NAME
```

Replace `YOUR_LOCAL_SHORT_NAME` with the ordinary macOS account that should read downloaded files. The script refuses an existing download directory or runtime files whose hashes differ from the reviewed baseline, snapshots an explicit list of source files, syntax-checks the snapshot, and backs up only the changed code in a root-only directory. It does not read or change credentials, chat policy, sudoers, Python dependencies or the owner's login flow. No Telegram login is required. Restart the MCP server after successful installation.

The updater prints a rollback label before changing the runtime. Keep it. If installation is interrupted after that line, use the same rollback procedure before restarting the service. This initial updater intentionally refuses to replace an existing downloads directory; later upgrades should preserve and review its permissions rather than resetting them.

The existing Codex registration may explicitly enable only the original five
tools. In its `[mcp_servers.telegram-readonly]` block, append
`"download_attachment"` to `enabled_tools` and set `tool_timeout_sec = 150` so
the service's 120-second download timeout can report an error and clean up.
Preserve the command, arguments, working directory and approval policy.
Restart/reload the MCP server after these changes. The updater does not edit
the user's Codex configuration.

## Roll back

Stop the MCP service and run the reviewed script with the label printed during installation:

```sh
sudo ./deployment/macos/update-attachments.sh rollback BACKUP_LABEL
```

Only the recorded code files are restored or removed. Downloads are retained for the owner to review and remove separately; the old runtime cannot create additional downloads. Credentials, policy and sudoers are unchanged. Restart MCP.

## Offline verification

Run the unit tests without credentials or network access:

```sh
PYTHONPATH=src:deployment/macos python -m pytest tests deployment/macos/test_telegram_egress.py
```

The profile tests verify the default write denial, the single explicit cache exception, rejection of symlink/noncanonical/group-writable directories, and immunity to a download-directory value in an inherited environment. A native macOS smoke test additionally verified that a synthetic file can be written in the cache and an adjacent write is denied. ACL inheritance was verified on a synthetic directory and file; no installed service files or account data were used for either check.
