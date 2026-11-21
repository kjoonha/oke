# Orchestrator: Orca

**Wake-ups.** Inside Orca, Orca also wakes you with its own messages from your PMs (`worker_done`, `escalation`). They only wake you: read the PM's report.md and handle it as the wait would, then start the wait again. Never answer them with `orca orchestration` commands.

**Setup.** OKe needs Orca's Agent Orchestration setting on, Orca's default for new agent tabs set to CLI (a terminal, not Chat), and the OKe plugin installed in Claude Code (Orca starts the agents itself, without OKe's `--plugin-dir`). A spawn or revive that fails with "OKe needs Orca's default for new agent tabs to be CLI": tell the user exactly that and stop until they changed it.

**"may have failed to start".** A screen that starts with `OKe: <agent> may have failed to start` is no dialog: send no key. It is often only slow: right before you ask, run `$OKE read <agent>` again; if that no longer starts with the note, it started meanwhile: ask nothing.

- The PM itself (a `blocked` wait result): it started meanwhile: start the wait again (it returns the PM's report if one came). Else ask: revive / wait (look at its tab). Revive: run the two commands the note names (`$OKE close ...`, then `$OKE revive <pm> --reset`), then start the wait again. Wait: `$OKE attach <pm>`, tell the user it turns back to working by itself if it was only slow, and start the wait again with `--after-blocked` only after the user says so.
- A child (a `blocked` queue item): it started meanwhile: the PM's `kind: dialog` is "Dialog answered for <child>." with no key, then steps 3 to 6 of "Give the job to the PM". Else ask: revive / wait (look at its tab). Revive: the PM's `kind: dialog` is "Dialog answered for <child>: revived.", then run the two commands the screen names (`$OKE close ...`, then `$OKE revive <child> --reset`), then steps 3 to 6. Wait: as "go to that pane" (attach, `job defer`), then "Dialog answered for <child>." with no key.

If the same note comes back, the agent is not just slow: ask revive / hold the job, not wait again.
