import os
import sys

# Ensure UTF-8 output encoding on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    import ctypes
    try:
        h_out = ctypes.windll.kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        ctypes.windll.kernel32.GetConsoleMode(h_out, ctypes.byref(mode))
        ctypes.windll.kernel32.SetConsoleMode(h_out, mode.value | 0x0004)
    except Exception:
        pass

import re
import time
import ctypes
import threading
import argparse
import subprocess
import heapq
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

try:
    import psutil
except ImportError:
    psutil = None

try:
    import msvcrt
    HAS_MSVCRT = True
except ImportError:
    HAS_MSVCRT = False


def load_collatz_library(custom_path: str | None = None) -> ctypes.CDLL | None:
    """Loads the native compiled collatz.dll shared library if available."""
    candidates = []
    if custom_path:
        candidates.append(Path(custom_path))

    script_dir = Path(__file__).resolve().parent
    cwd = Path.cwd()

    candidates.extend([
        cwd / "collatz.dll",
        script_dir / "collatz.dll",
        cwd / "libcollatz.so",
        script_dir / "libcollatz.so",
    ])

    for p in candidates:
        if p.is_file():
            try:
                lib = ctypes.CDLL(str(p.resolve()))
                lib.collatz_steps.argtypes = [ctypes.c_uint64]
                lib.collatz_steps.restype = ctypes.c_uint64

                lib.collatz_compute_batch.argtypes = [
                    ctypes.c_uint64,
                    ctypes.c_uint64,
                    ctypes.POINTER(ctypes.c_uint64),
                ]
                lib.collatz_compute_batch.restype = None

                lib.collatz_steps_bigint.argtypes = [ctypes.c_char_p]
                lib.collatz_steps_bigint.restype = ctypes.c_uint64

                if hasattr(lib, "collatz_compute_batch_fast"):
                    lib.collatz_compute_batch_fast.argtypes = [
                        ctypes.c_uint64,
                        ctypes.c_uint64,
                        ctypes.c_uint64,
                        ctypes.POINTER(ctypes.c_uint64),
                        ctypes.POINTER(ctypes.c_uint64),
                        ctypes.c_uint64,
                        ctypes.POINTER(ctypes.c_uint64),
                        ctypes.POINTER(ctypes.c_uint64),
                        ctypes.POINTER(ctypes.c_uint64),
                        ctypes.POINTER(ctypes.c_uint64),
                        ctypes.POINTER(ctypes.c_uint64),
                    ]
                    lib.collatz_compute_batch_fast.restype = None

                if hasattr(lib, "collatz_compute_batch_bigint"):
                    lib.collatz_compute_batch_bigint.argtypes = [
                        ctypes.c_char_p,
                        ctypes.c_uint64,
                        ctypes.POINTER(ctypes.c_uint64),
                    ]
                    lib.collatz_compute_batch_bigint.restype = None
                return lib
            except Exception as e:
                print(f"[!] Warning: Found library at {p} but failed to load: {e}")

    return None


def find_executable(custom_path: str | None = None) -> Path | None:
    """Locates the CollatzConjecture executable as fallback."""
    candidates = []
    if custom_path:
        candidates.append(Path(custom_path))

    script_dir = Path(__file__).resolve().parent
    cwd = Path.cwd()

    candidates.extend([
        cwd / "CollatzConjecture.exe",
        cwd / "CollatzConjecture",
        script_dir / "CollatzConjecture.exe",
        script_dir / "CollatzConjecture",
    ])

    for p in candidates:
        if p.is_file():
            return p.resolve()

    return None


