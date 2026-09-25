# Mooner510/workflows

모든 active project가 공유하는 canonical CI/CD implementation repository다.

- default branch: `master`
- stable consumer ref: `v1`
- shared implementation 변경은 `master`에 반영한 같은 작업에서 `v1`을 동일 commit으로 이동한다.
- consumer repository는 CI/CD 로직을 직접 구현하지 않는다.

## Canonical entrypoints

```text
.github/workflows/project-ci.yml   # PR / dev / production CI + dev automatic deployment
.github/workflows/production.yml   # repository_dispatch production deploy / rollback
.github/workflows/go-format.yml    # manual Go formatting + canonical CI redispatch
```

Consumer는 위 reusable workflow만 호출한다. `.github/actions/**`는 central implementation detail이며 consumer가 직접 조립하지 않는다.

기존 `pipeline.yml`, `docker-service-*`, `docker-process-*` 등은 active repositories를 새 contract로 이전하는 동안만 남기는 compatibility implementation이다. 신규 caller contract로 사용하지 않는다.

`security.yml` reusable workflow는 제거했다. Security와 language/migration CI orchestration은 각각 `.github/actions/ci/security`, `.github/actions/ci/group` 내부 action으로 통합하여 `project-ci.yml`과 legacy `pipeline.yml`이 같은 구현을 공유한다. `.github/workflows/validate.yml`은 이 implementation repository 자체의 workflow syntax/reference policy만 검증한다. `Mooner510/workflows`는 public repository이므로 이 repository 자체의 PR/push validation은 GitHub-hosted `ubuntu-latest`에서만 실행한다. Consumer repository에서 reusable workflow를 호출할 때의 canonical CI/CD는 기존 `[self-hosted, linux]` runner contract를 유지한다. Central `go-format.yml`도 `workflow_call` 전용이며 `workflows` repository 자체에서 직접 dispatch하지 않는다.

## One repository declaration

모든 project-specific CI/CD metadata의 유일한 source는 고정 경로다.

```text
.github/stacks.json
```

경로 자체는 변경할 수 없다. CI와 production deployment 모두 실행 중인 exact revision의 이 파일을 읽는다.

예:

```json
{
  "version": 1,
  "go_private_patterns": "github.com/example/*",
  "components": [
    {
      "name": "api",
      "type": "go",
      "path": "services/api",
      "watch": ["services/shared"],
      "migration_engine": "goose",
      "migration_path": "db/migrations"
    }
  ],
  "services": [
    {
      "name": "api",
      "path": "services/api",
      "component": "api",
      "database": true,
      "database_scope": "service",
      "migrate": true,
      "build_args": ["PUBLIC_ORIGIN"]
    }
  ]
}
```

### Component metadata

Component는 language CI와 migration-validation source만 설명한다.

공통:

```text
name
type = go | node | java-kotlin
path
watch
watch_path
```

필요한 경우에만 language/migration metadata를 추가한다.

```text
go_version / node_version / java_version
package_manager
build_tool / profile
gradle_tasks / maven_goals

migration_engine = goose | go-command | prisma | drizzle | flyway
migration_path
migration_schema
migration_config
migration_package
migration_package_manager
migration_build_tool
goose_version

postgres_test
postgres_database_env
postgres_image
postgres_test_args
postgres_test_env
```

### Service metadata

Service entry가 존재하면 deployable Docker service다.

허용되는 field는 다음뿐이다.

```text
name
path
component
watch
database
database_scope
migrate
build_args
manual
```

- `component`: 해당 service의 language/migration contract를 소유하는 component.
- `watch`: 없으면 component가 affected일 때 service도 affected. 있으면 해당 repository-relative path가 변경됐을 때만 service가 affected.
- `database`: runtime `DATABASE_URL`이 필요한 service.
- `database_scope`: `project`(default) 또는 `service`. `project`는 기존 `/opt/stacks/projects/<project>/db[.dev].env`, `service`는 `/opt/stacks/projects/<project>/<service>/db[.dev].env`를 사용한다. 기존 consumer 호환성을 위해 default는 `project`다.
- `migrate`: referenced component의 migration engine을 deploy 시 실행. `true`이면 database도 자동으로 필요하다. Migration credential scope는 같은 service의 `database_scope`를 따른다.
- `build_args`: build에 필요한 non-secret environment variable 이름. 값은 선택 environment의 canonical project/service `deploy.env`에서만 resolve한다.
- `manual`: `true`이면 CI image build/scan에는 포함하지만 dev automatic deployment와 service 미지정 production 전체 배포에서는 제외한다. Explicit service dispatch만 허용한다.

