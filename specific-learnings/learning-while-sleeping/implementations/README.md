# Implementations

This folder contains practical Python implementations demonstrating the concepts from the "Learning While You Sleep: Beyond Memory to Dreaming" video.

## Files

### 1. `memory_replay_and_dreaming.py`

Demonstrates core mechanisms of memory consolidation and dreaming:

- **ExperienceReplayBuffer**: Stores past experiences and allows sampling for replay
- **DreamingScheduler**: Manages when and how often dreaming occurs
- **MemoryConsolidator**: Strengthens important memories and prunes irrelevant ones
- **IdleTimeLearner**: Orchestrates the complete dreaming and consolidation process

**Key Concepts:**
- Experience replay (core to DQN and other RL algorithms)
- Scheduled dreaming during idle periods
- Adaptive memory importance tracking

**Usage:**
```bash
python memory_replay_and_dreaming.py
```

### 2. `sleep_based_optimization.py`

Demonstrates how models optimize themselves during sleep:

- **AdaptiveModelOptimizer**: Selects high-value experiences for fine-tuning
- **SleepCycle**: Manages different types of sleep (light and deep)
- Complete sleep-wake cycle simulation

**Key Concepts:**
- Selective experience replay
- Light vs. deep sleep phases
- Model weight updates during idle time
- Performance tracking across sleep cycles

**Usage:**
```bash
python sleep_based_optimization.py
```

### 3. `purchase_sleep_learning/`

A functional CSV purchase-categorization CLI using an OpenAI-compatible student
model, blind periodic teacher audits, recorded rewards, and persistent SQLite
example memory. Unlike the simulated weight updates above, it measures actual
predictions against a separate labeled holdout and reports regressions as well
as improvements. It changes prompts through memory, not model weights.

See [the project README](purchase_sleep_learning/README.md) for configuration,
fixed non-sensitive sample CSVs, commands, limitations, and tests.

## Running the Examples

Each script is self-contained and can be run independently:

```bash
# Run experience replay example
python memory_replay_and_dreaming.py

# Run sleep-based optimization example
python sleep_based_optimization.py
```

## Understanding the Output

Both scripts will produce:

1. **During execution**: Progress through different phases
2. **After execution**: Statistics about learning and improvement

Pay attention to:
- How many memories are replayed during each dream session
- The improvement metrics after consolidation
- The statistics comparing active learning vs. dreaming efficiency

## Extending These Examples

You can modify these implementations to:

- Integrate with actual ML models (PyTorch, TensorFlow)
- Connect to real data pipelines
- Add different memory selection strategies
- Implement various consolidation algorithms
- Measure actual model performance improvements

## Real-World Applications

These patterns are used in:

- **Reinforcement Learning**: Experience replay in DQN, PPO, and other algorithms
- **Continual Learning**: Preventing catastrophic forgetting through replay
- **Transfer Learning**: Fine-tuning on selected past examples
- **Model Optimization**: Leveraging idle compute resources
- **Knowledge Distillation**: Student models learning from teacher replay
