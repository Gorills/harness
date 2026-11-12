"""Separate global MCP catalog; no project admission or remote command capability."""

from __future__ import annotations

import json
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS, CallToolResult, InputRequiredResult
from mcp_types import Tool as MCPTool
from pydantic import StrictInt

from harness.ipc import request_servers
from harness.runtime_paths import default_runtime_paths

SERVER_MCP_PATH = "/servers/mcp"


class ServersMCPServer(MCPServer):
    async def list_tools(self) -> list[MCPTool]:
        tools = await super().list_tools()
        for tool in tools:
            tool.input_schema["additionalProperties"] = False
        return tools

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context[Any, Any] | None = None
    ) -> CallToolResult | InputRequiredResult:
        tools = {tool.name: tool for tool in await self.list_tools()}
        if name not in tools or set(arguments) - set(
            tools[name].input_schema.get("properties", {})
        ):
            raise MCPError(
                code=INVALID_PARAMS,
                message="Unknown server tool/fields; read the public schema and retry",
            )
        try:
            result = await super().call_tool(name, arguments, context)
        except ToolError as exc:
            raise MCPError(
                code=INVALID_PARAMS,
                message="Server request rejected; verify metadata, explicit identity and current revision",
            ) from exc
        if isinstance(result, CallToolResult):
            if result.is_error:
                # SDK input-validation exceptions may include the rejected value. Never forward it.
                raise MCPError(
                    code=INVALID_PARAMS,
                    message="Server request rejected; verify metadata, explicit identity and current revision",
                )
            result = result.model_copy(update={"content": []})
            if (
                len(
                    json.dumps(
                        result.model_dump(by_alias=True, exclude_none=True), ensure_ascii=True
                    ).encode()
                )
                > 52 * 1024
            ):
                raise MCPError(
                    code=INVALID_PARAMS,
                    message="Server response exceeds bounded contract; use smaller history/list pages",
                )
        return result


