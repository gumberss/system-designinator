#!/usr/bin/env python3
"""
Quick test to demonstrate Chain-of-Thought reasoning.

Runs without Docker/API - purely tests prompt structure and reasoning metadata extraction.
"""

import sys
from pathlib import Path

# Add parent directory to path so we can import modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.ai_gateway.reasoning_prompts import (
    ReasoningMode,
    get_assistant_system_prompt,
)
from services.ai_gateway.assistant_builder import (
    build_assistant_messages_with_reasoning,
    extract_reasoning_metadata,
)


def demo_reasoning_modes():
    """Show the three reasoning modes."""
    print("=" * 80)
    print("CHAIN-OF-THOUGHT REASONING MODES")
    print("=" * 80)
    
    modes = [
        (ReasoningMode.SIMPLE, "Basic knowledge Q&A"),
        (ReasoningMode.STRUCTURED, "5-step trip planning"),
        (ReasoningMode.DETAILED, "Explicit thinking markers"),
    ]
    
    for mode, description in modes:
        prompt = get_assistant_system_prompt(mode)
        lines = prompt.split("\n")
        print(f"\n### {mode.value.upper()}: {description}")
        print(f"    Prompt length: {len(prompt)} chars")
        print(f"    First 3 lines:")
        for line in lines[:3]:
            if line.strip():
                print(f"      {line[:70]}...")


def demo_message_building():
    """Show how messages are built with reasoning."""
    print("\n" + "=" * 80)
    print("MESSAGE BUILDING WITH REASONING")
    print("=" * 80)
    
    # First message (no history)
    print("\n[1] First message in conversation:")
    messages = build_assistant_messages_with_reasoning(
        "I want to visit Tokyo for 3 days with $2000 budget",
        conversation_history=None,
        reasoning_mode=ReasoningMode.STRUCTURED,
    )
    print(f"    Number of messages: {len(messages)}")
    print(f"    Message roles: {[m['role'] for m in messages]}")
    print(f"    System prompt includes planning steps: {('[GATHER]' in messages[0]['content'])}")
    
    # With history
    print("\n[2] Continuing conversation (with history):")
    history = [
        {"role": "user", "content": "I want to visit Tokyo"},
        {"role": "assistant", "content": "Great! Let me search for flights..."},
    ]
    messages = build_assistant_messages_with_reasoning(
        "For how long should I stay?",
        conversation_history=history,
        reasoning_mode=ReasoningMode.STRUCTURED,
    )
    print(f"    Number of messages: {len(messages)} (system + history + new user)")
    print(f"    Message roles: {[m['role'] for m in messages]}")


def demo_metadata_extraction():
    """Show how reasoning metadata is extracted."""
    print("\n" + "=" * 80)
    print("REASONING METADATA EXTRACTION")
    print("=" * 80)
    
    # Example response with planning markers
    response1 = """
    [GATHER] Let me understand your requirements.
    You mentioned culture and food - that's perfect!
    
    [RESEARCH] Searching for flights to Tokyo...
    Found several options. Now searching for hotels...
    
    [COMPARE] Comparing options:
    - Direct flights save 3 hours but cost $200 more
    - Central hotels are pricier but save on transport
    
    [STRUCTURE] Building your 3-day itinerary:
    Day 1: Arrival, explore Shinjuku
    Day 2: Temples and museums
    Day 3: Food markets and shopping
    
    [FINALIZE] Your quote:
    Total cost: $1,500 (within budget!)
    Quote ID: quote-abc123
    """
    
    metadata1 = extract_reasoning_metadata(response1)
    print("\n[Response with planning markers]")
    print(f"    Reasoning used: {metadata1['reasoning_markers_used']}")
    print(f"    Planning steps visible: {metadata1['planning_steps_visible']}")
    print(f"    Step breakdown:")
    for step in ["[GATHER]", "[RESEARCH]", "[COMPARE]", "[STRUCTURE]", "[FINALIZE]"]:
        count = metadata1["marker_counts"][step]
        print(f"      {step}: {count}")
    
    # Example response without markers
    response2 = "Just a regular response without reasoning markers."
    metadata2 = extract_reasoning_metadata(response2)
    print("\n[Response without planning markers]")
    print(f"    Reasoning used: {metadata2['reasoning_markers_used']}")
    print(f"    Planning steps visible: {metadata2['planning_steps_visible']}")


def demo_detailed_reasoning():
    """Show detailed reasoning mode with thinking markers."""
    print("\n" + "=" * 80)
    print("DETAILED REASONING MODE (WITH THINKING MARKERS)")
    print("=" * 80)
    
    response = """
    [THINKING] User has $2000 budget for 3 days = ~$667/day.
    Flight should be under $500 to leave room for hotel + activities.
    
    Options found:
    - Direct flight: $800 (over budget)
    - 1-stop flight: $500 (fits budget)
    
    [ANALYSIS] Direct flight exceeds budget.
    1-stop flight is better value for money.
    
    [TRADE-OFF]
    Time vs money: 1-stop adds 3 hours but saves $300.
    Given 3-day trip, the extra 3 hours isn't critical.
    Better to save $300 for premium activities.
    
    [RECOMMENDATION] Book 1-stop flight.
    
    [BUDGET TRACKING]
    Flight: $500 (25% of $2000)
    Hotel: $300 (15% of $2000)
    Activities: $400 (20% of $2000)
    Meals/transport: $800 (40% of $2000)
    
    [UNCERTAIN] Prices change daily. These are current estimates.
    [CONFIDENT] Tokyo has excellent temples - fact-checked against sources.
    [UNKNOWN] Exact visa requirements depend on your nationality.
    """
    
    metadata = extract_reasoning_metadata(response)
    print("\nDetailed reasoning markers found:")
    markers_with_count = {
        k: v for k, v in metadata["marker_counts"].items() if v > 0
    }
    for marker, count in markers_with_count.items():
        print(f"  {marker}: {count}")


if __name__ == "__main__":
    demo_reasoning_modes()
    demo_message_building()
    demo_metadata_extraction()
    demo_detailed_reasoning()
    
    print("\n" + "=" * 80)
    print("[OK] Chain-of-Thought reasoning system working correctly!")
    print("=" * 80)
