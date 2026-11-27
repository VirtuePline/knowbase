"""按现有 Docker 部署更新程序，通过实际发布端口验证应用就绪。"""
import json
import pathlib
import re
import shlex
import sys
import time
from contextlib import contextmanager
from ipaddress import ip_address
from urllib.parse import urlsplit

ROOT = pathlib.Path(__file__).resolve().parent.parent
REMOTE = "/home/knowbase"
HOST = "10.10.0.239"
USER = "root"
PASSWORD = "admin"


CONTAINER = "knowbase"
DATA_PATHS = ("/app/data/raw", "/app/data/index", "/app/data/images", "/app/data/attachments")


class DeployError(Exception):
    pass


def run(client, step, *args):
    """读取完整的 SSH 通道并检查退出码；远端错误输出可能包含密钥。"""
    with client.get_transport().open_session() as channel:
        channel.set_combine_stderr(True)
        channel.settimeout(1800)
        channel.exec_command(shlex.join(args))
        output = bytearray()
        while chunk := channel.recv(65536):
            output.extend(chunk)
            if len(output) > 2_000_000:
                del output[:-2_000_000]
        status = channel.recv_exit_status()
    if status:
        raise DeployError(f"{step}失败（退出码 {status}）")
    return output.decode("utf-8", "replace").strip()


def inspect(client, field):
    value = run(client, "读取容器信息", "docker", "inspect", "--format", "{{json " + field + "}}", CONTAINER)
    return json.loads(value)


@contextmanager
def deployment_lock(client):
    """SSH 连接断开后由内核释放发布锁。"""
    with client.get_transport().open_session() as channel:
        channel.settimeout(10)
        channel.exec_command("flock -n /tmp/knowbase-publish.lock sh -c 'echo locked; cat >/dev/null'")
        with channel.makefile("rb") as output:
            if output.readline() != b"locked\n":
                raise DeployError("另一项发布正在进行，或服务器无法获取发布锁")
            try:
                yield
            finally:
                channel.shutdown_write()


def collect_files(build_files=False):
    assets = {".html", ".css", ".js", ".svg", ".png", ".ico", ".woff", ".woff2"}
    files = [p for p in (ROOT / "app").rglob("*.py") if p.is_file() and not p.is_symlink()]
    files += [p for p in (ROOT / "web").rglob("*") if p.suffix.lower() in assets and p.is_file()
              and not p.is_symlink() and not any(part.startswith(".") for part in p.relative_to(ROOT).parts)]
    files += [ROOT / "config.yaml", ROOT / "requirements.txt"]
    if build_files:
        files += [ROOT / "Dockerfile", ROOT / ".dockerignore"]
    return sorted(files)


def compose_command(labels):
    prefix = "com.docker.compose."
    project, service = (labels.get(prefix + k, "") for k in ("project", "service"))
    directory = labels.get(prefix + "project.working_dir")
    configs = labels.get(prefix + "project.config_files", "").split(",")
    if (directory != REMOTE or not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", n) for n in (project, service))
            or not all(pathlib.PurePosixPath(p).is_absolute() for p in configs)):
        raise DeployError("没有找到对应现场目录的 Compose 项目，无法确认该容器的发布方式")
    command = ["docker", "compose", "--project-directory", directory, "-p", project]
    for file in configs:
        command += ["-f", file]
    return command, service


def volume_key(mounts, path):
    """比较目录实际所在的数据卷，兼容整棵 data 目录挂载。"""
    for target, source in sorted(mounts.items(), key=lambda item: -len(item[0])):
        subpath = pathlib.PurePosixPath(path)
        if subpath.is_relative_to(target):
            return (source[0], str(pathlib.PurePosixPath(source[1]) / subpath.relative_to(target)))
    return None