def run_single_trace(exe_path: Path, n: int) -> dict:
    """Fallback: invokes executable with `--trace <n>` and parses output."""
    try:
        proc = subprocess.run(
            [str(exe_path), "--trace", str(n)],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        output = proc.stdout
        steps_match = re.search(r"Steps(?:\s+to\s+1)?:\s*(\d+)", output)
        steps = int(steps_match.group(1)) if steps_match else 0
        return {"number": n, "steps": steps, "success": True}
    except Exception as e:
        return {"number": n, "steps": 0, "success": False, "error": str(e)}


class RealTimeCollatzRunner:
    def __init__(
        self,
        lib: ctypes.CDLL | None,
        exe_path: Path | None,
        start_num: int,
        end_num: int | None,
        csv_path: Path,
        records_csv_path: Path | None = None,
        checkpoint_path: Path | None = None,
        batch_size: int = 25000,
        csv_limit: int = 1000,
    ):
        self.lib = lib
        self.exe_path = exe_path
        self.start_num = start_num
        self.end_num = end_num
        self.csv_path = csv_path
        self.checkpoint_path = checkpoint_path or (self.csv_path.parent / "collatz_checkpoint.txt")
        self.batch_size = max(100, batch_size)
        self.csv_limit = max(1, csv_limit)

        # Threading controls
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()

        # Cumulative tracking stats (direct thread-safe integers/floats)
        self.total_processed = 0
        self.sum_steps = 0
        self.last_n = start_num
        self.last_steps = 0
        self.max_steps = 0
        self.max_num = start_num
        self.t_start = time.time()
        self.last_rate_time = time.time()
        self.last_rate_count = 0
        self.current_rate = 0.0

        # Worker thread handle
        self.worker_thread = None

        # Option A: Top highest-step records (Global Leaderboard)
        self.top_heap = []  # min-heap storing (steps, number)
        self.heap_lock = threading.Lock()
        self.last_csv_save_time = time.time()
        self.csv_dirty = False
        self.init_csv()

        # Option B: Record-breaking numbers (Historical Peak Champions - Unlimited)
        self.records_csv_path = records_csv_path or (self.csv_path.parent / "collatz_record_breakers.csv")
        self.record_breakers = []  # all historical record-breakers
        self.record_max_steps = 0
        self.records_lock = threading.Lock()
        self.last_records_save_time = time.time()
        self.records_dirty = False
        self.init_records_csv()

    def init_csv(self):
        """Initializes Option A CSV and loads existing leaderboard records if present."""
        # Load from target CSV if it exists
        if self.csv_path.exists() and self.csv_path.stat().st_size > 0:
            try:
                with open(self.csv_path, mode="r", encoding="utf-8") as f:
                    lines = f.readlines()
                for line in lines[1:]:
                    parts = line.strip().split(",")
                    if len(parts) >= 2:
                        try:
                            n = int(parts[0])
                            s = int(parts[1])
                            if len(self.top_heap) < self.csv_limit:
                                heapq.heappush(self.top_heap, (s, n))
                            elif s > self.top_heap[0][0]:
                                heapq.heappushpop(self.top_heap, (s, n))
                            if s > self.max_steps:
                                self.max_steps = s
                                self.max_num = n
                        except ValueError:
                            continue
            except Exception as e:
                print(f"[!] Warning: Error reading existing CSV leaderboard: {e}")

        # Also merge from legacy collatz_realtime_results.csv if it exists and is distinct
        legacy_path = self.csv_path.parent / "collatz_realtime_results.csv"
        if legacy_path.resolve() != self.csv_path.resolve() and legacy_path.exists() and legacy_path.stat().st_size > 0:
            try:
                with open(legacy_path, mode="r", encoding="utf-8") as f:
                    lines = f.readlines()
                for line in lines[1:]:
                    parts = line.strip().split(",")
                    if len(parts) >= 2:
                        try:
                            n = int(parts[0])
                            s = int(parts[1])
                            if len(self.top_heap) < self.csv_limit:
                                heapq.heappush(self.top_heap, (s, n))
                            elif s > self.top_heap[0][0]:
                                heapq.heappushpop(self.top_heap, (s, n))
                            if s > self.max_steps:
                                self.max_steps = s
                                self.max_num = n
                        except ValueError:
                            continue
            except Exception as e:
                pass

        # If primary CSV didn't exist, create it with header
        if not self.csv_path.exists():
            try:
                self.csv_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.csv_path, mode="w", newline="", encoding="utf-8") as f:
                    f.write("number,steps\n")
            except Exception as e:
                print(f"[!] Warning: Could not initialize CSV file: {e}")

    def init_records_csv(self):
        """Initializes Option B CSV and loads existing historical record-breakers if present."""
        if self.records_csv_path.exists() and self.records_csv_path.stat().st_size > 0:
            try:
                with open(self.records_csv_path, mode="r", encoding="utf-8") as f:
                    lines = f.readlines()
                seen = set()
                for line in lines[1:]:
                    parts = line.strip().split(",")
                    if len(parts) >= 2:
                        try:
                            n = int(parts[0])
                            s = int(parts[1])
                            if n not in seen:
                                seen.add(n)
                                self.record_breakers.append((n, s))
                            if s > self.record_max_steps:
                                self.record_max_steps = s
                            if s > self.max_steps:
                                self.max_steps = s
                                self.max_num = n
                        except ValueError:
                            continue
                self.record_breakers.sort(key=lambda item: item[0])
            except Exception as e:
                print(f"[!] Warning: Error reading existing record-breakers CSV: {e}")
        else:
            try:
                self.records_csv_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.records_csv_path, mode="w", newline="", encoding="utf-8") as f:
                    f.write("number,steps\n")
            except Exception as e:
                print(f"[!] Warning: Could not initialize record-breakers CSV file: {e}")

    def save_csv(self):
        """Saves the top highest-step records to CSV in descending order of steps."""
        with self.heap_lock:
            if not self.top_heap:
                return
            records = sorted(self.top_heap, key=lambda item: (-item[0], item[1]))
            self.csv_dirty = False

        tmp_path = self.csv_path.with_suffix(".tmp")
        try:
            with open(tmp_path, mode="w", newline="", encoding="utf-8") as f:
                f.write("number,steps\n")
                for s, n in records:
                    f.write(f"{n},{s}\n")
            os.replace(tmp_path, self.csv_path)
        except Exception:
            try:
                with open(self.csv_path, mode="w", newline="", encoding="utf-8") as f:
                    f.write("number,steps\n")
                    for s, n in records:
                        f.write(f"{n},{s}\n")
            except Exception:
                pass

    def save_records_csv(self):
        """Saves historical record-breaking numbers (Option B) to CSV in chronological order."""
        with self.records_lock:
            if not self.record_breakers:
                return
            records = list(self.record_breakers)
            self.records_dirty = False

        tmp_path = self.records_csv_path.with_suffix(".tmp")
        try:
            with open(tmp_path, mode="w", newline="", encoding="utf-8") as f:
                f.write("number,steps\n")
                for n, s in records:
                    f.write(f"{n},{s}\n")
            os.replace(tmp_path, self.records_csv_path)
        except Exception:
            try:
                with open(self.records_csv_path, mode="w", newline="", encoding="utf-8") as f:
                    f.write("number,steps\n")
                    for n, s in records:
                        f.write(f"{n},{s}\n")
            except Exception:
                pass

    def save_checkpoint(self):
        """Saves the last processed number to checkpoint file."""
        if self.last_n <= 0:
            return
        tmp_path = self.checkpoint_path.with_suffix(".tmp")
        try:
            with open(tmp_path, mode="w", encoding="utf-8") as f:
                f.write(f"{self.last_n}\n")
            os.replace(tmp_path, self.checkpoint_path)
        except Exception:
            try:
                with open(self.checkpoint_path, mode="w", encoding="utf-8") as f:
                    f.write(f"{self.last_n}\n")
            except Exception:
                pass

    def start_worker(self):
        """Launches the background computation thread."""
        self.worker_thread = threading.Thread(target=self._produce_numbers, daemon=True)
        self.worker_thread.start()

    def _produce_numbers(self):
        """High-throughput generation using native C++ DLL (or fallback subprocess)."""
        current_n = self.start_num

        if self.lib is not None:
            use_fast = hasattr(self.lib, "collatz_compute_batch_fast")
            MAX_QUAL = 65536
            qual_nums = (ctypes.c_uint64 * MAX_QUAL)()
            qual_steps = (ctypes.c_uint64 * MAX_QUAL)()
            qual_count = ctypes.c_uint64(0)
            out_sum = ctypes.c_uint64(0)
            out_max_steps = ctypes.c_uint64(0)
            out_max_num = ctypes.c_uint64(0)
            out_last_steps = ctypes.c_uint64(0)

            # Fallback buffer for legacy batch or BigInt
            c_buffer = (ctypes.c_uint64 * self.batch_size)()
            allocated_batch_size = self.batch_size

            while not self.stop_event.is_set():
                self.pause_event.wait()

                if self.end_num is not None and current_n > self.end_num:
                    break

                chunk_len = self.batch_size
                if self.end_num is not None:
                    chunk_len = min(chunk_len, self.end_num - current_n + 1)

                if chunk_len <= 0:
                    break

                U64_MAX = 18446744073709551615
                if use_fast and (current_n + chunk_len < U64_MAX):
                    with self.heap_lock:
                        heap_len = len(self.top_heap)
                        heap_min = self.top_heap[0][0] if heap_len >= self.csv_limit else 0

                    threshold = min(self.record_max_steps + 1, heap_min) if heap_len >= self.csv_limit else 0

                    self.lib.collatz_compute_batch_fast(
                        current_n,
                        chunk_len,
                        threshold,
                        qual_nums,
                        qual_steps,
                        MAX_QUAL,
                        ctypes.byref(qual_count),
                        ctypes.byref(out_sum),
                        ctypes.byref(out_max_steps),
                        ctypes.byref(out_max_num),
                        ctypes.byref(out_last_steps),
                    )

                    chunk_sum = out_sum.value
                    chunk_max_steps = out_max_steps.value
                    chunk_max_num = out_max_num.value
                    last_step_val = out_last_steps.value
                    n_qual = qual_count.value

                    self.total_processed += chunk_len
                    self.sum_steps += chunk_sum
                    self.last_n = current_n + chunk_len - 1
                    self.last_steps = last_step_val
                    if chunk_max_steps > self.max_steps:
                        self.max_steps = chunk_max_steps
                        self.max_num = chunk_max_num

                    if n_qual > 0:
                        # Option A: Global Top-1000 Leaderboard Update
                        with self.heap_lock:
                            added = False
                            for idx in range(n_qual):
                                q_num = qual_nums[idx]
                                q_step = qual_steps[idx]
                                if len(self.top_heap) < self.csv_limit:
                                    heapq.heappush(self.top_heap, (q_step, q_num))
                                    added = True
                                elif q_step > self.top_heap[0][0]:
                                    heapq.heappushpop(self.top_heap, (q_step, q_num))
                                    added = True
                            if added:
                                self.csv_dirty = True

                        # Option B: Unlimited Record-Breakers
                        if chunk_max_steps > self.record_max_steps:
                            with self.records_lock:
                                candidates = sorted(
                                    [(qual_nums[idx], qual_steps[idx]) for idx in range(n_qual)],
                                    key=lambda x: x[0]
                                )
                                for q_num, q_step in candidates:
                                    if q_step > self.record_max_steps:
                                        self.record_max_steps = q_step
                                        self.record_breakers.append((q_num, q_step))
                                        self.records_dirty = True

                else:
                    # Reallocate only if chunk_len changed (e.g. final batch before end_num)
                    if chunk_len != allocated_batch_size:
                        c_buffer = (ctypes.c_uint64 * chunk_len)()
                        allocated_batch_size = chunk_len

                    if current_n + chunk_len < U64_MAX:
                        self.lib.collatz_compute_batch(current_n, chunk_len, c_buffer)
                    else:
                        start_str = str(current_n).encode("ascii")
                        if hasattr(self.lib, "collatz_compute_batch_bigint"):
                            self.lib.collatz_compute_batch_bigint(start_str, chunk_len, c_buffer)
                        else:
                            for i in range(chunk_len):
                                c_buffer[i] = self.lib.collatz_steps_bigint(str(current_n + i).encode("ascii"))

                    steps_list = list(c_buffer)

                    chunk_max_steps = 0
                    chunk_max_num = current_n
                    chunk_sum = sum(steps_list)

                    for i, s in enumerate(steps_list):
                        if s > chunk_max_steps:
                            chunk_max_steps = s
                            chunk_max_num = current_n + i

                    # Direct thread-safe cumulative stat updates
                    self.total_processed += chunk_len
                    self.sum_steps += chunk_sum
                    self.last_n = current_n + chunk_len - 1
                    self.last_steps = steps_list[-1]
                    if chunk_max_steps > self.max_steps:
                        self.max_steps = chunk_max_steps
                        self.max_num = chunk_max_num

                    # Option A: Global Top-1000 Leaderboard Update
                    with self.heap_lock:
                        heap_len = len(self.top_heap)
                        min_thresh = self.top_heap[0][0] if heap_len >= self.csv_limit else 0
                        if heap_len < self.csv_limit or chunk_max_steps > min_thresh:
                            added = False
                            for i, s in enumerate(steps_list):
                                n = current_n + i
                                if len(self.top_heap) < self.csv_limit:
                                    heapq.heappush(self.top_heap, (s, n))
                                    added = True
                                    if len(self.top_heap) == self.csv_limit:
                                        min_thresh = self.top_heap[0][0]
                                elif s > min_thresh:
                                    heapq.heappushpop(self.top_heap, (s, n))
                                    min_thresh = self.top_heap[0][0]
                                    added = True
                            if added:
                                self.csv_dirty = True

                    # Option B: Unlimited Record-Breakers
                    if chunk_max_steps > self.record_max_steps:
                        with self.records_lock:
                            for i, s in enumerate(steps_list):
                                if s > self.record_max_steps:
                                    self.record_max_steps = s
                                    self.record_breakers.append((current_n + i, s))
                                    self.records_dirty = True

                # Periodic auto-save every 2 seconds
                now_t = time.time()
                if self.csv_dirty and (now_t - self.last_csv_save_time >= 2.0):
                    self.save_csv()
                    self.last_csv_save_time = now_t
                    self.save_checkpoint()
                if self.records_dirty and (now_t - self.last_records_save_time >= 2.0):
                    self.save_records_csv()
                    self.last_records_save_time = now_t
                    self.save_checkpoint()

                current_n += chunk_len

        else:
            # Fallback Subprocess Pool
            batch_concurrency = 16
            with ThreadPoolExecutor(max_workers=batch_concurrency) as executor:
                pending_futures = {}
                pending_results = {}
                next_to_deliver = self.start_num

                while not self.stop_event.is_set():
                    self.pause_event.wait()

                    while len(pending_futures) < batch_concurrency * 2:
                        if self.end_num is not None and current_n > self.end_num:
                            break
                        future = executor.submit(run_single_trace, self.exe_path, current_n)
                        pending_futures[future] = current_n
                        current_n += 1

                    if not pending_futures and (self.end_num is not None and next_to_deliver > self.end_num):
                        break

                    done_futures = [f for f in pending_futures if f.done()]
                    for f in done_futures:
                        n = pending_futures.pop(f)
                        pending_results[n] = f.result()

                    batch_steps = []
                    batch_start = next_to_deliver
                    while next_to_deliver in pending_results:
                        res = pending_results.pop(next_to_deliver)
                        s = res["steps"]
                        batch_steps.append(s)
                        next_to_deliver += 1

                    if batch_steps:
                        count = len(batch_steps)
                        b_sum = sum(batch_steps)
                        b_max = max(batch_steps)
                        b_max_idx = batch_steps.index(b_max)

                        self.total_processed += count
                        self.sum_steps += b_sum
                        self.last_n = next_to_deliver - 1
                        self.last_steps = batch_steps[-1]
                        if b_max > self.max_steps:
                            self.max_steps = b_max
                            self.max_num = batch_start + b_max_idx

                        with self.heap_lock:
                            min_thresh = self.top_heap[0][0] if len(self.top_heap) >= self.csv_limit else 0
                            added = False
                            for i, s in enumerate(batch_steps):
                                n = batch_start + i
                                if len(self.top_heap) < self.csv_limit:
                                    heapq.heappush(self.top_heap, (s, n))
                                    added = True
                                    if len(self.top_heap) == self.csv_limit:
                                        min_thresh = self.top_heap[0][0]
                                elif s > min_thresh:
                                    heapq.heappushpop(self.top_heap, (s, n))
                                    min_thresh = self.top_heap[0][0]
                                    added = True
                            if added:
                                self.csv_dirty = True

                        if b_max > self.record_max_steps:
                            with self.records_lock:
                                for i, s in enumerate(batch_steps):
                                    if s > self.record_max_steps:
                                        self.record_max_steps = s
                                        self.record_breakers.append((batch_start + i, s))
                                        self.records_dirty = True

                        now_t = time.time()
                        if self.csv_dirty and (now_t - self.last_csv_save_time >= 2.0):
                            self.save_csv()
                            self.last_csv_save_time = now_t
                        if self.records_dirty and (now_t - self.last_records_save_time >= 2.0):
                            self.save_records_csv()
                            self.last_records_save_time = now_t

                    if not done_futures:
                        time.sleep(0.005)

        self.save_csv()
        self.save_records_csv()
        self.save_checkpoint()

    def stop(self):
        """Stops runner, saves CSV files and checkpoint, and joins worker thread."""
        self.stop_event.set()
        self.pause_event.set()
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=1.0)
        self.save_csv()
        self.save_records_csv()
        self.save_checkpoint()

    def toggle_pause(self):
        """Pauses or resumes computation."""
        if self.pause_event.is_set():
            self.pause_event.clear()
            return False
        else:
            self.pause_event.set()
            return True


