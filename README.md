# workflows

공용 self-hosted GitHub Actions CI/CD 구현입니다.

기본 브랜치는 `master`입니다. `v1`은 검증된 최신 v1 구현을 가리키는 movable stable branch이며, `master` 변경은 실제 검증이 끝난 뒤에만 `v1`으로 승격합니다.

## 구조

```text
.github/workflows/
├─ pipeline.yml
└─ security.yml

.github/actions/
├─ ci/
│  ├─ go/
│  │  └─ action.yml
│  ├─ java-kotlin/
│  │  ├─ action.yml
│  │  ├─ build-tools/
│  │  │  ├─ gradle/action.yml
│  │  │  └─ maven/action.yml
│  │  └─ profiles/
│  │     ├─ android/action.yml
│  │     └─ spring-boot/action.yml
│  ├─ node/
│  │  ├─ action.yml
│  │  ├─ npm/action.yml
│  │  ├─ pnpm/action.yml
│  │  ├─ yarn/action.yml
│  │  └─ bun/action.yml
│  └─ migration/
│     ├─ action.yml
│     ├─ goose/action.yml
│     ├─ prisma/action.yml
│     └─ flyway/action.yml
└─ cd/
   ├─ guard/action.yml
   ├─ docker-service/action.yml
   ├─ docker/
   │  ├─ build/action.yml
   │  ├─ deploy/action.yml
   │  └─ health/action.yml
   ├─ migration/
   │  ├─ goose/action.yml
   │  ├─ prisma/action.yml
   │  └─ flyway/action.yml
   ├─ caddy/action.yml
   └─ android/
      └─ release/action.yml
```

모든 Job은 기본적으로 다음 runner를 사용합니다.

```yaml
runs-on: [self-hosted, linux]
```

Gharp ephemeral runner와 일반 organization/repository scoped self-hosted runner를 모두 사용할 수 있습니다.

# CI

프로젝트는 `.github/workflows/ci.yml`에서 중앙 `pipeline.yml`만 호출합니다.

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
            "migration_engine": "goose",
            "migration_path": "db/migrations"
          },
          {
            "name": "web",
            "type": "node",
            "path": "apps/web"
          },
          {
            "name": "backend",
            "type": "java-kotlin",
            "path": "services/backend",
            "build_tool": "gradle",
            "profile": "spring-boot",
            "migration_engine": "flyway"
          },
          {
            "name": "android",
            "type": "java-kotlin",
            "path": "apps/android",
            "build_tool": "gradle",
            "profile": "android"
          }
        ]
```

`android` component type은 기존 caller 호환을 위해 계속 지원되며 내부적으로 다음과 동일하게 정규화됩니다.

```json
{
  "type": "java-kotlin",
  "build_tool": "gradle",
  "profile": "android"
}
```

## CI 흐름

```text
Detect changes
├─ Security
└─ Dynamic CI matrix
   ├─ Go <version>
   │  └─ Migration CI (optional)
   ├─ Node <version> / <package manager>
   │  └─ Migration CI (optional)
   └─ Java <version> / <build tool> / <profile>
      └─ Migration CI (optional)
```

변경된 component만 선택하고 실제 필요한 group만 matrix entry로 생성합니다.

### Go

```text
gofmt
go mod download
go mod verify
go vet
go test
go build
```

Private Go module은 기존과 같이 `go_private_patterns`와 `CI_PRIVATE_REPO_TOKEN`을 사용합니다.

### Node

Node CI는 framework를 구분하지 않습니다. Next.js, NestJS, Vite, React, Vue, Nuxt 등의 차이는 `package.json` scripts가 담당합니다.

공통 실행 순서:

```text
locked install
lint
check-types / typecheck / type-check / check
test
build
```

lockfile을 기준으로 package manager를 판별하고 실제 설치 구현은 분리되어 있습니다.

```text
node
├─ npm  -> npm ci
├─ pnpm -> pnpm install --frozen-lockfile
├─ yarn -> yarn install --frozen-lockfile / --immutable
└─ bun  -> bun install --frozen-lockfile
```

pnpm, Yarn, Bun은 root `package.json`의 `packageManager` 버전을 고정해야 합니다.

### Java/Kotlin

Java/Kotlin CI는 build tool과 profile을 서로 독립된 축으로 취급합니다.

```text
java-kotlin
├─ build-tools
│  ├─ gradle
│  └─ maven
└─ profiles
   ├─ android
   └─ spring-boot
