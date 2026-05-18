"""OpenWRT-specific tools for MCP server."""

import base64
import json
import logging
import re
from datetime import datetime
from typing import Any

from .ssh_client import ssh_client
from .security import SecurityValidator
from .config import settings

logger = logging.getLogger(__name__)


class OpenWRTTools:
    """Collection of OpenWRT management tools."""

    @staticmethod
    async def execute_command(command: str) -> dict[str, Any]:
        """
        Execute a validated command on the OpenWRT router.

        Args:
            command: Shell command to execute

        Returns:
            dict: Execution result
        """
        # Validate command
        is_valid, error_msg = SecurityValidator.validate_command(command)
        if not is_valid:
            return {
                "success": False,
                "error": error_msg,
                "output": "",
            }

        # Execute
        await ssh_client.ensure_connected()
        result = await ssh_client.execute(command)

        return {
            "success": result["success"],
            "output": result["stdout"],
            "error": result["stderr"],
            "exit_code": result["exit_code"],
            "execution_time": result["execution_time"],
        }

    @staticmethod
    async def get_system_info() -> dict[str, Any]:
        """
        Get OpenWRT system information (uptime, memory, load).

        Returns:
            dict: System information
        """
        try:
            await ssh_client.ensure_connected()

            # Execute multiple commands to gather system info
            commands = {
                "board": "ubus call system board",
                "info": "ubus call system info",
                "uptime": "cat /proc/uptime",
                "loadavg": "cat /proc/loadavg",
            }

            results = {}
            for key, cmd in commands.items():
                result = await ssh_client.execute(cmd)
                if result["success"]:
                    if key in ["board", "info"]:
                        # Parse JSON output from ubus
                        try:
                            results[key] = json.loads(result["stdout"])
                        except json.JSONDecodeError:
                            results[key] = result["stdout"]
                    else:
                        results[key] = result["stdout"]
                else:
                    results[key] = {"error": result["stderr"]}

            return {
                "success": True,
                "system_info": results,
            }

        except Exception as e:
            logger.error(f"Failed to get system info: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    @staticmethod
    async def restart_interface(interface: str) -> dict[str, Any]:
        """
        Restart a network interface.

        Args:
            interface: Interface name (e.g., 'wan', 'lan')

        Returns:
            dict: Operation result
        """
        command = f"ubus call network.interface.{interface} restart"

        # Validate interface name (alphanumeric and underscore only)
        if not interface.replace("_", "").isalnum():
            return {
                "success": False,
                "error": "Invalid interface name",
            }

        result = await OpenWRTTools.execute_command(command)

        if result["success"]:
            return {
                "success": True,
                "message": f"Interface '{interface}' restarted successfully",
                "output": result["output"],
            }
        else:
            return {
                "success": False,
                "error": f"Failed to restart interface '{interface}': {result['error']}",
            }

    @staticmethod
    async def get_wifi_status() -> dict[str, Any]:
        """
        Get WiFi status and connected clients.

        Returns:
            dict: WiFi status information
        """
        command = "ubus call network.wireless status"
        result = await OpenWRTTools.execute_command(command)

        if result["success"]:
            try:
                wifi_data = json.loads(result["output"])
                return {
                    "success": True,
                    "wifi_status": wifi_data,
                }
            except json.JSONDecodeError:
                return {
                    "success": True,
                    "wifi_status": result["output"],
                }
        else:
            return {
                "success": False,
                "error": result["error"],
            }

    @staticmethod
    async def list_dhcp_leases() -> dict[str, Any]:
        """
        List DHCP leases (connected devices).

        Returns:
            dict: DHCP leases information
        """
        # Try both possible locations for DHCP leases file
        commands = [
            "cat /tmp/dhcp.leases",
            "cat /var/dhcp.leases",
        ]

        for cmd in commands:
            result = await OpenWRTTools.execute_command(cmd)
            if result["success"] and result["output"]:
                # Parse DHCP leases
                leases = []
                for line in result["output"].strip().split("\n"):
                    if line:
                        parts = line.split()
                        if len(parts) >= 4:
                            leases.append(
                                {
                                    "timestamp": parts[0],
                                    "mac": parts[1],
                                    "ip": parts[2],
                                    "hostname": parts[3] if len(parts) > 3 else "",
                                    "client_id": parts[4] if len(parts) > 4 else "",
                                }
                            )

                return {
                    "success": True,
                    "leases": leases,
                    "count": len(leases),
                }

        return {
            "success": False,
            "error": "Could not read DHCP leases file",
        }

    @staticmethod
    async def get_firewall_rules() -> dict[str, Any]:
        """
        Get firewall rules.

        Returns:
            dict: Firewall rules
        """
        command = "iptables -L -n -v"
        result = await OpenWRTTools.execute_command(command)

        if result["success"]:
            return {
                "success": True,
                "rules": result["output"],
            }
        else:
            return {
                "success": False,
                "error": result["error"],
            }

    @staticmethod
    async def read_config(config_name: str) -> dict[str, Any]:
        """
        Read a UCI configuration file.

        Args:
            config_name: Configuration name (e.g., 'network', 'wireless', 'dhcp')

        Returns:
            dict: Configuration content
        """
        # Whitelist of allowed config names
        allowed_configs = ["network", "wireless", "dhcp", "firewall", "system"]

        if config_name not in allowed_configs:
            return {
                "success": False,
                "error": f"Configuration '{config_name}' not allowed. Allowed: {', '.join(allowed_configs)}",
            }

        command = f"uci show {config_name}"
        result = await OpenWRTTools.execute_command(command)

        if result["success"]:
            return {
                "success": True,
                "config_name": config_name,
                "config": result["output"],
            }
        else:
            return {
                "success": False,
                "error": result["error"],
            }

    @staticmethod
    async def read_file(path: str, max_lines: int = 100) -> dict[str, Any]:
        """
        Read a file from the OpenWRT router.

        Only files under whitelisted path prefixes (configured via
        READ_FILE_ALLOWED_PATHS in .env) can be read.

        Args:
            path: Absolute file path on the router (e.g. /var/log/messages)
            max_lines: Maximum number of lines to read (default: 100, max: 500)

        Returns:
            dict: File contents or error
        """
        # Validate max_lines
        if max_lines < 1:
            max_lines = 1
        elif max_lines > 500:
            max_lines = 500

        # Parse allowed prefixes from config (comma-separated)
        allowed_prefixes = [
            p.strip() for p in settings.read_file_allowed_paths.split(",") if p.strip()
        ]

        # Check path against whitelist
        allowed = False
        for prefix in allowed_prefixes:
            if path.startswith(prefix):
                allowed = True
                break

        if not allowed:
            logger.warning(f"Read-file denied (not in whitelist): {path}")
            return {
                "success": False,
                "error": (
                    f"Access denied: '{path}' is not in the allowed paths whitelist. "
                    f"Configure READ_FILE_ALLOWED_PATHS in .env to add it."
                ),
            }

        # Path traversal check
        if ".." in path.split("/"):
            return {
                "success": False,
                "error": "Path traversal detected ('..') — access denied.",
            }

        try:
            await ssh_client.ensure_connected()
            command = f"head -n {max_lines} '{path}'"
            result = await ssh_client.execute(command)

            if result["success"]:
                return {
                    "success": True,
                    "path": path,
                    "content": result["stdout"],
                    "lines_read": len(result["stdout"].split("\n")) if result["stdout"] else 0,
                }
            else:
                if "No such file" in result["stderr"] or "cannot open" in result["stderr"]:
                    return {
                        "success": False,
                        "error": f"File not found: {path}",
                    }
                return {
                    "success": False,
                    "error": f"Failed to read file: {result['stderr']}",
                }

        except Exception as e:
            logger.error(f"Failed to read file '{path}': {e}")
            return {
                "success": False,
                "error": str(e),
            }

    @staticmethod
    async def bootstrap() -> dict[str, Any]:
        """Run full router discovery and return a compiled snapshot.

        Collects system info, all UCI configs, key system files, package
        inventory, and network status. Partial failures are collected
        per-section so a broken config never blocks the whole bootstrap.

        Returns:
            dict: { "success": bool, "snapshot": { ... }, "errors": [...] }
        """
        snapshot: dict[str, Any] = {}
        errors: list[str] = []

        conn = await OpenWRTTools.test_connection()
        if not conn.get("connected", False):
            return {"success": False, "error": "Connection failed", "snapshot": None}
        snapshot["router"] = (
            f"{settings.openwrt_user}@{settings.openwrt_host}:{settings.openwrt_port}"
        )

        try:
            info = await OpenWRTTools.get_system_info()
            if info.get("success"):
                snapshot["system_info"] = info.get("system_info", {})
        except Exception as e:
            errors.append(f"system_info: {e}")

        for cfg in ("network", "wireless", "dhcp", "firewall", "system"):
            try:
                result = await OpenWRTTools.read_config(cfg)
                if result.get("success"):
                    snapshot[f"uci_{cfg}"] = result.get("config", "")
            except Exception as e:
                errors.append(f"uci_{cfg}: {e}")

        sys_files = (
            "/etc/banner",
            "/etc/openwrt_release",
            "/proc/cpuinfo",
            "/proc/meminfo",
            "/proc/loadavg",
            "/proc/uptime",
        )
        for path in sys_files:
            try:
                result = await OpenWRTTools.read_file(path, max_lines=30)
                if result.get("success"):
                    key = path.lstrip("/").replace("/", "_")
                    snapshot[f"file_{key}"] = result.get("content", "")
            except Exception as e:
                errors.append(f"read {path}: {e}")

        try:
            pkgs = await OpenWRTTools.opkg_list_installed()
            if pkgs.get("success"):
                snapshot["package_count"] = pkgs.get("count", 0)
                snapshot["packages"] = pkgs.get("packages", [])
        except Exception as e:
            errors.append(f"packages: {e}")

        try:
            ws = await OpenWRTTools.get_wifi_status()
            if ws.get("success"):
                snapshot["wifi_status"] = ws.get("wifi_status", {})
        except Exception as e:
            errors.append(f"wifi: {e}")

        try:
            dhcp = await OpenWRTTools.list_dhcp_leases()
            if dhcp.get("success"):
                snapshot["dhcp_leases"] = dhcp.get("leases", [])
                snapshot["dhcp_count"] = dhcp.get("count", 0)
        except Exception as e:
            errors.append(f"dhcp: {e}")

        try:
            fw = await OpenWRTTools.get_firewall_rules()
            if fw.get("success"):
                snapshot["firewall_rules"] = fw.get("rules", "")
        except Exception as e:
            errors.append(f"firewall: {e}")

        # Write snapshot cache to router for persistence across sessions
        try:
            md = OpenWRTTools._format_snapshot_markdown(snapshot, errors)
            await OpenWRTTools._write_router_text("/tmp/AGENTS.md", md)
        except Exception as e:
            errors.append(f"cache_write: {e}")

        return {
            "success": True,
            "snapshot": snapshot,
            "errors": errors,
            "error_count": len(errors),
        }

    @staticmethod
    def _format_snapshot_markdown(snapshot: dict[str, Any], errors: list[str] | None = None) -> str:
        """Compile a bootstrap snapshot into a rich AGENTS-style cached markdown."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines: list[str] = []
        lines.append("# Router Snapshot")
        lines.append(f"# Generated: {now}")
        lines.append("")

        router = snapshot.get("router", "?")
        lines.append(f"**Router:** {router}")
        lines.append("")

        # --- System Overview ---
        si = snapshot.get("system_info", {})
        board = si.get("board", {}) if isinstance(si, dict) else {}
        info = si.get("info", {}) if isinstance(si, dict) else {}
        model = "?"
        kernel = "?"
        if board:
            model = board.get("model", board.get("model_name", "?"))
            kernel = board.get("kernel", "?")
        mem_total = 0
        if info:
            mem = info.get("memory", {})
            mem_total = mem.get("total", 0) if isinstance(mem, dict) else 0

        uptime_raw = snapshot.get("file_proc_uptime", "0")
        uptime_str = "?"
        try:
            uptime_sec = float(uptime_raw.split()[0]) if uptime_raw else 0
            days = int(uptime_sec // 86400)
            hours = int((uptime_sec % 86400) // 3600)
            uptime_str = f"{days}d {hours}h"
        except (ValueError, IndexError):
            pass

        pkg_count = snapshot.get("package_count", 0)
        dhcp_count = snapshot.get("dhcp_count", 0)

        lines.append("## System Overview")
        lines.append(f"| Property | Value |")
        lines.append(f"|----------|-------|")
        lines.append(f"| Model | {model} |")
        lines.append(f"| Kernel | {kernel} |")
        lines.append(f"| Memory | {mem_total // 1024} MB" if mem_total else f"| Memory | ? |")
        lines.append(f"| Uptime | {uptime_str} |")
        lines.append(f"| DHCP Leases | {dhcp_count} active |")
        lines.append(f"| Installed Packages | {pkg_count} |")
        lines.append("")

        # --- Available MCP Tools ---
        lines.append("## Available MCP Tools")
        lines.append("")
        lines.append("### System & Network")
        lines.append("| Tool | Purpose | Live Status |")
        lines.append("|------|---------|-------------|")
        lines.append("| `openwrt_test_connection` | Verify SSH connectivity | Connected |")
        lines.append("| `openwrt_execute_command` | Run arbitrary shell commands | Available |")
        lines.append("| `openwrt_get_system_info` | Get board/CPU/memory info | Available |")
        lines.append("| `openwrt_restart_interface` | Restart network interfaces | Available |")
        lines.append("| `openwrt_get_wifi_status` | Query WiFi configuration | Available |")
        lines.append(f"| `openwrt_list_dhcp_leases` | List connected clients | {dhcp_count} active devices |")
        lines.append("| `openwrt_get_firewall_rules` | Inspect firewall rules | Available |")
        lines.append("| `openwrt_read_config` | Read UCI configuration files | Available |")
        lines.append("| `openwrt_read_file` | Read arbitrary router files | Available |")
        lines.append("")
        lines.append("### Package Management")
        lines.append("| Tool | Purpose | Live Status |")
        lines.append("|------|---------|-------------|")
        lines.append("| `openwrt_opkg_update` | Refresh package lists | Available |")
        lines.append("| `openwrt_opkg_install` | Install packages | Available |")
        lines.append("| `openwrt_opkg_remove` | Remove packages | Available |")
        lines.append(f"| `openwrt_opkg_list_installed` | List installed packages | {pkg_count} installed |")
        lines.append("| `openwrt_opkg_info` | Get package details | Available |")
        lines.append("| `openwrt_opkg_list_available` | Browse repo packages | Available |")
        lines.append("")
        lines.append("### OpenThread Border Router")
        lines.append("| Tool | Purpose | Live Status |")
        lines.append("|------|---------|-------------|")
        lines.append("| `openwrt_thread_get_state` | Check Thread status | Available |")
        lines.append("| `openwrt_thread_create_network` | Setup Thread network | Available |")
        lines.append("| `openwrt_thread_get_dataset` | Get Thread config | Available |")
        lines.append("| `openwrt_thread_get_info` | Thread border router info | Available |")
        lines.append("| `openwrt_thread_enable_commissioner` | Enable commissioning | Available |")
        lines.append("")

        # --- Network Interfaces (parsed from UCI) ---
        uci_net = snapshot.get("uci_network", "")
        if uci_net:
            lines.append("## Network Interfaces")
            lines.append("| Interface | Type | Status |")
            lines.append("|-----------|------|--------|")
            interfaces = []
            current_iface = None
            for line in uci_net.split("\n"):
                if "=interface" in line:
                    current_iface = line.split(".")[0].replace("network.", "")
                    iface_type = line.split("=", 1)[1].strip("'") if "=" in line else "?"
                    interfaces.append((current_iface, iface_type))
            for name, iftype in interfaces:
                lines.append(f"| `{name}` | {iftype} | — |")
            lines.append("")

        # --- DHCP Leases ---
        leases = snapshot.get("dhcp_leases", [])
        if leases:
            lines.append("## Active DHCP Clients")
            lines.append(f"**{dhcp_count} device(s) connected**")
            lines.append("")
            lines.append("| Hostname | IP Address | MAC Address |")
            lines.append("|----------|------------|-------------|")
            for lease in leases:
                hostname = lease.get("hostname", "?") or "?"
                ip = lease.get("ip", "?")
                mac = lease.get("mac", "?")
                lines.append(f"| {hostname} | {ip} | {mac} |")
            lines.append("")

        # --- WiFi Status ---
        ws = snapshot.get("wifi_status", {})
        if ws and isinstance(ws, dict):
            lines.append("## WiFi Status")
            radios = ws.get("radio", {}) if isinstance(ws, dict) else None
            if not radios:
                # ubus output might have a different structure
                for key in ws:
                    if isinstance(ws[key], dict) and "up" in ws[key]:
                        radios = {key: ws[key]}
                        break
            if isinstance(radios, dict):
                for radio_name, radio_data in radios.items():
                    up = radio_data.get("up", False)
                    status = "🟢 Enabled" if up else "🔴 Disabled"
                    channels = radio_data.get("channel", "?")
                    htmode = radio_data.get("htmode", "")
                    lines.append(f"- **{radio_name}**: {status} (channel {channels}{', ' + htmode if htmode else ''})")
                # Count associated clients
                total_sta = 0
                for radio_data in radios.values():
                    if isinstance(radio_data, dict):
                        total_sta += len(radio_data.get("assoc_stations", {}))
                lines.append(f"- **Connected stations**: {total_sta}")
            lines.append("")

        # --- Firewall Summary ---
        fw = snapshot.get("firewall_rules", "")
        if fw:
            lines.append("## Firewall Summary")
            # Count chains and rules
            chains = [l for l in fw.split("\n") if l.startswith("Chain ")]
            rule_lines = [l for l in fw.split("\n") if l.strip() and not l.startswith("Chain ") and not l.startswith("target")]
            lines.append(f"- **{len(chains)} chains**, **{len(rule_lines)} rules**")
            # Show first few policy lines
            for ch in chains[:6]:
                parts = ch.split()
                chain_name = parts[1] if len(parts) > 1 else "?"
                policy = parts[3] if len(parts) > 3 else "?"
                if chain_name in ("INPUT", "FORWARD", "OUTPUT"):
                    lines.append(f"- **{chain_name}** default policy: **{policy}**")
            lines.append("")

        # --- UCI Config Summary ---
        lines.append("## UCI Configuration Summary")
        lines.append("| Namespace | Sections | Key Settings |")
        lines.append("|-----------|----------|--------------|")
        for cfg in ("network", "wireless", "dhcp", "firewall", "system"):
            raw = snapshot.get(f"uci_{cfg}")
            if raw:
                stanzas = [l for l in raw.split("\n") if l.startswith("cfg") or l.startswith("package")]
                # Extract a few key option lines as preview
                key_options = []
                for kw in ("hostname", ".lan.", ".wan.", "encryption", "ssid"):
                    for l in raw.split("\n"):
                        if kw in l.lower():
                            key_options.append(l.strip())
                            break
                preview = "; ".join(key_options[:3]) if key_options else ""
                lines.append(f"| {cfg} | {len(stanzas)} | {preview} |")
            else:
                lines.append(f"| {cfg} | (not available) | |")
        lines.append("")

        # --- Errors / Warnings ---
        if errors:
            lines.append("## Bootstrap Warnings")
            for e in errors[:8]:
                lines.append(f"- ⚠️ {e}")
            if len(errors) > 8:
                lines.append(f"- … and {len(errors) - 8} more")
            lines.append("")

        lines.append("---")
        lines.append("*Snapshot refreshed after every tool call. Run `openwrt_bootstrap` to force refresh.*")
        return "\n".join(lines)

    @staticmethod
    async def _write_router_text(path: str, content: str) -> dict[str, Any]:
        """Write a text file to the router using base64 encoding.

        Args:
            path: Absolute path on the router (e.g. /tmp/AGENTS.md)
            content: Text content to write

        Returns:
            dict: Execution result
        """
        content_b64 = base64.b64encode(content.encode()).decode()
        command = f"echo '{content_b64}' | base64 -d > {path}"
        return await OpenWRTTools.execute_command(command)

    @staticmethod
    async def test_connection() -> dict[str, Any]:
        """
        Test SSH connection to the router.

        Returns:
            dict: Connection test result
        """
        return await ssh_client.test_connection()

    # ========== OpenThread Border Router (OTBR) Tools ==========

    @staticmethod
    async def thread_get_state() -> dict[str, Any]:
        """
        Get current OpenThread state.

        Returns:
            dict: Thread state (disabled, detached, child, router, leader)
        """
        command = "/usr/sbin/ot-ctl state"
        result = await OpenWRTTools.execute_command(command)

        if result["success"]:
            return {
                "success": True,
                "state": result["output"].strip(),
            }
        else:
            return {
                "success": False,
                "error": result["error"],
            }

    @staticmethod
    async def thread_create_network(
        network_name: str = "OpenWRT-Thread",
        channel: int = 15,
        panid: str = None,
    ) -> dict[str, Any]:
        """
        Create a new Thread network.

        Args:
            network_name: Network name (default: OpenWRT-Thread)
            channel: Thread channel 11-26 (default: 15)
            panid: PAN ID in hex format (auto-generated if not provided)

        Returns:
            dict: Operation result with network credentials
        """
        try:
            await ssh_client.ensure_connected()

            # Validate parameters
            if not network_name.replace("-", "").replace("_", "").isalnum():
                return {
                    "success": False,
                    "error": "Invalid network name. Use only alphanumeric, dash, and underscore.",
                }

            if not (11 <= channel <= 26):
                return {
                    "success": False,
                    "error": "Channel must be between 11 and 26",
                }

            # Generate random PAN ID if not provided
            if not panid:
                import secrets

                panid = f"0x{secrets.randbelow(0xFFFF):04x}"

            # Step 1: Initialize new dataset
            result = await ssh_client.execute("/usr/sbin/ot-ctl dataset init new")
            if not result["success"]:
                return {
                    "success": False,
                    "error": f"Failed to initialize dataset: {result['stderr']}",
                }

            # Step 2: Set network parameters
            commands = [
                f"/usr/sbin/ot-ctl channel {channel}",
                f"/usr/sbin/ot-ctl panid {panid}",
                f"/usr/sbin/ot-ctl networkname {network_name}",
            ]

            for cmd in commands:
                result = await ssh_client.execute(cmd)
                if not result["success"]:
                    return {
                        "success": False,
                        "error": f"Failed to execute '{cmd}': {result['stderr']}",
                    }

            # Step 3: Commit dataset
            result = await ssh_client.execute("/usr/sbin/ot-ctl dataset commit active")
            if not result["success"]:
                return {
                    "success": False,
                    "error": f"Failed to commit dataset: {result['stderr']}",
                }

            # Step 4: Bring up interface
            result = await ssh_client.execute("/usr/sbin/ot-ctl ifconfig up")
            if not result["success"]:
                return {
                    "success": False,
                    "error": f"Failed to bring up interface: {result['stderr']}",
                }

            # Step 5: Start Thread
            result = await ssh_client.execute("/usr/sbin/ot-ctl thread start")
            if not result["success"]:
                return {
                    "success": False,
                    "error": f"Failed to start Thread: {result['stderr']}",
                }

            # Step 6: Get network credentials
            import asyncio

            await asyncio.sleep(2)  # Wait for network to stabilize

            credentials = {}

            # Get network key
            result = await ssh_client.execute("/usr/sbin/ot-ctl networkkey")
            if result["success"]:
                credentials["network_key"] = result["stdout"].strip()

            # Get extended PAN ID
            result = await ssh_client.execute("/usr/sbin/ot-ctl extpanid")
            if result["success"]:
                credentials["ext_panid"] = result["stdout"].strip()

            # Get dataset in hex format
            result = await ssh_client.execute("/usr/sbin/ot-ctl dataset active -x")
            if result["success"]:
                credentials["dataset_hex"] = result["stdout"].strip()

            # Get current state
            result = await ssh_client.execute("/usr/sbin/ot-ctl state")
            if result["success"]:
                credentials["state"] = result["stdout"].strip()

            return {
                "success": True,
                "message": f"Thread network '{network_name}' created successfully",
                "network_name": network_name,
                "channel": channel,
                "panid": panid,
                "credentials": credentials,
            }

        except Exception as e:
            logger.error(f"Failed to create Thread network: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    @staticmethod
    async def thread_get_dataset() -> dict[str, Any]:
        """
        Get active Thread dataset (network credentials).

        Returns:
            dict: Active dataset information
        """
        command = "/usr/sbin/ot-ctl dataset active"
        result = await OpenWRTTools.execute_command(command)

        if result["success"]:
            # Also get hex format for easy sharing
            hex_result = await OpenWRTTools.execute_command("/usr/sbin/ot-ctl dataset active -x")

            return {
                "success": True,
                "dataset": result["output"],
                "dataset_hex": hex_result["output"].strip() if hex_result["success"] else None,
            }
        else:
            return {
                "success": False,
                "error": result["error"],
            }

    @staticmethod
    async def thread_get_info() -> dict[str, Any]:
        """
        Get comprehensive Thread network information.

        Returns:
            dict: Network state, neighbors, routes, etc.
        """
        try:
            await ssh_client.ensure_connected()

            info = {}

            # Get various Thread info
            commands = {
                "state": "/usr/sbin/ot-ctl state",
                "channel": "/usr/sbin/ot-ctl channel",
                "panid": "/usr/sbin/ot-ctl panid",
                "networkname": "/usr/sbin/ot-ctl networkname",
                "extpanid": "/usr/sbin/ot-ctl extpanid",
                "ipaddr": "/usr/sbin/ot-ctl ipaddr",
                "rloc16": "/usr/sbin/ot-ctl rloc16",
                "leaderdata": "/usr/sbin/ot-ctl leaderdata",
                "neighbor_table": "/usr/sbin/ot-ctl neighbor table",
                "child_table": "/usr/sbin/ot-ctl child table",
            }

            for key, cmd in commands.items():
                result = await ssh_client.execute(cmd)
                if result["success"]:
                    info[key] = result["stdout"].strip()
                else:
                    info[key] = None

            return {
                "success": True,
                "thread_info": info,
            }

        except Exception as e:
            logger.error(f"Failed to get Thread info: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    @staticmethod
    async def thread_enable_commissioner(passphrase: str = "THREAD123") -> dict[str, Any]:
        """
        Enable Thread Commissioner to allow devices to join.

        Args:
            passphrase: Joiner passphrase (default: THREAD123)

        Returns:
            dict: Operation result
        """
        try:
            await ssh_client.ensure_connected()

            # Start commissioner
            result = await ssh_client.execute("/usr/sbin/ot-ctl commissioner start")
            if not result["success"]:
                return {
                    "success": False,
                    "error": f"Failed to start commissioner: {result['stderr']}",
                }

            # Add joiner with wildcard (any device can join with this passphrase)
            result = await ssh_client.execute(
                f"/usr/sbin/ot-ctl commissioner joiner add * {passphrase}"
            )
            if not result["success"]:
                return {
                    "success": False,
                    "error": f"Failed to add joiner: {result['stderr']}",
                }

            return {
                "success": True,
                "message": "Thread Commissioner enabled",
                "passphrase": passphrase,
                "note": "Devices can now join using this passphrase",
            }

        except Exception as e:
            logger.error(f"Failed to enable commissioner: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    # ========== Package Management (opkg) Tools ==========

    @staticmethod
    async def opkg_update() -> dict[str, Any]:
        """
        Update package lists from repositories.

        Returns:
            dict: Operation result
        """
        command = "opkg update"
        result = await OpenWRTTools.execute_command(command)

        if result["success"]:
            return {
                "success": True,
                "message": "Package lists updated successfully",
                "output": result["output"],
            }
        else:
            return {
                "success": False,
                "error": f"Failed to update package lists: {result['error']}",
            }

    @staticmethod
    async def opkg_install(package_name: str) -> dict[str, Any]:
        """
        Install a package using opkg.

        Args:
            package_name: Name of the package to install

        Returns:
            dict: Operation result
        """
        # Validate package name (alphanumeric, dash, underscore, dot)
        if not re.match(r"^[a-zA-Z0-9._-]+$", package_name):
            return {
                "success": False,
                "error": "Invalid package name. Use only alphanumeric characters, dash, underscore, and dot.",
            }

        command = f"opkg install {package_name}"
        result = await OpenWRTTools.execute_command(command)

        if result["success"]:
            return {
                "success": True,
                "message": f"Package '{package_name}' installed successfully",
                "output": result["output"],
            }
        else:
            return {
                "success": False,
                "error": f"Failed to install package '{package_name}': {result['error']}",
                "output": result["output"],
            }

    @staticmethod
    async def opkg_remove(package_name: str) -> dict[str, Any]:
        """
        Remove a package using opkg.

        Args:
            package_name: Name of the package to remove

        Returns:
            dict: Operation result
        """
        # Validate package name
        if not re.match(r"^[a-zA-Z0-9._-]+$", package_name):
            return {
                "success": False,
                "error": "Invalid package name. Use only alphanumeric characters, dash, underscore, and dot.",
            }

        command = f"opkg remove {package_name}"
        result = await OpenWRTTools.execute_command(command)

        if result["success"]:
            return {
                "success": True,
                "message": f"Package '{package_name}' removed successfully",
                "output": result["output"],
            }
        else:
            return {
                "success": False,
                "error": f"Failed to remove package '{package_name}': {result['error']}",
                "output": result["output"],
            }

    @staticmethod
    async def opkg_list_installed() -> dict[str, Any]:
        """
        List all installed packages.

        Returns:
            dict: List of installed packages
        """
        command = "opkg list-installed"
        result = await OpenWRTTools.execute_command(command)

        if result["success"]:
            # Parse package list
            packages = []
            for line in result["output"].strip().split("\n"):
                if line:
                    parts = line.split(" - ")
                    if len(parts) >= 2:
                        packages.append(
                            {
                                "name": parts[0],
                                "version": parts[1],
                            }
                        )

            return {
                "success": True,
                "packages": packages,
                "count": len(packages),
            }
        else:
            return {
                "success": False,
                "error": result["error"],
            }

    @staticmethod
    async def opkg_info(package_name: str) -> dict[str, Any]:
        """
        Get information about a package.

        Args:
            package_name: Name of the package

        Returns:
            dict: Package information
        """
        # Validate package name
        if not re.match(r"^[a-zA-Z0-9._-]+$", package_name):
            return {
                "success": False,
                "error": "Invalid package name. Use only alphanumeric characters, dash, underscore, and dot.",
            }

        command = f"opkg info {package_name}"
        result = await OpenWRTTools.execute_command(command)

        if result["success"]:
            # Parse package info
            info = {}
            for line in result["output"].strip().split("\n"):
                if ": " in line:
                    key, value = line.split(": ", 1)
                    info[key.lower().replace(" ", "_")] = value

            return {
                "success": True,
                "package_info": info,
            }
        else:
            return {
                "success": False,
                "error": result["error"],
                "output": result["output"],
            }

    @staticmethod
    async def opkg_list_available() -> dict[str, Any]:
        """
        List all available packages from repositories.

        Returns:
            dict: List of available packages
        """
        command = "opkg list"
        result = await OpenWRTTools.execute_command(command)

        if result["success"]:
            # Parse package list (can be very large)
            packages = []
            lines = result["output"].strip().split("\n")

            for line in lines[:500]:  # Limit to first 500 packages to avoid huge responses
                if line:
                    parts = line.split(" - ")
                    if len(parts) >= 2:
                        packages.append(
                            {
                                "name": parts[0],
                                "version": parts[1],
                                "description": parts[2] if len(parts) > 2 else "",
                            }
                        )

            total_lines = len(result["output"].strip().split("\n"))
            truncated = total_lines > 500

            return {
                "success": True,
                "packages": packages,
                "count": len(packages),
                "truncated": truncated,
                "total_available": total_lines,
                "note": "List limited to 500 packages. Use opkg_info to search for specific packages."
                if truncated
                else "",
            }
        else:
            return {
                "success": False,
                "error": result["error"],
            }