Private dependency가 Docker build 중 필요한 경우 caller의 optional `CI_PRIVATE_REPO_TOKEN`을 central image builder가 BuildKit secret `CI_PRIVATE_REPO_TOKEN`으로만 전달한다. Token은 build arg, image layer, project metadata에 저장하지 않는다. Dockerfile이 해당 secret을 사용하지 않으면 아무 효과가 없다.

Consumer가 다음 deployment controls를 선언하는 것은 금지한다.

```text
kind
dockerfile
build context
Docker target
container port / host port
health path
readiness command / expression / attempts
image name / tag
container name
network
security options / capabilities
restart policy
state path
rollback algorithm
scanner / severity / scan order
migration execution order
```

필요한 공용 기능이 없으면 repository-local CI/CD를 추가하지 않고 먼저 이 repository의 generic capability를 확장한다.

## Docker packaging contract

모든 deployable service는 동일한 packaging 규약을 따른다.

```text
Dockerfile  = <service.path>/Dockerfile
build context = repository root
```

다른 Dockerfile 경로나 build context override는 지원하지 않는다.

### Port

Consumer는 port를 선언하지 않는다.

Built image의 Docker metadata가 source of truth다.

```text
0 exposed TCP ports -> non-routable/process service
1 exposed TCP port  -> canonical routable service port
2+ exposed ports    -> CI failure
UDP EXPOSE          -> CI failure
```

각 container는 독립 network namespace를 가지므로 여러 service가 같은 internal port를 사용해도 충돌하지 않는다. Canonical runtime은 host port를 publish하지 않는다.

한 TCP port가 존재하면 central runtime이 `caddy-shared`에 container를 연결하고 다음 label을 자동 생성한다.

```text
kr.mooner510.stacks.container-port=<EXPOSE port>
```

### Readiness

1 TCP port를 EXPOSE하는 routable image는 Dockerfile에 유효한 `HEALTHCHECK`를 반드시 정의한다.

0-port process image는 HEALTHCHECK를 생략할 수 있다. 이 경우 central runtime은 container가 시작 직후 종료하지 않고 안정적으로 running 상태를 유지하는지 확인한다. Process image가 HEALTHCHECK를 정의하면 해당 health status를 그대로 사용한다.

Routable image의 HEALTHCHECK가 없거나, health status가 `unhealthy`이거나, process container가 안정화 전에 종료되면 deployment는 실패한다.

## Canonical CI

`project-ci.yml`이 다음 전체 orchestration을 소유한다.

```text
Load .github/stacks.json
→ Detect affected components/services
├─ Security
├─ Language / migration CI
└─ affected Docker image build + Trivy image scan
→ verification + optional canonical image publication + temporary image cleanup
```

Security:

```text
Semgrep CE
OSV-Scanner
Gitleaks current tree + Git history
Trivy IaC/Dockerfile misconfiguration
Trivy built-image HIGH/CRITICAL vulnerabilities
```

Image scan은 CI 밖의 후처리가 아니다. Docker build와 built-image Trivy gate까지 성공해야 해당 revision의 CI가 성공한다.

Language CI:

```text
Go:
  gofmt
  go mod tidy must produce no go.mod/go.sum diff
  go mod download
  go mod verify
  go vet
  go test
  go build

Node:
  canonical default Node.js 24.21.0
  explicit node_version은 exact x.y.z만 허용
  package manager version은 repository packageManager exact pin을 사용
  canonical preferred pnpm은 11.27.1이며 예외는 project SOT에 기록
  locked install
  lint
  type check
  test
  build

Java/Kotlin:
  centralized Gradle/Maven lifecycle
  optional Android/Spring Boot profile
```

