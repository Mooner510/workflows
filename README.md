# workflows

공용 self-hosted GitHub Actions CI/CD 구현입니다.

- default branch: `master`
- stable caller ref: movable `v1`
- `master`에 공용 workflow/action 변경이 merge되면 같은 작업에서 `v1`을 해당 `master` commit으로 즉시 이동

## 구조

```text
.github/workflows/
├─ pipeline.yml
├─ security.yml
└─ go-format.yml

.github/actions/
├─ ci/
│  ├─ go/
│  ├─ node/{npm,pnpm,yarn,bun}/
│  ├─ java-kotlin/
│  │  ├─ build-tools/{gradle,maven}/
│  │  └─ profiles/{android,spring-boot}/
│  ├─ go-postgres/
│  └─ migration/{goose,go-command,prisma,drizzle,flyway}/
└─ cd/
   ├─ guard/
   ├─ development-enabled/
   ├─ resolve-service-metadata/
   ├─ docker-service-production/
   ├─ docker-service-development/
   ├─ docker-process-production/
   ├─ docker-process-development/
   ├─ docker-process/
   ├─ android-production/
   ├─ docker-service/
   ├─ docker-service-rollback/
   ├─ docker/{build,deploy,health}/
   ├─ migration/{goose,go-command,prisma,drizzle,flyway}/
   ├─ state/
   └─ android/release/
```

Canonical runner selector는 CI/CD 모두 다음을 사용합니다.

```yaml
runs-on: [self-hosted, linux]
```

Personal repository는 Gharp, Organization repository는 Organization scoped self-hosted runner를 사용합니다. 현재 운영 제약상 같은 runner가 CI/CD를 수행합니다. Production은 `workflow_dispatch`와 실행자 검증으로 제한하고, development는 `dev` branch push에서만 허용합니다.

## CI

Project는 `.github/workflows/ci.yml`에서 중앙 `pipeline.yml`을 호출합니다. Node package manager는 lockfile에서, Go version은 가장 가까운 `go.mod`의 `toolchain` 또는 `go` directive에서 자동 감지하므로 특별한 이유가 없으면 caller가 중복 지정하지 않습니다. CI concurrency와 동일 언어 monorepo impact detection도 shared pipeline이 소유합니다. 실제 PostgreSQL application behavior test가 필요한 Go component는 `postgres_test` contract를 선언하면 disposable PostgreSQL lifecycle과 test 실행을 중앙이 처리합니다.

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
            "go_version": "1.27.1",
            "migration_engine": "goose",
            "migration_path": "db/migrations"
          }
        ]
