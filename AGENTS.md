# Agent workflow

- Use `main` as the target branch for completed repository changes unless the task explicitly names another branch.
- After implementing a change, run the relevant local tests and required repository checks.
- When the implementation is complete, required checks pass, there are no unresolved conflicts, and repository rules allow it, merge the pull request into `main` during the same task.
- If the repository uses a merge queue, add the pull request to the queue or enable auto-merge, then verify that the merge completes.
- Do not merge when required checks fail, conflicts remain, or branch protection requires an unresolved approval.
- Report the resulting merge commit or the exact blocking condition.
