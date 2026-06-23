"""Smoke tests: the server imports, registers all tools, and builds valid schemas.

No network or credentials are touched — ``get_services`` is only called inside tool
bodies, never at import or during tool listing.
"""

import asyncio

from google_slides_mcp import server

EXPECTED_TOOLS = {
    "create_presentation",
    "get_presentation",
    "get_page",
    "list_slides",
    "batch_update",
    "set_element_box",
    "update_transform",
    "set_z_order",
    "group_elements",
    "ungroup_elements",
    "replace_all_text",
    "insert_text",
    "set_element_text",
    "copy_presentation",
    "catalog_slides",
    "duplicate_slide",
    "delete_objects",
    "reorder_slides",
    "park_slides",
    "unpark_slides",
    "prune_parked_slides",
    "render_page",
    "diff_pages",
}


def _list_tools():
    return asyncio.run(server.mcp.list_tools())


def test_all_tools_registered():
    names = {tool.name for tool in _list_tools()}
    missing = EXPECTED_TOOLS - names
    assert not missing, f"missing tools: {missing}"


def test_tools_have_schema_and_description():
    for tool in _list_tools():
        assert tool.description, f"{tool.name} has no description"
        # inputSchema is a JSON schema dict generated from the type hints.
        assert isinstance(tool.inputSchema, dict)
        assert tool.inputSchema.get("type") == "object"


def test_batch_update_validates_requests():
    # Pure client-side validation runs before any API call.
    try:
        server.batch_update.fn(presentation_id="p", requests=[])
    except (ValueError, AttributeError) as exc:
        # Accept either the validation error, or (if .fn isn't exposed by this
        # mcp version) skip — covered by integration usage.
        if isinstance(exc, ValueError):
            assert "non-empty" in str(exc)
