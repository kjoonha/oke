# PM: kind: dialog

The orchestrator answered a child's dialog. If the assignment also has an `Answer:` line, it is the user's answer to your open question: handle the dialog line first, then continue as for `kind: answer`.

- "Dialog answered for <child>": start that child's wait again with `--after-blocked` added. Rewrite the job's report with the new seq: `question` if the job still has an open question (copy it), otherwise `waiting`.
- "Dialog denied for <child>": the user refused that action. Give the child a new instruction saying "The user denied <the action>. Do not try it again. If you cannot finish without it, report `question` and say what you need.", rewrite the job's report with the new seq as above, then wait with the child's new seq.