```

언어별 기본 검증:

```text
Go          -> gofmt / mod verify / vet / test / build
Node        -> locked install / lint / type / test / build
Java/Kotlin -> Gradle or Maven + optional Android/Spring Boot profile
```

Migration CI는 언어 CI와 분리되어 상위 pipeline에서 한 번만 실행합니다. 일반 코드만 변경된 경우에는 실행하지 않고, 해당 component의 `migration_path`가 변경된 경우에만 실행합니다. Prisma는 `migration_schema`, Drizzle은 `migration_config` 변경도 migration 변경으로 취급합니다. `force_all`이거나 신뢰할 수 있는 diff 기준이 없는 경우에는 안전하게 전체 migration을 검증합니다.

네 engine 모두 component별 disposable `postgres:17-alpine`을 동적 loopback port로 시작하고, clean PostgreSQL에 전체 migration history를 실제 적용한 뒤 즉시 제거합니다.

```text
Goose      -> goose validate + goose up
Go command -> existing Go migrator + disposable PostgreSQL
Prisma     -> prisma validate + prisma migrate deploy
Drizzle    -> drizzle-kit check + drizzle-kit migrate
Flyway     -> source/plugin validation + flyway migrate
```

### Drizzle Kit migration CI/CD

Node/TypeScript service가 Drizzle ORM schema를 source of truth로 사용할 때 `migration_engine: drizzle`을 사용할 수 있습니다.

```json
{
  "name": "api",
  "type": "node",
  "path": "services/api",
  "migration_engine": "drizzle",
  "migration_path": "drizzle",
  "migration_config": "drizzle.config.ts"
}
```

CI는 `drizzle-kit check` 후 disposable PostgreSQL에 `drizzle-kit migrate`를 실제 적용합니다. CD는 같은 project-local `drizzle-kit`과 lockfile을 사용해 production DB에 pending migration을 적용합니다. Production CD에서 `drizzle-kit push`는 사용하지 않습니다.

### Goose SQL migration CI

Goose CI는 애플리케이션 언어와 무관합니다. ORM이나 Goose Go library가 없어도 SQL migration 디렉터리만 있으면 사용할 수 있습니다.

```json
{
  "name": "api",
  "type": "node",
  "path": "services/api",
  "migration_engine": "goose",
  "migration_path": "db/migrations"
}
```

동작:

```text
migration_path 확인
→ Goose SQL source validate
→ component 전용 임시 PostgreSQL container 시작
→ 모든 pending migration 실제 적용
→ PostgreSQL container 제거
```

Go migration source(`*.go`)는 지원하지 않습니다. 공용 CI에서는 portable Goose SQL migration(`*.sql`)만 허용합니다.

Goose 버전 우선순위:

```text
1. explicit goose_version
2. component에서 repository root 방향으로 가장 가까운 go.mod의 github.com/pressly/goose/v3 버전
3. 중앙 기본 버전 v3.28.0
```

Goose CLI는 언어 runtime에 의존하지 않도록 공식 Linux binary를 사용하고 release checksum을 검증한 뒤 runner tool cache에 저장합니다. CD도 같은 resolution 정책을 사용합니다.

Prisma와 Drizzle은 Node component, Flyway는 Java/Kotlin component에서 사용합니다. Prisma의 `migration_path`는 schema 파일 옆의 native `migrations` directory를 가리켜야 합니다. Drizzle은 project-local `drizzle-kit`, version-controlled SQL migration, `migration_config`를 사용합니다.

CI의 PostgreSQL은 migration 검증 전용 disposable instance이며 production DB/credential을 사용하지 않습니다. CI에서는 production image를 `docker build`하지 않습니다.

Security:

```text
Semgrep CE
OSV-Scanner
Gitleaks current + history
Trivy misconfiguration
```

## Manual Go formatting

Go repository에서 필요할 때만 수동으로 `gofmt`를 적용하는 reusable workflow입니다.

```text
.github/workflows/go-format.yml
```

Caller repository의 수동 실행 화면에서 **branch를 선택**하고 `path`만 입력합니다. `path` 기본값은 `/`이며 repository 전체의 모든 `*.go` 파일을 포맷합니다. 하위 디렉터리만 처리하려면 `services/api`처럼 repository-relative directory를 입력합니다.

Caller 예시:

```yaml
name: Go Format

on:
  workflow_dispatch:
    inputs:
      path:
        description: Repository-relative directory. / formats the whole repository.
        required: false
        type: string
        default: /

permissions:
  contents: write

jobs:
  format:
    name: Format Go files
    uses: Mooner510/workflows/.github/workflows/go-format.yml@v1
    with:
      path: ${{ inputs.path }}
```

동작:

```text
selected branch checkout
→ path가 repository 내부 directory인지 검증
→ path 하위 모든 *.go에 gofmt -w
→ 변경이 있을 때 workflow 실행 사용자 명의로 "style: apply gofmt" commit
→ 선택한 branch로 fast-forward push
→ 방금 push한 commit 기준으로 공용 Go CI(gofmt / mod verify / vet / test / build) 실행
→ 결과를 workflow summary에 표시
```

Go 파일이 없거나 이미 포맷되어 있으면 commit을 만들지 않습니다. Push는 기본 `GITHUB_TOKEN`을 사용하므로 이 포맷 커밋 자체가 별도의 `on: push` CI를 다시 트리거하지 않습니다. 대신 같은 Go Format run 안에서 push된 commit을 바로 검증합니다. Caller의 `contents: write` 권한이 필요하며, branch protection이 GitHub Actions push를 막는 branch에는 직접 push할 수 없습니다.

## CD

Docker CD는 production과 development를 명시적으로 분리합니다.

```text
Production
workflow_dispatch
+ github.actor == Mooner510
+ github.triggering_actor == Mooner510
+ caller repository default production branch
→ production resources

Development
push to dev
→ caller CI success
→ development entrypoint
→ development resources
```

Production은 stable GitHub Release로 자동 배포하지 않습니다. `release` event는 production mutation source가 아닙니다.

Generic HTTP Docker service entrypoints:

```text
.github/actions/cd/docker-service-production
.github/actions/cd/docker-service-development
```

Non-HTTP process entrypoints:

```text
.github/actions/cd/docker-process-production
.github/actions/cd/docker-process-development
```

Android production release는 `.github/actions/cd/android-production`을 사용하며 역시 `workflow_dispatch` 전용입니다. `version-env-prefix`를 쓰는 caller는 manual dispatch input을 `version`으로 전달합니다.

### Caller trigger contract

Development caller는 반드시 `dev` branch push에서 실행하고 CI 성공 이후 deploy job이 실행되도록 `needs`를 둡니다. 중앙 development action도 event/ref를 다시 검증합니다.

```yaml
on:
  push:
    branches: [dev]
  workflow_dispatch:
    inputs:
      operation:
        type: choice
        options: [deploy, rollback]
        default: deploy

