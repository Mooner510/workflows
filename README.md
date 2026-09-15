# workflows

공용 self-hosted GitHub Actions workflow 모음입니다.

지원 범위:

- Go
- Node.js / JavaScript / TypeScript
  - React / Next.js / Vite
  - NestJS / Express
- Android / Java / Kotlin

모든 중앙 Job은 다음 runner를 사용합니다.

```yaml
runs-on: [self-hosted, linux]
```

## 사용 방법

프로젝트에는 보통 두 workflow만 둡니다.

```text
.github/workflows/
├─ ci.yml
└─ deploy.yml   # production 배포가 필요한 경우
```

### 1. CI 추가

프로젝트의 `.github/workflows/ci.yml`에서 `pipeline.yml`을 호출합니다.

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
            "watch": ["services/api", "shared/proto"]
          },
          {
            "name": "web",
            "type": "node",
            "path": "apps/web",
            "watch": ["apps/web", "packages/ui", "pnpm-lock.yaml"]
          },
          {
            "name": "android",
            "type": "android",
            "path": "apps/android"
          }
        ]
```

변경된 component만 자동으로 감지해 **Security와 CI를 병렬 실행**합니다.

```text
detect
  ├─ security
  └─ CI
```

### 2. Component 설정

필수 값:

| 필드 | 설명 |
| --- | --- |
| `name` | component 이름 |
| `type` | `go`, `node`, `android` |
| `path` | repository 기준 component 경로 |

필요한 경우만 사용:

| 필드 | 설명 |
| --- | --- |
| `watch` | 이 component에 영향을 주는 추가 경로 |
| `go_version` | Go 버전, 기본 `stable` |
| `node_version` | Node.js 버전, 기본 `24` |
| `java_version` | Java 버전, 기본 `17` |
| `gradle_tasks` | Android CI task 변경 |

공용 package나 root lockfile 변경도 영향을 받는다면 `watch`에 추가합니다.

```json
{
  "name": "web",
  "type": "node",
  "path": "apps/web",
  "watch": ["apps/web", "packages/ui", "pnpm-lock.yaml"]
}
```

### 3. Production 배포 추가

일반 push에서는 production을 변경하지 않습니다.

Production은 stable Release 또는 `workflow_dispatch` 같은 **명시적 배포 workflow**에서 실행합니다.

프로젝트의 GitHub Environment에 `production`을 만들고 application/runtime secret을 등록합니다.

예:

```text
Settings
→ Environments
→ production
→ Environment secrets
```

`.github/workflows/deploy.yml` 예시:

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
            "path": "services/api"
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
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
        with:
          persist-credentials: false

      - name: Deploy
        uses: Mooner510/workflows/.github/actions/deploy@v1
        with:
          working-directory: services/api
```

각 프로젝트는 실제 배포 로직만 component 내부에 둡니다.

```text
services/api/.ci/deploy.sh
```

중앙 deploy action은 이 script의 경로를 검증한 뒤 실행합니다.

일반 application/runtime secret은 GitHub Environment Secrets를 사용합니다. 서버에 장기 보관하는 예외는 DB CLI가 관리하는 DB credential과 Android signing material입니다.

## 기본 CI

### Go

```text
gofmt check
go mod verify
go vet ./...
go test ./...
go build ./...
```

### Node.js

lockfile을 기준으로 의존성을 설치하고, 존재하는 script만 순서대로 실행합니다.

```text
lint
check-types / typecheck / type-check
test
build
```

pnpm 프로젝트는 `packageManager` 버전을 고정해야 합니다.

```json
{
  "packageManager": "pnpm@10.17.1"
}
```

### Android

기본 task:

```text
lintDebug
testDebugUnitTest
assembleDebug
```

## Security

변경된 component 전체를 검사합니다.

- Semgrep: SAST
- OSV-Scanner: dependency vulnerability
- Trivy: secret / misconfiguration

정기 전체 검사가 필요하면 caller에서 `force_all: true`로 실행합니다.

## Runner 요구사항

self-hosted Linux runner에 다음이 필요합니다.

```text
Bash
Git
jq
Docker
```

Android 빌드는 Android SDK도 필요합니다.

## Version

기본 사용:

```text
@v1
```

완전한 immutable pin이 필요하면 commit SHA를 사용합니다.