def format_seconds(secs: float) -> str:
    secs = int(secs)
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def strip_ansi(s: str) -> str:
    return re.sub(r"\033\[[0-9;?]*[a-zA-Z]", "", s)


def make_row(content: str, width: int = 74, c_border: str = "\033[1;36m") -> str:
    vis_len = len(strip_ansi(content))
    pad = max(0, width - vis_len)
    return f"{c_border}| \033[0m{content}{' ' * pad} {c_border}|\033[0m"


def format_huge_int(n: int, max_digits: int = 18) -> str:
    """Formats an arbitrary-length integer cleanly without float overflow or box wrapping."""
    s = str(n)
    if len(s) <= max_digits:
        return f"{n:,}"
    exp = len(s) - 1
    short_str = f"{s[:6]}...{s[-4:]}"
    return f"{short_str} (~{s[0]}.{s[1:3]}e+{exp})"


def start_realtime_monitor(runner: RealTimeCollatzRunner, refresh_rate: int = 10):
    """Real-time terminal dashboard with memory, process, and mathematical metrics."""
    runner.start_worker()

    process = psutil.Process() if psutil is not None else None
    sleep_time = max(0.02, 1.0 / max(1, refresh_rate))

    # ANSI Colors
    C_RESET = "\033[0m"
    C_BOLD = "\033[1m"
    C_CYAN = "\033[1;36m"
    C_GREEN = "\033[1;32m"
    C_YELLOW = "\033[1;33m"
    C_MAGENTA = "\033[1;35m"
    C_WHITE = "\033[1;37m"
    C_GRAY = "\033[90m"

    # Hide cursor and clear screen
    sys.stdout.write("\033[?25l\033[2J")
    sys.stdout.flush()

    engine_name = "Native C++ DLL (collatz.dll) [OpenMP Parallel]" if runner.lib is not None else "C++ Subprocess (.exe)"

    try:
        while not runner.stop_event.is_set():
            # Keyboard handling (Windows non-blocking)
            if HAS_MSVCRT and msvcrt.kbhit():
                key = msvcrt.getch()
                if key in (b" ", b"p", b"P"):
                    runner.toggle_pause()
                elif key in (b"s", b"S"):
                    runner.save_csv()
                    runner.save_records_csv()
                elif key in (b"q", b"Q", b"\x03"):
                    break

            now = time.time()
            dt = now - runner.last_rate_time
            if dt >= 0.4:
                dn = runner.total_processed - runner.last_rate_count
                runner.current_rate = dn / dt
                runner.last_rate_time = now
                runner.last_rate_count = runner.total_processed

            elapsed_s = max(0.001, now - runner.t_start)
            avg_speed = runner.total_processed / elapsed_s
            is_running = runner.pause_event.is_set()
            status_text = f"{C_GREEN}RUNNING{C_RESET}" if is_running else f"{C_YELLOW}PAUSED {C_RESET}"

            # Memory & process information
            rss_mb = 0.0
            vms_mb = 0.0
            cpu_pct = 0.0
            threads_count = 1
            if process is not None:
                try:
                    mem = process.memory_info()
                    rss_mb = mem.rss / (1024 * 1024)
                    vms_mb = getattr(mem, "vms", 0) / (1024 * 1024)
                    cpu_pct = process.cpu_percent(interval=None)
                    threads_count = process.num_threads()
                except Exception:
                    pass

            avg_steps = runner.sum_steps / max(1, runner.total_processed)

            with runner.heap_lock:
                top_count = len(runner.top_heap)
                top_cutoff = runner.top_heap[0][0] if runner.top_heap else 0

            with runner.records_lock:
                rec_count = len(runner.record_breakers)
                latest_rec = runner.record_breakers[-1] if runner.record_breakers else (0, 0)

            # Build fixed-width box HUD (74 interior characters width)
            w = 74
            divider_double = f"{C_CYAN}+" + "=" * (w + 2) + f"+{C_RESET}"
            divider_single = f"{C_CYAN}+" + "-" * (w + 2) + f"+{C_RESET}"

            box = [
                divider_double,
                make_row(f"{C_BOLD}{C_WHITE}{'COLLATZ CONJECTURE HIGH-SPEED ENGINE MONITOR':^{w}}", width=w),
                divider_double,
                make_row(f"{C_GRAY}Engine:{C_RESET}       {C_WHITE}{engine_name}", width=w),
                make_row(f"{C_GRAY}Status:{C_RESET}       {status_text} {C_GRAY}(Space/P: Pause | S: Save CSVs | Q: Exit){C_RESET}", width=w),
                make_row(f"{C_GRAY}Process:{C_RESET}      {C_WHITE}PID: {os.getpid()}{C_RESET}  |  {C_WHITE}CPU: {cpu_pct:4.1f}%{C_RESET}  |  {C_WHITE}Threads: {threads_count}{C_RESET}", width=w),
                make_row(f"{C_GRAY}Memory (RAM):{C_RESET} {C_MAGENTA}{rss_mb:5.1f} MB Working Set{C_RESET}  |  {C_MAGENTA}{vms_mb:5.1f} MB Commit Charge{C_RESET}", width=w),
                make_row(f"{C_GRAY}Elapsed Time:{C_RESET} {C_WHITE}{format_seconds(elapsed_s)}{C_RESET}  |  {C_GRAY}Batch Size:{C_RESET} {C_WHITE}{runner.batch_size:,} nums/batch{C_RESET}", width=w),
                divider_single,
                make_row(f"{C_BOLD}{C_YELLOW}PROGRESS & NUMERICAL STATISTICS{C_RESET}", width=w),
                make_row(f"  {C_GRAY}Current Number (n):{C_RESET}     {C_WHITE}{C_BOLD}{format_huge_int(runner.last_n)}{C_RESET}", width=w),
                make_row(f"  {C_GRAY}Last Stopping Time:{C_RESET}     {C_WHITE}{runner.last_steps} steps{C_RESET}", width=w),
                make_row(f"  {C_GRAY}Total Evaluated:{C_RESET}        {C_WHITE}{runner.total_processed:,} numbers{C_RESET}", width=w),
                make_row(f"  {C_GRAY}Average Steps to 1:{C_RESET}     {C_WHITE}{avg_steps:.2f} steps{C_RESET}", width=w),
                make_row(f"  {C_GRAY}Throughput Speed:{C_RESET}       {C_GREEN}{C_BOLD}{runner.current_rate:,.0f} nums/sec{C_RESET} {C_GRAY}(Overall Avg: {avg_speed:,.0f}){C_RESET}", width=w),
                divider_single,
                make_row(f"{C_BOLD}{C_YELLOW}RECORDS & MILESTONES{C_RESET}", width=w),
                make_row(f"  {C_GRAY}All-Time Peak Steps:{C_RESET}    {C_YELLOW}{C_BOLD}{runner.max_steps} steps{C_RESET} {C_GRAY}(at n = {format_huge_int(runner.max_num)}){C_RESET}", width=w),
                make_row(f"  {C_GRAY}Record-Breakers (Opt B):{C_RESET}{C_GREEN}{C_BOLD} {rec_count:,} milestones found{C_RESET} {C_GRAY}(unlimited){C_RESET}", width=w),
                make_row(f"  {C_GRAY}Latest Milestone:{C_RESET}       {C_WHITE}n = {format_huge_int(latest_rec[0])} -> {latest_rec[1]} steps{C_RESET}", width=w),
                make_row(f"  {C_GRAY}Top 1k Cutoff (Opt A):{C_RESET}  {C_WHITE}>= {top_cutoff} steps{C_RESET} {C_GRAY}(min steps for Top 1,000){C_RESET}", width=w),
                divider_single,
                make_row(f"{C_BOLD}{C_YELLOW}CSV PERSISTENCE & STORAGE{C_RESET}", width=w),
                make_row(f"  {C_GRAY}Option A (Top 1K):{C_RESET}      {C_WHITE}{top_count:,} / {runner.csv_limit:,} records -> {runner.csv_path.name}{C_RESET}", width=w),
                make_row(f"  {C_GRAY}Option B (Milestones):{C_RESET}  {C_WHITE}{rec_count:,} records (all)      -> {runner.records_csv_path.name}{C_RESET}", width=w),
                make_row(f"  {C_GRAY}Auto-Save Interval:{C_RESET}     {C_GREEN}Active (every 2.0s){C_RESET}", width=w),
                divider_double
            ]

            # In-place overwrite with \033[H and \033[K
            sys.stdout.write("\033[H" + "\n".join(line + "\033[K" for line in box) + "\n")
            sys.stdout.flush()

            if runner.end_num is not None and runner.last_n >= runner.end_num:
                break

            time.sleep(sleep_time)

    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h\n")
        sys.stdout.flush()
        runner.stop()
        print(f"\n[+] Monitor stopped. Successfully saved:")
        print(f"    - Option A (Top Leaderboard): {len(runner.top_heap):,} records -> {runner.csv_path.name}")
        print(f"    - Option B (Record-Breakers): {len(runner.record_breakers):,} records -> {runner.records_csv_path.name}")
        print(f"    - Checkpoint:                 Last n = {format_huge_int(runner.last_n, max_digits=24)} -> {runner.checkpoint_path.name}\n")


