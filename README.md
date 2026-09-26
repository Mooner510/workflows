# Mooner510/workflows

모든 active project가 공유하는 canonical CI/CD implementation repository다.

- default branch: `master`
- stable consumer ref: `v1`
- shared implementation 변경은 `master`에 반영한 같은 작업에서 `v1`을 동일 commit으로 이동한다.
- consumer repository는 CI/CD 로직을 직접 구현하지 않는다.
- central reusable workflow와 composite action 내부의 `Mooner510/workflows` 참조는 모두 `@v1`을 사용한다.

## Canonical entrypoints

```text
.github/workflows/project-ci.yml   # PR / dev / production CI + dev automatic deployment
.github/workflows/production.yml   # repository_dispatch production deploy / rollback
.github/workflows/go-format.yml    # manual Go formatting + canonical CI redispatch
```

Consumer는 위 reusable workflow만 호출한다. `.github/actions/**`는 central implementation detail이며 consumer가 직접 조립하지 않는다.

기존 `pipeline.yml`, `docker-service-*`, `docker-process-*` 등은 active repositories를 새 contract로 이전하는 동안만 남기는 compatibility implementation이다. 신규 caller contract로 사용하지 않는다.

`security.yml` reusable workflow는 제거했다. Security와 language/migration CI orchestration은 각각 `.github/actions/ci/security`, `.github/actions/ci/group` 내부 action으로 통합하여 `project-ci.yml`과 legacy `pipeline.yml`이 같은 구현을 공유한다. `.github/workflows/validate.yml`은 이 implementation repository 자체의 workflow syntax/reference policy만 검증한다. `Mooner510/workflows`는 public repository이므로 이 repository 자체의 PR/push validation은 GitHub-hosted `ubuntu-latest`에서만 실행한다. Private consumer repository의 canonical CI는 Gharp가 job마다 발급하는 disposable repository-scoped JIT runner에서 실행한다. Public implementation repository인 `Mooner510/workflows` 자체 검증만 GitHub-hosted `ubuntu-latest`를 사용한다. Central `go-format.yml`도 `workflow_call` 전용이며 `workflows` repository 자체에서 직접 dispatch하지 않는다.

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

Language CI job names are kept compact and symmetric in the GitHub Actions sidebar:

```text
CI (Go 1.27.1)
CI (pnpm 11.27.1)
```

Node job display names intentionally show the exact package-manager version rather than repeating the Node runtime version. The runtime version remains part of the actual CI group configuration.

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

Production/default-branch CI is verification-only while the trusted deployment controller is not yet active.

```text
Security + Language/Migration CI + rootless BuildKit OCI build + Trivy
→ Verification
→ END
```

Repository runners do not publish canonical host images, import images into the trusted Docker runtime, read host deployment configuration, or mutate production state. A successful CI therefore means the revision and its OCI image were verified; it does not mean a production artifact was published.

Artifact publication will be re-enabled only through the separate trusted deployment controller.

## Development deployment

Automatic development host mutation is temporarily disabled under the isolated-runner model.

A push to `dev` still runs the complete Gharp verification plane:

```text
Security
Language / migration validation
rootless BuildKit OCI build
Trivy image scan
Verification
```

It does not read `/opt/stacks`, migrate the real development database, import an image into the host Docker runtime, or replace a development container. Those operations move to the trusted deployment controller.

## Trusted runtime ownership

The trusted deployment controller is the only future component allowed to own host mutation:

```text
runtime env / secret resolution
DB credential resolution
migration against real dev/prod DB
trusted image import/publication
container replacement
Docker hardening
HEALTHCHECK / readiness
state/history/rollback
```

Repository JIT runners never receive Docker/Podman/containerd runtime sockets, `/opt/stacks`, `/var/lib/stacks`, deployment secrets, or production/development database credentials.

Until the controller is implemented and verified, these runtime operations are intentionally unavailable from GitHub repository jobs.

## Production control plane

The thin consumer `deploy.yml` may continue to accept server-originated `repository_dispatch` events so the public API contract does not need another migration later.

Current behavior is fail closed:

```text
repository_dispatch
→ production.yml@v1
→ validate request/default branch/service
→ refuse host runtime mutation
```

No repository-runner production migration, container replacement, Docker image import, state mutation, or Caddy mutation is permitted.

When the trusted deployment controller is implemented, the same operator surface can hand the verified revision/artifact to that controller using short-lived, narrowly scoped authority.

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

Private personal/organization repositories는 모두 Gharp의 disposable repository-scoped JIT runner를 사용한다. 상주형 personal/organization self-hosted runner는 canonical execution plane이 아니다.

각 job은 generic `[self-hosted, linux]`만으로 배정하지 않고 run-scoped Gharp label을 함께 요구한다.

```text
gharp-contract-<run-id>-<attempt>
gharp-security-<run-id>-<attempt>
gharp-ci-<run-id>-<attempt>
gharp-image-<run-id>-<attempt>
gharp-verify-<run-id>-<attempt>
gharp-deploy-<run-id>-<attempt>
```

Repository job은 Docker/Podman/containerd socket, `/opt/stacks`, `/var/lib/stacks`, deployment secret, production runtime mutation 권한을 받지 않는다. OCI build는 runner에 의도적으로 노출된 rootless BuildKit socket만 사용한다.

Public repository는 self-hosted runner를 사용하지 않고 GitHub-hosted runner를 사용한다. Production/development host mutation은 repository runner와 분리된 trusted deployment controller가 소유하며, controller가 준비되기 전 publication/deployment는 fail closed한다.