Migration validation은 configured migration source가 affected일 때 disposable PostgreSQL에서 수행한다. Production DB를 CI validation에 사용하지 않는다.

Explicit maintenance dispatch의 `diff_base`는 full 40-character ancestor SHA만 허용하며 실제 `diff_base..HEAD` change detection에 사용한다. `.github/stacks.json`이 변경되면 contract 자체가 달라졌으므로 모든 declared component/service를 affected 처리한다.

Public/fork/untrusted pull request는 self-hosted runner에서 checkout/build하지 않는다. Canonical CI의 최초 contract job과 compatibility pipeline detector가 caller repository ownership을 확인한 뒤에만 trusted source code를 실행한다.

## Production image gate

Production default-branch CI에서는 Security/Language CI와 Docker image preparation을 병렬로 수행할 수 있다. 실제 production service는 이 단계에서 변경하지 않는다.

각 affected service는 먼저 run-scoped temporary tag로 build/scan한다.

```text
<project>/<service>:ci-<run-id>-<attempt>-<sha>
```

모든 requested validation이 성공한 뒤에만 affected service 전체를 canonical revision tag로 publish한다.

```text
<project>/<service>:<sha>
```

Affected service 집합은 revision 단위 all-or-nothing이다. 하나라도 build/scan/CI/security에 실패하면 canonical tag를 새로 publish하지 않고 해당 run의 temporary tag를 정리한다.

재실행에서 기존 canonical SHA tag가 이미 존재하면 finalize 실패 시 이전 tag target을 복원한다.

Production CI는 다음을 절대 수행하지 않는다.

```text
production migration
production container stop/create/start
production deployment state mutation
production Caddy mutation
```

## Development deployment

`dev` branch도 Security/Language CI와 image build/Trivy가 모두 성공해야 한다.

Dev canonical tag:

```text
<project>/<service>:dev-<sha>
```

Flow:

```text
dev revision
→ central CI/security/image gate
→ all success
→ canonical dev image finalize
→ affected service automatic migration/deploy
→ Docker HEALTHCHECK healthy
→ dev state record
```

실패한 검증이 하나라도 있으면 running development service를 변경하지 않는다.

`/opt/stacks/projects/<project>/.dev-disabled` 또는 service `.dev-disabled` marker는 automatic dev deploy만 skip한다.

## Runtime ownership

Central service deployment이 다음을 소유한다.

```text
project/service identity
prod/dev network identity
runtime env overlay
DB credential resolution (project/service scope)
CORS metadata injection
managed TLS identity/trust mounts
migration execution
container replacement
Docker hardening
HEALTHCHECK wait
state/history
automatic restore after failed replacement
manual rollback
```

Fixed hardening:

```text
--init
--security-opt no-new-privileges:true
--cap-drop ALL
--restart unless-stopped
stop timeout = 20 seconds
```

Consumer override는 제공하지 않는다.

Runtime env:

```text
prod:
  project deploy.env
  service deploy.env
  service secret/deploy.env

dev:
  project deploy.dev.env
  service deploy.dev.env
  service secret/deploy.dev.env
```

Environment file이 하나도 없는 service도 허용한다. Development는 production config로 fallback하지 않는다.

Mutable volume은 selected runtime config의 `DEPLOY_VOLUMES_JSON`만 사용한다.

## Production control plane

Production Docker mutation은 GitHub UI `workflow_dispatch`로 시작하지 않는다.

Consumer의 thin `deploy.yml`은 다음 event만 받는다.

```yaml
on:
  repository_dispatch:
    types: [production_deploy, production_rollback]
```

그리고 `Mooner510/workflows/.github/workflows/production.yml@v1`만 호출한다.

`repository_dispatch`는 GitHub 규약상 default branch의 최신 commit을 `GITHUB_SHA`와 `GITHUB_REF`로 사용한다. Central workflow는 이를 다시 검증한다.

Payload:

```text
event_type = production_deploy | production_rollback
client_payload.service = optional logical service
```

- service가 있으면 해당 service 하나.
- service가 없으면 declared Docker services 전체.
- repository-local deployment logic은 허용하지 않는다.

### Deploy

