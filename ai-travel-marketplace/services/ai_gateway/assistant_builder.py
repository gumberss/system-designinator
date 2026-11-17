"""
Assistant message builder with Chain-of-Thought support.

Handles building assistant prompts with reasoning mode and tool context.
"""

from __future__ import annotations

from typing import Any

from services.ai_gateway.reasoning_prompts import (
    ReasoningMode,
    build_assistant_system_message,
)


def build_assistant_messages_with_reasoning(
    user_message: str,
    conversation_history: list[dict[str, str]] | None = None,
    reasoning_mode: ReasoningMode = ReasoningMode.STRUCTURED,
) -> list[dict[str, Any]]:
    """
    Build messages for assistant chat with Chain-of-Thought.
    
    Args:
        user_message: The current user query
        conversation_history: Previous messages in conversation (including system)
        reasoning_mode: Level of explicit reasoning (simple, structured, detailed)
    
    Returns:
        List of message dicts ready for provider.complete_with_tools()
    """
    # Start with system prompt (structured for reasoning)
    system_msg = build_assistant_system_message(reasoning_mode)
    
    if not conversation_history:
        # First message in conversation
        return [system_msg, {"role": "user", "content": user_message}]
    
    # Add conversation history (without re-adding system prompt if already there)
    messages = []
    
    # Check if system prompt is already in history (it shouldn't be, but be defensive)
    if conversation_history and conversation_history[0].get("role") != "system":
        messages.append(system_msg)
    
    # Add all history except system (or keep system if it's there)
    for msg in conversation_history:
        if msg.get("role") == "system" and system_msg in messages:
            # Skip duplicate system
            continue
        messages.append(msg)
    
    # Add new user message
    messages.append({"role": "user", "content": user_message})
    
    return messages


def format_reasoning_response(
    answer: str,
    reasoning_markers_used: bool = False,
) -> str:
    """
    Format assistant response to highlight reasoning markers.
    
    When the model uses [THINKING], [ANALYSIS], [RECOMMENDATION] markers,
    this could enhance them for display. For now, returns answer as-is.
    """
    return answer


def extract_reasoning_metadata(
    response: str,
) -> dict[str, Any]:
    """
    Extract metadata about reasoning from response.
    
    Counts markers used, identifies reasoning patterns, etc.
    """
    markers = {
        "[GATHER]": response.count("[GATHER]"),
        "[RESEARCH]": response.count("[RESEARCH]"),
        "[COMPARE]": response.count("[COMPARE]"),
        "[STRUCTURE]": response.count("[STRUCTURE]"),
        "[FINALIZE]": response.count("[FINALIZE]"),
        "[THINKING]": response.count("[THINKING]"),
        "[ANALYSIS]": response.count("[ANALYSIS]"),
        "[RECOMMENDATION]": response.count("[RECOMMENDATION]"),
        "[TRADE-OFF]": response.count("[TRADE-OFF]"),
        "[BUDGET TRACKING]": response.count("[BUDGET TRACKING]"),
        "[UNCERTAIN]": response.count("[UNCERTAIN]"),
        "[CONFIDENT]": response.count("[CONFIDENT]"),
    }
    
    return {
        "reasoning_markers_used": any(count > 0 for count in markers.values()),
        "marker_counts": markers,
        "planning_steps_visible": sum(
            [
                markers["[GATHER]"],
                markers["[RESEARCH]"],
                markers["[COMPARE]"],
                markers["[STRUCTURE]"],
                markers["[FINALIZE]"],
            ]
        ),
    }
