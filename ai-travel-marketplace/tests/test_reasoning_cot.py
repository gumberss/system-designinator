"""
Tests for Chain-of-Thought and Reasoning Templates.

Validates that:
1. Structured reasoning prompts are used correctly
2. Reasoning metadata is extracted and tracked
3. Planning steps are visible in responses
4. Safety guardrails remain intact
"""

from __future__ import annotations

import pytest
from services.ai_gateway.reasoning_prompts import (
    ReasoningMode,
    RAG_SYSTEM_PROMPT,
    ASSISTANT_SYSTEM_PROMPT_STRUCTURED,
    ASSISTANT_SYSTEM_PROMPT_DETAILED,
    get_assistant_system_prompt,
    build_assistant_system_message,
)
from services.ai_gateway.assistant_builder import (
    build_assistant_messages_with_reasoning,
    extract_reasoning_metadata,
)


def test_reasoning_modes_exist() -> None:
    """Verify all reasoning modes are defined."""
    assert hasattr(ReasoningMode, "SIMPLE")
    assert hasattr(ReasoningMode, "STRUCTURED")
    assert hasattr(ReasoningMode, "DETAILED")


def test_rag_system_prompt_is_defined() -> None:
    """RAG prompt exists and includes safety guardrails."""
    assert len(RAG_SYSTEM_PROMPT) > 100
    assert "untrusted reference data" in RAG_SYSTEM_PROMPT
    assert "source_id" in RAG_SYSTEM_PROMPT
    assert "Do not claim current" in RAG_SYSTEM_PROMPT


def test_structured_assistant_prompt_includes_planning_steps() -> None:
    """Structured prompt includes explicit [GATHER], [RESEARCH], etc. steps."""
    assert "[GATHER]" in ASSISTANT_SYSTEM_PROMPT_STRUCTURED
    assert "[RESEARCH]" in ASSISTANT_SYSTEM_PROMPT_STRUCTURED
    assert "[COMPARE]" in ASSISTANT_SYSTEM_PROMPT_STRUCTURED
    assert "[STRUCTURE]" in ASSISTANT_SYSTEM_PROMPT_STRUCTURED
    assert "[FINALIZE]" in ASSISTANT_SYSTEM_PROMPT_STRUCTURED
    assert "Step 1:" in ASSISTANT_SYSTEM_PROMPT_STRUCTURED or "Step 1" in ASSISTANT_SYSTEM_PROMPT_STRUCTURED


def test_detailed_prompt_includes_reasoning_markers() -> None:
    """Detailed prompt includes [THINKING], [ANALYSIS], [TRADE-OFF] etc."""
    assert "[THINKING]" in ASSISTANT_SYSTEM_PROMPT_DETAILED
    assert "[ANALYSIS]" in ASSISTANT_SYSTEM_PROMPT_DETAILED
    assert "[RECOMMENDATION]" in ASSISTANT_SYSTEM_PROMPT_DETAILED
    assert "[TRADE-OFF]" in ASSISTANT_SYSTEM_PROMPT_DETAILED
    assert "[BUDGET TRACKING]" in ASSISTANT_SYSTEM_PROMPT_DETAILED


def test_structured_prompt_includes_tool_instructions() -> None:
    """Structured prompt explains how to use search_travel and other tools."""
    assert "search_travel" in ASSISTANT_SYSTEM_PROMPT_STRUCTURED
    assert "create_itinerary" in ASSISTANT_SYSTEM_PROMPT_STRUCTURED or "itinerary" in ASSISTANT_SYSTEM_PROMPT_STRUCTURED.lower()
    assert "create_quote" in ASSISTANT_SYSTEM_PROMPT_STRUCTURED


def test_prompts_maintain_safety_guardrails() -> None:
    """All prompts include safety guardrails about claims and authorization."""
    for prompt in [
        RAG_SYSTEM_PROMPT,
        ASSISTANT_SYSTEM_PROMPT_STRUCTURED,
        ASSISTANT_SYSTEM_PROMPT_DETAILED,
    ]:
        # Check for uncertainty handling
        assert any(
            phrase in prompt
            for phrase in ["uncertain", "unknown", "Admit uncertainty", "I don't know", "uncertainty"]
        )
        # Check for no-confirmation guardrails
        assert any(
            phrase in prompt
            for phrase in ["confirm on the user's behalf", "cannot confirm", "cannot", "authorization", "CRITICAL", "cannot confirm on the user"]
        )


def test_get_assistant_system_prompt_selects_by_mode() -> None:
    """get_assistant_system_prompt returns correct prompt by mode."""
    simple = get_assistant_system_prompt(ReasoningMode.SIMPLE)
    assert simple == RAG_SYSTEM_PROMPT
    
    structured = get_assistant_system_prompt(ReasoningMode.STRUCTURED)
    assert "[GATHER]" in structured
    
    detailed = get_assistant_system_prompt(ReasoningMode.DETAILED)
    assert "[THINKING]" in detailed


def test_build_assistant_system_message_includes_tools() -> None:
    """System message includes tool usage instructions."""
    msg = build_assistant_system_message(ReasoningMode.STRUCTURED)
    assert msg["role"] == "system"
    assert len(msg["content"]) > 200
    assert "search_travel" in msg["content"]


