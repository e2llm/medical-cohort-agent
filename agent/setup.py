#!/usr/bin/env python3
"""Set up Agent Builder agent, tools, and workflow via Kibana API."""

import argparse
import json
import os
import sys
from pathlib import Path

import requests

AGENT_ID = "medical-data-agent"

# Built-in platform tools to attach to the agent
TOOL_IDS = [
    "platform.core.execute_esql",
    "platform.core.generate_esql",
    "platform.core.list_indices",
    "platform.core.get_index_mapping",
    "platform.core.search",
]

HEADERS = {
    "kbn-xsrf": "true",
    "Content-Type": "application/json",
    "elastic-api-version": "2023-10-31",
}


def setup_connector(kibana_url: str, auth: tuple, openai_api_key: str) -> str:
    """Create or find an OpenAI connector. Returns connector ID."""
    resp = requests.get(
        f"{kibana_url}/api/actions/connectors",
        auth=auth,
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()

    for conn in resp.json():
        if conn.get("connector_type_id") == ".gen-ai" and "openai" in conn.get("name", "").lower():
            print(f"  Found existing OpenAI connector: {conn['id']}")
            return conn["id"]

    if not openai_api_key:
        print("Error: No existing OpenAI connector found and OPENAI_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    connector_body = {
        "name": "OpenAI (Hackathon)",
        "connector_type_id": ".gen-ai",
        "config": {
            "apiProvider": "OpenAI",
            "apiUrl": "https://api.openai.com/v1/chat/completions",
        },
        "secrets": {
            "apiKey": openai_api_key,
        },
    }

    resp = requests.post(
        f"{kibana_url}/api/actions/connector",
        auth=auth,
        headers=HEADERS,
        json=connector_body,
        timeout=30,
    )
    resp.raise_for_status()
    connector_id = resp.json()["id"]
    print(f"  Created OpenAI connector: {connector_id}")
    return connector_id


def import_workflow(kibana_url: str, auth: tuple) -> str | None:
    """Import the build_cohort workflow YAML into Kibana.

    Uses POST /api/workflows with the YAML content.
    Returns the workflow ID if successful, None otherwise.
    """
    workflow_path = Path(__file__).parent.parent / "workflow" / "build_cohort.yaml"
    if not workflow_path.exists():
        print(f"  Warning: {workflow_path} not found — skipping workflow import")
        return None

    yaml_content = workflow_path.read_text()

    # Workflow API requires x-elastic-internal-origin header
    workflow_headers = {**HEADERS, "x-elastic-internal-origin": "Kibana"}

    resp = requests.post(
        f"{kibana_url}/api/workflows",
        auth=auth,
        headers=workflow_headers,
        json={"yaml": yaml_content},
        timeout=60,
    )

    if resp.status_code in (200, 201):
        data = resp.json()
        workflow_id = data.get("id", "unknown")
        print(f"  Imported workflow: {data.get('name', 'Build Patient Cohort')} (id: {workflow_id})")
        return workflow_id

    print(f"  Warning: Could not import workflow via API (status {resp.status_code})")
    if resp.text:
        print(f"  Response: {resp.text[:200]}")
    print("  → Import manually: Kibana → Workflows → Create → paste build_cohort.yaml")
    return None


def setup_workflow_tool(kibana_url: str, auth: tuple, workflow_id: str | None) -> str | None:
    """Register the build_cohort workflow as an Agent Builder tool.

    Returns the tool ID if successful, None if the API isn't available.
    """
    # Check for existing workflow tool
    resp = requests.get(
        f"{kibana_url}/api/agent_builder/tools",
        auth=auth,
        headers=HEADERS,
        timeout=30,
    )
    if resp.status_code != 200:
        print("  Warning: Could not list tools — workflow tool registration skipped")
        return None

    tools = resp.json()
    for tool in tools if isinstance(tools, list) else tools.get("results", []):
        tool_id = tool.get("id", "")
        if tool_id == "build_cohort" or "cohort" in tool_id:
            print(f"  Found existing workflow tool: {tool_id}")
            return tool_id

    # Create the workflow tool (API uses id/description/type — no 'name' field)
    workflow_tool_body = {
        "id": "build_cohort",
        "description": (
            "Creates a normalized patient cohort index by searching across all 4 medical facilities. "
            "Takes structured criteria (conditions, age, gender, smoking, medications) and a search_text "
            "parameter for semantic kNN matching via E5 embeddings. Produces a cohort_<name> index with "
            "strict/probable confidence classification. The search_text should be the original research "
            "question in Hebrew — it powers the semantic pass that catches OCR artifacts and synonyms."
        ),
        "type": "workflow",
        "configuration": {"workflow_id": workflow_id} if workflow_id else {},
    }

    resp = requests.post(
        f"{kibana_url}/api/agent_builder/tools",
        auth=auth,
        headers=HEADERS,
        json=workflow_tool_body,
        timeout=30,
    )

    if resp.status_code in (200, 201):
        tool_id = resp.json().get("id", "build_cohort")
        print(f"  Created workflow tool: {tool_id}")
        return tool_id

    print(f"  Warning: Could not create workflow tool via API (status {resp.status_code})")
    print("  → Create manually: Kibana → Agent Builder → Tools → New → Workflow")
    return None


def setup_agent(kibana_url: str, auth: tuple, tool_ids: list[str]) -> str:
    """Create or update the Agent Builder agent. Returns agent ID."""
    config_path = Path(__file__).parent / "agent_config.json"
    with open(config_path) as f:
        agent_config = json.load(f)

    # Check if agent already exists
    resp = requests.get(
        f"{kibana_url}/api/agent_builder/agents",
        auth=auth,
        headers=HEADERS,
        timeout=30,
    )
    existing = False
    if resp.status_code == 200:
        for agent in resp.json().get("results", []):
            if agent.get("id") == AGENT_ID:
                existing = True
                break

    agent_body = {
        "name": agent_config["name"],
        "description": agent_config["description"],
        "configuration": {
            "instructions": agent_config["instructions"],
            "tools": [{"tool_ids": tool_ids}],
        },
    }

    if existing:
        resp = requests.put(
            f"{kibana_url}/api/agent_builder/agents/{AGENT_ID}",
            auth=auth,
            headers=HEADERS,
            json=agent_body,
            timeout=30,
        )
        resp.raise_for_status()
        print(f"  Updated agent: {AGENT_ID}")
    else:
        agent_body["id"] = AGENT_ID
        resp = requests.post(
            f"{kibana_url}/api/agent_builder/agents",
            auth=auth,
            headers=HEADERS,
            json=agent_body,
            timeout=30,
        )
        resp.raise_for_status()
        print(f"  Created agent: {AGENT_ID}")

    return AGENT_ID


def main():
    # Load .env before argparse so env vars are available for defaults
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description="Set up Agent Builder agent via Kibana API")
    parser.add_argument("--kibana-url", default=os.environ.get("KIBANA_URL", "http://localhost:5601"))
    parser.add_argument("--kibana-user", default=os.environ.get("KIBANA_USER", "elastic"))
    parser.add_argument("--kibana-password", default=os.environ.get("ELASTIC_PASSWORD", "changeme"))
    parser.add_argument("--openai-api-key", default=os.environ.get("OPENAI_API_KEY"))
    parser.add_argument("--skip-workflow", action="store_true", help="Skip workflow tool registration")
    args = parser.parse_args()

    auth = (args.kibana_user, args.kibana_password)

    print("Setting up OpenAI connector...")
    connector_id = setup_connector(args.kibana_url, auth, args.openai_api_key)
    print(f"  Connector ID: {connector_id}")

    # Collect tool IDs
    tool_ids = list(TOOL_IDS)

    if not args.skip_workflow:
        print("Importing workflow...")
        workflow_id = import_workflow(args.kibana_url, auth)

        print("Setting up workflow tool...")
        workflow_tool_id = setup_workflow_tool(args.kibana_url, auth, workflow_id)
        if workflow_tool_id:
            tool_ids.append(workflow_tool_id)
    else:
        print("Skipping workflow import (--skip-workflow)")
        # Still include existing build_cohort tool so the agent can use it
        tool_ids.append("build_cohort")

    print("Setting up agent...")
    agent_id = setup_agent(args.kibana_url, auth, tool_ids)

    print(f"\nDone. Agent '{agent_id}' is ready.")
    print(f"Tools: {', '.join(tool_ids)}")
    print(f"Open {args.kibana_url} → Agent Builder → select '{agent_id}'")
    print("Select the 'OpenAI (Hackathon)' connector when prompted.")

    if args.skip_workflow:
        print("\nNote: Workflow import skipped. build_cohort tool still attached to agent.")


if __name__ == "__main__":
    main()
