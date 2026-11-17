# Getting Started

This guide will help you understand and explore the "Learning While You Sleep: Beyond Memory to Dreaming" learning module.

## Quick Start

### 1. **Read the Summary**
   Start with [`summary.md`](./summary.md) to get an overview of key concepts.

### 2. **Watch/Read the Transcript**
   Review the video transcript in [`transcripts/transcript.txt`](./transcripts/transcript.txt) to understand the full presentation.

### 3. **Explore the Implementations**
   Jump into the practical code examples:
   
   ```bash
   # Run the complete system example (best starting point)
   cd implementations
   python complete_system_example.py
   
   # Run individual examples
   python memory_replay_and_dreaming.py
   python sleep_based_optimization.py
   ```

## Prerequisites

- Python 3.8 or later
- NumPy (install with: `pip install numpy`)

## Understanding the Code

### Example 1: Memory Replay and Dreaming
**File:** `implementations/memory_replay_and_dreaming.py`

This demonstrates:
- How experiences are stored (like memories)
- How the system schedules dreaming sessions
- How memories are consolidated and strengthened

**Key Classes:**
- `ExperienceReplayBuffer` - Memory storage
- `DreamingScheduler` - When to dream
- `MemoryConsolidator` - Strengthening memories
- `IdleTimeLearner` - Full orchestration

### Example 2: Sleep-Based Optimization
**File:** `implementations/sleep_based_optimization.py`

This shows:
- How models select important experiences
- Light vs. deep sleep phases
- Fine-tuning during sleep

**Key Classes:**
- `AdaptiveModelOptimizer` - Model improvement
- `SleepCycle` - Sleep phase management

### Example 3: Complete System
**File:** `implementations/complete_system_example.py`

A full integration showing:
- Day-night cycles
- Active learning phases
- Sleep and optimization phases
- Performance reporting

## Concepts to Understand

### 1. Experience Replay
The AI system stores experiences and replays them during idle time. This is like studying flashcards - repetition strengthens memory.

### 2. Memory Consolidation
Just like humans consolidate memories during sleep, AI systems can strengthen important patterns through replay.

### 3. Dreaming Process
The "dreaming" phase where the system processes memories without new external input, improving internal representations.

### 4. Sleep Cycles
- **Light Sleep:** Quick processing, maintenance
- **Deep Sleep:** Intensive learning, optimization

## Real-World Applications

These concepts are used in:

1. **Reinforcement Learning**
   - Experience replay in DQN, PPO, SAC
   - Improving sample efficiency

2. **Continual Learning**
   - Preventing catastrophic forgetting
   - Maintaining old knowledge while learning new

3. **Model Optimization**
   - Leveraging idle GPU/CPU resources
   - Continuous improvement without service disruption

4. **Knowledge Distillation**
   - Student models learning from teacher experience

## Learning Path

```
START HERE
    |
    v
Read summary.md
    |
    v
Review transcript
    |
    v
Run complete_system_example.py
    |
    v
Study memory_replay_and_dreaming.py
    |
    v
Study sleep_based_optimization.py
    |
    v
Modify examples for your use case
    |
    v
Explore advanced topics in resources.md
```

## Exercises

### Beginner
1. Run `complete_system_example.py` and observe the output
2. Modify the number of steps and cycles to see how it affects performance
3. Change sleep phases duration and observe the changes

### Intermediate
1. Add logging to track memory consolidation progress
2. Implement a different experience selection strategy
3. Add visualization of sleep cycles

### Advanced
1. Integrate with a real reinforcement learning agent
2. Implement adaptive sleep duration based on performance
3. Create a cost/benefit analysis for sleep vs. active learning time

## Troubleshooting

### Import Errors
If you get `ModuleNotFoundError: No module named 'numpy'`:
```bash
pip install numpy
```

### Encoding Issues
If you see `UnicodeEncodeError`, ensure your terminal supports UTF-8 or run with:
```bash
python -X utf8 script.py
```

## Next Steps

1. **Understand the biological inspiration** - Research human sleep and memory consolidation
2. **Apply to your domain** - Adapt these concepts to your specific AI problem
3. **Measure impact** - Track how dreaming improves model performance
4. **Scale up** - Implement in production systems

## Resources

- See [`resources.md`](./resources.md) for papers and additional learning materials
- Video: [Learning while you sleep: Beyond memory to dreaming](https://www.youtube.com/watch?v=tTcxVv8HHNw)
- Full transcript in [`transcripts/transcript.txt`](./transcripts/transcript.txt)

## Questions?

Refer to:
- The code comments in implementation files
- The transcript for detailed explanations
- Resources.md for academic references

Good luck learning! 🌙