def test_build_assistant_messages_with_reasoning_first_message() -> None:
    """First message includes system prompt + user message."""
    messages = build_assistant_messages_with_reasoning(
        "What should I visit in Tokyo?",
        conversation_history=None,
        reasoning_mode=ReasoningMode.STRUCTURED,
    )
    
    assert len(messages) >= 2
    assert messages[0]["role"] == "system"
    assert "[GATHER]" in messages[0]["content"]  # Structured mode
    assert messages[1]["role"] == "user"
    assert "Tokyo" in messages[1]["content"]


def test_build_assistant_messages_with_history() -> None:
    """Subsequent messages include conversation history."""
    history = [
        {"role": "user", "content": "I want to visit Tokyo"},
        {"role": "assistant", "content": "Great choice! Let me search for flights."},
    ]
    
    messages = build_assistant_messages_with_reasoning(
        "For how long should I stay?",
        conversation_history=history,
        reasoning_mode=ReasoningMode.STRUCTURED,
    )
    
    # Should have system + history + new user message
    assert len(messages) >= 3
    assert messages[0]["role"] == "system"
    assert messages[-1]["role"] == "user"
    assert "stay" in messages[-1]["content"]


def test_extract_reasoning_metadata_detects_planning_markers() -> None:
    """Extract reasoning metadata counts planning step markers."""
    response = """
    [GATHER] Let me understand your requirements first.
    [RESEARCH] Searching for flights to Tokyo...
    [COMPARE] Found 3 options. Let me compare them.
    [FINALIZE] Total cost is $2000.
    """
    
    metadata = extract_reasoning_metadata(response)
    assert metadata["reasoning_markers_used"] is True
    assert metadata["marker_counts"]["[GATHER]"] == 1
    assert metadata["marker_counts"]["[RESEARCH]"] == 1
    assert metadata["marker_counts"]["[COMPARE]"] == 1
    assert metadata["marker_counts"]["[FINALIZE]"] == 1
    assert metadata["planning_steps_visible"] == 4


def test_extract_reasoning_metadata_detects_detailed_markers() -> None:
    """Extract reasoning metadata counts detailed reasoning markers."""
    response = """
    [THINKING] User has $2000 budget and 3 days.
    [ANALYSIS] This suggests mid-range hotel and guided tours.
    [TRADE-OFF] Direct flights save time but cost $200 more.
    [RECOMMENDATION] Book the 1-stop flight and upgrade hotel.
    """
    
    metadata = extract_reasoning_metadata(response)
    assert metadata["reasoning_markers_used"] is True
    assert metadata["marker_counts"]["[THINKING]"] == 1
    assert metadata["marker_counts"]["[ANALYSIS]"] == 1
    assert metadata["marker_counts"]["[TRADE-OFF]"] == 1
    assert metadata["marker_counts"]["[RECOMMENDATION]"] == 1


def test_extract_reasoning_metadata_detects_uncertainty_markers() -> None:
    """Extract reasoning metadata identifies uncertainty handling."""
    response = """
    [UNCERTAIN] Current flight prices change hourly.
    [CONFIDENT] Tokyo has excellent temples in Asakusa.
    [UNKNOWN] Visa requirements depend on your nationality.
    """
    
    metadata = extract_reasoning_metadata(response)
    assert metadata["marker_counts"]["[UNCERTAIN]"] == 1
    assert metadata["marker_counts"]["[CONFIDENT]"] == 1


def test_extract_reasoning_metadata_no_markers() -> None:
    """Extract reasoning metadata returns defaults when no markers present."""
    response = "Just a regular response without any reasoning markers."
    
    metadata = extract_reasoning_metadata(response)
    assert metadata["reasoning_markers_used"] is False
    assert metadata["planning_steps_visible"] == 0
    assert all(count == 0 for count in metadata["marker_counts"].values())


def test_assistant_prompt_includes_uncertainty_handling() -> None:
    """Structured prompt teaches model to handle uncertainty."""
    assert "uncertain" in ASSISTANT_SYSTEM_PROMPT_STRUCTURED.lower()
    # Detailed mode has [UNCERTAIN], structured has "uncertainty" in guardrails
    assert any(
        phrase in ASSISTANT_SYSTEM_PROMPT_STRUCTURED
        for phrase in ["[UNCERTAIN]", "Admit uncertainty", "uncertain"]
    )


def test_assistant_prompt_forbids_booking_without_confirmation() -> None:
    """Structured prompt emphasizes explicit confirmation requirement."""
    assert "confirm" in ASSISTANT_SYSTEM_PROMPT_STRUCTURED.lower()
    assert "cannot confirm on the user's behalf" in ASSISTANT_SYSTEM_PROMPT_STRUCTURED or "cannot confirm on the user" in ASSISTANT_SYSTEM_PROMPT_STRUCTURED.lower() or "CRITICAL" in ASSISTANT_SYSTEM_PROMPT_STRUCTURED


def test_structured_prompt_example_sections() -> None:
    """Structured prompt includes worked examples of planning."""
    assert "Example 1:" in ASSISTANT_SYSTEM_PROMPT_STRUCTURED or "example" in ASSISTANT_SYSTEM_PROMPT_STRUCTURED.lower()
    # Check for example with specific trip details
    assert any(place in ASSISTANT_SYSTEM_PROMPT_STRUCTURED for place in ["Tokyo", "Kyoto", "Morocco"])
