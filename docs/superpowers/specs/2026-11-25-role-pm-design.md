# Role PMs (2026-11-25)

A PM is a name the user gives a role (`backend`, `docs`, ...). It owns no folders. Every job belongs to one PM and has one path, which the PM finds while planning unless the user gave it.

## User decisions

- Only role PMs. No PM is created automatically.
- For every new job the orchestrator asks the user to pick a PM: all PMs plus "new PM". Recommended: the PM the user named, else the PM whose `places` holds the path the user said, else none. It never routes by itself.
- A job has exactly one path. Work on several paths is split into one job per path.
- The wiki is per PM only: `<wiki_path>/<pm name>/`, no tags and no split inside. Old per-repository wikis are not read.
- Removing a PM that has open or held jobs: the orchestrator asks the user, then the jobs are cancelled and the PM is removed.
- No backup of a direct job's originals.
- A direct job (a folder that is not a git repository, `~` included) never writes into a git repository nested in its folder, nor through a link whose real path leaves the folder.
- The company PC uses herdr; bg stays supported.

## Defaults chosen without asking

- PM names: `^[a-z][a-z0-9-]{0,23}$`, not `orchestrator`, not ending in `-w<digits>` or `-rv<digits>` (child names), unique. The PM's agent name is the PM name. A herdr workspace for a PM is labelled with its name.
- PM folder: `$OKE_HOME/pms/<name>/` (assignment.md, board.md, children.json, report files). The PM's session runs there. OKe writes the standing assignment (`pm add`, `pm rename`).
- Rename refuses while the PM has open or held jobs. It renames the state entry, the PM folder, the inbox and the wiki folder.
- Idle PMs are not closed automatically.
- Peer asks go to every other PM with an open job.
- Migration: an old `state.json` with `repos` and no open job is migrated by dropping `repos`. With open jobs from before, OKe refuses to run and names them (finish or cancel them with the old OKe, or move `state.json` aside).

## State

```
state["pms"][<name>] = {"pm": <agent record>, "places": [<paths of its jobs so far, newest first, no repeats>]}
state["jobs"][<job>] = {"pm": <name>, "path": <path or null>, "git_key": <private key or absent>, "direct": <bool>, ...}
```

`git_key` is internal and never shown: for a git path its git common dir, for a folder `<folder>/.git`. Worktree, merge, check, baseline and verify use it; one merge at a time per `git_key`; one open direct job per folder.

## Commands