```

일반 Java/Kotlin component는 `profile: none`을 사용합니다.

Gradle 기본 task:

```text
build
```

Android profile 기본 task:

```text
lintDebug testDebugUnitTest assembleDebug
```

Maven 기본 goal:

```text
verify
```

필요하면 component의 `gradle_tasks` 또는 `maven_goals`로 덮어쓸 수 있습니다.

### Migration CI

Migration CI는 `migration_engine`을 지정한 component에서 언어 CI 뒤에 자동 실행됩니다.

```text
Go          -> goose
Node        -> prisma
Java/Kotlin -> flyway
```

지원 값:

```text
none | goose | prisma | flyway
```

`migration_engine`을 생략하면 migration CI를 수행하지 않습니다.

Migration CI는 production DB에 연결하거나 migration을 적용하지 않습니다.

- Goose: pinned `github.com/pressly/goose/v3`의 `goose validate` 실행
- Prisma: local Prisma CLI의 `prisma validate` + migration directory/SQL 구조 검증
- Flyway: 기본 migration naming/중복/빈 파일 검증 + Gradle/Maven Flyway plugin availability 검증

기본 경로:

```text
Goose  -> db/migrations
Prisma -> prisma/migrations
Flyway -> src/main/resources/db/migration
```

필요하면 `migration_path`로 덮어쓸 수 있습니다. Prisma schema는 `migration_schema`로 지정하며 기본값은 `prisma/schema.prisma`입니다.

실제 DB schema history와 pending migration 적용 여부는 CI가 아니라 CD의 migration 단계가 담당합니다.

## CI에서 Docker image를 build하지 않음

CI는 source/dependency/lint/type/test/build/migration-source 검증까지만 담당합니다.

```text
CI
→ source validation
→ language build
→ tests
→ migration source validation
```

`docker build`는 production CD의 첫 단계에서만 실행합니다. Security workflow의 Semgrep/OSV/Gitleaks/Trivy scanner container 실행은 Docker image build가 아니므로 그대로 유지합니다.

# Security

공용 Security:

- Semgrep CE: SAST
- OSV-Scanner: dependency vulnerability
- Gitleaks: current tree + Git history secret scan
- Trivy: Dockerfile/IaC misconfiguration
- third-party GitHub Action full SHA pin 검증

Go, Node, Java/Kotlin, legacy Android component를 모두 처리합니다.

# CD

CD 구현도 중앙화합니다. 단, caller의 GitHub Environment와 secrets 경계를 유지하기 위해 프로젝트에는 매우 얇은 `deploy.yml`을 둡니다.

프로젝트 workflow가 소유하는 것:

```text
trigger
environment: production
runner
permissions
secrets
checkout
```

실제 배포 구현은 중앙 Composite Action이 소유합니다.

## API / Web Docker service

API와 Web은 언어와 무관하게 동일한 Docker service CD를 사용합니다.

```text
Guard
↓
Docker build
↓
Migration (API only, optional)
↓
Docker replace
↓
HTTP health check
↓
Caddy reverse proxy registration
```

공통 action:

```text
.github/actions/cd/docker-service
```

Web은 `migration-engine: none`을 사용합니다.

API migration engine:

```text
goose
prisma
flyway
```

기본 기술 정책:

```text
Go          -> Goose
Node        -> Prisma
Java/Kotlin -> Flyway
```

Migration은 Git diff로 실행 여부를 결정하지 않습니다. API deploy마다 migrator를 항상 실행하고, 이미 적용된 migration은 Goose/Prisma/Flyway의 DB migration history에 의해 no-op 처리됩니다.

이 규칙은 재배포, 이전 실패 후 재시도, 여러 release 건너뛰기에서도 migration 누락을 방지합니다.

### Docker service 예시

```yaml
jobs:
  deploy:
    environment: production
    runs-on: [self-hosted, linux]
    permissions:
      contents: read

    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          fetch-depth: 0
          persist-credentials: false

      - name: Deploy API
        uses: Mooner510/workflows/.github/actions/cd/docker-service@v1
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
        with:
          production-branch: main
          working-directory: services/api
          service-name: my-api
          image-name: my-api
          container-port: '8080'
          host-port: '40100'
          domain: api.example.com
          migration-engine: goose
          migration-path: db/migrations
          env-files-json: '["/opt/stacks/projects/my-project/api/deploy.env", "/opt/stacks/projects/my-project/api/secret/deploy.env"]'