def compose_plan(client, mounts, image_id):
    command, service = compose_command(inspect(client, ".Config.Labels"))
    # Compose 展开的配置可能含口令，只在远端提取构建路径、镜像名和挂载。
    extract = (
        "import json,sys;c=json.load(sys.stdin);s=c['services'][sys.argv[1]];b=s.get('build',{});"
        "print(json.dumps([b.get('context'), b.get('dockerfile','Dockerfile'), s.get('image'),"
        "{m['target']:[m['type'],c.get('volumes',{}).get(m.get('source'),{}).get('name',m.get('source',''))]"
        " for m in s.get('volumes',[])}]))"
    )
    reader = ["docker", "run", "--rm", "-i", "--network", "none", "--entrypoint", "python", image_id, "-c", extract, service]
    cmd = shlex.join([*command, "config", "--format", "json"]) + " 2>/dev/null | " + shlex.join(reader)
    context, dockerfile, image, desired = json.loads(run(client, "读取现场 Compose 配置", "bash", "-o", "pipefail", "-c", cmd))
    if context not in (None, REMOTE) or dockerfile not in ("Dockerfile", "./Dockerfile"):
        raise DeployError("Compose 构建目录或 Dockerfile 与项目不匹配")
    current = {m["Destination"]: (m["Type"], m.get("Name") or m["Source"]) for m in mounts}
    for path in DATA_PATHS:
        old = volume_key(current, path)
        if old:
            if old != volume_key(desired, path):
                raise DeployError(f"Compose 将改变 {path} 的现有挂载，请先迁移数据")
        else:
            probe = 'if [ -d "$1" ]; then find "$1" -mindepth 1 -maxdepth 1 -print -quit; fi'
            if run(client, f"核对 {path} 的容器内数据", "docker", "exec", CONTAINER, "sh", "-ec", probe, "sh", path):
                raise DeployError(f"{path} 中有未挂载的业务数据，请先迁移到持久卷后发布")
    # 未显式设置 image 时 Compose 使用 project-service，运行中容器给出实际镜像引用。
    image = image or inspect(client, ".Config.Image")
    if not image or image.startswith("-") or "@" in image:
        raise DeployError("Compose 镜像标签不能用于构建和恢复")
    return command, service, image


PUBLISH = """
import json,os,shutil,sys,tempfile
from pathlib import Path
mode,stage,root=sys.argv[1:]
stage,root=Path(stage),Path(root).resolve()
incoming,backup,manifest=stage/'source',stage/'previous',stage/'manifest.json'
def replace(src,dst):
    dst.parent.mkdir(exist_ok=True,parents=True)
    fd,tmp=tempfile.mkstemp(dir=dst.parent)
    os.close(fd)
    try:
        shutil.copy2(src,tmp)
        os.replace(tmp,dst)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
if mode=='publish':
    entries={}
    for src in incoming.rglob('*'):
        if not src.is_file(): continue
        rel=src.relative_to(incoming)
        dst=root/rel
        if dst.resolve()!=dst: raise RuntimeError('unexpected symlink in deployment path')
        entries[str(rel)]=dst.exists()
        if dst.exists(): replace(dst,backup/rel)
    manifest.write_text(json.dumps(entries))
    for rel in entries: replace(incoming/rel,root/rel)
elif manifest.is_file():
    for rel,exists in json.loads(manifest.read_text()).items():
        if exists: replace(backup/rel,root/rel)
        elif (root/rel).is_file(): (root/rel).unlink()
"""


def upload(client, stage, files):
    with client.open_sftp() as sftp:
        directories = set()
        for path in files:
            name = f"{stage}/source/{path.relative_to(ROOT).as_posix()}"
            directories.add(name.rsplit("/", 1)[0])
        run(client, "创建暂存目录", "mkdir", "-p", *sorted(directories))
        for path in files:
            sftp.put(str(path), f"{stage}/source/{path.relative_to(ROOT).as_posix()}")


PROBE_PYTHON = """
import json,platform
from importlib.metadata import distributions
from pip._vendor.packaging.tags import sys_tags
from pip._vendor.packaging.utils import canonicalize_name
installed = {canonicalize_name(d.metadata['Name']): d for d in distributions() if d.metadata['Name']}
print(json.dumps({
    'python': platform.python_version(),
    'machine': platform.machine(),
    'platforms': list(dict.fromkeys(tag.platform for tag in sys_tags() if tag.platform != 'any')),
    'installed': {name: d.version for name,d in installed.items()},
    'requires_dist': {name: d.requires or [] for name,d in installed.items()},
}))
"""


