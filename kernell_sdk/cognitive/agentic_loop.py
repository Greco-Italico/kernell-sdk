"""
Kernell OS SDK — Agentic Runtime Loop (The ReAct Core)
══════════════════════════════════════════════════════
This module defines the actual execution loop for an individual task.
It takes inspiration from Antigravity and OpenCode:
It loops between the LLM and the ToolRegistry, restricted by the Firewall.
"""
from __future__ import annotations

import logging
import json
from typing import Dict, Any

from .task import Task, TaskStatus
from .agent_role import CognitiveAgent
from .cognitive_router import RouterDecision
from .tools import ToolRegistry
from .intent_firewall import IntentFirewall

logger = logging.getLogger("kernell.cognitive.runtime")


class AgenticLoop:
    """
    The runtime execution loop for a single agent working on a single task.
    Implements a Tool-Use (ReAct / Function Calling) loop.
    """
    def __init__(self, firewall: IntentFirewall, max_iterations: int = 15):
        self.firewall = firewall
        self.tools = ToolRegistry(firewall)
        self.max_iterations = max_iterations

    async def run(
        self, 
        task: Task, 
        agent: CognitiveAgent, 
        decision: RouterDecision, 
        llm_client: Any  # E.g., the BaseLLMProvider returned by the Router
    ) -> str:
        """
        Executes the task using the chosen model.
        Loops until the LLM returns a final answer or max iterations reached.
        """
        logger.info(f"AgenticLoop starting for Task {task.task_id} on Model {decision.selected_model}")
        
        system_prompt = (
            f"You are an autonomous agent with role: {agent.role.value}.\n"
            f"Your task is: {task.description}\n"
            f"You have access to tools to modify the environment.\n"
            f"Use them to achieve the goal, then output the final result."
        )
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Begin task execution."}
        ]

        iterations = 0
        while iterations < self.max_iterations:
            iterations += 1
            
            # 1. Call LLM (Pseudo-code for the BaseLLMProvider)
            # In real implementation, this passes self.tools.get_tool_schemas()
            try:
                response = await llm_client.complete_async(
                    messages=messages, 
                    tools=self.tools.get_tool_schemas()
                )
            except Exception as e:
                logger.error(f"LLM failure on task {task.task_id}: {e}")
                raise Exception(f"Model failure: {e}")

            # Record token usage to update costs
            task.prompt_tokens_used += getattr(response, 'prompt_tokens', 0)
            task.completion_tokens_used += getattr(response, 'completion_tokens', 0)

            # 2. Check if the LLM wants to use a tool
            tool_calls = getattr(response, "tool_calls", [])
            
            if not tool_calls:
                # No tools called -> the LLM has finished the task
                logger.info(f"Task {task.task_id} completed successfully in {iterations} iterations.")
                return response.content

            # Add assistant's tool intent to message history
            messages.append({"role": "assistant", "content": response.content, "tool_calls": tool_calls})

            # 3. Execute Tools (Intercepted by Firewall)
            for tool_call in tool_calls:
                tool_name = tool_call["name"]
                tool_args = tool_call["arguments"]
                
                logger.info(f"Agent {agent.agent_id} calling tool {tool_name}")
                
                # Execute (Firewall validation happens inside the tool)
                tool_result = self.tools.invoke(tool_name, agent.agent_id, tool_args)
                
                # 4. Feed Observation back to LLM
                messages.append({
                    "role": "tool", 
                    "tool_call_id": tool_call.get("id", "call_1"), 
                    "content": tool_result
                })

        # Max iterations reached without a final answer
        raise TimeoutError(f"Agent loop exceeded max iterations ({self.max_iterations}) without resolving.")
