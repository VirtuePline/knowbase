#!/usr/bin/env bash
# 整个项目挂载在 /app 的旧部署：在原镜像的临时容器内准备依赖，保留现场容器、端口与挂载。
set -euo pipefail
action=${1:?ready, prepare, activate or rollback}
container=${2:?container name}
requirements=${3:?requirements file}
wheelhouse=${4:-}
[[ $action == ready || $action == prepare || $action == activate || $action == rollback ]] && [[ -f $requirements ]] || exit 2
image=$(docker inspect -f '{{.Image}}' "$container")
project=$(docker inspect -f '{{range .Mounts}}{{if and (eq .Destination "/app") (eq .Type "bind")}}{{.Source}}{{end}}{{end}}' "$container")
[[ -n $project && -d $project/app ]] || { echo 'Expected a project bind mount at /app' >&2; exit 1; }
root="$project/.deploy-runtime"
mkdir -p "$root"
exec 9>"$root/runtime.lock"
flock -w 300 9
digest=$( { printf 'v1\n%s\n' "$image"; cat "$requirements"; } | sha256sum | cut -d' ' -f1)
release="$root/$digest"
inside="/app/.deploy-runtime/$digest"
marker=00_knowbase_runtime.pth

if [[ $action == ready ]]; then
    # 当前 requirements 已 prepare 且容器 .pth 已指向本次运行时：无需重新上传安装包。
    if [[ ! -f $release/ready ]]; then echo missing; exit 0; fi
    base_site=$(cat "$release/base-site")
    current=$(docker exec "$container" cat "$base_site/$marker" 2>/dev/null || true)
    if [[ $current == *"$inside/venv/"* ]]; then echo ready; else echo missing; fi
    exit 0
fi

if [[ $action == prepare ]]; then
    if [[ -f $release/ready ]]; then echo 'Runtime already prepared'; exit 0; fi
    stage=$(mktemp -d "$root/.build-XXXXXXXX")
    trap 'rm -rf -- "$stage"' EXIT
    # 构建容器只能看到依赖清单和新的虚拟环境，运行所需的既有二进制包沿用原镜像。
    mounts=(-v "$requirements:/tmp/knowbase-requirements.txt:ro" -v "$stage:$inside")
    if [[ -n $wheelhouse && -d $wheelhouse ]]; then
        mounts+=(-v "$wheelhouse:/tmp/knowbase-wheels:ro")
    fi
    docker run --rm --entrypoint sh "${mounts[@]}" "$image" -ec '
        python -m venv --without-pip --system-site-packages "$1/venv"
        if [ -d /tmp/knowbase-wheels ]; then
          # 本机已下载并上传全部 Linux 安装包：全程离线安装，与服务器网络无关。
          "$1/venv/bin/python" -m pip install --disable-pip-version-check --no-cache-dir \
            --no-index --find-links=/tmp/knowbase-wheels -r /tmp/knowbase-requirements.txt
        else
          "$1/venv/bin/python" -m pip install --disable-pip-version-check --no-cache-dir \
            --index-url https://pypi.tuna.tsinghua.edu.cn/simple --timeout 30 --retries 1 \
            -r /tmp/knowbase-requirements.txt || \
          "$1/venv/bin/python" -m pip install --disable-pip-version-check --no-cache-dir \
            --timeout 60 --retries 2 -r /tmp/knowbase-requirements.txt
        fi
        "$1/venv/bin/python" -m pip check
        python -c "import sysconfig; print(sysconfig.get_path(\"purelib\"))" > "$1/base-site"
        "$1/venv/bin/python" -c "import sysconfig; print(sysconfig.get_path(\"purelib\"))" > "$1/venv-site"
      ' sh "$inside"
    venv_site=$(cat "$stage/venv-site")
    [[ $venv_site == "$inside/venv/"* ]]
    # .pth 仅将本次锁定目录放到旧镜像依赖之前，版本和新增依赖均可随指针一起切换。
    printf "import sys,site; p='%s'; site.addsitedir(p); sys.path.insert(0,sys.path.pop(sys.path.index(p)))\n" \
      "$venv_site" > "$stage/$marker"
    chmod 755 "$stage"
    touch "$stage/ready"
    mv -T "$stage" "$release"
    trap - EXIT
    echo 'Runtime prepared'
    exit 0
fi

[[ -f $release/ready ]] || { echo 'Prepare the runtime before activation' >&2; exit 1; }
base_site=$(cat "$release/base-site")
[[ $base_site == /*/site-packages && $base_site != *..* ]]
install_path="$base_site/$marker"
backup="$(dirname "$0")/runtime-previous"
restore_runtime() {
    if [[ ! -f $backup/armed ]]; then return 0; fi
    docker stop -t 20 "$container" >/dev/null
    docker cp "$backup/$marker" "$container:$install_path"
    local previous_state
    previous_state=$(cat "$backup/state")
    if [[ $previous_state != exited && $previous_state != created ]]; then docker start "$container"; fi
}
if [[ $action == rollback ]]; then restore_runtime; exit 0; fi
# 先用相同的项目和 Python 加载路径验证导入，再切换正在运行的容器。
docker run --rm --network none --entrypoint python -w /app \
  -v "$project:/app:ro" -v "$release/$marker:$install_path:ro" "$image" -c 'import app.main'
mkdir -m 700 "$backup"
state=$(docker inspect -f '{{.State.Status}}' "$container")
printf '%s\n' "$state" > "$backup/state"
# 为空的 .pth 等价于全新镜像的原始 Python 路径。
if ! docker cp "$container:$install_path" "$backup/$marker" 2>"$backup/copy-error"; then
    if ! grep -Eiq 'could not find|no such file' "$backup/copy-error"; then
        echo 'Could not back up the previous runtime pointer' >&2
        exit 1
    fi
    : > "$backup/$marker"
fi
touch "$backup/armed"
if [[ $state != exited && $state != created ]]; then docker stop -t 20 "$container"; fi
if ! docker cp "$release/$marker" "$container:$install_path" || ! docker start "$container"; then
    restore_runtime
    echo 'Runtime activation failed, restored the previous Python path' >&2
    exit 1
fi
echo 'Runtime activated'