jobs:
  ci:
    # shared pipeline caller

  deploy-dev:
    name: Deploy development
    if: github.event_name == 'push' && github.ref == 'refs/heads/dev'
    needs: ci
    runs-on: [self-hosted, linux]
    steps:
      - name: Deploy development API
        uses: Mooner510/workflows/.github/actions/cd/docker-service-development@v1
        with:
          working-directory: services/api
          service-name: my-project-api
          container-port: '8080'

  deploy-prod:
    name: Deploy production
    if: github.event_name == 'workflow_dispatch' && github.actor == 'Mooner510' && github.triggering_actor == 'Mooner510'
    runs-on: [self-hosted, linux]
    steps:
      - name: Deploy production API
        uses: Mooner510/workflows/.github/actions/cd/docker-service-production@v1
        with:
          working-directory: services/api
          service-name: my-project-api
          container-port: '8080'
```

Production caller의 `if`는 간단한 UI/run-level 차단입니다. 중앙 production guard도 같은 actor/event 조건을 다시 검증합니다. Workflow 자체를 수정할 수 있는 write 권한자를 상대로 한 강한 보안 경계로 취급하지 않습니다.

### Environment isolation

Production의 기존 host path와 resource 이름은 호환성을 위해 변경하지 않습니다. Development만 새 suffix/path를 사용합니다.

```text
Production
/opt/stacks/projects/<project>/db.env
/opt/stacks/projects/<project>/deploy.env
/opt/stacks/projects/<project>/<service>/deploy.env
/opt/stacks/projects/<project>/<service>/secret/deploy.env
network: project-<project>
service: <service>
image: <image>
state: /var/lib/stacks/projects/<project>/<service>/deploy/

Development
/opt/stacks/projects/<project>/db.dev.env
/opt/stacks/projects/<project>/deploy.dev.env
/opt/stacks/projects/<project>/<service>/deploy.dev.env
/opt/stacks/projects/<project>/<service>/secret/deploy.dev.env
network: project-<project>-dev
service: <service>-dev
image: <image>-dev
state: /var/lib/stacks/projects/<project>/<service>/dev/deploy/
```

Development resolver는 production `db.env`, `deploy.env`, `secret/deploy.env`로 fallback하지 않습니다. Dev config가 없으면 명시적으로 실패합니다. DB instance는 shared PostgreSQL을 공유하되 prod/dev database와 role은 별도입니다.

Runtime overlay는 환경 안에서만 적용합니다.

```text
prod: project deploy.env -> service deploy.env -> service secret/deploy.env
dev:  project deploy.dev.env -> service deploy.dev.env -> service secret/deploy.dev.env
```

`DEPLOY_DOMAIN`은 routing source가 아닙니다. Routing은 `project` CLI가 소유하는 `addr.env` / `addr.dev.env`와 Caddy reconcile 로직에서만 결정합니다. Central workflows는 address를 해석하거나 Caddy 파일을 직접 쓰지 않습니다. `DEPLOY_VOLUMES_JSON`은 선택된 runtime env 파일을 계속 사용합니다. Mutable persistent volume은 dev가 prod와 같은 host path/volume을 지정하지 않는 것이 원칙이며 중앙 workflow는 임의 path를 자동 변환하지 않습니다.

### Address / CORS metadata

Address와 CORS는 `deploy.env`에서 분리된 host metadata입니다. Project/service registry와 project network는 배포 전에 이미 존재해야 하며 workflows는 이를 생성하지 않습니다.

```text
project CLI owns:
- /opt/stacks/projects/<project>/<service>/ service registry
- project-<project> / project-<project>-dev networks
- addr metadata and generated Caddy routes

central workflows:
- require the registry and project network to exist
- always attach HTTP containers to existing caddy-shared
- inject CORS_ORIGINS from cors metadata
- request route reconciliation only through project-addr-sync
```


```text
Production
/opt/stacks/projects/<project>/addr.env
/opt/stacks/projects/<project>/<service>/addr.env
/opt/stacks/projects/<project>/cors.env
/opt/stacks/projects/<project>/<service>/cors.env

Development
/opt/stacks/projects/<project>/addr.dev.env
/opt/stacks/projects/<project>/<service>/addr.dev.env
/opt/stacks/projects/<project>/cors.dev.env
/opt/stacks/projects/<project>/<service>/cors.dev.env
```

Address resolution:

```text
prod:
  service addr.env
  -> project addr.env
  -> none

dev:
  service addr.dev.env
  -> project addr.dev.env
  -> derive from effective production address by prefixing hostname with dev-
  -> none