```

Docker service는 host에 `127.0.0.1:<host-port>:<container-port>`로만 bind하고 Caddy가 public ingress를 담당합니다.

Caddy 기본 경로:

```text
/opt/stacks/shared/caddy/Caddyfile
/opt/stacks/shared/caddy/sites/*.caddy
```

main Caddyfile은 sites directory를 import하도록 서버에서 한 번 구성해야 합니다.

기본 rollout은 A/B가 아닌 simple replace입니다. 새 image build와 migration은 기존 container를 건드리기 전에 끝납니다. 새 container가 시작되지 않거나 health/Caddy 단계가 실패하면 가능한 경우 이전 image로 복귀합니다.

Migration은 backward-compatible schema change를 기본 전제로 합니다. DB migration 자체를 자동 rollback하지는 않습니다.

## Migration engine contracts

### Goose

- `DATABASE_URL` 필요
- `github.com/pressly/goose/v3`가 Go module graph에 version pin되어 있어야 함
- 기본 migration path: `db/migrations`

### Prisma

- `DATABASE_URL` 필요
- npm/pnpm/Yarn/Bun 지원
- lockfile install 후 local Prisma CLI 실행
- 기본 schema: `prisma/schema.prisma`

### Flyway

- `FLYWAY_URL` 필요
- Gradle/Maven wrapper 지원
- 기본 migration path: `src/main/resources/db/migration`
- `FLYWAY_USER`, `FLYWAY_PASSWORD` 등 Flyway 환경변수는 caller Environment Secret에서 전달

# Android CD

Android는 Docker CD와 완전히 분리합니다.

```text
Guard
↓
Java / Android SDK
↓
App Signing
↓
Gradle release build
↓
SHA-256
↓
GitHub Release upload (optional)
```

공통 action:

```text
.github/actions/cd/android/release
```

필요한 signing 환경변수:

```text
ANDROID_KEYSTORE_PASSWORD
ANDROID_KEY_ALIAS
ANDROID_KEY_PASSWORD
```

Gradle에는 다음 project property로 전달합니다.

```text
releaseStoreFile
releaseStorePassword
releaseKeyAlias
releaseKeyPassword
```

각 Android project의 signingConfig는 이 property contract를 사용해야 합니다.

GitHub Release upload를 사용할 경우 caller는 `contents: write`와 `GH_TOKEN`을 release step에 제공해야 합니다.

# Runner / 권한

현재 운영 모델은 한 대의 self-hosted 서버에서 CI와 CD를 모두 수행할 수 있도록 설계합니다.

```text
1 physical server
└─ Gharp or organization scoped self-hosted runner
   ├─ CI
   └─ CD
```

CI에서는 production secret을 주입하지 않고 Docker image도 build하지 않습니다.

CD step에서만 production Environment Secret, host Docker, Caddy, `/opt/stacks` 배포 파일을 사용합니다.

필수 runner 도구:

```text
Bash
Git
jq
curl
Docker
```

CD 사용 시 추가:

```text
Caddy CLI
```

Android GitHub Release upload 사용 시:

```text
gh
```

## Persistent cache

Gharp에서는 선택적으로 다음 경로를 재사용합니다.

```text
/opt/cache/
├─ android-sdk/
├─ gradle/
└─ tool-cache/
```

일반 self-hosted runner에서는 `/opt/cache` 없이 runner 기본 경로를 사용합니다.

# Version

검증된 caller는 기본적으로 다음 movable stable ref를 사용합니다.

```text
@v1
```

`master`에 직접 반영된 변경은 canary/실제 CI/CD 검증이 끝나기 전에는 `v1`을 이동하지 않습니다.
