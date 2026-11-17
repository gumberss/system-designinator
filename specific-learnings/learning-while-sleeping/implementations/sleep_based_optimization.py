"""
Sleep-Based Model Optimization

Demonstrates how a model can improve itself during idle periods
by fine-tuning on selected past experiences (sleeping and dreaming).
"""

import numpy as np
from typing import List, Dict, Tuple
from dataclasses import dataclass
from datetime import datetime


@dataclass
class TrainingSession:
    """Record of a training session during idle time."""
    timestamp: datetime
    session_id: int
    memories_used: int
    improvement: float
    processing_time: float


class AdaptiveModelOptimizer:
    """
    Manages model optimization during idle periods.
    Selects high-value memories and uses them to fine-tune the model.
    """
    
    def __init__(self, 
                 model_learning_rate: float = 0.001,
                 selection_threshold: float = 0.7):
        """
        Initialize the optimizer.
        
        Args:
            model_learning_rate: Learning rate for fine-tuning
            selection_threshold: Threshold for selecting important experiences
        """
        self.model_learning_rate = model_learning_rate
        self.selection_threshold = selection_threshold
        self.model_weights = self._initialize_model_weights()
        self.training_history: List[TrainingSession] = []
        self.session_counter = 0
    
    def _initialize_model_weights(self) -> np.ndarray:
        """Initialize random model weights."""
        return np.random.randn(10, 5)  # Simplified model weights
    
    def select_important_memories(self, 
                                 experiences: List[Dict],
                                 scores: List[float]) -> List[Dict]:
        """
        Select memories that are worth replaying during sleep.
        High-value experiences get more attention during dreaming.
        
        Args:
            experiences: List of experience dictionaries
            scores: Performance scores associated with each experience
            
        Returns:
            Filtered list of important experiences
        """
        selected = []
        for exp, score in zip(experiences, scores):
            if score > self.selection_threshold:
                selected.append(exp)
        
        return selected
    
    def fine_tune_on_experiences(self, 
                                experiences: List[Dict],
                                epochs: int = 1) -> float:
        """
        Fine-tune the model on selected experiences.
        This is the 'dreaming' process where the model practices.
        
        Args:
            experiences: Selected experiences for replay
            epochs: Number of times to process the batch
            
        Returns:
            Improvement metric
        """
        if not experiences:
            return 0.0
        
        total_adjustment = 0.0
        
        for epoch in range(epochs):
            for exp in experiences:
                # Simulate gradient computation
                gradient = np.random.randn(*self.model_weights.shape) * 0.01
                
                # Apply learning update
                self.model_weights += self.model_learning_rate * gradient
                
                # Compute improvement (simplified)
                total_adjustment += np.abs(gradient).mean()
        
        improvement = total_adjustment / len(experiences)
        return improvement
    
    def sleep_and_optimize(self,
                          experiences: List[Dict],
                          scores: List[float]) -> Dict:
        """
        Main method to handle optimization during sleep period.
        
        Args:
            experiences: Pool of recent experiences
            scores: Quality scores for each experience
            
        Returns:
            Summary of the optimization session
        """
        import time
        start_time = time.time()
        
        # Select important memories
        important = self.select_important_memories(experiences, scores)
        
        if not important:
            return {
                'success': False,
                'reason': 'No important memories found'
            }
        
        # Fine-tune on selected memories
        improvement = self.fine_tune_on_experiences(important, epochs=3)
        
        elapsed = time.time() - start_time
        
        # Record session
        self.session_counter += 1
        session = TrainingSession(
            timestamp=datetime.now(),
            session_id=self.session_counter,
            memories_used=len(important),
            improvement=improvement,
            processing_time=elapsed
        )
        self.training_history.append(session)
        
        return {
            'success': True,
            'session_id': self.session_counter,
            'memories_selected': len(important),
            'memories_total': len(experiences),
            'improvement': improvement,
            'processing_time': elapsed
        }
    
    def get_optimization_report(self) -> Dict:
        """Generate a report on all optimization sessions."""
        if not self.training_history:
            return {'sessions': 0, 'total_improvements': 0}
        
        total_improvement = sum(s.improvement for s in self.training_history)
        total_time = sum(s.processing_time for s in self.training_history)
        avg_memories = np.mean([s.memories_used for s in self.training_history])
        
        return {
            'sessions_completed': len(self.training_history),
            'total_improvement': total_improvement,
            'average_improvement': total_improvement / len(self.training_history),
            'total_processing_time': total_time,
            'average_memories_per_session': avg_memories
        }


