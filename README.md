# workflows

공용 self-hosted GitHub Actions workflow 모음입니다.

지원 범위:

- Go
- Node.js / JavaScript / TypeScript
- Android / Java / Kotlin

모든 중앙 Job은 다음 runner를 사용합니다.

```yaml
runs-on: [self-hosted, linux]
```

## 사용 방법

프로젝트에는 보통 다음 두 workflow만 둡니다.

```text
.github/workflows/
├─ ci.yml
└─ deploy.yml   # production 배포가 필요한 경우
```

> `v1` tag가 생성된 뒤에는 `@v1`을 사용합니다. 그 전 테스트는 검증할 commit SHA로 `@<sha>`를 사용합니다.

### 1. CI 추가

`.github/workflows/ci.yml`:

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
            "watch": ["shared/proto"]
          },
          {
            "name": "web",
            "type": "node",
            "path": "apps/web",
            "watch": ["packages/ui", "pnpm-lock.yaml"]
          },
          {
            "name": "android",
            "type": "android",
            "path": "apps/android"
          }
        ]
```

`path`는 항상 자동 감시됩니다. `watch`는 여기에 **추가로** 영향을 주는 경로만 적습니다.

변경된 component만 감지하여 Security와 CI를 실행합니다.

```text
detect
  ├─ security
  ├─ Go CI
  ├─ Node CI
  └─ Android CI
```

같은 Go module, 같은 Node lockfile root, 같은 언어 버전을 공유하는 component는 setup/install/verify를 가능한 한 한 번만 수행합니다.

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
| `watch` | `path` 외에 이 component에 영향을 주는 경로 |
| `go_version` | Go 버전, 기본 `stable` |
| `node_version` | Node.js 버전, 기본 `24` |
| `java_version` | Java 버전, 기본 `17` |
| `gradle_tasks` | Android task 변경. 기본 `lintDebug testDebugUnitTest assembleDebug` |

파일 rename/move는 이전 경로와 새 경로가 모두 변경으로 취급됩니다.

### 3. Production 배포 추가

일반 push는 production을 변경하지 않습니다.

Production은 stable GitHub Release 또는 `workflow_dispatch`에서 명시적으로 실행합니다.

먼저 repository에 GitHub Environment `production`을 만들고 application/runtime secret을 등록합니다.

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

    steps:
      - name: Checkout production source
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          fetch-depth: 0
          persist-credentials: false

      - name: Deploy
        uses: Mooner510/workflows/.github/actions/deploy@v1
        env:
          OAUTH_CLIENT_SECRET: ${{ secrets.OAUTH_CLIENT_SECRET }}
          JWT_SECRET: ${{ secrets.JWT_SECRET }}
        with:
          working-directory: services/api
          production-branch: master
```

`production-branch`에는 실제 production source branch를 적습니다. `main`을 쓰는 프로젝트라면 `main`으로 변경합니다.

`workflow_dispatch` 배포는 **반드시 해당 `production-branch`를 선택한 상태에서 실행**해야 합니다.

중앙 deploy action은 다음을 검사한 뒤 project의 deploy script를 실행합니다.

- 실행 이벤트가 `workflow_dispatch` 또는 stable published Release인지
- manual 배포가 정확히 `production-branch`에서 실행됐는지
- 배포 commit이 `production-branch` history에 포함되는지
- Release 사용 시 tag가 `vMAJOR.MINOR.PATCH`이고 draft/pre-release가 아닌지
- deploy script가 선택한 component 내부에 있는지

실제 배포 로직은 project에 둡니다.

```text
services/api/.ci/deploy.sh
```

배포 script는 project 정책에 따라 immutable artifact/digest, 필요한 migration, rollout, health 확인을 수행합니다.

Application/runtime secret은 GitHub Environment Secrets를 사용하고 필요한 Deploy step에만 주입합니다. DB CLI가 직접 관리하는 DB credential과 Android signing material은 서버 장기 보관 예외입니다.

## 기본 CI

### Go

```text
gofmt check
go mod download
go mod verify
go vet
go test
go build
```

같은 Go module의 format/download/verify는 한 번만 수행합니다.

### Node.js

lockfile root마다 의존성을 한 번 설치한 뒤 각 component의 존재하는 script를 실행합니다.

```text
lint
check-types / typecheck / type-check
test
build
```

pnpm 또는 Yarn을 사용하는 프로젝트는 root `package.json`의 `packageManager` 버전을 고정해야 합니다.

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

변경된 component를 한 Security job에서 중복 없이 검사합니다.

- **Semgrep CE**: source SAST
- **OSV-Scanner**: dependency vulnerability
- **Gitleaks**: 현재 파일 + git history secret scan
- **Trivy**: Dockerfile/IaC 등 misconfiguration

스캐너는 self-hosted runner에서 로컬로 실행하며 서비스별 유료 요청량에 의존하지 않습니다. Scanner image는 digest로 고정합니다.

Gitleaks는 현재 파일과 현재 commit range를 모두 검사하므로, 현재 남아 있는 secret과 commit 후 삭제된 secret을 함께 검사합니다. 전체 재검사가 필요한 실행에서는 전체 history를 검사합니다.

Semgrep rule과 OSV/Trivy database·check는 최신 보안 정보를 사용하기 위해 실행 시 네트워크에서 갱신될 수 있습니다. 분석 자체는 self-hosted runner에서 수행됩니다.

Android는 Gradle dependency locking 또는 `gradle/verification-metadata.xml` 같은 지원 metadata가 없으면 dependency vulnerability coverage가 제한되며 warning을 출력합니다.

## Runner 요구사항

self-hosted Linux runner에 다음이 필요합니다.

```text
Bash
Git
jq
Docker
```

Android 빌드는 Android SDK도 필요합니다.

**persistent self-hosted runner에서는 신뢰하지 않는 fork/public PR 코드를 실행하지 않습니다.** CI 과정의 dependency install/test/build 자체가 repository 코드를 실행할 수 있기 때문입니다.

## Version

안정 버전이 발행되면 기본적으로 major tag를 사용합니다.

```text
@v1
```

특정 버전을 완전히 고정해야 하면 commit SHA를 사용합니다.
