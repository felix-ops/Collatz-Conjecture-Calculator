# Collatz Conjecture High-Speed Engine & Real-Time Monitor

A high-performance, multithreaded engine and live terminal monitor for exploring the **Collatz Conjecture** ($3n + 1$ problem). It combines an optimized C++ OpenMP shared library (`collatz.dll`) capable of evaluating **tens of millions of numbers per second** with a Python terminal HUD dashboard, state checkpointing, and dual CSV persistence.
<img width="1057" height="672" alt="Screenshot 2026-09-22 163346" src="https://github.com/user-attachments/assets/9ec47184-856a-47a9-8f84-781f42eed0ea" />

---

## Features

- **Blazing Fast Native Computation**: C++20 engine with OpenMP multi-threading, bitwise trailing zero pruning (`std::countr_zero`), and arbitrary-precision `BigInt` support for numbers $> 2^{64}-1$.
- **Real-Time Terminal Dashboard**: In-place, flicker-free ANSI console monitor reporting live throughput, progress, CPU %, RAM (Working Set & Commit), and records.
- **Zero Memory Leaks**: Memory footprint stays flat and capped at ~48 MB regardless of how long it runs.
- **Dual CSV Persistence (No Overwrite / Merged State)**:
  - **`collatz_top_steps.csv` (Option A)**: Maintains the **Top 1,000** whole numbers with the highest stopping times (leaderboard sorted by steps descending).
  - **`collatz_record_breakers.csv` (Option B)**: Stores **all historical milestone record-breakers** ($n$ and steps strictly increasing) in chronological order without limits.
  - Periodic atomic auto-saves every 2 seconds and on graceful exit.
  - Automatically loads existing CSV data on startup so previous discoveries and peak records are never lost when resuming.
- **Automatic Checkpointing & Resuming**:
  - Automatically tracks the last processed number in `collatz_checkpoint.txt`.
  - Resume anytime seamlessly without duplicate calculations or breaking previous CSV records.
- **Interactive Console Controls**: Non-blocking keyboard hotkeys for pausing, resuming, force-saving, and exiting.

---

## Requirements

### Python
- Python 3.10 or newer.
- Install dependencies via:
  ```bash
  pip install -r requirements.txt
  ```
  *(Dependencies: `psutil` for live RAM/CPU metrics).*

### C++ Compiler
- GCC / G++ (MinGW-w64) with OpenMP and C++20 support.

---

## C++ Shared Library & Compilation

The native C++ acceleration engine is located in the `cpp/` directory:
- **`cpp/collatz_dll.cpp`**: Main shared library source with OpenMP parallel batching.
- **`cpp/BigInt.hpp`**: Header-only arbitrary-precision integer library.

### GCC / G++ (MinGW-w64)
From the project root directory, run (optimized with native CPU instructions):
```bash
g++ -O3 -shared -fopenmp -std=c++20 -march=native -mbmi -mbmi2 cpp/collatz_dll.cpp -o collatz.dll
```

*(Alternatively, if running from inside the `cpp/` folder):*
```bash
g++ -O3 -shared -fopenmp -std=c++20 -march=native -mbmi -mbmi2 collatz_dll.cpp -o ../collatz.dll
```

### MSVC (Visual Studio)
From the project root directory:
```cmd
cl /O2 /Oi /Ot /openmp /std:c++20 /LD cpp/collatz_dll.cpp /Fe:collatz.dll
```

---

## Execution Commands

### 1. One-Click Batch Files (Windows)
- **`run.bat`**: Automatically checks for an existing checkpoint. If found, it resumes from where you left off; otherwise, it starts from $n = 1$.
- **`resume.bat`**: Explicitly resumes execution from the last evaluated number in `collatz_checkpoint.txt`.

### 2. Python CLI Commands

#### Starting from 1
```bash
py collatz_realtime_plotter.py --start 1
```

#### Resuming from Last Progress Automatically
```bash
py collatz_realtime_plotter.py --resume
```

#### Starting from a Specific Custom Number
You can start from any custom number (e.g., $100,000,000$). The engine automatically loads previous peak records and merges new leaderboard entries safely:
```bash
py collatz_realtime_plotter.py --start 100000000
```

#### Custom Range Execution
Run a bounded range (e.g., from 1 to 10,000,000):
```bash
py collatz_realtime_plotter.py --start 1 --end 10000000
```

#### Tuning Performance & Leaderboard Size
```bash
# Adjust batch size and customize the Top Leaderboard limit (e.g., top 5,000)
py collatz_realtime_plotter.py --start 1 --batch-size 50000 --csv-limit 5000

# Specify custom CSV output paths
py collatz_realtime_plotter.py --start 1 --csv my_top_records.csv --records-csv my_milestones.csv

# Adjust terminal refresh rate (default: 10 Hz)
py collatz_realtime_plotter.py --start 1 --refresh-rate 5
```

---

## Interactive Controls

While the terminal dashboard is running, you can press:
- `[Space]` or `[P]`: **Pause / Resume** computation.
- `[S]`: **Force Save** both CSV files and checkpoint immediately.
- `[Q]` or `[Ctrl+C]`: **Save and Exit** cleanly.

---

## CLI Options Reference

| Argument | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `--start` | `int` | `1` | Starting whole number ($n \ge 1$) |
| `--end` | `int` | `None` | Ending whole number (runs continuously if omitted) |
| `--resume` | `flag` | `False` | Resume execution from the last saved checkpoint |
| `--checkpoint` | `str` | `collatz_checkpoint.txt` | Path for progress checkpoint file |
| `--dll` | `str` | `None` | Custom path to `collatz.dll` (auto-detects if omitted) |
| `--exe` | `str` | `None` | Custom path to fallback executable |
| `--csv` | `str` | `collatz_top_steps.csv` | Output file for Option A Top Leaderboard |
| `--records-csv` | `str` | `collatz_record_breakers.csv` | Output file for Option B Record-Breakers |
| `--csv-limit` | `int` | `1000` | Max records retained in Option A Leaderboard |
| `--batch-size` | `int` | `25000` | In-memory OpenMP calculation batch size |
| `--refresh-rate` | `int` | `10` | Terminal dashboard update frequency in Hz |
