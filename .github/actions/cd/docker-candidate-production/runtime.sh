#!/usr/bin/env bash
set -euo pipefail

CANDIDATE_ROOT="${STACKS_CANDIDATE_ROOT:-/var/lib/stacks/candidates}"
STATE_ROOT="${STACKS_STATE_ROOT:-/var/lib/stacks/projects}"
PROJECT_ROOT="${STACKS_PROJECT_ROOT:-/opt/stacks/projects}"
PKI_ROOT="${STACKS_PKI_ROOT:-/opt/stacks/shared/pki}"
PROXY_NETWORK="${STACKS_PROXY_NETWORK:-caddy-shared}"
TOOL_CACHE="${RUNNER_TOOL_CACHE:-/opt/cache/tool-cache}"

fail() { echo "Error: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || fail "$1 is required."; }
trim() { sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' <<<"$1"; }

need docker
need jq
need python3
need curl
need tar
need flock

[[ $# -eq 2 ]] || fail "Usage: production-runtime.sh <deploy|rollback> <project[/service]>"
operation="$1"
target="$2"
case "$operation" in deploy|rollback) ;; *) fail "operation must be deploy or rollback." ;; esac

project="${target%%/*}"
service=""
if [[ "$target" == */* ]]; then service="${target#*/}"; fi
[[ "$project" =~ ^[a-z][a-z0-9-]{0,62}$ ]] || fail "Invalid project: $project"
[[ -z "$service" || "$service" =~ ^[a-z][a-z0-9-]{0,62}$ ]] || fail "Invalid service: $service"
[[ -d "$PROJECT_ROOT/$project" ]] || fail "Project registry is missing: $PROJECT_ROOT/$project"

services=()
if [[ -n "$service" ]]; then
  [[ -d "$PROJECT_ROOT/$project/$service" ]] || fail "Service registry is missing: $PROJECT_ROOT/$project/$service"
  services+=("$service")
elif [[ "$operation" == "deploy" ]]; then
  root="$CANDIDATE_ROOT/$project"
  [[ -d "$root" ]] || fail "No production candidates exist for project '$project'."
  while IFS= read -r name; do services+=("$name"); done < <(
    find "$root" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | while IFS= read -r candidate_service; do [[ -d "$PROJECT_ROOT/$project/$candidate_service" ]] && printf '%s\n' "$candidate_service"; done | LC_ALL=C sort
  )
else
  root="$STATE_ROOT/$project"
  [[ -d "$root" ]] || fail "No production deployment state exists for project '$project'."
  while IFS= read -r name; do
    [[ -f "$root/$name/deploy/current.json" ]] && services+=("$name")
  done < <(find "$root" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | LC_ALL=C sort)
fi
((${#services[@]} > 0)) || fail "No services found for '$target'."

urlencode() { python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }

read_env_value() {
  local file="$1" key="$2" line value
  [[ -f "$file" ]] || return 1
  line="$(grep -E "^[[:space:]]*$key=" "$file" | tail -n1 || true)"
  [[ -n "$line" ]] || return 1
  value="${line#*=}"
  value="$(trim "$value")"
  if (( ${#value} >= 2 )); then
    if [[ "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then value="${value:1:${#value}-2}"; fi
    if [[ "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then value="${value:1:${#value}-2}"; fi
  fi
  printf '%s' "$value"
}

resolve_db() {
  local db_file="$PROJECT_ROOT/$project/db.env" key
  DB_RUNTIME_URL=""
  DB_MIGRATION_URL=""
  FLYWAY_URL=""
  FLYWAY_USER=""
  FLYWAY_PASSWORD=""
  [[ -f "$db_file" ]] || return 1
  declare -A v=()
  for key in DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD; do
    v[$key]="$(read_env_value "$db_file" "$key" || true)"
    [[ -n "${v[$key]}" ]] || fail "$db_file is missing $key."
  done
  local pw
  pw="$(urlencode "${v[DB_PASSWORD]}")"
  DB_RUNTIME_URL="postgresql://${v[DB_USER]}:${pw}@${v[DB_HOST]}:${v[DB_PORT]}/${v[DB_NAME]}"
  DB_MIGRATION_URL="postgresql://${v[DB_USER]}:${pw}@127.0.0.1:${v[DB_PORT]}/${v[DB_NAME]}"
  FLYWAY_URL="jdbc:postgresql://127.0.0.1:${v[DB_PORT]}/${v[DB_NAME]}"
  FLYWAY_USER="${v[DB_USER]}"
  FLYWAY_PASSWORD="${v[DB_PASSWORD]}"
}

resolve_cors() {
  local svc="$1" file raw value tmp
  tmp="$(mktemp)"
  for file in "$PROJECT_ROOT/$project/cors.env" "$PROJECT_ROOT/$project/$svc/cors.env"; do
    [[ -f "$file" ]] || continue
    while IFS= read -r raw || [[ -n "$raw" ]]; do
      raw="${raw//$'\r'/}"
      value="$(trim "$raw")"
      [[ -n "$value" ]] || continue
      [[ "$value" =~ ^https?://(\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.-]+)(:([0-9]{1,5}))?$ ]] || { rm -f "$tmp"; fail "Invalid CORS origin in $file: $value"; }
      if [[ -n "${BASH_REMATCH[3]:-}" ]]; then
        (( 10#${BASH_REMATCH[3]} >= 1 && 10#${BASH_REMATCH[3]} <= 65535 )) || { rm -f "$tmp"; fail "Invalid CORS port in $file: $value"; }
      fi
      printf '%s\n' "$value" >>"$tmp"
    done <"$file"
  done
  CORS_ORIGINS=""
  [[ ! -s "$tmp" ]] || CORS_ORIGINS="$(LC_ALL=C sort -u "$tmp" | paste -sd, -)"
  rm -f "$tmp"
}

state_dir_for() { printf '%s/%s/%s/deploy' "$STATE_ROOT" "$project" "$1"; }

record_state() {
  local svc="$1" revision="$2" image="$3" image_id="$4" event="$5" state_dir current history now record tmp_current tmp_history
  state_dir="$(state_dir_for "$svc")"
  current="$state_dir/current.json"
  history="$state_dir/history.jsonl"
  mkdir -p "$state_dir"
  now="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
  record="$(jq -cn --arg project "$project" --arg service "$svc" --arg revision "$revision" --arg image "$image" --arg imageId "$image_id" --arg deployedAt "$now" --arg event "$event" '{project:$project,service:$service,environment:"prod",revision:$revision,image:$image,imageId:$imageId,deployedAt:$deployedAt,event:$event}')"
  tmp_current="$(mktemp "$state_dir/.current.XXXXXX")"
  tmp_history="$(mktemp "$state_dir/.history.XXXXXX")"
  printf '%s\n' "$record" >"$tmp_current"
  [[ ! -f "$history" ]] || cat "$history" >"$tmp_history"
  printf '%s\n' "$record" >>"$tmp_history"
  chmod 0644 "$tmp_current" "$tmp_history"
  mv -f "$tmp_current" "$current"
  mv -f "$tmp_history" "$history"
}

previous_state() {
  local svc="$1" state_dir current history current_id
  state_dir="$(state_dir_for "$svc")"; current="$state_dir/current.json"; history="$state_dir/history.jsonl"
  [[ -f "$current" && -s "$history" ]] || return 1
  current_id="$(jq -r '.imageId // empty' "$current")"
  [[ -n "$current_id" ]] || return 1
  jq -cs --arg current "$current_id" '[.[] | select(.imageId != $current)] | last // empty' "$history"
}

resolve_candidate_manifest() {
  local svc="$1" op="$2" manifest prev revision fallback
  if [[ "$op" == "deploy" ]]; then
    manifest="$CANDIDATE_ROOT/$project/$svc/latest.json"
    [[ -f "$manifest" ]] || fail "No verified production candidate exists for $project/$svc."
    cat "$manifest"
    return
  fi
  prev="$(previous_state "$svc" || true)"
  [[ -n "$prev" && "$prev" != "null" ]] || fail "No previous known-good deployment exists for $project/$svc."
  revision="$(jq -r '.revision' <<<"$prev")"
  manifest="$CANDIDATE_ROOT/$project/$svc/$revision/manifest.json"
  if [[ -f "$manifest" ]]; then
    jq --arg image "$(jq -r '.image' <<<"$prev")" --arg imageId "$(jq -r '.imageId' <<<"$prev")" '.image=$image | .imageId=$imageId' "$manifest"
    return
  fi
  fallback="$CANDIDATE_ROOT/$project/$svc/latest.json"
  [[ -f "$fallback" ]] || fail "Rollback candidate contract is unavailable for $project/$svc@$revision."
  jq --arg revision "$revision" --arg image "$(jq -r '.image' <<<"$prev")" --arg imageId "$(jq -r '.imageId' <<<"$prev")" '.revision=$revision | .image=$image | .imageId=$imageId' "$fallback"
}

resolve_runtime_env_files() {
  local svc="$1" source_root="$2" manifest="$3" item path
  ENV_FILES=()
  while IFS= read -r item; do
    [[ -n "$item" && "$item" != /* && "$item" != *".."* ]] || fail "Invalid repository runtime env path: $item"
    path="$(realpath -e "$source_root/$item" 2>/dev/null || true)"
    [[ -f "$path" && ( "$path" == "$source_root"/* ) ]] || fail "Repository runtime env file is unavailable: $item"
    ENV_FILES+=("$path")
  done < <(jq -r '.runtime.envFiles[]?' <<<"$manifest")
  [[ ! -f "$PROJECT_ROOT/$project/deploy.env" ]] || ENV_FILES+=("$PROJECT_ROOT/$project/deploy.env")
  [[ ! -f "$PROJECT_ROOT/$project/$svc/deploy.env" ]] || ENV_FILES+=("$PROJECT_ROOT/$project/$svc/deploy.env")
  [[ ! -f "$PROJECT_ROOT/$project/$svc/secret/deploy.env" ]] || ENV_FILES+=("$PROJECT_ROOT/$project/$svc/secret/deploy.env")
}

resolve_volumes() {
  local manifest="$1" value file
  VOLUMES_JSON="$(jq -c '.runtime.volumes // []' <<<"$manifest")"
  if [[ "$VOLUMES_JSON" == "[]" ]]; then
    for file in "${ENV_FILES[@]}"; do
      value="$(read_env_value "$file" DEPLOY_VOLUMES_JSON || true)"
      [[ -z "$value" ]] || VOLUMES_JSON="$(jq -c 'if type=="array" and all(.[];type=="string") then . else error("DEPLOY_VOLUMES_JSON must be a string array") end' <<<"$value")"
    done
  fi
}

append_tls_and_trust_volumes() {
  local svc="$1" cert key gid registry ca root
  TLS_GID=""
  cert="$PROJECT_ROOT/$project/$svc/secret/tls.crt"
  key="$PROJECT_ROOT/$project/$svc/secret/tls.key"
  if [[ -e "$cert" || -e "$key" ]]; then
    [[ -f "$cert" && -f "$key" ]] || fail "Managed TLS identity is incomplete for $project/$svc [prod]."
    gid="$(stat -c '%g' "$key")"; TLS_GID="$gid"
    VOLUMES_JSON="$(jq -c --arg v "$cert:/opt/stacks/tls/tls.crt:ro" '. + [$v]' <<<"$VOLUMES_JSON")"
    VOLUMES_JSON="$(jq -c --arg v "$key:/opt/stacks/tls/tls.key:ro" '. + [$v]' <<<"$VOLUMES_JSON")"
  fi
  registry="$PKI_ROOT/registry.json"
  if [[ -f "$registry" ]]; then
    jq -e '.version==1 and (.trust|type=="object")' "$registry" >/dev/null || fail "Invalid PKI registry: $registry"
    while IFS= read -r ca; do
      [[ "$ca" =~ ^[a-z][a-z0-9-]{0,62}$ ]] || fail "Invalid trusted CA name: $ca"
      root="$PKI_ROOT/$ca/prod/root_ca.crt"
      [[ -f "$root" ]] || fail "Trusted CA root is missing: $root"
      VOLUMES_JSON="$(jq -c --arg v "$root:/opt/stacks/tls/trust/$ca.crt:ro" '. + [$v]' <<<"$VOLUMES_JSON")"
    done < <(jq -r --arg target "$project/$svc" '.trust[$target].prod // [] | unique[]' "$registry")
  fi
}

install_node_dependencies() {
  local component="$1" manager="$2"
  case "$manager" in
    npm) (cd "$component" && npm ci --ignore-scripts=false) ;;
    pnpm) (cd "$component" && pnpm install --frozen-lockfile) ;;
    yarn) (cd "$component" && yarn install --immutable) ;;
    bun) (cd "$component" && bun install --frozen-lockfile) ;;
    *) fail "Unsupported package manager: $manager" ;;
  esac
}

apply_migrations() {
  local manifest="$1" source_root="$2" engine component path config package dialect version manager build_tool migrations goose asset arch cache binary tmp expected
  engine="$(jq -r '.migration.engine' <<<"$manifest")"
  [[ "$operation" == "deploy" ]] || return 0
  [[ "$engine" != "none" ]] || return 0
  resolve_db || fail "Production DB credentials are required for migration."
  component="$(realpath -e "$source_root/$(jq -r '.migration.workingDirectory' <<<"$manifest")" 2>/dev/null || true)"
  [[ -d "$component" && "$component" == "$source_root"/* || "$component" == "$source_root" ]] || fail "Invalid migration working directory."
  path="$(jq -r '.migration.path' <<<"$manifest")"
  case "$engine" in
    goose)
      migrations="$(realpath -e "$component/$path" 2>/dev/null || true)"
      [[ -d "$migrations" && "$migrations" == "$component"/* ]] || fail "Invalid Goose migration path: $path"
      ! find "$migrations" -type f -name '*.go' -print -quit | grep -q . || fail "Shared Goose production migration supports SQL files only."
      find "$migrations" -type f -name '*.sql' -print -quit | grep -q . || fail "No Goose SQL migration files found."
      version="$(jq -r '.migration.gooseVersion // empty' <<<"$manifest")"
      if [[ -z "$version" && -f "$component/go.mod" ]]; then version="$(awk '$1=="require"&&$2=="github.com/pressly/goose/v3"{print $3;exit}$1=="github.com/pressly/goose/v3"{print $2;exit}' "$component/go.mod")"; fi
      [[ -n "$version" ]] || version="$(jq -r '.migration.gooseDefaultVersion' <<<"$manifest")"
      [[ "$version" == v* ]] || version="v$version"
      [[ "$version" =~ ^v3\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.-]+)?$ ]] || fail "Invalid Goose version: $version"
      case "$(uname -m)" in x86_64|amd64) asset=goose_linux_x86_64; arch=x86_64 ;; aarch64|arm64) asset=goose_linux_arm64; arch=arm64 ;; *) fail "Unsupported Goose architecture." ;; esac
      cache="$TOOL_CACHE/goose/$version/$arch"; binary="$cache/goose"
      if [[ ! -x "$binary" ]]; then
        mkdir -p "$cache"; tmp="$(mktemp -d)"
        curl -fsSL "https://github.com/pressly/goose/releases/download/$version/checksums.txt" -o "$tmp/checksums.txt"
        curl -fsSL "https://github.com/pressly/goose/releases/download/$version/$asset" -o "$tmp/$asset"
        expected="$(awk -v a="$asset" '$2==a{print $1;exit}' "$tmp/checksums.txt")"; [[ -n "$expected" ]] || fail "Goose checksum missing."
        printf '%s  %s\n' "$expected" "$tmp/$asset" | sha256sum -c - >/dev/null || fail "Goose checksum verification failed."
        install -m 0755 "$tmp/$asset" "$binary"; rm -rf "$tmp"
      fi
      "$binary" -dir "$migrations" validate
      "$binary" -dir "$migrations" "$(jq -r '.migration.dialect' <<<"$manifest")" "$DB_MIGRATION_URL" up
      ;;
    go-command)
      need go
      package="$(jq -r '.migration.package' <<<"$manifest")"
      [[ "$package" == ./* && "$package" != *".."* ]] || fail "Invalid Go migration package."
      (cd "$component" && DATABASE_URL="$DB_MIGRATION_URL" go run "$package")
      ;;
    prisma)
      need node
      manager="$(jq -r '.migration.packageManager' <<<"$manifest")"; need "$manager"
      install_node_dependencies "$component" "$manager"
      config="$(realpath -e "$component/$path" 2>/dev/null || true)"; [[ -f "$config" ]] || fail "Prisma schema unavailable."
      export DATABASE_URL="$DB_MIGRATION_URL"
      case "$manager" in npm) (cd "$component" && npm exec --offline -- prisma migrate deploy --schema "$config") ;; pnpm) (cd "$component" && pnpm exec prisma migrate deploy --schema "$config") ;; yarn) (cd "$component" && yarn exec prisma migrate deploy --schema "$config") ;; bun) (cd "$component" && bunx prisma migrate deploy --schema "$config") ;; esac
      ;;
    drizzle)
      need node
      manager="$(jq -r '.migration.packageManager' <<<"$manifest")"; need "$manager"
      install_node_dependencies "$component" "$manager"
      config="$(realpath -e "$component/$(jq -r '.migration.config' <<<"$manifest")" 2>/dev/null || true)"; [[ -f "$config" ]] || fail "Drizzle config unavailable."
      migrations="$(realpath -e "$component/$path" 2>/dev/null || true)"; [[ -d "$migrations" ]] || fail "Drizzle migration directory unavailable."
      export DATABASE_URL="$DB_MIGRATION_URL"
      case "$manager" in npm) (cd "$component" && npm exec --offline -- drizzle-kit migrate --config "$config") ;; pnpm) (cd "$component" && pnpm exec drizzle-kit migrate --config "$config") ;; yarn) (cd "$component" && yarn exec drizzle-kit migrate --config "$config") ;; bun) (cd "$component" && bunx --bun drizzle-kit migrate --config "$config") ;; esac
      ;;
    flyway)
      build_tool="$(jq -r '.migration.buildTool' <<<"$manifest")"
      migrations="$(realpath -e "$component/$path" 2>/dev/null || true)"; [[ -d "$migrations" ]] || fail "Flyway migration directory unavailable."
      export FLYWAY_URL FLYWAY_USER FLYWAY_PASSWORD FLYWAY_LOCATIONS="filesystem:$migrations"
      case "$build_tool" in gradle) [[ -f "$component/gradlew" ]] || fail "gradlew is missing."; (cd "$component" && chmod +x ./gradlew && ./gradlew --no-daemon flywayMigrate) ;; maven) [[ -f "$component/mvnw" ]] || fail "mvnw is missing."; (cd "$component" && chmod +x ./mvnw && ./mvnw -B -ntp flyway:migrate) ;; *) fail "Invalid Flyway build tool." ;; esac
      ;;
    *) fail "Unsupported migration engine: $engine" ;;
  esac
}

prepare_container_args() {
  local svc="$1" manifest="$2" revision="$3" kind="$4" network="$5" file name volume opt cap
  CREATE_ARGS=(docker create --name "$project.$svc" --restart "$(jq -r '.runtime.restartPolicy' <<<"$manifest")")
  [[ "$(jq -r '.runtime.init' <<<"$manifest")" != "true" ]] || CREATE_ARGS+=(--init)
  [[ -z "$TLS_GID" ]] || CREATE_ARGS+=(--group-add "$TLS_GID")
  for file in "${ENV_FILES[@]}"; do CREATE_ARGS+=(--env-file "$file"); done
  if [[ -n "${DB_RUNTIME_URL:-}" ]]; then CREATE_ARGS+=(-e DATABASE_URL); export DATABASE_URL="$DB_RUNTIME_URL"; fi
  if [[ -n "$CORS_ORIGINS" ]]; then CREATE_ARGS+=(-e CORS_ORIGINS); export CORS_ORIGINS; fi
  while IFS= read -r name; do
    [[ -z "$name" || "$name" == "DATABASE_URL" || "$name" == "CORS_ORIGINS" ]] && continue
    local found=false
    for file in "${ENV_FILES[@]}"; do read_env_value "$file" "$name" >/dev/null 2>&1 && found=true; done
    [[ "$found" == "true" ]] || fail "Runtime env '$name' is not provided by production host config."
  done < <(jq -r '.runtime.envNames[]?' <<<"$manifest")
  while IFS= read -r volume; do CREATE_ARGS+=(-v "$volume"); done < <(jq -r '.[]' <<<"$VOLUMES_JSON")
  while IFS= read -r opt; do CREATE_ARGS+=(--security-opt "$opt"); done < <(jq -r '.runtime.securityOpts[]?' <<<"$manifest")
  while IFS= read -r cap; do CREATE_ARGS+=(--cap-drop "$cap"); done < <(jq -r '.runtime.capDrop[]?' <<<"$manifest")
  CREATE_ARGS+=(--network "$network")
  CREATE_ARGS+=(--label "kr.mooner510.stacks.managed=true" --label "kr.mooner510.stacks.project=$project" --label "kr.mooner510.stacks.service=$svc" --label "kr.mooner510.stacks.environment=prod" --label "kr.mooner510.stacks.kind=$kind" --label "org.opencontainers.image.revision=$revision")
}

run_http_container() {
  local svc="$1" manifest="$2" image_id="$3" revision="$4" port health attempts i binding host_port
  port="$(jq -r '.containerPort' <<<"$manifest")"; health="$(jq -r '.healthPath' <<<"$manifest")"; attempts="$(jq -r '.healthAttempts' <<<"$manifest")"
  CREATE_ARGS+=(-p "127.0.0.1::$port" --label "kr.mooner510.stacks.container-port=$port" "$image_id")
  "${CREATE_ARGS[@]}" >/dev/null
  docker network connect "$PROXY_NETWORK" "$project.$svc"
  docker start "$project.$svc" >/dev/null
  binding="$(docker port "$project.$svc" "$port/tcp" | head -n1)"; [[ "$binding" =~ ^127\.0\.0\.1:([0-9]+)$ ]] || return 1; host_port="${BASH_REMATCH[1]}"
  for ((i=0;i<attempts;i++)); do curl -fsS --max-time 3 "http://127.0.0.1:$host_port$health" >/dev/null && return 0; sleep 2; done
  docker logs "$project.$svc" >&2 || true
  return 1
}

run_process_container() {
  local svc="$1" manifest="$2" image_id="$3" attempts i ready output
  CREATE_ARGS+=("$image_id")
  "${CREATE_ARGS[@]}" >/dev/null; docker start "$project.$svc" >/dev/null
  attempts="$(jq -r '.readiness.attempts' <<<"$manifest")"; ready=false
  mapfile -t cmd < <(jq -r '.readiness.command[]?' <<<"$manifest")
  if ((${#cmd[@]}==0)); then sleep 2; [[ "$(docker inspect "$project.$svc" --format '{{.State.Running}}')" == true ]] && ready=true
  else
    for ((i=0;i<attempts;i++)); do
      if output="$(docker exec "$project.$svc" "${cmd[@]}" 2>/dev/null)"; then
        if [[ -z "$(jq -r '.readiness.jq' <<<"$manifest")" ]] || jq -e "$(jq -r '.readiness.jq' <<<"$manifest")" <<<"$output" >/dev/null 2>&1; then ready=true; break; fi
      fi
      sleep 2
    done
  fi
  [[ "$ready" == true ]] || { docker logs "$project.$svc" >&2 || true; return 1; }
}

deploy_service() {
  local svc="$1" manifest image_id image revision kind archive tmp source_root network state_dir lock stop_timeout previous_image previous_revision previous_ref
  manifest="$(resolve_candidate_manifest "$svc" "$operation")"
  [[ -d "$PROJECT_ROOT/$project/$svc" ]] || fail "Service registry is missing: $PROJECT_ROOT/$project/$svc"
  jq -e --arg project "$project" --arg service "$svc" '.version==1 and .project==$project and .service==$service and (.kind|IN("http","process"))' <<<"$manifest" >/dev/null || fail "Invalid candidate manifest for $project/$svc."
  image_id="$(jq -r '.imageId' <<<"$manifest")"; image="$(jq -r '.image' <<<"$manifest")"; revision="$(jq -r '.revision' <<<"$manifest")"; kind="$(jq -r '.kind' <<<"$manifest")"
  docker image inspect "$image_id" >/dev/null 2>&1 || fail "Candidate image is unavailable locally: $image_id"
  network="project-$project"; docker network inspect "$network" >/dev/null 2>&1 || fail "Project network '$network' does not exist."
  [[ "$kind" != http ]] || docker network inspect "$PROXY_NETWORK" >/dev/null 2>&1 || fail "Proxy network '$PROXY_NETWORK' does not exist."

  state_dir="$(state_dir_for "$svc")"; mkdir -p "$state_dir"; exec {lock}>"$state_dir/.lock"; flock "$lock"

  archive="$CANDIDATE_ROOT/$project/$svc/$revision/source.tar.gz"
  if [[ ! -f "$archive" ]]; then archive="$(jq -r '.sourceArchive // empty' <<<"$manifest")"; fi
  [[ -f "$archive" ]] || fail "Candidate source archive is unavailable for $project/$svc@$revision."
  tmp="$(mktemp -d)"
  tar -xzf "$archive" -C "$tmp"; source_root="$(realpath -e "$tmp")"

  resolve_runtime_env_files "$svc" "$source_root" "$manifest"
  resolve_volumes "$manifest"
  append_tls_and_trust_volumes "$svc"
  resolve_cors "$svc"
  DB_RUNTIME_URL=""; if [[ "$(jq -r '.runtime.database' <<<"$manifest")" == true || "$(jq -r '.migration.engine' <<<"$manifest")" != none ]] || jq -e '.runtime.envNames|index("DATABASE_URL")!=null' <<<"$manifest" >/dev/null; then resolve_db || fail "Production DB credentials are required for $project/$svc."; fi

  apply_migrations "$manifest" "$source_root"

  previous_image=""; previous_revision=""; previous_ref=""
  if docker container inspect "$project.$svc" >/dev/null 2>&1; then
    previous_image="$(docker inspect "$project.$svc" --format '{{.Image}}')"
    previous_ref="$(docker inspect "$project.$svc" --format '{{.Config.Image}}')"
    previous_revision="$(docker inspect "$project.$svc" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')"
    stop_timeout="$(jq -r '.runtime.stopTimeout' <<<"$manifest")"
    docker stop -t "$stop_timeout" "$project.$svc" >/dev/null || true
    docker rm -f "$project.$svc" >/dev/null || true
  fi

  prepare_container_args "$svc" "$manifest" "$revision" "$kind" "$network"
  if [[ "$kind" == http ]]; then
    if ! run_http_container "$svc" "$manifest" "$image_id" "$revision"; then
      docker rm -f "$project.$svc" >/dev/null 2>&1 || true
      [[ -z "$previous_image" ]] || { prepare_container_args "$svc" "$manifest" "${previous_revision:-unknown}" "$kind" "$network"; run_http_container "$svc" "$manifest" "$previous_image" "${previous_revision:-unknown}" || true; }
      fail "HTTP service $project/$svc failed readiness; previous container restoration was attempted."
    fi
  else
    if ! run_process_container "$svc" "$manifest" "$image_id"; then
      docker rm -f "$project.$svc" >/dev/null 2>&1 || true
      [[ -z "$previous_image" ]] || { prepare_container_args "$svc" "$manifest" "${previous_revision:-unknown}" "$kind" "$network"; run_process_container "$svc" "$manifest" "$previous_image" || true; }
      fail "Process service $project/$svc failed readiness; previous container restoration was attempted."
    fi
  fi

  record_state "$svc" "$revision" "$image" "$image_id" "$operation"
  rm -rf "$tmp"
  flock -u "$lock"
  eval "exec ${lock}>&-"
  echo "Production $operation complete: $project/$svc@$revision"
}

for svc in "${services[@]}"; do deploy_service "$svc"; done
