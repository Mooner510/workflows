# workflows

Central reusable GitHub Actions workflows for self-hosted Linux runners.

Supported targets:

- Go services and APIs
- Android projects using Java/Kotlin + Gradle
- JavaScript/TypeScript web apps such as React, Next.js, and Vite
- JavaScript/TypeScript Node.js servers such as NestJS and Express

Every job runs on:

```yaml
runs-on: [self-hosted, linux]
```

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
          optional CD
```

Change detection runs once. Only affected components are sent to Security and CI. Security and CI are independent and therefore run in parallel when runner capacity is available. A deployment job can depend on the reusable pipeline job, so CD cannot start unless all required Security and CI jobs succeed.

## Reusable workflows

- `.github/workflows/pipeline.yml`: change detection and orchestration
- `.github/workflows/security.yml`: shared security scanning
- `.github/workflows/ci-go.yml`: Go CI
- `.github/workflows/ci-node.yml`: Node.js CI
- `.github/workflows/ci-android.yml`: Android CI
- `.github/workflows/deploy.yml`: optional deployment entrypoint

`pipeline.yml` uses GitHub's `$/` same-repository syntax for nested reusable workflows. This keeps the nested workflow files on the exact same central-workflows commit as the pipeline that was selected by the caller.

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
- `deploy`: expose the changed component for deployment when `true`
- `deploy_script`: deployment script relative to the component, default `.ci/deploy.sh`
- `go_version`: default `stable`
- `node_version`: default `24`
- `java_version`: default `17`
- `gradle_tasks`: default `lint test assembleDebug`

Example:

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
            "path": "apps/android",
            "java_version": "17"
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

Scanner images are pinned by digest and receive the source tree read-only. The Docker socket is not mounted into the scanner containers.

Node dependency scanning requires one committed `pnpm-lock.yaml`, `yarn.lock`, or `package-lock.json`. The workflow walks upward from the component to the repository root, so monorepos with a shared root lockfile are supported.

Go components require `go.mod`.

Android dependency-vulnerability coverage is complete when the repository contains supported dependency metadata such as Gradle lockfiles or `gradle/verification-metadata.xml`. If those files are absent, the workflow warns and still runs SAST, secret, and configuration scans instead of pretending dependency coverage is complete.

A scheduled caller can periodically set `force_all: true` to rescan unchanged components for newly disclosed vulnerabilities.

## CI behavior

Go:

```text
go mod download
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

Android uses the repository Gradle wrapper. Default tasks:

```text
lint test assembleDebug
```

## Optional deployment

Deployment is deliberately outside `pipeline.yml`, so deployment secrets do not enter Security or CI jobs.

```yaml
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
            "deploy": true
          }
        ]

  deploy:
    needs: pipeline
    if: >-
      github.event_name == 'push' &&
      github.ref == 'refs/heads/master' &&
      needs.pipeline.outputs.has_deployments == 'true'
    strategy:
      fail-fast: false
      matrix:
        component: ${{ fromJSON(needs.pipeline.outputs.deploy_components) }}
    uses: Mooner510/workflows/.github/workflows/deploy.yml@v1
    with:
      working_directory: ${{ matrix.component.path }}
      script: ${{ matrix.component.deploy_script || '.ci/deploy.sh' }}
    secrets:
      deploy_env: ${{ secrets.DEPLOY_ENV }}
```

The project owns the deployment implementation at a checked-in script such as:

```text
<component>/.ci/deploy.sh
```

The reusable deployment workflow validates the script path and does not `eval` caller-provided commands.

## Versioning

Consumers should normally use a stable major tag such as `@v1`. For maximum immutability, pin the reusable workflow to a full commit SHA.

Third-party GitHub Actions and security-scanner images used internally are pinned to immutable commit or image digests.

## Self-hosted runner trust boundary

Do not execute arbitrary untrusted pull-request code on a persistent self-hosted runner that also has deployment credentials or access to sensitive internal networks.

For production, prefer separate trust boundaries:

- CI/Security runners without deployment secrets
- restricted deployment runners for trusted deployment events

If public or otherwise untrusted contributions are allowed, use ephemeral isolation for PR execution.