def upload_wheels(client, stage, image):
    if __package__:
        from .deploy_wheels import prepare_wheels
    else:
        from deploy_wheels import prepare_wheels

    runtime = json.loads(run(client, "读取 Linux 镜像的 Python 版本与兼容标签",
                             "docker", "run", "--rm", "--network", "none", "--entrypoint", "python",
                             image, "-c", PROBE_PYTHON))
    print(f"正在本机下载 Linux 安装包（Python {runtime['python']}，{runtime['machine']}）…", flush=True)
    wheelhouse = prepare_wheels(ROOT / "requirements.txt", runtime, ROOT / ".deploy-cache")
    wheels = sorted(wheelhouse.glob("*.whl"))
    remote = stage + "/wheelhouse"
    run(client, "创建离线安装包目录", "mkdir", "-p", remote)
    print(f"正在上传 {len(wheels)} 个 Linux 安装包…", flush=True)
    with client.open_sftp() as sftp:
        for wheel in wheels:
            sftp.put(str(wheel), f"{remote}/{wheel.name}", confirm=True)


OFFLINE_DOCKERFILE = """FROM {base}
COPY source/requirements.txt /app/requirements.txt
COPY wheelhouse/ /tmp/knowbase-offline-wheels/
RUN python -m pip install --disable-pip-version-check --no-cache-dir --no-index --only-binary=:all: \\
        --find-links=/tmp/knowbase-offline-wheels -r /app/requirements.txt \\
    && python -m pip check \\
    && rm -rf /tmp/knowbase-offline-wheels /app/app /app/web
COPY source/app /app/app
COPY source/web /app/web
COPY source/config.yaml /app/config.yaml
"""


def published_urls(ports):
    urls = []
    for mappings in (ports or {}).values():
        for bind in mappings or []:
            host = bind["HostIp"]
            if host in ("", "0.0.0.0"):
                host = "127.0.0.1"
            if ":" in host:
                host = "[::1]" if host == "::" else f"[{host}]"
            port = int(bind["HostPort"])
            if 0 < port <= 65535:
                urls.append(f"http://{host}:{port}")
    return list(dict.fromkeys(urls))


def client_address(ports, verified):
    for mappings in (ports or {}).values():
        for bind in mappings or []:
            if verified not in published_urls({"web": [bind]}):
                continue
            addr = bind["HostIp"]
            if addr in ("0.0.0.0", "::", ""):
                addr = HOST
            elif ip_address(addr).is_loopback:
                continue
            host = f"[{addr}]" if ":" in addr else addr
            port = urlsplit(verified).port
            return f"http://{host}{':' + str(port) if port != 80 else ''}/"
    return None


def health(client):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        for url in published_urls(inspect(client, ".NetworkSettings.Ports")):
            try:
                curl = ["curl", "-gfsS", "--noproxy", "*", "--max-time", "4"]
                mode = json.loads(run(client, "验证工作台接口", *curl, url + "/api/mode"))
                if isinstance(mode, dict) and mode.get("portal") in ("admin", "search") and "authenticated" in mode:
                    page = run(client, "验证工作台首页", *curl, url + "/")
                    if "<title>企业知识库</title>" in page:
                        return url
            except (DeployError, ValueError):
                pass
        time.sleep(3)
    raise DeployError("容器的实际映射端口未能同时返回工作台首页与 /api/mode")


