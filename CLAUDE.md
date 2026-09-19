# Working in worktree-convention

Conventions for anyone — human or agent — making changes here.

## Your worktree is yours

You are not in the main checkout. You are in a **git worktree** of it, on a
branch named `agent/<topic>`, and no other agent is in that tree or on that
branch. Your briefing names the path; `pwd` will confirm it.

This is the whole point, so use it:

- **`git add -A` is fine.** So is `git stash`, `git reset`, `git checkout .`,
  `git rebase`. There is nobody else's work in here to destroy.
- **Commit early and often, and push.** `git push -u origin agent/<topic>`.
  "Working tree clean" is not "pushed" — the main session lands from `origin`.
- **A typecheck or test failure in a file you did not edit is real**, not
  another agent mid-edit. Your tree is a clean branch off `origin/main`.
  Investigate it rather than writing it off.

Two things are not yours:

- **The main checkout** (`/Volumes/Parkins/Eli/projects/parkins-ai-walkthrough`) is read-only for agents. It stays clean
  and deployable, and deploys are cut from a named commit, not from a working
  tree. Do not `cd` there to "just check something", and certainly do not edit
  there.
- **`main` itself.** Do not merge, and do not open a PR by hand. Say in your
  callback that the work is ready and name the branch; the main session runs
  `registry.py land --topic <topic>`, which opens the PR, merges it, and takes
  your worktree and branch away.

The PR is a record and a diff, not a gate — it is there so a change can be
found and read afterwards, not so somebody has to approve it. If a change
genuinely wants a second pair of eyes before it merges, say so and say why;
that is the exception, and it is the main session's call, not yours.

<!-- Applied by ~/Eli/ops/subagent-registry/apply_convention.py on 2026-09-19.
     Edit convention/worktree-convention.md and re-run; do not hand-edit here,
     or this repo silently drifts from the other 14. -->
