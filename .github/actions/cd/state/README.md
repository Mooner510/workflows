# Deployment state action

Persistent production state is stored at:

```text
/var/lib/stacks/projects/<project>/<service>/deploy/
├─ current.json
└─ history.jsonl
```

`current.json` is the last verified known-good deployment. `history.jsonl` appends successful deploy/rollback records.

Use this action through `cd/docker-service`; direct calls are only for central CD composition.