class SleepCycle:
    """
    Simulates complete sleep cycles for the AI system.
    Includes light sleep (simple consolidation) and deep sleep (intense dreaming).
    """
    
    def __init__(self):
        """Initialize sleep cycle manager."""
        self.optimizer = AdaptiveModelOptimizer()
        self.cycle_count = 0
    
    def light_sleep(self, experiences: List[Dict]) -> Dict:
        """
        Light sleep: Quick consolidation without intensive processing.
        """
        # Sample subset of experiences
        sample_size = min(10, len(experiences))
        sample = np.random.choice(len(experiences), sample_size, replace=False)
        sampled_exp = [experiences[i] for i in sample]
        
        # Generate moderate scores
        scores = np.random.rand(len(sampled_exp)) * 0.8
        
        return self.optimizer.sleep_and_optimize(sampled_exp, scores)
    
    def deep_sleep(self, experiences: List[Dict]) -> Dict:
        """
        Deep sleep: Intensive processing of high-value experiences.
        """
        # Use more experiences
        sample_size = min(50, len(experiences))
        sample = np.random.choice(len(experiences), sample_size, replace=False)
        sampled_exp = [experiences[i] for i in sample]
        
        # Focus on high-quality experiences
        scores = np.random.rand(len(sampled_exp)) * 0.5 + 0.5
        
        return self.optimizer.sleep_and_optimize(sampled_exp, scores)
    
    def complete_sleep_cycle(self, experiences: List[Dict]) -> Dict:
        """
        Complete sleep cycle with both light and deep sleep phases.
        """
        self.cycle_count += 1
        
        # Light sleep phase
        light_result = self.light_sleep(experiences)
        
        # Deep sleep phase
        deep_result = self.deep_sleep(experiences)
        
        return {
            'cycle_number': self.cycle_count,
            'light_sleep': light_result,
            'deep_sleep': deep_result,
            'optimizer_report': self.optimizer.get_optimization_report()
        }


# Example usage
if __name__ == "__main__":
    # Create sleep cycle manager
    sleep_manager = SleepCycle()
    
    # Simulate accumulated experiences from active learning
    print("=== Simulating AI Learning and Sleep Cycles ===\n")
    
    total_experiences = 100
    experiences = [
        {'id': i, 'data': np.random.rand(5), 'timestamp': i}
        for i in range(total_experiences)
    ]
    
    # Run multiple sleep cycles
    for cycle in range(3):
        print(f"--- Sleep Cycle {cycle + 1} ---")
        result = sleep_manager.complete_sleep_cycle(experiences)
        
        light_imp = result['light_sleep'].get('improvement', 0)
        deep_imp = result['deep_sleep'].get('improvement', 0)
        
        if isinstance(light_imp, (int, float)):
            print(f"Light sleep improvement: {light_imp:.6f}")
        else:
            print(f"Light sleep improvement: {light_imp}")
            
        if isinstance(deep_imp, (int, float)):
            print(f"Deep sleep improvement: {deep_imp:.6f}")
        else:
            print(f"Deep sleep improvement: {deep_imp}")
        
        # Print optimizer report
        report = result['optimizer_report']
        print(f"Sessions completed: {report['sessions_completed']}")
        print(f"Average improvement: {report['average_improvement']:.6f}\n")
    
    # Final analysis
    print("=== Final Report ===")
    final_report = sleep_manager.optimizer.get_optimization_report()
    for key, value in final_report.items():
        if isinstance(value, float):
            print(f"{key}: {value:.6f}")
        else:
            print(f"{key}: {value}")
