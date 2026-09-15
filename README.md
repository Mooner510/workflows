# workflows

Central reusable GitHub Actions workflows for self-hosted Linux runners.

Supported targets:

- Go services and APIs
- Android projects using Java/Kotlin + Gradle
- JavaScript/TypeScript web apps such as React, Next.js, and Vite
- JavaScript/TypeScript Node.js servers such as NestJS and Express

All central jobs use:

```yaml
runs-on: [self-hosted, linux]
```

Additional runner labels such as `x64` or `prod` are not required.

## Architecture

```text
detect changes
      |
      +----------------+
      |                |
      v                v
   security            CI
      |                |
      +--------+-------+
               |
        pipeline success
               |
     explicit production CD
```

Change detection runs once. Only affected components are sent to Security and CI. Security and CI are independent and can run in parallel when runner capacity is available.

Production mutation is not triggered by a normal push. A deployment workflow must be explicitly started by the project policy, normally by a stable Release or `workflow_dispatch`, perform full verification, and then deploy from a job that targets the caller repository's `production` GitHub Environment.

## Shared building blocks

- `.github/workflows/pipeline.yml`: change detection and orchestration
- `.github/workflows/security.yml`: shared security scanning
- `.github/workflows/ci-go.yml`: Go CI
- `.github/workflows/ci-node.yml`: Node.js CI
- `.github/workflows/ci-android.yml`: Android CI
- `.github/actions/deploy/action.yml`: validated deployment-script entrypoint for production jobs

`pipeline.yml` uses GitHub's `$/` same-repository syntax for nested reusable workflows so nested workflows are taken from the same central-workflows commit selected by the caller.

Deployment is a composite action instead of a reusable workflow. GitHub Environment secrets belong to the caller repository and cannot be attached to a job that only calls a reusable workflow. A normal caller job can set `environment: production`, receive its Environment secrets, and invoke the central deploy action as a step.

## Runner requirements

The Linux self-hosted runner must provide:

- Bash
- Git
- `jq`
- Docker

Android runners also need a working Android SDK installation and the SDK packages required by the project. Java, Go, and Node.js are provisioned by the workflows.

## Component configuration

The caller passes a JSON array to `pipeline.yml`.

Required fields:

- `name`: unique component name
- `type`: `go`, `node`, or `android`
- `path`: component root relative to the repository

Optional fields:

- `watch`: repository-relative paths that affect the component; exact/path-prefix matching, not globs
- `deploy`: expose the component through deployment outputs when `true`
- `deploy_script`: deployment script relative to the component, default `.ci/deploy.sh`
- `go_version`: default `stable`
- `node_version`: default `24`
- `java_version`: default `17`
- `gradle_tasks`: default `lintDebug testDebugUnitTest assembleDebug`

Example CI caller:

```yaml
name: CI

on:
  pull_request:
  push:
    branches: [master]

permissions:
  contents: read

jobs:
  pipeline:
    uses: Mooner510/workflows/.github/workflows/pipeline.yml@v1
    with:
      components: |
        [
          {
            "name": "api",
            "type": "go",
            "path": "services/api",
            "watch": ["services/api", "shared/proto"],
            "deploy": true
          },
          {
            "name": "web",
            "type": "node",
            "path": "apps/web",
            "watch": ["apps/web", "packages/ui", "pnpm-lock.yaml", "pnpm-workspace.yaml"],
            "deploy": true
          },
          {
            "name": "android",
            "type": "android",
            "path": "apps/android"
          }
        ]
```

If shared code or a root lockfile affects a component, include it in that component's `watch` list. Changes under `.github/workflows/` affect every configured component automatically.

For events without a reliable diff base, or when `force_all: true` is used, every configured component runs rather than risking a missed check.

## Security

Every changed component gets:

- Semgrep OSS for SAST
- OSV-Scanner for known dependency vulnerabilities
- Trivy for exposed secrets and configuration mistakes

Scanner images are pinned by digest and receive the source tree read-only. The Docker socket is not mounted into scanner containers.

Node dependency scanning requires one committed `pnpm-lock.yaml`, `yarn.lock`, or `package-lock.json`. The workflow walks upward from the component to the repository root, so monorepos with a shared root lockfile are supported.

Go components require `go.mod`.

Android dependency-vulnerability coverage is complete when supported dependency metadata such as Gradle lockfiles or `gradle/verification-metadata.xml` is present. Without it, the workflow warns and still runs SAST, secret, and configuration scans.

A scheduled caller can set `force_all: true` to rescan unchanged components for newly disclosed vulnerabilities.

## CI behavior

Go:

```text
gofmt check
go mod download
go mod verify
go vet ./...
go test ./...
go build ./...
```

Node.js finds the nearest package-manager lockfile between the component and repository root, installs with the locked package manager, and runs configured scripts in this order:

```text
lint
check-types / typecheck / type-check
test
build
```

pnpm projects must commit a root `package.json` with a pinned `packageManager`, for example:

```json
{
  "packageManager": "pnpm@10.17.1"
}
```

Android uses the repository Gradle wrapper. Default tasks:

```text
lintDebug
testDebugUnitTest
assembleDebug
```

## Production deployment

Application/runtime secrets should normally be stored as GitHub Environment secrets in the caller repository's `production` environment. They are exposed only to the production job that needs them.

Host-persistent exceptions are limited to infrastructure material that must be read directly by server-side tooling, such as project DB credentials managed by the DB CLI and Android signing material managed by the Android CLI.

A reusable workflow cannot consume the caller repository's Environment secrets by attaching the caller's environment to the reusable-workflow call. Therefore the production job is intentionally local to the project and calls the central composite deploy action.

Example manual production workflow:

```yaml
name: Deploy

on:
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: production
  cancel-in-progress: false

jobs:
  verify:
    uses: Mooner510/workflows/.github/workflows/pipeline.yml@v1
    with:
      force_all: true
      components: |
        [
          {
            "name": "api",
            "type": "go",
            "path": "services/api",
            "deploy": true
          }
        ]

  deploy:
    needs: verify
    environment: production
    runs-on: [self-hosted, linux]
    timeout-minutes: 30
    env:
      OAUTH_CLIENT_SECRET: ${{ secrets.OAUTH_CLIENT_SECRET }}
      JWT_SECRET: ${{ secrets.JWT_SECRET }}
    steps:
      - name: Checkout release source
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false

      - name: Deploy
        uses: Mooner510/workflows/.github/actions/deploy@v1
        with:
          working-directory: services/api
```

The project owns the deployment implementation at a checked-in entrypoint such as:

```text
<component>/.ci/deploy.sh
```

The central deploy action validates that the entrypoint stays inside the selected component and executes it without `eval`. Environment secrets remain job environment variables; the central action does not serialize them into a long-lived `secret/deploy.env` file.

Normal application deployment scripts must preserve the production contract: verified immutable artifact/digest, migration before rollout when required, deployment, and health verification. Secrets must not be written to repository history, deployment history, status, or logs.

## Versioning

Consumers should normally use a stable major tag such as `@v1`. For maximum immutability, pin the reusable workflow/action to a full commit SHA.

Third-party GitHub Actions and security-scanner images used internally are pinned to immutable commit or image digests.

## Self-hosted runner trust boundary

Do not execute arbitrary untrusted pull-request code on a persistent self-hosted runner that has Docker access, deployment credentials, or sensitive internal-network access.

GitHub Environment secrets reduce long-lived secret storage on the server, but once a production job starts, those secrets are available to that self-hosted runner for the duration of the job. Treat the runner as trusted production infrastructure.
