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

`v1`은 검증된 v1 계열 최신 버전을 가리키는 **movable stable branch**입니다. Caller는 기본적으로 `@v1`을 사용하고, 중앙 변경은 검증 완료 후에만 `v1` ref를 앞으로 이동합니다.

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
            "path": "apps/web"
          },
          {
            "name": "android",
            "type": "android",
            "path": "apps/android"
          }
        ]
```

기본적으로 `path`는 자동 감시되고 `watch`는 추가 영향 경로입니다.

Repository root가 실제 작업 경로지만 일부 경로 변경에만 반응해야 하면:

```json
{
  "name": "go",
  "type": "go",
  "path": ".",
  "watch_path": false,
  "watch": ["go.mod", "go.sum", "cmd", "internal", "pkg"]
}
```

`watch_path: false`는 `path` 자체를 변경 감지에서 제외합니다. 이 경우 `watch`가 최소 하나 필요합니다.

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
| `path` | component 작업 경로 |
| `watch` | 추가 영향 경로 |
| `watch_path` | 기본 `true`; `false`면 `path`를 변경 감지에서 제외 |
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

Private GitHub Go module이 있으면 caller가 범위와 read-only token만 전달합니다.

```yaml
with:
  go_private_patterns: 'github.com/example/*'
secrets:
  go_private_token: ${{ secrets.GO_PRIVATE_TOKEN }}
```

토큰은 runner의 임시 Git config에만 기록되고 job 종료 시 삭제됩니다.

Node.js:

```text
locked install
lint
check-types / typecheck / type-check / check
test
build
```

존재하는 script만 실행하며 type/check 계열은 첫 번째로 발견된 하나만 실행합니다. pnpm/Yarn은 root `package.json`의 `packageManager` 버전을 고정합니다.

Android:

```text
lintDebug
testDebugUnitTest
assembleDebug
```

Android SDK command-line tools와 라이선스는 CI가 자동으로 준비하며, Gradle이 프로젝트에 필요한 SDK 패키지를 내려받을 수 있습니다.

## Security

공용 Security는 다음 네 도구만 사용합니다.

- Semgrep CE: SAST
- OSV-Scanner: dependency vulnerability
- Gitleaks: 현재 파일 + Git history secret scan
- Trivy: Dockerfile/IaC misconfiguration

모두 self-hosted runner에서 실행하며 scanner image는 digest로 고정합니다.

## CD

CD도 공통 부분은 중앙화하지만 **전체 deploy workflow는 프로젝트에 둡니다.**

Project별 Release/image build, GHCR naming, migration, Android release, service rollout, Environment Secret 이름은 서로 다릅니다. 또한 reusable workflow 호출 job에는 caller의 `environment:`를 붙일 수 없으므로, project `deploy.yml`이 `environment: production`을 소유합니다.

필요한 secret만 Deploy step에 명시적으로 주입하고 공용 deploy action을 호출합니다.

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
        with:
          working-directory: .
          production-branch: main
          script: infra/deploy/release.sh
          arguments-json: '["v1.2.0", "<commit>", "<image-digest>"]'
```

`arguments-json`은 version, commit, digest 같은 비밀이 아닌 script 인자를 JSON string array로 전달합니다. Secret은 `env:`로 주입합니다.

공용 deploy action은 다음만 담당합니다.

- `workflow_dispatch` 또는 stable published Release인지 검증
- manual deploy가 production branch에서 시작됐는지 검증
- 실제 checkout된 commit이 production branch history에 포함되는지 검증
- deploy script 경로와 인자 형식 검증
- project-owned deploy script 실행

GHCR build/publish, migration, service rollout 같은 project-specific CD 로직은 각 project에 둡니다.

Application/runtime secret은 GitHub Environment Secrets를 사용합니다. DB CLI credential과 Android signing material만 서버 장기 보관 예외입니다.

## Runner

필수:

```text
Bash
Git
jq
Docker
```

Persistent self-hosted runner에서는 신뢰하지 않는 fork/public PR 코드를 실행하지 않습니다.

## Version

기본 호출은 movable major ref를 사용합니다.

```text
@v1
```

`v1`은 검증된 v1 계열 변경에 대해서만 앞으로 이동합니다. 실행을 특정 구현에 완전히 고정해야 하는 caller만 full commit SHA를 사용합니다.
