# memguard

**Run heavy local AI jobs one at a time, and only when your PC has the memory for them.**
A small Windows command-line wrapper for model evals, training runs, benchmarks and big downloads. Instead of crashing
the machine or getting killed halfway through, a job waits its turn until RAM and VRAM can hold it.

> Created by **0xydendrum**. Free for noncommercial use under the PolyForm Noncommercial License 1.0.0.

```
memguard --ram 2 --vram 10 -- python eval.py
```

## Why

On Windows, when a CUDA program asks for more VRAM than the card has, the NVIDIA driver does not fail. It quietly moves
the overflow into system RAM ("CUDA sysmem fallback", on by default since driver 536.40), where it runs about 10x slower.
So a VRAM shortfall turns into **RAM** pressure, and tools that only watch VRAM miss it. Start two jobs that each fit on
their own and the PC runs out of RAM: background jobs get killed, or the whole machine stalls.

memguard was written after exactly that: four overnight jobs were killed when free RAM dropped to about 5 GiB. The GPU
was fine. A model server was holding 18 GB of RAM.

## What it does

- **Budgets RAM and VRAM as one pool.** A job starts only when
  `free RAM - reserve >= job RAM + max(0, job VRAM - free VRAM)`. Whatever doesn't fit in VRAM is counted against RAM,
  because that's where the driver will put it.
- **Runs heavy jobs one at a time, first come first served.** A machine-wide queue built on Windows file locks works
  across terminals, SSH sessions and admin windows. If a holder dies, Windows releases the lock automatically.
- **Leaves no orphans.** The job runs inside a Windows job object, so if memguard is killed (closed terminal, Ctrl+C,
  Task Manager) the whole process tree dies with it and nothing keeps holding memory after the lock is gone.
- **Reports what happened.** Runtime, exit code, peak committed memory and the lowest free RAM seen. It warns if free
  RAM drops below half the reserve, and it keeps the PC from sleeping while a job runs.

## Install

Requires Windows and Python 3.9+. No dependencies. NVIDIA GPUs are read through `nvidia-smi`; without one, memguard
still budgets RAM.

```
pipx install git+https://github.com/0xydendrum/memguard
```

Or from a clone: `pip install .`, or run it without installing as `python -m memguard`.

## Usage

```
memguard [--ram GiB] [--vram GiB] [--reserve GiB] [--lock NAME | --no-lock] [--max-wait MIN]
         [--no-spill] [--no-kill-tree] -- command args...
memguard --check [--ram GiB] [--vram GiB] [--max-wait MIN]    # exit 0 if it fits now; takes no lock
memguard --status                                             # memory, lock holder, queue
memguard-sysmem                                               # shows the driver's sysmem fallback setting
```

`--ram` and `--vram` are what the job will **add** on top of current use. Rough numbers are fine; err high.

| Example | What happens |
|---|---|
| `memguard --ram 2 --vram 10 -- python bench.py` | waits for the queue and for memory, then runs |
| `memguard --no-lock --ram 1 -- curl -O https://...` | waits for memory only, doesn't block other jobs |
| `memguard --max-wait 30 --vram 18 -- python train.py` | gives up with exit code 3 after 30 minutes |
| `memguard --no-spill --vram 10 -- ...` | requires the VRAM to really be free (no spilling) |

**Exit codes:** the job's own; `3` if memory never fitted within `--max-wait` or the job can never fit on this PC;
`2` for usage errors. `130` on Ctrl+C.

**Long-running servers** (llama-server, Ollama) should not run *under* memguard: when memguard exits, the job object
stops everything in it. Use `memguard --check --vram 14 && llama-server ...` instead.

**Nested calls** are safe: a job that already holds the lock can call memguard again and runs straight through.

### Worked example

`memguard --ram 2 --vram 10 -- python bench.py` with 7 GiB of VRAM free: 3 GiB will spill into RAM, so the job really
needs 2 + 3 = 5 GiB of RAM.

- 20 GiB of RAM free: room is 20 - 6 (reserve) = 14, which covers 5, so it starts and warns that the GPU part runs slower.
- 10 GiB free: room is 4, short of 5, so it waits, checking every 10 seconds and saying why once a minute.
- A job that can never fit (more than all your RAM, or a spill over half your RAM, which is the Windows cap on shared GPU
  memory) is refused straight away instead of waiting forever.

### Configuration

| Setting | Default | Meaning |
|---|---|---|
| `--reserve` | 6 GiB | RAM that must stay free so the desktop and other apps stay responsive |
| `--lock` | `heavy` | queue name; jobs with different names don't wait for each other |
| `MEMGUARD_STATE` | `%LOCALAPPDATA%\memguard` | where the lock and queue files live. Every memguard that should share a queue must use the same folder |



## Limitations

- Windows only (job objects, file locks, and the driver behaviour it budgets for are Windows-specific).
- Reads GPU 0 only.
- Budgets memory, not CPU. A CPU-heavy job started outside memguard can still slow a GPU job down; run it through
  memguard too, or cap its threads.
- `--ram`/`--vram` are your estimates. memguard reports the real peak afterwards so you can tune them.

More detail and the gotchas found while building it: [docs/NOTES.md](docs/NOTES.md).

## Credits

memguard was created by **0xydendrum**.

## License

[PolyForm Noncommercial License 1.0.0](LICENSE). Copyright (c) 2026 0xydendrum.

- **Free** for personal use, hobby projects, research, education, charities and government.
- **You may modify it and share your reworks** for free, as long as every copy keeps the license and this line:
  `Required Notice: Copyright (c) 2026 0xydendrum (https://github.com/0xydendrum/memguard)`
- **No commercial use.** You may not sell memguard or reworks of it, or use it for commercial purposes.
  For commercial licensing, contact 0xydendrum.
