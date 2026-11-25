---
name: automate
description: Use when the user runs /oke:automate or wants Claude Code to run a prompt on a schedule (every morning, every 2 hours, weekly...), or asks to list, test or stop such scheduled runs. You ask what, where and how often, then register it with crontab (Linux, macOS) or Task Scheduler (Windows); at each scheduled time a background Claude Code session starts in that folder with the saved prompt.
---

# OKe automate

## Tool

```
OKE='python3 "${CLAUDE_PLUGIN_ROOT}/bin/oke.py"'     # use python if python3 does not exist
```

`$OKE_HOME` is OKe's home folder (`~/.oke` when the variable is not set). Each automation has its own folder, `$OKE_HOME/automations/<name>/` (`automation.json`, `prompt.md`, and `run.log` with one line per run, manual ones included); `add` and `list` print it as `folder`. Below, the *work folder* is where the session runs; the *automation folder* is this one. Never edit its files by hand: run `$OKE automate add` again to change one.

At each scheduled time the OS scheduler runs `$OKE automate run <automation folder> --scheduled`, which starts `claude "<prompt>" --bg --name auto-<name>-<MMDD-HHMMSS>` in the work folder, with the PATH and locale the user had at `add` on Linux and macOS (cron's own are bare); on Windows with the user's login environment, not the shell's (a PATH set only in the shell, such as `nvm use` or a conda env, is missing). The session runs on its own: nobody is watching it. A run is skipped (one `skipped` line in `run.log`) while the previous run of the same automation still works or waits at a dialog.

## Set one up

Talk to the user in their language. Ask each question with `AskUserQuestion` when it has choices; take what the user already said instead of asking it again.

1. **What.** What should the session do each time? Then write the prompt it starts with. That session sees nothing of this conversation, so the prompt must stand alone: the goal, the files or commands involved, what counts as done, where to write the result (a file, a commit on a branch, a report), and what to do when something fails. Show the prompt to the user and change it until they agree.
2. **Where.** The work folder the session starts in (offer the current working folder first). It must be a folder Claude Code already trusts: a background session cannot answer the trust dialog, so the run fails there. If the user is not sure, tell them to open `claude` in that folder once and accept.
3. **When.** Offer: every day at a time, weekdays at a time, some weekdays at a time, every N hours, every N minutes. Turn the answer into one schedule string (local time of this computer, 24 hours):
   - `every 30m` (N divides 60), `every 2h` (N divides 24)
   - `daily 09:00`, `weekdays 09:00`, `weekly mon,thu 09:00`
   - `cron <minute hour day month weekday>` for anything else (not on Windows)
4. **Permissions.** Ask how the session may act, since nobody answers its dialogs:
   - the user's own default (leave `--permission-mode` out): it stops at its first permission dialog until someone runs `claude attach <session>`, and later runs are skipped meanwhile;
   - `acceptEdits`: file edits without asking, other tools still ask;
   - `auto`: Claude decides what is safe;
   - `bypassPermissions`: everything without asking. Say it can change or delete anything in that folder; only when the user accepted bypass mode in Claude Code before.
5. **Model** (optional): leave it out to use the user's default, or `haiku`, `sonnet`, `opus`.
6. **Name**: short, lowercase letters, digits and `-` (for example `nightly-tests`). The same name replaces an existing automation.
7. Show the whole setup (name, schedule in words, work folder, permissions, model, prompt) and ask: register / change something.
8. Write the prompt to a file with the Write tool, at an absolute path: `<home>/automate-prompt.md`, where `<home>` is the value of `OKE_HOME` if it is set (`echo $OKE_HOME`), else the `.oke` folder in the user's home folder, written out in full. Then run:
   ```
   $OKE automate add <name> --schedule "<schedule>" --cwd "<work folder>" --prompt-file "<that file>" [--permission-mode <mode>] [--model <model>]
   ```
   On an error, tell the user what it says, fix the input and run it again (a failed `add` changes nothing). Delete the prompt file after it succeeded. An `entry` that says it runs only on AC power: tell the user so.
9. Ask whether to try it once now. Yes: `$OKE automate run <name>` and show its line. A `started` line names the session: the user can watch it with `claude attach <session>`, or list sessions with `claude agents`.
10. Tell the user, in a few lines: when it runs next, that the computer must be on at that time (on Windows the user must also be logged in), where each run's line goes (`run.log` in the automation folder `add` printed), and how to stop it (ask you, or `$OKE automate remove <name>`). A session the run started can change files in the work folder only as its permission mode allows.

## List, test, stop

- See them: `$OKE automate list`. Show each one's name, schedule, work folder (`cwd`) and `last_run`. `registered: false` means the scheduler does not hold it (removed or commented out by hand): offer to register it again with `add` (same values; its prompt is `prompt.md` in its `folder`) or to remove it.
- Try one now: `$OKE automate run <name>`.
- Change one: run `add` again with the same name and the new values.
- Stop one: ask the user to confirm the name, then `$OKE automate remove <name>`. It removes the scheduler entry and the automation folder (never the work folder); sessions already started stay in `claude agents`. If the scheduler entry cannot be removed, nothing is removed: tell the user the error.