```

Derived development address preserves scheme and explicit port. Production address가 없으면 development도 자동 생성하지 않습니다.

HTTP deploy/rollback은 health 성공 후 제한된 `project-addr-sync` wrapper를 호출합니다. Wrapper는 `project addr <project>/<service> sync [-dev]`만 실행할 수 있으며 Caddy 파일을 직접 수정하는 코드는 `project` CLI에만 존재합니다. Effective address가 없거나 service가 실행 중이 아니면 project CLI가 자신이 관리하는 stale route를 제거합니다. `project addr ... set/rm`도 동일한 reconcile 경로를 사용합니다.

CORS는 environment 간 자동 상속이나 derivation을 하지 않습니다.

```text
prod CORS = project cors.env + service cors.env
dev CORS  = project cors.dev.env + service cors.dev.env
```

각 파일은 origin 하나당 한 줄이며 중복 제거 후 `CORS_ORIGINS` comma-separated environment variable로 container에 주입됩니다. HTTP/non-HTTP service 종류와 관계없이 동일하게 적용하며 파일이 없으면 변수를 주입하지 않습니다. CORS 파일 변경은 실행 중 container에 즉시 반영하지 않으므로 redeploy가 필요합니다.

### Development enable flag

Development deployment는 기본 enabled입니다. 다음 marker 중 하나가 있으면 자동 dev deployment를 성공적으로 skip합니다.

```text
/opt/stacks/projects/<project>/.dev-disabled
/opt/stacks/projects/<project>/<service>/.dev-disabled
```

Project marker가 service marker보다 우선합니다. Marker는 dev 설정 파일 편집이나 DB 관리 자체를 막지 않고 자동 dev service 실행만 막습니다.

### Deployment identity and labels

Canonical labels:

```text
com.mooner510.stacks.managed=true
com.mooner510.stacks.project=<project>
com.mooner510.stacks.service=<logical-service>
com.mooner510.stacks.environment=prod|dev
com.mooner510.stacks.kind=http|process
com.mooner510.stacks.container-port=<port>  # HTTP only
```

기존 `com.mooner510.workflows.service`와 OCI revision label도 유지합니다. Project CLI가 생성하는 Caddy site는 project/service/environment namespace를 포함한 `<project>--<service>--<prod|dev>.caddy`를 사용하고 ownership marker를 기록합니다.

### Rollback and state

Production manual rollback은 기존 dispatch operation을 사용합니다. Development는 push deployment 전용이며 manual rollback dispatch entrypoint를 제공하지 않습니다. HTTP deploy 실패 시 environment별 previous known-good image를 이용한 자동 rollback은 유지됩니다.

```text
prod: /var/lib/stacks/projects/<project>/<service>/deploy/
dev:  /var/lib/stacks/projects/<project>/<service>/dev/deploy/
```

새 state record에는 `environment` field가 저장됩니다. 기존 production record에 이 field가 없어도 읽기 호환성을 유지합니다. Production 기존 path는 migration하지 않습니다.

## Migration CD

DB가 있는 service는 deploy마다 선택된 environment database에 migrator를 호출합니다. Production/development migrator는 self-hosted runner host에서 실행되므로 shared PostgreSQL의 loopback publish(`127.0.0.1:<DB_PORT>`)를 사용하고, application container runtime은 선택된 `db.env` 또는 `db.dev.env`의 Docker DNS host를 사용합니다.

```text
Goose   -> DATABASE_URL
Prisma  -> DATABASE_URL
Drizzle -> DATABASE_URL
Flyway  -> FLYWAY_URL (+ optional user/password)
```

이미 적용된 migration은 각 engine의 migration history가 no-op 처리합니다. Git diff로 migration 실행 여부를 결정하지 않습니다.

DB destructive down migration은 자동 rollback하지 않습니다.

## Android

Android production release는 Docker CD와 분리합니다.

```text
.github/actions/cd/android/release
```

Signing material은 CI가 아니라 production CD에서만 사용합니다.

## Cache

Gharp host-persistent cache:

```text
/opt/cache/
├─ android-sdk/
├─ gradle/
└─ tool-cache/
```

일반 self-hosted runner에서는 runner 기본 cache 경로를 사용합니다.

## Version policy

```text
master -> current development implementation
v1     -> verified movable stable implementation
```

`Mooner510/workflows`의 공용 workflow/action 변경은 `master` merge와 같은 작업에서 `v1`을 동일 commit으로 이동합니다. Caller는 항상 `@v1`을 사용하며 `@master`나 특정 workflows commit SHA를 실행 ref로 사용하지 않습니다.

외부 third-party GitHub Action의 full commit SHA pinning 정책은 이 규칙과 별개이며 그대로 유지합니다.