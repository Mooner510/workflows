# Deployment state action

Persistent production state is stored at:

```text
/var/lib/stacks/projects/<project>/<service>/deploy/
├─ current.json
└─ history.jsonl
```

`current.json` is the last verified known-good deployment. `history.jsonl` records successful deploy/rollback events and successful restoration after a failed rollback (`rollback-restore`). Record writes stage both files before replacement so a history write failure restores the previous current state.

Use this action through `cd/docker-service` or `cd/docker-service-rollback`; direct calls are only for central CD composition. Deploy and rollback callers for the same service must share one GitHub Actions concurrency group.
