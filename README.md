# workflows

공용 self-hosted GitHub Actions CI/CD 구현입니다.

- default branch: `master`
- stable caller ref: movable `v1`
- `master` 변경은 실제 caller 검증 후에만 `v1`으로 승격

## 구조

```text
.github/workflows/
├─ pipeline.yml
└─ security.yml

.github/actions/
├─ ci/
│  ├─ go/
│  ├─ node/{npm,pnpm,yarn,bun}/
│  ├─ java-kotlin/
│  │  ├─ build-tools/{gradle,maven}/
│  │  └─ profiles/{android,spring-boot}/
│  └─ migration/{goose,prisma,flyway}/
└─ cd/
   ├─ guard/
   ├─ docker-service/
   ├─ docker/{build,deploy,health}/
   ├─ migration/{goose,prisma,flyway}/
   ├─ caddy/
   ├─ state/
   └─ android/release/
```

기본 runner:

```yaml
runs-on: [self-hosted, linux]
```

## CI

Project는 `.github/workflows/ci.yml`에서 중앙 `pipeline.yml`을 호출합니다.

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
            "go_version": "1.27.1"
          }
        ]
```

언어별 기본 검증:

```text
Go          -> gofmt / mod verify / vet / test / build
Node        -> locked install / lint / type / test / build
Java/Kotlin -> Gradle or Maven + optional Android/Spring Boot profile
```

CI에서는 production image를 `docker build`하지 않습니다.

Security:

```text
Semgrep CE
OSV-Scanner
Gitleaks current + history
Trivy misconfiguration
```

Migration source action은 다음 engine 기준으로 구성합니다.

```text
Go API          -> Goose
Node API        -> Prisma Migrate
Java/Kotlin API -> Flyway
```

## CD

API/Web Docker service의 기본 entrypoint:

```text
.github/actions/cd/docker-service
```

Flow:

```text
Guard
→ read previous known-good state
→ Docker build
→ migration when configured
→ Docker simple replace
→ HTTP health
→ Caddy validate/reload
→ state/history record
```

Example:

```yaml
- name: Deploy API
  uses: Mooner510/workflows/.github/actions/cd/docker-service@v1
  env:
    DATABASE_URL: ${{ secrets.DATABASE_URL }}
  with:
    production-branch: main
    project: my-project
    working-directory: services/api
    service-name: api
    image-name: my-project-api
    container-port: '8080'
    host-port: '40100'
    domain: api.example.com
    migration-engine: goose
    migration-path: db/migrations
```

Project workflow에는 가능한 한 다음만 남깁니다.

```text
trigger
concurrency
environment: production
permissions
checkout
secrets
central action inputs
```

## Deployment state

Canonical storage:

```text
/var/lib/stacks/projects/<project>/<service>/deploy/
├─ current.json
└─ history.jsonl
```

`current.json`은 마지막 verified known-good deployment입니다.

저장 값:

```text
project
service
revision
image
imageId
deployedAt
event
```

`history.jsonl`에는 성공한 `deploy`/`rollback`만 append합니다.

Rollback 기준:

```text
1. current.json의 마지막 known-good imageId
2. state가 없는 최초 중앙 CD 전환에서만 기존 running container image ID
```

새 container가 실제로 배포되기 전 build/migration 단계에서 실패하면 기존 service를 건드리지 않습니다.

Replace 이후 health/Caddy/state 기록이 실패하면 previous known-good image ID로 복구하고 rollback health를 확인합니다.

기존 `deploy` CLI의 state/history/rollback 책임은 중앙 CD가 직접 소유합니다.

## Migration CD

DB가 있는 API는 deploy마다 migrator를 호출합니다.

```text
Goose  -> DATABASE_URL
Prisma -> DATABASE_URL
Flyway -> FLYWAY_URL (+ optional user/password)
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

`v1`은 caller canary와 action path/state/rollback 검증이 끝나기 전에는 이동하지 않습니다.

현재 `master`에 구현이 존재하더라도 실제 caller 검증 전에는 stable로 간주하지 않습니다.
