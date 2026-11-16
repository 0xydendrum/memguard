# spillsafe

**Run heavy local AI jobs on Windows without running out of memory. Queues jobs and starts each one only when RAM + VRAM can really hold it, counting the VRAM that NVIDIA quietly spills into RAM. By 0xydendrum.**

spillsafe is a small command-line tool that you put in front of any heavy command: a model eval, a training run, a
benchmark, a big download. It holds the job back until your PC really has room for it, runs heavy jobs one at a time in
the order they arrived, and cleans up completely when each one finishes.

```
spillsafe --ram 2 --vram 10 -- python eval.py
```

In plain words: *"This job needs about 2 GiB of RAM and 10 GiB of graphics memory. Start it when there's room."*

> Created by **0xydendrum**. Free for noncommercial use under the PolyForm Noncommercial License 1.0.0.

## The problem it solves

Two kinds of memory matter for AI work on a PC:

- **VRAM**, the fast memory on your graphics card. This is where a model wants to live.
- **RAM**, the ordinary system memory that everything else shares: Windows, your browser, every other program.

You might expect a program that asks for more VRAM than your card has to stop with an "out of memory" error. On Windows
with an NVIDIA card, it doesn't. The driver quietly puts the part that doesn't fit into RAM instead (NVIDIA calls this
"CUDA sysmem fallback", and it has been on by default since driver 536.40). The job keeps running, about 10 times
slower, and it is now using RAM that no other program knows about.

That's how a PC that looks fine falls over. You start two jobs that would each fit on their own. The second one doesn't
fit in VRAM, so its overflow spills into RAM. RAM runs out, and Windows starts killing programs or freezes altogether.
Hours of work are lost. Tools that only watch VRAM never see it coming, because VRAM was never the problem.

spillsafe was built after exactly that happened: four overnight jobs were killed when free RAM dropped to about 5 GiB.
The graphics card had room to spare. A model server was holding 18 GB of RAM.

## How it works

1. **You say roughly what the job needs**, with `--ram` and `--vram` in GiB. Rough guesses are fine: spillsafe reports
   the real peak afterwards so you can tune them.
2. **The job waits in line.** Heavy jobs run one at a time, first come first served. There is one line for the whole
   PC, so jobs started from different terminals, SSH sessions or scripts all wait in the same queue. If a job crashes,
   its place is released automatically.
3. **It waits for memory.** When it's the job's turn, spillsafe works out what the job will really cost: its RAM, plus
   whatever part of its VRAM won't fit on the card, because that part is going to land in RAM. The job starts only when
   that total fits and still leaves a safety margin free (6 GiB by default) so the desktop stays responsive. Until then
   it checks every 10 seconds and tells you what it's waiting for. A job that could never fit on this PC is refused
   straight away instead of waiting forever.

   The rule, exactly: `free RAM - reserve >= job RAM + max(0, job VRAM - free VRAM)`.
4. **It runs the job and cleans up after it.** The job, and every process it starts, runs inside a Windows *job
   object*: a container that Windows closes as a whole. Close the terminal, press Ctrl+C or end spillsafe in Task
   Manager, and everything the job started goes with it. Nothing is left behind holding memory.
5. **It tells you what happened**: how long the job ran, how it exited, its peak memory use and the lowest free RAM seen
   along the way. It also stops the PC from going to sleep in the middle of a job.

## Who it's for

Anyone running AI locally on a single Windows PC: people using llama.cpp, Ollama, ComfyUI, fine-tuning scripts or eval
suites, and AI coding agents that start heavy jobs on their own. These tools share one machine without knowing about
each other. spillsafe makes them take turns.

It works best with an NVIDIA card, but doesn't need one: without a GPU it still budgets RAM and runs the queue.

## Install

Requires Windows and Python 3.9+. No dependencies. NVIDIA GPUs are read through `nvidia-smi`; without one, spillsafe
still budgets RAM.

```
pipx install git+https://github.com/0xydendrum/spillsafe
```

Or from a clone: `pip install .`, or run it without installing as `python -m spillsafe`.

## Usage

```
spillsafe [--ram GiB] [--vram GiB] [--reserve GiB] [--lock NAME | --no-lock] [--max-wait MIN]
         [--no-spill] [--no-kill-tree] -- command args...
spillsafe --check [--ram GiB] [--vram GiB] [--max-wait MIN]    # exit 0 if it fits now; takes no lock
spillsafe --status                                             # memory, lock holder, queue
spillsafe-sysmem                                               # shows the driver's sysmem fallback setting
```

`--ram` and `--vram` are what the job will **add** on top of current use. Rough numbers are fine; err high.

| Example | What happens |
|---|---|
| `spillsafe --ram 2 --vram 10 -- python bench.py` | waits for the queue and for memory, then runs |
| `spillsafe --no-lock --ram 1 -- curl -O https://...` | waits for memory only, doesn't block other jobs |
| `spillsafe --max-wait 30 --vram 18 -- python train.py` | gives up with exit code 3 after 30 minutes |
| `spillsafe --no-spill --vram 10 -- ...` | requires the VRAM to really be free (no spilling) |

**Exit codes:** the job's own; `3` if memory never fitted within `--max-wait` or the job can never fit on this PC;
`2` for usage errors. `130` on Ctrl+C.

**Long-running servers** (llama-server, Ollama) should not run *under* spillsafe: when spillsafe exits, the job object
stops everything in it. Use `spillsafe --check --vram 14 && llama-server ...` instead.

**Nested calls** are safe: a job that already holds the lock can call spillsafe again and runs straight through.

### Worked example

`spillsafe --ram 2 --vram 10 -- python bench.py` with 7 GiB of VRAM free: 3 GiB will spill into RAM, so the job really
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
| `SPILLSAFE_STATE` | `%LOCALAPPDATA%\spillsafe` | where the lock and queue files live. Every spillsafe that should share a queue must use the same folder |



## Limitations

- Windows only (job objects, file locks, and the driver behaviour it budgets for are Windows-specific).
- Reads GPU 0 only.
- Budgets memory, not CPU. A CPU-heavy job started outside spillsafe can still slow a GPU job down; run it through
  spillsafe too, or cap its threads.
- `--ram`/`--vram` are your estimates. spillsafe reports the real peak afterwards so you can tune them.

More detail and the gotchas found while building it: [docs/NOTES.md](docs/NOTES.md).

## Credits

spillsafe was created by **0xydendrum**.

## License

[PolyForm Noncommercial License 1.0.0](LICENSE). Copyright (c) 2026 0xydendrum.

- **Free** for personal use, hobby projects, research, education, charities and government.
- **You may modify it and share your reworks** for free, as long as every copy keeps the license and this line:
  `Required Notice: Copyright (c) 2026 0xydendrum (https://github.com/0xydendrum/spillsafe)`
- **No commercial use.** You may not sell spillsafe or reworks of it, or use it for commercial purposes.
  For commercial licensing, contact 0xydendrum.
