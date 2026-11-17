# Chain-of-Thought Reasoning Guide

## Overview

The AI Gateway now includes Chain-of-Thought (CoT) reasoning templates that guide the assistant through explicit planning steps when building travel itineraries.

## Reasoning Modes

### 1. SIMPLE (RAG Knowledge Mode)
Used for `/api/ai/chat` - read-only knowledge questions.

**Example:**
```
Q: What should I pack for a trip to Japan?
A: Based on Japanese travel guides, you should pack...
   [sources cited with source_id]
```

**Characteristics:**
- No tool calls
- Focus on citing sources
- Admits uncertainty about current information
- No planning steps

---

### 2. STRUCTURED (Default - Recommended)
Used for `/api/ai/assistant/chat` - trip planning with tools.

**Process made explicit:**

```
[GATHER] Understand requirements
  ↓
[RESEARCH] Search for options
  ↓
[COMPARE] Analyze trade-offs
  ↓
[STRUCTURE] Build itinerary
  ↓
[FINALIZE] Prepare quote
```

**Example flow:**
```
User: "3 days in Tokyo with $2000, interested in culture"