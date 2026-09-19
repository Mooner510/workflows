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
   ├─ docker-service-rollback/
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

Production host와 runner를 공유하므로 중앙 pipeline은 `pull_request_target`과 fork PR을 self-hosted runner에서 실행하지 않습니다. Fork 기여를 별도로 검증하려면 production host와 분리된 runner/workflow를 사용합니다.

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

Migration CI는 언어 CI와 분리되어 상위 pipeline에서 한 번만 실행합니다. 일반 코드만 변경된 경우에는 실행하지 않고, 해당 component의 `migration_path`가 변경된 경우에만 실행합니다. Prisma는 `migration_schema` 변경도 migration 변경으로 취급합니다. `force_all`이거나 신뢰할 수 있는 diff 기준이 없는 경우에는 안전하게 전체 migration을 검증합니다.

세 engine 모두 component별 disposable `postgres:17-alpine`을 동적 loopback port로 시작하고, clean PostgreSQL에 전체 migration history를 실제 적용한 뒤 즉시 제거합니다.

```text
Goose  -> goose validate + goose up
Prisma -> prisma validate + prisma migrate deploy
Flyway -> source/plugin validation + flyway migrate
```

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

Prisma는 Node component, Flyway는 Java/Kotlin component에서 사용합니다. Prisma의 `migration_path`는 schema 파일 옆의 native `migrations` directory를 가리켜야 합니다.

CI의 PostgreSQL은 migration 검증 전용 disposable instance이며 production DB/credential을 사용하지 않습니다. CI에서는 production image를 `docker build`하지 않습니다.

Security:

```text
Semgrep CE
OSV-Scanner
Gitleaks current + history
Trivy misconfiguration
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
    SESSION_SECRET: ${{ secrets.SESSION_SECRET }}
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
    env-names-json: '["SESSION_SECRET"]'
```

### Database credential 자동 주입 (기본값)

`db-credential-source` 기본값은 `auto`입니다. CD는 다음 순서로 database credential을 맞춥니다.

```text
1. caller step env / GitHub Environment secret이 이미 있으면 그대로 사용
2. 없으면 /opt/stacks/projects/<project>/db.env에서 자동 resolve
3. migration에 필요한데 어느 쪽도 없으면 실패
```

따라서 self-hosted production CD에서는 보통 아래만으로 충분합니다.

```yaml
with:
  project: my-project
  migration-engine: goose
```

`DATABASE_URL`은 migration과 container env에 자동 주입됩니다. `db github-sync`나 deploy step의 `env: DATABASE_URL`은 필요 없습니다.

DB 외 secret(`SESSION_SECRET` 등)은 여전히 GitHub Environment에서 caller `env:`로 전달합니다.

GitHub에 `DATABASE_URL`을 미리 넣어두면 `auto`는 그 값을 우선 사용합니다. host `db.env`를 절대 읽지 않으려면 `db-credential-source: github`를 지정합니다.

`env-files-json`은 repository 안의 **비밀이 아닌** 설정 파일만 허용합니다. `/opt/stacks/projects/<project>/db.env`는 `env-files-json`로 mount하지 않고 `auto` resolve가 처리합니다.

선택적으로 `db github-sync`로 GitHub Environment secret을 미리 등록할 수 있습니다. `auto` 모드에서는 필수가 아닙니다.

공통 Docker runtime hardening이 필요한 service는 caller input으로 다음을 사용할 수 있습니다.

```text
init
security-opts-json
cap-drop-json
stop-timeout
```

예: `init: true`, `security-opts-json: '["no-new-privileges:true"]'`, `cap-drop-json: '["ALL"]'`.

Project workflow에는 가능한 한 다음만 남깁니다.

```text
trigger
concurrency
environment
permissions
checkout
secrets / environment variables
central action inputs
```

같은 service의 deploy와 manual rollback은 **동일한 concurrency group**을 사용해야 합니다. 중앙 composite action 자체는 job-level `concurrency`를 선언할 수 없으므로 caller가 직렬화를 소유합니다.

```yaml
concurrency:
  group: production-my-project-api
  cancel-in-progress: false
```

동일 service를 병렬로 mutate하면 container/state/history가 서로 경합할 수 있으므로 production caller에서 concurrency를 생략하지 않습니다.

## Deployment state / rollback

Canonical storage:

```text
/var/lib/stacks/projects/<project>/<service>/deploy/
├─ current.json
└─ history.jsonl
```

`current.json`은 마지막 verified known-good deployment입니다. `history.jsonl`에는 성공한 `deploy`/`rollback`과 rollback 실패 후 정상 원상복구(`rollback-restore`)만 기록합니다. State 갱신은 임시 파일을 사용해 current/history 불일치를 최소화합니다.

자동 rollback:

```text
새 container replace 이후 health/Caddy/state 실패
→ 직전 known-good image ID 복구
→ rollback health 확인
```

명시적 수동 rollback:

```text
.github/actions/cd/docker-service-rollback
```

이 action은 `history.jsonl`에서 현재 image와 다른 가장 최근 known-good deployment를 선택하고, 해당 image ID가 host에 남아 있는지 확인한 뒤 복구합니다. rollback 자체가 실패하면 원래 current deployment를 다시 복구합니다. DB migration은 down하지 않습니다.

따라서 image pruning은 최소한 현재 image와 직전 rollback 후보를 보존해야 합니다.

최초 중앙 배포가 health/Caddy 이후 state 기록 단계에서 실패하면 새 container뿐 아니라 새로 적용한 Caddy route도 제거하여 dangling route를 남기지 않습니다.

기존 `deploy` CLI의 state/history/rollback 책임은 중앙 CD가 직접 소유합니다.

## Migration CD

DB가 있는 service는 deploy마다 migrator를 호출합니다.

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