---
name: vps-connect
description: Connect to Todd's VPS / servers through the Desktop Commander connector to run commands, check the trading bot, read logs or edit files on the server. Use whenever the user mentions the VPS, "the server", Desktop Commander, "connect to my server", or wants something done on the machine where the bot runs.
---

# Connecting to the VPS via Desktop Commander

Desktop Commander is a **claude.ai connector** (named `Remote_Desktop_Commander`),
not something installed in the cloud container. Its tools are named
`mcp__Remote_Desktop_Commander__*`.

## Steps

1. **Load the tools.** They are usually deferred. Run ToolSearch with
   `select:mcp__Remote_Desktop_Commander__list_devices` (add `start_process`,
   `read_process_output`, `read_file`, `list_directory`, `write_file`, `edit_block` as needed).
   If nothing is found, search `desktop commander`. The server may still be
   connecting at session start; ToolSearch waits for it.
2. **Find the device.** Call `list_devices`. Pick the one with `Status: Online`.
   Known devices:
   - `vps-9f90eb86` (ID `0ec8e944-cc4e-4b39-8e67-d6ee0b357094`), the main VPS
   - `ubuntu-s-1vcpu-1gb-nyc1` (ID `0b3f3c67-1cd9-434a-bd51-cbd48465c7e9`), older droplet, usually offline
   - `localhost` (ID `4316fbaf-5ba5-44ff-a686-91aa285c01d0`), usually offline
   IDs can change; trust `list_devices` over this list.
3. **Run commands.** Pass `deviceId` on every call when more than one device is
   online. Use `start_process` for shell commands (e.g. `docker ps`,
   `docker compose logs --tail 100`), then `read_process_output` for long output.
   Remote calls wait at most ~3s, so poll `read_process_output` for slow commands.
   Prefer absolute paths.

## If it doesn't work

- **No `Remote_Desktop_Commander` tools at all:** the connector isn't attached.
  Tell the user to enable it at https://claude.ai/customize/connectors and
  start a new session (connectors load at session start).
- **Needs authentication:** the user must re-authorize it in claude.ai connector settings.
  Never ask for tokens in chat.
- **Device offline:** the Desktop Commander app must be running on that machine.
  Devices are managed at https://mcp.desktopcommander.app/.

## Safety

The VPS runs the live trading bot. Confirm with the user before stopping,
restarting or redeploying containers, deleting files, or changing config/keys.
Read-only checks (status, logs, disk usage) are fine without asking.
