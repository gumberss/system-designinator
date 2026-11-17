"""
Memory Replay and Dreaming Implementation

This module demonstrates how AI systems can implement memory consolidation
and dreaming-like processes during idle periods to improve learning.
"""

from collections import deque
from typing import Tuple, List
import numpy as np


class ExperienceReplayBuffer:
    """
    A buffer that stores past experiences (memories) and allows for
    replay to strengthen important patterns - similar to dreaming.
    """
    
    def __init__(self, max_size: int = 10000):
        """
        Initialize the replay buffer.
        
        Args:
            max_size: Maximum number of experiences to store
        """
        self.buffer = deque(maxlen=max_size)
        self.max_size = max_size
    
    def add(self, state, action, reward, next_state, done):
        """Add a new experience to the buffer (new memory)."""
        self.buffer.append((state, action, reward, next_state, done))
    
    def sample_batch(self, batch_size: int) -> Tuple[List, List, List, List, List]:
        """
        Sample a batch of memories for replay (dreaming).
        
        Args:
            batch_size: Number of experiences to sample
            
        Returns:
            Tuple of states, actions, rewards, next_states, and done flags
        """
        if len(self.buffer) < batch_size:
            batch_size = len(self.buffer)
        
        indices = np.random.choice(len(self.buffer), batch_size, replace=False)
        batch = [self.buffer[i] for i in indices]
        
        states = np.array([x[0] for x in batch])
        actions = np.array([x[1] for x in batch])
        rewards = np.array([x[2] for x in batch])
        next_states = np.array([x[3] for x in batch])
        dones = np.array([x[4] for x in batch])
        
        return states, actions, rewards, next_states, dones
    
    def size(self) -> int:
        """Return current size of the buffer."""
        return len(self.buffer)


class DreamingScheduler:
    """
    Manages when and how the system performs 'dreaming' (memory consolidation)
    during idle periods.
    """
    
    def __init__(self, 
                 dream_interval: int = 100,
                 dream_batch_size: int = 32,
                 dream_repeats: int = 5):
        """
        Initialize the dreaming scheduler.
        
        Args:
            dream_interval: Steps between dreaming sessions
            dream_batch_size: Number of memories to process during dreaming
            dream_repeats: How many times to replay memories
        """
        self.dream_interval = dream_interval
        self.dream_batch_size = dream_batch_size
        self.dream_repeats = dream_repeats
        self.step_count = 0
        self.dream_count = 0
    
    def should_dream(self) -> bool:
        """Determine if it's time to dream based on interval."""
        self.step_count += 1
        if self.step_count % self.dream_interval == 0:
            self.dream_count += 1
            return True
        return False
    
    def get_dream_config(self) -> dict:
        """Get configuration for the upcoming dream session."""
        return {
            'batch_size': self.dream_batch_size,
            'repeats': self.dream_repeats,
            'dream_number': self.dream_count
        }


class MemoryConsolidator:
    """
    Simulates the biological process of memory consolidation.
    Strengthens important connections and prunes less relevant ones.
    """
    
    def __init__(self, consolidation_strength: float = 0.1):
        """
        Initialize the consolidator.
        
        Args:
            consolidation_strength: How much to adjust memory weights
        """
        self.consolidation_strength = consolidation_strength
        self.memory_importance = {}
    
    def mark_important_memory(self, memory_id: str, importance: float):
        """Mark a memory as important (stronger consolidation)."""
        self.memory_importance[memory_id] = importance
    
    def consolidate(self, memories: List[Tuple]) -> List[Tuple]:
        """
        Process memories to strengthen important ones.
        
        Args:
            memories: List of memory tuples to consolidate
            
        Returns:
            Adjusted memories with stronger important patterns
        """
        consolidated = []
        
        for i, memory in enumerate(memories):
            # Increase influence of important memories
            importance = self.memory_importance.get(str(i), 0.5)
            adjusted_reward = memory[2] * (1 + importance * self.consolidation_strength)
            
            adjusted_memory = (memory[0], memory[1], adjusted_reward, 
                             memory[3], memory[4])
            consolidated.append(adjusted_memory)
        
        return consolidated
    
    def get_consolidation_stats(self) -> dict:
        """Get statistics about memory consolidation."""
        if not self.memory_importance:
            return {'total_memories': 0, 'avg_importance': 0}
        
        importances = list(self.memory_importance.values())
        return {
            'total_memories': len(importances),
            'avg_importance': np.mean(importances),
            'max_importance': np.max(importances),
            'min_importance': np.min(importances)
        }


class IdleTimeLearner:
    """
    Main system that orchestrates learning during idle periods.
    Combines replay buffer, dreaming scheduler, and memory consolidator.
    """
    
    def __init__(self, replay_buffer_size: int = 10000):
        """Initialize the idle-time learner system."""
        self.replay_buffer = ExperienceReplayBuffer(max_size=replay_buffer_size)
        self.dreaming_scheduler = DreamingScheduler()
        self.consolidator = MemoryConsolidator()
        self.learning_log = []
    
    def add_experience(self, state, action, reward, next_state, done):
        """Add a new experience from active learning."""
        self.replay_buffer.add(state, action, reward, next_state, done)
    
    def process_idle_time(self) -> dict:
        """
        Process available idle time to improve the model.
        This is where 'dreaming' happens.
        """
        if not self.dreaming_scheduler.should_dream():
            return {'dreaming': False}
        
        dream_config = self.dreaming_scheduler.get_dream_config()
        
        # Sample memories for replay
        states, actions, rewards, next_states, dones = \
            self.replay_buffer.sample_batch(dream_config['batch_size'])
        
        # Consolidate memories
        memories = list(zip(states, actions, rewards, next_states, dones))
        consolidated_memories = self.consolidator.consolidate(memories)
        
        learning_info = {
            'dreaming': True,
            'dream_number': dream_config['dream_number'],
            'memories_replayed': len(memories),
            'replay_repeats': dream_config['repeats'],
            'consolidation_stats': self.consolidator.get_consolidation_stats()
        }
        
        self.learning_log.append(learning_info)
        return learning_info
    
    def get_statistics(self) -> dict:
        """Get overall learning statistics."""
        return {
            'total_experiences': self.replay_buffer.size(),
            'total_dreams': self.dreaming_scheduler.dream_count,
            'learning_log_entries': len(self.learning_log)
        }


# Example usage
if __name__ == "__main__":
    # Initialize the idle-time learner
    learner = IdleTimeLearner(replay_buffer_size=1000)
    
    # Simulate active learning phase
    print("=== Active Learning Phase ===")
    for step in range(250):
        # Simulate experiences
        state = np.random.rand(4)
        action = np.random.randint(0, 2)
        reward = np.random.rand()
        next_state = np.random.rand(4)
        done = np.random.rand() > 0.9
        
        learner.add_experience(state, action, reward, next_state, done)
        
        # Process idle time (dreaming)
        result = learner.process_idle_time()
        if result['dreaming']:
            print(f"\nDream #{result['dream_number']} at step {step}")
            print(f"  Replayed {result['memories_replayed']} memories")
            print(f"  Consolidation stats: {result['consolidation_stats']}")
    
    # Final statistics
    print("\n=== Final Statistics ===")
    stats = learner.get_statistics()
    for key, value in stats.items():
        print(f"{key}: {value}")