- `oke pm add <name>`, `oke pm list` (name, places, alive, open and held jobs), `oke pm rename <old> <new>`, `oke pm remove <name> [--cancel]`. Without `--cancel`, remove refuses while open or held jobs exist and lists them.
- `oke job new <job> --pm <name> [--path <path>] [--research]`. The orchestrator gives `--path` only when the user said one.
- `oke job path <job> <path>`: sets the job's path, detects git or direct, applies the one-direct-job-per-folder rule, puts the path first in the PM's `places`. The same folder (compared as normalize_path does: case, slashes, a trailing slash) is a no-op at any time. Another folder is refused once the job has worked, has a worktree, or was ever approved: work there is a split job. A research job may set it in any open phase while it is unset. A Windows drive root stays `c:/`.
- `oke job split <job> <new job> --path <path> [--after <job>]` (never for a research job): a new job of the same PM with a copy of the intake (and interview), that path, and `after:` when given (a parent that never merges, direct or research, counts as done once its phase is `done`, archived too). A folder job (it never merges) waits for its `after:` job at `job phase working` instead of at its merge: the refusal keeps the plan approved and marks the job, the board shows it as `after_wait`, and `oke pending` shows it as `after_ready` ("job <job> may start: after-job <x> is finished") once nothing holds it, ("its after-jobs were dropped" when none is left). An after-job that can never finish (cancelled, unknown, archived without a verified merge) is one `after_broken` item, for a folder job at its start and a git job at its merge alike: "job <job> cannot start|merge: <why>; ask the user: drop it (`$OKE job after <job> --drop <x>`) or cancel <job>". The item carries the job's `phase`; after a drop in `merging` the orchestrator tells the PM to run the merge again, in `awaiting_merge` the `merge` item comes back by itself. The PM is not asked too: the merge refusal, and the cancel mail (only to jobs in `awaiting_merge` or `merging`), tell it to keep the job waiting. While a job waits, its reports and peer asks still show. `oke job after <job> --drop <x>` drops an after-job (the user's call). The orchestrator sets `working` first and writes the execute assignment only after; a move back from `reviewing` has no gate. Whenever a job is finished for `after` (merge verified, or `done` for a job that never merges), the PM of each job waiting on it in `merging` or `awaiting_merge` gets "Job <job>: after-job <x> is finished". A path that an open split job of the same parent already has is refused, and the output names it as `existing`. The orchestrator sees it as a `split` item in `oke pending`: "PM <pm> split job <job>: new job <new job> (<path>)"; `oke status` shows each job's `split_from`. `job path` refuses a branch `oke/<job>` that already exists and tells the PM to report a question (at `job new --path` the orchestrator picks another name). Each job is planned, approved and merged on its own.
- `oke job phase <job> working` (execute), `oke worktree`, `oke check`, `oke baseline` and `oke job merge` refuse a job with no path.
- `oke place <pm>` opens the PM's place (herdr: a workspace labelled with the name, cwd the PM folder). A crashed PM is revived in such a place too, never beside the orchestrator.
- `oke pm rename` and `oke pm remove` move the PM's folders first (all or nothing; remove moves its folder, inbox and wiki aside as `<name>.removed-<time>`, and archiving a removed PM's job counts no wiki use and makes no wiki) and save state last, so a failed move changes nothing and the command can run again.
- `oke path-check --pane <pane> --path <file>` answers whether an Edit, Write or NotebookEdit of that file is allowed (see Access).
- `oke wiki ... --pm <name>`; `--job` resolves through the job's PM.

## Finding the path

The planner gets the user's words, the PM's wiki and the PM's `places`, and answers with `path:` lines (one or more) or `path: unknown`. The PM looks in this order: the user's words, the wiki, `places`; when none settles it, it reports `question` ("which folder ...?"). It then runs `oke job path`. Several paths: it keeps one for this job and runs `oke job split` for each other.

## Access

- The PM session reads the whole file system: its additional directories are the file system roots, `/` on POSIX and every existing drive root (`C:/`, `D:/`, ...) on Windows (lean: `permissions.additionalDirectories`; otherwise `--add-dir`). So a path found while planning needs no new start. Writes stay with the write guard below. Workers and reviewers get only their job's areas.
- Edit, Write and NotebookEdit in PM, worker and reviewer sessions are allowed only in the agent's own folder under `$OKE_HOME` (a PM's folder and its open jobs' `jobs/<job>/` folders; never state.json, config.json, other PMs' folders, `automations/`, the wikis, or the files only OKe writes: ledger, baseline, interview, peers, intake, children, launch settings, check logs), `$OKE_HOME/tmp/`, the agent's own Claude memory folders (the one named after its cwd and, in git, the repository's shared one named after the main checkout, which all its worktrees use), and an open job's write area: its worktree (or main checkout without a worktree) for a git job; its folder for a direct job, without git repositories nested in it; for either, without real paths that leave it. A research job has none. Every check compares paths with links resolved and, on Windows, short names made long. `oke path-check` decides and refuses whenever it cannot check; the tick gives the mod the agent's folder, the memory folders and the write areas so obvious cases need no python. A worker's or reviewer's areas from its last good tick stay (they change only when its job is held, cancelled or closed, and it is closed then too); a PM's mod whose last good tick is over 30 seconds old asks path-check for every write.
- Bash writes are covered by the PM and worker skill text.

## Board and status

`board_tree` gives one entry per PM: `{"name", "key", "pm", "jobs"}`; each job shows its path's folder name, or `path?` while unset. `status` gives `pms[<name>] = {pm, agent, pane, session, pm_dir, places}` and each job its `path` and `direct`. A finished job (`done` or `failed`) whose PM no longer exists, or that has no PM (an old OKe's job), is classed `closed`, never `disconnected`. The board has no line for a removed PM whose jobs are all finished.
