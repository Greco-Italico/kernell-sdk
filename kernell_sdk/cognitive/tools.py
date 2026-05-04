"""
Kernell OS SDK — Agentic Tools (The 'Antigravity / Claude Code' Capabilities)
═════════════════════════════════════════════════════════════════════════════
This module provides the core toolset that allows agents to interact with
the environment. By default, everything runs in a Docker sandbox, but
can escalate to host-level (like Antigravity) if permissions are granted.

Every tool invocation MUST pass through the IntentFirewall.
"""
from __future__ import annotations

import logging
import os
import subprocess
from typing import Callable, Dict, Any, List

from .intent_firewall import IntentFirewall, AgentIntent, ActionType, FirewallVerdict

logger = logging.getLogger("kernell.cognitive.tools")


class AgentTool:
    """Base class for any capability an agent can use."""
    name: str
    description: str
    action_type: ActionType
    
    def __init__(self, firewall: IntentFirewall):
        self.firewall = firewall

    def _request_permission(self, agent_id: str, target: str, payload: str = "") -> bool:
        """Runs the action through the immune system before execution."""
        intent = AgentIntent(
            agent_id=agent_id,
            action_type=self.action_type,
            target=target,
            payload=payload,
            context=f"Tool invocation: {self.name}"
        )
        decision = self.firewall.evaluate(intent)
        
        # If pending, we would normally wait for human interaction via WebSockets.
        # For simplicity in the sync execution flow, we block or fail.
        # In the full async runtime, this suspends the agent.
        if decision.verdict == FirewallVerdict.PENDING:
            logger.warning(f"Tool {self.name} blocked by Firewall (PENDING HUMAN APPROVAL).")
            return False
            
        return decision.verdict == FirewallVerdict.APPROVED

    def execute(self, agent_id: str, **kwargs) -> str:
        raise NotImplementedError


class BashExecutionTool(AgentTool):
    """
    Executes bash commands. (Inspired by Claude Code & Antigravity).
    Runs inside a container by default, or host if permitted.
    """
    name = "execute_bash"
    description = "Run a terminal command. Do not use interactive commands."
    action_type = ActionType.EXECUTE_COMMAND

    def execute(self, agent_id: str, command: str, cwd: str = ".") -> str:
        if not self._request_permission(agent_id, target=command):
            return "ERROR: Command blocked by Intent Firewall."

        import shlex
        try:
            argv = shlex.split(command, posix=True)
        except ValueError as e:
            return f"ERROR: Malformed command: {e}"
        if not argv:
            return "ERROR: Empty command."

        try:
            # SECURITY: shell=False is mandatory. Never use shell=True.
            result = subprocess.run(
                argv, shell=False, cwd=cwd, text=True,
                capture_output=True, timeout=120
            )
            out = result.stdout + "\n" + result.stderr
            return out.strip() or "Command executed successfully (no output)."
        except Exception as e:
            return f"ERROR executing command: {e}"


class FileEditorTool(AgentTool):
    """
    Advanced file editing. (Inspired by Antigravity's multi_replace_file_content).
    """
    name = "edit_file"
    description = "Replace specific blocks of text in a file."
    action_type = ActionType.WRITE_FILE

    def execute(self, agent_id: str, filepath: str, target_content: str, replacement_content: str) -> str:
        if not self._request_permission(agent_id, target=filepath, payload=replacement_content):
            return "ERROR: File write blocked by Intent Firewall."

        # SECURITY: Path containment — block traversal attacks
        resolved = os.path.realpath(filepath)
        if resolved.startswith(('/etc', '/root', '/proc', '/sys', '/var/run')):
            return f"ERROR: Access to {resolved} is forbidden."

        if not os.path.exists(filepath):
            return f"ERROR: File {filepath} does not exist."

        with open(filepath, "r") as f:
            content = f.read()

        if target_content not in content:
            return "ERROR: Target content not found in file. Ensure exact match including whitespace."

        new_content = content.replace(target_content, replacement_content)
        
        with open(filepath, "w") as f:
            f.write(new_content)

        return f"Successfully updated {filepath}."


class SemanticSearchTool(AgentTool):
    """
    Ripgrep/AST search for codebase navigation. (Inspired by Antigravity grep_search).
    """
    name = "semantic_search"
    description = "Search the codebase for specific patterns or code definitions."
    action_type = ActionType.READ_FILE

    def execute(self, agent_id: str, query: str, path: str = ".") -> str:
        if not self._request_permission(agent_id, target=f"{path}::{query}"):
            return "ERROR: Search blocked by Intent Firewall."

        try:
            # Fallback to grep if rg not installed
            result = subprocess.run(
                ["grep", "-rnI", query, path], text=True, capture_output=True
            )
            lines = result.stdout.splitlines()
            if not lines:
                return "No matches found."
            return "\n".join(lines[:50]) + ("\n...[truncated]" if len(lines) > 50 else "")
        except Exception as e:
            return f"ERROR during search: {e}"


class ToolRegistry:
    """Manages available tools for an agent."""
    def __init__(self, firewall: IntentFirewall):
        self.tools: Dict[str, AgentTool] = {
            "execute_bash": BashExecutionTool(firewall),
            "edit_file": FileEditorTool(firewall),
            "semantic_search": SemanticSearchTool(firewall),
        }

    def get_tool_schemas(self) -> List[Dict]:
        """Returns JSON schema definitions for LLM function calling."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": { "type": "object" } # Simplified for architecture demo
            }
            for t in self.tools.values()
        ]

    def invoke(self, tool_name: str, agent_id: str, kwargs: dict) -> str:
        if tool_name not in self.tools:
            return f"ERROR: Tool {tool_name} not found."
        return self.tools[tool_name].execute(agent_id, **kwargs)
