"""
Integration Example: Complete AI System with Sleep and Dreaming

This example shows how to integrate memory replay and sleep-based
optimization into a complete AI system lifecycle.
"""

from memory_replay_and_dreaming import (
    ExperienceReplayBuffer, 
    DreamingScheduler, 
    IdleTimeLearner
)
from sleep_based_optimization import SleepCycle
import numpy as np
import time


class CompleteAISystem:
    """
    A complete AI system that learns during active periods and
    optimizes during idle periods (sleep).
    """
    
    def __init__(self):
        """Initialize the complete AI system."""
        self.learner = IdleTimeLearner(replay_buffer_size=5000)
        self.sleep_manager = SleepCycle()
        self.step_count = 0
        self.active_learning_time = 0
        self.sleep_time = 0
    
    def active_learning_phase(self, num_steps: int = 100):
        """
        Simulate the AI system learning from active tasks.
        """
        print(f"\n=== ACTIVE LEARNING PHASE ({num_steps} steps) ===\n")
        
        start_time = time.time()
        
        for step in range(num_steps):
            # Simulate a task/interaction
            state = np.random.rand(4)
            action = np.random.randint(0, 2)
            reward = np.random.rand()
            next_state = np.random.rand(4)
            done = np.random.rand() > 0.95
            
            # Store the experience
            self.learner.add_experience(state, action, reward, next_state, done)
            
            # Check if it's time to dream
            result = self.learner.process_idle_time()
            if result['dreaming']:
                print(f"Step {step:3d}: Dream #{result['dream_number']} | " +
                      f"Memories replayed: {result['memories_replayed']}")
            
            self.step_count += 1
        
        self.active_learning_time += time.time() - start_time
        
        print(f"\nActive phase completed. Total experiences: " +
              f"{self.learner.replay_buffer.size()}")
    
    def sleep_phase(self, num_cycles: int = 2):
        """
        Simulate the AI system sleeping and optimizing itself.
        """
        print(f"\n=== SLEEP PHASE ({num_cycles} cycles) ===\n")
        
        start_time = time.time()
        
        # Get all experiences for sleep optimization
        experiences = [
            {'id': i, 'data': np.random.rand(5)}
            for i in range(self.learner.replay_buffer.size())
        ]
        
        for cycle in range(num_cycles):
            print(f"Sleep Cycle {cycle + 1}:")
            result = self.sleep_manager.complete_sleep_cycle(experiences)
            
            light = result['light_sleep'].get('improvement', 0)
            deep = result['deep_sleep'].get('improvement', 0)
            
            if isinstance(light, (int, float)):
                print(f"  Light sleep improvement: {light:.6f}")
            if isinstance(deep, (int, float)):
                print(f"  Deep sleep improvement: {deep:.6f}")
            
            print(f"  Sessions completed: " +
                  f"{result['optimizer_report']['sessions_completed']}")
        
        self.sleep_time += time.time() - start_time
    
    def get_system_report(self) -> dict:
        """Generate a complete system performance report."""
        return {
            'total_steps': self.step_count,
            'total_experiences': self.learner.replay_buffer.size(),
            'total_dreams': self.learner.dreaming_scheduler.dream_count,
            'active_learning_time': self.active_learning_time,
            'sleep_time': self.sleep_time,
            'learner_stats': self.learner.get_statistics()
        }
    
    def print_report(self):
        """Print a formatted system report."""
        report = self.get_system_report()
        
        print("\n" + "="*50)
        print("AI SYSTEM PERFORMANCE REPORT")
        print("="*50)
        
        print(f"\nActivity Metrics:")
        print(f"  Total steps processed:    {report['total_steps']}")
        print(f"  Total experiences stored: {report['total_experiences']}")
        print(f"  Total dream sessions:     {report['total_dreams']}")
        
        print(f"\nTiming:")
        print(f"  Active learning time: {report['active_learning_time']:.4f}s")
        print(f"  Sleep time:           {report['sleep_time']:.4f}s")
        
        total_time = report['active_learning_time'] + report['sleep_time']
        if total_time > 0:
            sleep_percent = (report['sleep_time'] / total_time) * 100
            print(f"  Sleep/Total ratio:    {sleep_percent:.1f}%")
        
        print(f"\nDreaming Efficiency:")
        if report['total_dreams'] > 0:
            avg_exp_per_dream = report['total_experiences'] / report['total_dreams']
            print(f"  Avg experiences per dream: {avg_exp_per_dream:.1f}")
        
        print("\n" + "="*50)


def demonstrate_complete_system():
    """Run a complete demonstration of the AI system."""
    
    print("\n" + "*"*50)
    print("*  AI SYSTEM WITH SLEEP AND DREAMING")
    print("*"*50)
    
    system = CompleteAISystem()
    
    # Day 1: Active Learning
    print("\n--- DAY 1: Active Learning ---")
    system.active_learning_phase(num_steps=200)
    
    # Night 1: Sleep and Optimization
    print("\n--- NIGHT 1: Sleep and Optimization ---")
    system.sleep_phase(num_cycles=2)
    
    # Day 2: More Active Learning
    print("\n--- DAY 2: More Active Learning ---")
    system.active_learning_phase(num_steps=150)
    
    # Night 2: More Sleep
    print("\n--- NIGHT 2: More Sleep ---")
    system.sleep_phase(num_cycles=3)
    
    # Generate final report
    system.print_report()
    
    print("\n✓ Demonstration complete!\n")


# Example with detailed explanations
def demonstrate_with_explanations():
    """
    Demonstrate the system with detailed explanations of what's happening.
    """
    
    print("""
+==============================================================+
|   AI LEARNING WITH SLEEP AND DREAMING                       |
|                                                              |
|  This demonstration shows how an AI system can:            |
|  1. Learn from active tasks (Day phase)                    |
|  2. Consolidate memories during sleep (Night phase)        |
|  3. Optimize itself through dreaming (memory replay)        |
|                                                              |
|  This is inspired by how biological systems learn!         |
+==============================================================+
    """)
    
    demonstrate_complete_system()
    
    print("""
KEY CONCEPTS DEMONSTRATED:
--------------------------

1. EXPERIENCE REPLAY
   * New experiences are stored in memory
   * During sleep, the system replays important memories
   * This strengthens neural patterns and improves learning

2. SLEEP CYCLES
   * Light sleep: Quick consolidation of routine experiences
   * Deep sleep: Intensive processing of complex patterns
   * Multiple cycles ensure thorough optimization

3. ADAPTIVE LEARNING
   * High-value experiences get more attention during sleep
   * The system learns which memories are most important
   * This mimics how biological brains prioritize learning

4. EFFICIENCY
   * Active learning: Handles real-time tasks
   * Sleep learning: Leverages idle computational resources
   * Combined approach maximizes learning efficiency

BIOLOGICAL PARALLELS:
---------------------

Just like humans:
+ Learn better with sleep
+ Consolidate memories during rest
+ Process information through dreams
+ Optimize performance through repeated practice

AI systems using these principles:
+ Require less data to achieve same performance
+ Better retain learned knowledge
+ Adapt more effectively to new scenarios
+ Recover better from errors
    """)


if __name__ == "__main__":
    # Run the demonstration with explanations
    demonstrate_with_explanations()