def deploy(client):
    mounts = inspect(client, ".Mounts")
    app_mount = next((m for m in mounts if m["Destination"] == "/app"), None)
    if app_mount and (app_mount["Type"] != "bind" or app_mount["Source"] != REMOTE):
        raise DeployError("现有 /app 挂载与现场发布目录不匹配")
    for m in mounts:
        if any(m["Destination"] == p or m["Destination"].startswith(p + "/") for p in ("/app/app", "/app/web")):
            raise DeployError("存在独立的源码挂载，请先核对现场发布目录")
    original_image = inspect(client, ".Image")
    compose = None if app_mount else compose_plan(client, mounts, original_image)
    if app_mount:
        run(client, "验证整目录部署", "test", "-f", REMOTE + "/app/main.py")
        run(client, "验证整目录部署", "test", "-f", REMOTE + "/web/index.html")
    stage = run(client, "创建发布临时目录", "mktemp", "-d", "/tmp/knowbase-upload.XXXXXXXX")
    if not re.fullmatch(r"/tmp/knowbase-upload\.[A-Za-z0-9]{8}", stage):
        raise DeployError("服务器未返回有效的临时目录路径")
    req, helper = stage + "/source/requirements.txt", stage + "/deploy_runtime.sh"
    publisher = ["docker", "run", "--rm", "--network", "none", "-v", f"{stage}:/release",
                 "-v", f"{REMOTE}:/project", "--entrypoint", "python", original_image, "-c", PUBLISH]
    publishing = switching = False
    remove_stage = True
    temporary_tags = []
    try:
        upload(client, stage, collect_files(build_files=bool(compose)))
        if app_mount:
            with client.open_sftp() as sftp:
                with sftp.file(helper, "wb") as target:
                    target.write((ROOT / "scripts" / "deploy_runtime.sh").read_bytes().replace(b"\r\n", b"\n"))
            status = run(client, "核对已准备的运行依赖", "bash", helper, "ready", CONTAINER, req)
            if status == "missing":
                upload_wheels(client, stage, original_image)
            elif status != "ready":
                raise DeployError("服务器返回的运行依赖状态无效")
            print("正在原镜像中准备并验证运行依赖…", flush=True)
            run(client, "准备运行依赖", "bash", helper, "prepare", CONTAINER, req, stage + "/wheelhouse")
        else:
            upload_wheels(client, stage, original_image)
            suffix = stage.rsplit(".", 1)[1]
            base_tag = "knowbase-deploy-base:" + suffix
            candidate = "knowbase-deploy-candidate:" + suffix
            run(client, "保留当前基础镜像", "docker", "tag", original_image, base_tag)
            temporary_tags.append(base_tag)
            with client.open_sftp() as sftp:
                with sftp.file(stage + "/Dockerfile", "wb") as target:
                    target.write(OFFLINE_DOCKERFILE.format(base=base_tag).encode("utf-8"))
            print("正在用已上传的安装包离线构建候选镜像…", flush=True)
            run(client, "离线构建候选镜像", "docker", "build", "--network=none", "--pull=false", "-t", candidate, stage)
            temporary_tags.append(candidate)
        publishing = True
        run(client, "发布程序文件", *publisher, "publish", "/release", "/project")
        if compose:
            cmd, service, image = compose
            switching = True
            run(client, "启用已构建的镜像标签", "docker", "tag", candidate, image)
            run(client, "启动新版容器", *cmd, "up", "-d", "--no-build", "--no-deps", service)
        else:
            print("正在启用新依赖并启动原容器…", flush=True)
            switching = True
            run(client, "启用运行依赖", "bash", helper, "activate", CONTAINER, req)
        url = health(client)
        address = client_address(inspect(client, ".NetworkSettings.Ports"), url)
        print(f"服务已就绪，已验证首页和 API。浏览器访问 {address}" if address else
              f"服务已就绪，端口仅发布到服务器本机 {url}", flush=True)
    except BaseException:
        if publishing:
            try:
                run(client, "恢复原程序文件", *publisher, "restore", "/release", "/project")
                if switching and compose:
                    run(client, "恢复原镜像标签", "docker", "tag", original_image, image)
                    run(client, "恢复原镜像容器", *cmd, "up", "-d", "--no-build", "--no-deps", service)
                elif switching:
                    run(client, "恢复原容器运行依赖", "bash", helper, "rollback", CONTAINER, req)
            except Exception:
                remove_stage = False
                raise DeployError(f"发布失败，自动恢复未完成。原文件保存在 {stage}/previous") from None
        raise
    finally:
        if remove_stage:
            for tag in reversed(temporary_tags):
                run(client, "清理部署临时镜像标签", "docker", "image", "rm", "--no-prune", tag)
            run(client, "清理本次发布暂存目录", "rm", "-rf", "--", stage)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        import paramiko
    except ImportError:
        print("上传工具需要 Paramiko，请在本项目虚拟环境安装 paramiko。", file=sys.stderr)
        return 1
    try:
        with paramiko.SSHClient() as client:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(HOST, username=USER, password=PASSWORD, timeout=30)
            client.get_transport().set_keepalive(30)
            with deployment_lock(client):
                deploy(client)
    except DeployError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"服务器发布异常（{type(exc).__name__}）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
