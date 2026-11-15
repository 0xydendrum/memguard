# Notes and gotchas

Things learned while building spillsafe. Each one cost real time to find.

## The setting ID often quoted for "Sysmem Fallback" is wrong

The NVIDIA driver setting "CUDA - Sysmem Fallback Policy" has the hidden ID **0x10ECECC9** (0 = driver default, falls
back to RAM; 1 = prefer no fallback, allocations fail; 2 = prefer fallback). An ID sometimes quoted for it,
0x10ECDB82, is actually "Antialiasing - Behavior Flags": writing it would quietly change an antialiasing flag for every
game. The real ID comes from nvidiaProfileInspector's `CustomSettingNames.xml` and from users who found it by flipping
the Control Panel switch.

**Rule:** look a driver or registry ID up in the API's own name table or a primary source before writing it.

## "Not set" from GetSetting doesn't prove a hidden setting is at its default

`NvAPI_DRS_GetSetting` returns `SETTING_NOT_FOUND` for this hidden ID, which could mean "default" or "the API can't see
hidden IDs". `NvAPI_DRS_EnumSettings` does return hidden settings, which is how `spillsafe-sysmem` reads it. A runtime
test confirmed the result: allocating 14 GiB on a 12 GiB card succeeded.

## NVAPI can't write the hidden setting

On driver 621.14, `NvAPI_DRS_SetSetting` refuses 0x10ECECC9 on every profile while accepting public IDs. To change the
policy, use NVIDIA Control Panel > Manage 3D settings > CUDA - Sysmem Fallback Policy.

## The "GPU out of memory" was system RAM

The crash that started spillsafe looked like a VRAM problem. It was RAM: llama-server held 18 GB (a memory-mapped copy
of the weights plus an 8 GB prompt cache) while the model ran fully on the GPU. `--cache-ram 0 --load-mode none` brought
that down. Spilling makes RAM pressure worse, not better, which is why spillsafe budgets both as one pool.

## A child with a new hidden console loses its output

When spillsafe runs without a console (started from another tool, or orphaned), console programs such as Git Bash fail
at start with 0xC0000142, so spillsafe gives the child a hidden console of its own. That console brings its own standard
handles, so without passing stdin/stdout/stderr explicitly the job's output disappears and its last `echo` fails, making
a successful job look like it exited 1.

**Rule:** when you change a child's console flags, pass the std handles explicitly and test from a console-less parent.

## Assign the job object before the child runs

spillsafe starts the child suspended, puts it in the kill-on-close job object, then resumes it. Assigning after the child
starts leaves a window where it can spawn grandchildren outside the job.

## Sleep during a GPU job can hang the PC

An idle sleep transition while the GPU was busy hung one machine hard enough to need a power-off. spillsafe calls
`SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` while a job runs (the display can still turn off).

## Memory isn't the only shared resource

A CPU-only job started outside spillsafe saturated every core, and a GPU model server under spillsafe dropped from about
17 to under 3 tokens/s per slot until it finished. Run anything compute-heavy through spillsafe, or cap its threads
(`OMP_NUM_THREADS=2`).

## Killing the shell doesn't kill spillsafe

Killing the shell that started spillsafe can leave spillsafe itself running, holding its queue place. That's usually what
you want for long jobs. To stop a job, stop the spillsafe process: the job object takes the job's whole tree with it.