```text
repository_dispatch
→ current default-branch SHA checkout
→ all selected exact-SHA images preflight
→ migration
→ central runtime replace
→ HEALTHCHECK
→ state/history
```

Expected production image:

```text
<project>/<service>:<current-default-branch-sha>
```

Image가 없거나 revision/project/service provenance label이 맞지 않으면 mutation 전에 실패한다. 이전 성공 SHA나 `latest`를 추측하지 않는다.

### Rollback

```text
repository_dispatch production_rollback
→ state/history previous known-good imageId
→ all selected rollback images preflight
→ replace
→ HEALTHCHECK
→ rollback state record
```

Rollback은 DB down migration을 실행하지 않는다.

Project-only production deploy는 모든 selected service image/rollback target을 먼저 preflight한 뒤 service별 lifecycle을 수행한다. Runtime mutation은 distributed transaction이 아니며 service별로 독립적이다. 각 service replacement 실패는 해당 service의 직전 known-good image로 자동 restore하지만, 다른 service의 이미 성공한 deployment를 연쇄 rollback하지 않는다.

State는 기존 canonical path를 유지한다.

```text
prod: /var/lib/stacks/projects/<project>/<service>/deploy/
dev : /var/lib/stacks/projects/<project>/<service>/dev/deploy/
```

## Migration engines

Supported application migration engines:

```text
Goose SQL
Go command
Prisma Migrate
Drizzle Kit
Flyway
```

Production/development deploy마다 service가 `migrate:true`이면 selected environment DB에 pending migrations를 적용한다. Engine history가 이미 적용된 migration을 no-op 처리한다.

Migration source와 command는 exact deployed revision checkout에서 실행한다. Migration implementation을 host CLI로 복제하지 않는다.

## Go Format maintenance

Go Format workflow는 branch에서 `gofmt` + `go mod tidy`를 적용하고 변경이 있으면 maintenance commit을 push한다.

GitHub의 `GITHUB_TOKEN` push는 일반 push workflow를 재귀 실행하지 않으므로 Go Format은 push 직전 SHA를 `diff_base`로 보존하고 caller의 `ci.yml`을 명시적으로 dispatch한다.

```text
format/tidy
→ commit
→ push
→ ci.yml workflow_dispatch(diff_base=<old-sha>)
→ canonical project-ci
```

따라서 Go Format commit도 일반 commit과 동일하게:

- dev: full CI/security/image gate → automatic deploy
- production branch: full CI/security/image gate → verified production image only

를 수행한다.

## Thin consumer workflows

### ci.yml

Consumer는 trigger와 canonical call만 소유한다.

```yaml
name: CI

on:
  pull_request:
  push:
    branches: [master, dev]
  workflow_dispatch:
    inputs:
      diff_base:
        required: false
        type: string
        default: ''

permissions:
  contents: read

jobs:
  ci:
    uses: Mooner510/workflows/.github/workflows/project-ci.yml@v1
    with:
      diff_base: ${{ inputs.diff_base || '' }}
    secrets: inherit
```

Repository default branch가 `main`인 project는 push branch만 `main`으로 선언한다. 실제 production branch 판정은 central workflow가 repository default branch에서 자동으로 결정한다.

### deploy.yml

```yaml
name: Production

on:
  repository_dispatch:
    types: [production_deploy, production_rollback]

permissions:
  contents: read

jobs:
  production:
    uses: Mooner510/workflows/.github/workflows/production.yml@v1
```

### go-format.yml

```yaml
name: Go Format

on:
  workflow_dispatch:
    inputs:
      path:
        required: false
        type: string
        default: /

permissions:
  contents: write
  actions: write

jobs:
  format:
    uses: Mooner510/workflows/.github/workflows/go-format.yml@v1
    with:
      path: ${{ inputs.path }}
    secrets: inherit
```

Consumer workflow에 별도 `run:` deployment/CI implementation을 추가하지 않는다.

## Runner

CI/CD는 모두 canonical selector를 사용한다.

```yaml
runs-on: [self-hosted, linux]
```

Personal repositories는 Gharp, organization repositories는 organization-scoped self-hosted runner를 사용한다. CI/CD 역할별 별도 runner는 만들지 않는다.