def build_server_mcp() -> MCPServer:
    server = ServersMCPServer(
        "Harness Servers",
        description="Shared server inventory for the current local OS user, independent of projects.",
        instructions="Discover server_list; choose an explicit server_id, fetch server_get. Multiple matches are ambiguous. Imported metadata is unverified until checked live. Classify project code/business diagnosis versus infrastructure before execution: SSH/Docker access alone does not authorize server history or fact writes. Mixed work keeps project details in its Task and actual infrastructure work in server history. Writes require server_id and expected_revision. No SSH execution or private vault access. Use server-operations skill. Restart Codex and open a fresh chat after global setup.",
        log_level="WARNING",
    )

    def call(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return request_servers(default_runtime_paths().socket, operation, arguments)

    @server.tool(
        description="Search current local-user inventory by server/site name, alias, address or domain. Multiple matches remain visible; never guess. Pagination is bounded."
    )
    def server_list(
        query: str = "",
        archived: bool = False,
        environment: str = "",
        limit: StrictInt = 20,
        offset: StrictInt = 0,
    ) -> dict[str, Any]:
        return call(
            "list",
            {
                "query": query,
                "archived": archived,
                "environment": environment,
                "limit": limit,
                "offset": offset,
            },
        )

    @server.tool(
        description="Fetch explicit server base metadata and bounded child/history pages. metadata.sites and platform_profiles are partial pages, never full replacement data. Filter sites_query by ID/name/purpose/domain; site_id or profile_id selects one exact record. sites_page/profiles_page provide total (filtered/selected), full_total, offset, truncated and next_offset. Traverse next_offset with expected_revision from the first response; conflict requires restarting traversal. Limit0 omits that collection, default sites5(max10), profiles4(max8). next_before pages history. For the linked map use collection(application/domain/route/directory/service/certificate/certificate_version/certificate_use/checks/schema; schema returns exact typed fields/enums/evidence/limits), object_id for details, query/status/application_id filters, sort(name/status/environment/size_bytes/not_after/checked_at), descending, offset and limit1..100. Map replies expose total/matched/rows/next_offset/complete; details have bounded links with links_total/links_next_offset. Traverse expected_revision. Responses max48KiB ASCII JSON; actual child page size may be reduced. No project context or vault disclosure."
    )
    def server_get(
        server_id: str,
        history_limit: StrictInt = 5,
        before: StrictInt | None = None,
        sites_query: str = "",
        sites_offset: StrictInt = 0,
        sites_limit: StrictInt = 5,
        site_id: str | None = None,
        profiles_offset: StrictInt = 0,
        profiles_limit: StrictInt = 4,
        profile_id: str | None = None,
        expected_revision: StrictInt | None = None,
        collection: str | None = None,
        object_id: str | None = None,
        query: str = "",
        status: str = "",
        application_id: str = "",
        sort: str = "name",
        descending: bool = False,
        offset: StrictInt = 0,
        limit: StrictInt = 50,
        links_offset: StrictInt = 0,
    ) -> dict[str, Any]:
        return call(
            "get",
            {
                "server_id": server_id,
                "history_limit": history_limit,
                "before": before,
                "sites_query": sites_query,
                "sites_offset": sites_offset,
                "sites_limit": sites_limit,
                "site_id": site_id,
                "profiles_offset": profiles_offset,
                "profiles_limit": profiles_limit,
                "profile_id": profile_id,
                "expected_revision": expected_revision,
                "collection": collection,
                "object_id": object_id,
                "query": query,
                "status": status,
                "application_id": application_id,
                "sort": sort,
                "descending": descending,
                "offset": offset,
                "limit": limit,
                "links_offset": links_offset,
            },
        )

    @server.tool(
        description="CAS write with server_id/expected_revision. Choose exactly ONE: map_operation or metadata base patch, complete site upsert, complete profile upsert, delete_site_id or delete_profile_id. Revision0 creates from metadata (name required; initial children optional within request budget). Existing metadata is a partial base patch: omitted fields preserved; sites/platform_profiles are REJECTED there. Child operations atomically preserve all neighbors/base/links; profile deletion fails while referenced. Base fields: name, aliases, role, environment, addresses, domains, services, paths, rules, project_ids, connection(ssh_alias/user/port/key_reference), passport, facts(text/source/checked_at;optional category), provenance(source/imported_at/checked_at). Passport is a whole-block replacement: hostname/provider/location/virtualization/os/kernel/cpu_model(text max256bytes), cpu_cores(integer1..65536 logical CPUs), memory_mib(integer1..2^40 total RAM), swap_mib(integer0..2^40;0 means none); omitted/null numbers unknown; storage(max16 text entries of256bytes; canonical device; capacity+units; type; mount, e.g. /dev/vda; 64424509440 bytes; virtual disk; mount=/), source(nonempty;default operator), checked_at(timezone ISO or null). Stable machine characteristics go in passport; transient observations in facts. Fact category: system/hardware/resources/services/access/network/limitations/notes; legacy omitted category allowed. Site: site_id(stable), profile_id(same-server profile or empty), name, purpose, access_policy, domains, environment, stack([{name,version};empty version unknown]), runtime, ports(integer1..65535), paths, dependencies, source(required), checked_at(timezone ISO or null). Profile: profile_id, name, environment, stack, domain_pattern, path_pattern, runtime, access_policy, steps(max12 concise strings), dependencies, source, checked_at. Other nested lists max16; each child6KiB ASCII JSON, base32KiB, base metadata512KiB/profiles32; normalized map quota10000 objects per server; public read48KiB/request64KiB. map_operation is typed: upsert(action,kind,object_id,data), delete(action,kind,object_id), link(action,link), unlink(action,link_id), check_start(action,check_id,coverage,source,started_at), check_append(action,check_id,batch,observations), check_publish(action,check_id,coverage,finished_at), check_fail(action,check_id,reason). Exact fields/enum/evidence schemas in docs/servers.md; never guess unknown fields. Object12KiB, lists64, evidence32. Links have explicit typed endpoints, role/source/checked_at/basis/reason; dependencies block deletion. Directory entry_type is unknown/directory/file/socket/symlink/other; consumer_checks lists only fully checked docker/systemd/cron/proxy/symlinks sources. HTTP probe_scope unknown/transport/endpoint/functional; endpoint/functional require non-secret probe_url and expected_result. Root-only API403/404 does not prove functional failure; retain transport result. Read understanding separates current technical consumption from protected operator usage. Checks preserve operator purpose/owner/usage/procedure and omitted observations; drafts do not change current state. Each check has up to400 portions of1..25 observations; repeats require identical content. Save results are paged: fetch the exact changed child. Save verified infrastructure context only; project code/SQL/business diagnostics stay in the project Task, not facts/sites. Metadata CRUD is not a remote operation. Profiles are guidance, never permission. No secrets, raw commands/env or private-key bytes. Project links grant no project permission."
    )
    def server_save(
        server_id: str,
        expected_revision: StrictInt,
        metadata: dict[str, Any] | None = None,
        site: dict[str, Any] | None = None,
        profile: dict[str, Any] | None = None,
        delete_site_id: str | None = None,
        delete_profile_id: str | None = None,
        map_operation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return call(
            "save",
            {
                "server_id": server_id,
                "expected_revision": expected_revision,
                "metadata": metadata,
                "site": site,
                "profile": profile,
                "delete_site_id": delete_site_id,
                "delete_profile_id": delete_profile_id,
                "map_operation": map_operation,
            },
        )

    @server.tool(
        description="Archive or restore an explicit server with CAS. Preserves card and audit history; no remote action."
    )
    def server_archive(
        server_id: str, expected_revision: StrictInt, archived: bool = True
    ) -> dict[str, Any]:
        return call(
            "archive",
            {"server_id": server_id, "expected_revision": expected_revision, "archived": archived},
        )

    @server.tool(
        description="Append SERVER INFRASTRUCTURE evidence with explicit server identity/revision. Classify scope before execution/journaling. operation: actual OS/config/proxy/cert/access/directories/deployment runtime/artifact/service/restart/DB-engine intervention; state what changed, when and verification. audit: explicitly requested primary infrastructure audit; state read-only/no changes if applicable. Project code/SQL/GraphQL/business-data diagnosis, application DML/schema migrations and local tests stay in the project Task even over SSH/Docker; no server event merely for connection/hostname/container checks. Mixed work records only its actual infrastructure portion here, never copies the project report. Registry metadata CRUD is not a remote operation. event: kind(audit/operation), summary, findings(list), checked_at(timezone ISO), source. Append-only history; preserve existing events. Records results only: no SSH, remote changes, monitoring or alerts. Exclude secrets/raw env/dumps."
    )
    def server_record(
        server_id: str, expected_revision: StrictInt, event: dict[str, Any]
    ) -> dict[str, Any]:
        return call(
            "record",
            {"server_id": server_id, "expected_revision": expected_revision, "event": event},
        )

    return server
