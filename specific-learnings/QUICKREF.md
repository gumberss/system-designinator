# Quick Reference Guide

## File Map

```
specific-learnings/
├─ README.md ............................ Repository overview
├─ INDEX.md ............................. Complete guide (THIS IS BEST!)
│
└─ learning-while-sleeping/
   ├─ GETTING_STARTED.md ................ Start here! (5 min read)
   ├─ summary.md ........................ Key concepts (10 min read)
   ├─ resources.md ...................... References and papers
   │
   ├─ transcripts/
   │  └─ transcript.txt ................. Full video (30 min read)
   │
   └─ implementations/
      ├─ README.md ...................... How to run scripts
      ├─ memory_replay_and_dreaming.py .. Core mechanisms
      ├─ sleep_based_optimization.py .... Model optimization
      └─ complete_system_example.py ..... Full demo (START HERE!)
```

## What Each File Does

### Documentation
- **README.md** - What is this repository?
- **INDEX.md** - Complete learning guide
- **GETTING_STARTED.md** - Fast track to understanding
- **summary.md** - Key concepts at a glance
- **resources.md** - Papers and external links
- **transcript.txt** - Everything the video said

### Python Code
| File | What It Does | Run Time |
|------|-------------|----------|
| complete_system_example.py | Shows everything together | 5 sec |
| memory_replay_and_dreaming.py | Shows memory mechanisms | 2 sec |
| sleep_based_optimization.py | Shows model optimization | 3 sec |

## How to Use (3 Levels)

### Level 1: Quick Overview (15 minutes)
```bash
1. Read: GETTING_STARTED.md (5 min)
2. Run: python complete_system_example.py (5 min)
3. Read: summary.md (5 min)
```

### Level 2: Deep Dive (2 hours)
```bash
1. Do Level 1
2. Study: memory_replay_and_dreaming.py (30 min)
3. Study: sleep_based_optimization.py (30 min)
4. Read: transcript.txt sections (30 min)
5. Modify code examples (30 min)
```

### Level 3: Expert (Full day)
```bash
1. Do Level 2
2. Read: Full transcript.txt (30 min)
3. Research: Papers in resources.md (2 hours)
4. Integrate: With your own project (rest of day)
```

## Common Tasks

### "I want to understand the concept"
→ Read `summary.md` (10 min)

### "Show me how it works"
→ Run `python complete_system_example.py` (2 min)

### "I need detailed code"
→ Study `implementations/` folder files (1-2 hours)

### "I need to watch/read the full video"
→ Review `transcripts/transcript.txt` (30 min)

### "I want background research"
→ Check `resources.md` for papers

### "I want to modify the code"
→ Read `implementations/README.md` first

## Key Concepts Summary

### What is Dreaming in AI?
- Systems replaying past experiences during idle time
- Like humans reviewing memories while sleeping
- Strengthens important patterns

### Why is it Important?
- Better learning with less data
- Improved memory retention
- Efficient use of idle computing power
- Inspired by biological sleep

### Where Can I Use This?
- Reinforcement learning (DQN, PPO)
- Continual learning systems
- Model optimization
- Knowledge distillation

## Running the Code

### Quick Test
```bash
cd learning-while-sleeping/implementations
python complete_system_example.py
```

### All Examples
```bash
cd learning-while-sleeping/implementations
python complete_system_example.py      # Full system
python memory_replay_and_dreaming.py   # Core mechanisms
python sleep_based_optimization.py     # Optimization
```

### With Output Capture
```bash
cd learning-while-sleeping/implementations
python complete_system_example.py > output.txt
```

## Troubleshooting

### "ModuleNotFoundError: numpy"
```bash
pip install numpy
```

### "Text encoding error"
```bash
python -X utf8 script.py
```

### "Script runs but I don't understand output"
→ Read `implementations/README.md` first

### "I want to modify the code"
→ All files are well-commented
→ See `implementations/README.md` for ideas

## Best Practices

- ✓ Start with GETTING_STARTED.md
- ✓ Run complete_system_example.py first
- ✓ Read comments in code
- ✓ Study one file at a time
- ✓ Modify and experiment

## Time Estimates

| Task | Time |
|------|------|
| Read README | 5 min |
| Read GETTING_STARTED | 5 min |
| Read summary | 10 min |
| Run complete example | 2 min |
| Study one implementation | 30 min |
| Read full transcript | 30 min |
| Integrate with own project | 2-4 hours |

## File Sizes

| File | Size | Read Time |
|------|------|-----------|
| GETTING_STARTED.md | ~5 KB | 5 min |
| summary.md | ~2 KB | 10 min |
| resources.md | ~1.5 KB | 5 min |
| transcript.txt | ~30 KB | 30 min |
| memory_replay*.py | ~8.7 KB | 30 min study |
| sleep_based*.py | ~9.4 KB | 30 min study |
| complete_system*.py | ~8.2 KB | 30 min study |

## Navigation Tips

### Starting Fresh?
1. GETTING_STARTED.md
2. complete_system_example.py (run it)
3. summary.md

### Want Deep Dive?
1. summary.md
2. Each implementation file
3. Full transcript

### Need Reference?
1. This file (quickref.md)
2. INDEX.md
3. specific-learnings/README.md

## Quick Links

- **Video:** https://www.youtube.com/watch?v=tTcxVv8HHNw
- **Concept Overview:** summary.md
- **Getting Started:** GETTING_STARTED.md
- **Code Guide:** implementations/README.md
- **External Resources:** resources.md

---

**Last Updated:** 2026-11-12  
**Status:** Complete and tested ✓  
**Language:** English  
**Technology:** Python 3.8+ with NumPy