def main():
    parser = argparse.ArgumentParser(
        description="High-Speed Real-Time Collatz Conjecture Runner & Detailed Terminal Monitor."
    )
    parser.add_argument(
        "--start",
        type=int,
        default=1,
        help="Starting whole number (default: 1)"
    )
    parser.add_argument(
        "--end",
        type=int,
        default=None,
        help="Ending whole number (default: None / runs continuously)"
    )
    parser.add_argument(
        "--dll",
        type=str,
        default=None,
        help="Path to collatz.dll (default: auto-detect)"
    )
    parser.add_argument(
        "--exe",
        type=str,
        default=None,
        help="Path to CollatzConjecture executable fallback (default: auto-detect)"
    )
    parser.add_argument(
        "--csv",
        type=str,
        default="collatz_top_steps.csv",
        help="Path for Option A top-records CSV output (default: collatz_top_steps.csv)"
    )
    parser.add_argument(
        "--records-csv",
        type=str,
        default="collatz_record_breakers.csv",
        help="Path for Option B record-breakers CSV output (default: collatz_record_breakers.csv)"
    )
    parser.add_argument(
        "--csv-limit",
        type=int,
        default=1000,
        help="Maximum number of highest-step records to retain in Option A CSV (default: 1000)"
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume computation from the last saved checkpoint"
    )
    parser.add_argument(
        "--checkpoint",
        type=str,
        default="collatz_checkpoint.txt",
        help="Path for checkpoint file (default: collatz_checkpoint.txt)"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100000,
        help="In-memory calculation batch size (default: 100000)"
    )
    parser.add_argument(
        "--refresh-rate",
        type=int,
        default=10,
        help="Terminal dashboard update rate in Hz (default: 10)"
    )

    args = parser.parse_args()

    csv_path = Path(args.csv).resolve()
    records_csv_path = Path(args.records_csv).resolve()
    checkpoint_path = Path(args.checkpoint).resolve()

    # Handle automatic resume
    if args.resume:
        resumed_n = None
        if checkpoint_path.is_file() and checkpoint_path.stat().st_size > 0:
            try:
                with open(checkpoint_path, "r", encoding="utf-8") as f:
                    val = int(f.read().strip())
                    if val > 0:
                        resumed_n = val + 1
            except Exception:
                pass
        if resumed_n is None and records_csv_path.is_file() and records_csv_path.stat().st_size > 0:
            try:
                with open(records_csv_path, "r", encoding="utf-8") as f:
                    lines = [l.strip() for l in f if l.strip()]
                    if len(lines) > 1:
                        last_line_n = int(lines[-1].split(",")[0])
                        if last_line_n > 0:
                            resumed_n = last_line_n + 1
            except Exception:
                pass

        if resumed_n is not None:
            args.start = resumed_n
            print(f"[+] Resuming execution: starting at whole number {args.start:,}")
        else:
            print("[*] No existing checkpoint found. Starting from beginning.")

    if args.start < 1:
        print("Error: Starting whole number must be >= 1.")
        sys.exit(1)
    if args.end is not None and args.end < args.start:
        print(f"Error: Ending number ({args.end}) must be >= starting number ({args.start}).")
        sys.exit(1)
    if args.csv_limit < 1:
        print("Error: CSV limit must be >= 1.")
        sys.exit(1)

    lib = load_collatz_library(args.dll)
    exe_path = None

    if lib is not None:
        print("[+] Engine: High-Performance In-Memory C++ DLL (collatz.dll) loaded successfully!")
    else:
        print("[!] collatz.dll not found, checking executable fallback...")
        exe_path = find_executable(args.exe)
        if exe_path is None:
            print("Error: Neither collatz.dll nor CollatzConjecture.exe could be found.")
            sys.exit(1)
        print(f"[+] Fallback executable found: {exe_path}")

    print(f"[*] Option A CSV (Top):     {csv_path} (limit: {args.csv_limit:,})")
    print(f"[*] Option B CSV (Records): {records_csv_path} (unlimited record-breakers)")
    print(f"[*] Checkpoint File:        {checkpoint_path}")
    print(f"[*] Range:                  {args.start} -> {'Infinity' if args.end is None else f'{args.end:,}'}")
    print(f"[*] Batch Size:             {args.batch_size:,}")
    print("[*] Launching Real-Time Monitor...\n")

    runner = RealTimeCollatzRunner(
        lib=lib,
        exe_path=exe_path,
        start_num=args.start,
        end_num=args.end,
        csv_path=csv_path,
        records_csv_path=records_csv_path,
        checkpoint_path=checkpoint_path,
        batch_size=args.batch_size,
        csv_limit=args.csv_limit
    )

    start_realtime_monitor(runner, refresh_rate=args.refresh_rate)


if __name__ == "__main__":
    main()
