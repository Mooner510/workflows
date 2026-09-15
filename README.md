# workflows

공용 self-hosted GitHub Actions 구성입니다.

공용으로 유지하는 것은 다음뿐입니다.

```text
.github/workflows/
├─ pipeline.yml
├─ security.yml
├─ ci-go.yml
├─ ci-node.yml
└─ ci-android.yml

.github/actions/
└─ deploy/action.yml
```

모든 중앙 Job은 다음 runner를 사용합니다.

```yaml
runs-on: [self-hosted, linux]
```

> `v1` tag 생성 전 테스트는 정확한 commit SHA를 사용합니다.

## CI

프로젝트의 `.github/workflows/ci.yml`에서 `pipeline.yml`을 호출합니다.

```yaml
name: CI

on:
  pull_request:
  push:
    branches: [main]

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

`path`는 항상 감시됩니다. `watch`는 추가 영향 경로입니다.

변경된 component만 대상으로 다음을 실행합니다.

```text
detect
  ├─ security
  ├─ Go CI
  ├─ Node CI
  └─ Android CI
```

같은 Go module, Node lockfile root, 언어 버전은 가능한 한 setup/install/verify를 공유합니다.

### Component

| 필드 | 설명 |
| --- | --- |
| `name` | component 이름 |
| `type` | `go`, `node`, `android` |
| `path` | component 경로 |
| `watch` | 추가 영향 경로 |
| `go_version` | 기본 `stable` |
| `node_version` | 기본 `24` |
| `java_version` | 기본 `17` |
| `gradle_tasks` | 기본 `lintDebug testDebugUnitTest assembleDebug` |

rename/move는 이전 경로와 새 경로를 모두 변경으로 처리합니다.

## 기본 검증

Go:

```text
gofmt
go mod download
go mod verify
go vet
go test
go build
```

Node.js:

```text
locked install
lint
check-types / typecheck / type-check
test
build
```

pnpm/Yarn은 root `package.json`의 `packageManager` 버전을 고정합니다.

Android:

```text
lintDebug
testDebugUnitTest
assembleDebug
```

## Security

공용 Security는 다음 네 도구만 사용합니다.

- Semgrep CE: SAST
- OSV-Scanner: dependency vulnerability
- Gitleaks: 현재 파일 + Git history secret scan
- Trivy: Dockerfile/IaC misconfiguration

모두 self-hosted runner에서 실행하며 scanner image는 digest로 고정합니다.

## CD

CD도 공통 부분은 중앙화합니다. 다만 **전체 deploy workflow는 프로젝트에 둡니다.**

프로젝트마다 다음이 다르기 때문입니다.

- Release/image build와 publish 방식
- 서비스 개수와 image 이름
- DB migration
- Android release
- 필요한 Environment Secret 이름

GitHub에서 reusable workflow를 호출하는 job에는 `environment:`를 지정할 수 없습니다. 모든 Environment Secret을 범용 workflow에서 자동 전달하기 위해 전체 `secrets` context를 직렬화하는 방식도 사용하지 않습니다.

따라서 프로젝트의 `deploy.yml`이 `environment: production`을 소유하고 필요한 secret만 Deploy step에 명시적으로 주입한 뒤, 공용 `deploy` action을 호출합니다.

```yaml
jobs:
  deploy:
    environment: production
    runs-on: [self-hosted, linux]

    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
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
          production-branch: main
```

공용 deploy action은 다음만 담당합니다.

- `workflow_dispatch` 또는 stable published Release인지 검증
- manual deploy가 production branch에서 시작됐는지 검증
- 실제 checkout된 commit이 production branch history에 포함되는지 검증
- deploy script 경로 검증
- project-owned deploy script 실행

실제 배포 구현은 project에 둡니다.

```text
<component>/.ci/deploy.sh
```

즉 중앙 repo에는 공통 안전 절차만 있고, GHCR build/publish, migration, service rollout 같은 project-specific CD 로직은 넣지 않습니다.

Application/runtime secret은 GitHub Environment Secrets를 사용합니다. DB CLI credential과 Android signing material만 서버 장기 보관 예외입니다.

## Runner

필수:

```text
Bash
Git
jq
Docker
```

Android CI에는 Android SDK가 추가로 필요합니다.

Persistent self-hosted runner에서는 신뢰하지 않는 fork/public PR 코드를 실행하지 않습니다.

## Version

안정 버전은 major tag를 사용합니다.

```text
@v1
```

더 강한 고정이 필요하면 full commit SHA를 사용합니다.
