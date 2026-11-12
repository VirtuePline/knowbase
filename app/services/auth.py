"""企业登录：MongoDB 持久化会话与账号体系。

- 会话：随机 token 写入 HttpOnly cookie，数据库只存令牌哈希，支持两个端口共享会话。
- 账号：pbkdf2 加盐哈希存储，两端始终验证登录；部门和角色实时从数据库读取。
- admin 可注册普通 user；首次未配置管理员密码时生成本机初始化凭据文件。
本进程保留内存缓存供受控测试使用，实际请求以数据库会话为准。
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from app.config import settings
from app.services import database, mongo

SESSION_COOKIE = "admin_session"
SESSION_TTL = 12 * 3600   # 会话有效期 12 小时
_PBKDF2_ITER = 120_000

_lock = threading.Lock()
_sessions: dict[str, dict] = {}   # token -> {expires, user, password_version}
_bootstrap_lock = threading.Lock()
_ACCOUNT = re.compile(r"[a-z][a-z0-9_.-]{0,31}\Z")
_DISPLAY_NAME = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fffA-Za-z0-9][\u3400-\u4dbf\u4e00-\u9fffA-Za-z0-9_.-]{0,31}\Z")


def _session_collection():
    db = mongo.db()
    return db.auth_sessions if isinstance(db, Database) else None


def _token_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(username: str = "", password_version: int = 0) -> str:
    """签发新会话 token，并顺手清掉已过期的旧会话。"""
    token = secrets.token_hex(24)
    now = time.time()
    with _lock:
        for expired in [t for t, s in _sessions.items() if s["expires"] < now]:
            _sessions.pop(expired, None)
        _sessions[token] = {"expires": now + SESSION_TTL, "user": username, "password_version": password_version}
    collection = _session_collection()
    if collection is not None:
        collection.create_index("expire_at", expireAfterSeconds=0)
        collection.insert_one({"_id": _token_id(token), **_sessions[token],
                               "expire_at": datetime.fromtimestamp(now + SESSION_TTL, timezone.utc)})
    return token


def verify(token: str | None) -> bool:
    """校验会话和数据库中的密码版本；改密后其他进程的旧会话也失效。"""
    return session_identity(token) is not None


def _active_session(token: str | None) -> dict | None:
    if not token:
        return None
    collection = _session_collection()
    if collection is not None:
        row = collection.find_one({"_id": _token_id(token)})
        if row is None or row["expires"] < time.time():
            return None
        return row
    with _lock:
        session = _sessions.get(token)
        if session is None:
            return None
        if session["expires"] < time.time():
            _sessions.pop(token, None)
            return None
        return dict(session)


def revoke(token: str | None) -> None:
    if token:
        collection = _session_collection()
        if collection is not None:
            collection.delete_one({"_id": _token_id(token)})
        with _lock:
            _sessions.pop(token, None)


def session_user(token: str | None) -> str:
    """有效会话对应的账号；已撤销或密码版本失效时返回空串。"""
    identity = session_identity(token)
    return identity["username"] if identity else ""


def session_identity(token: str | None) -> dict | None:
    """有效会话对应的账号和当前角色；账号被删除后会话立即失效。"""
    session = _active_session(token)
    username = session.get("user") if session else ""
    if not username:
        return None
    row = mongo.db().users.find_one({"_id": username})
    if row is None or _password_version(row) != session.get("password_version", 0):
        revoke(token)
        return None
    return _row_identity(row)


def _password_version(row: dict) -> int | None:
    version = row.get("password_version", 0)
    return version if type(version) is int and version >= 0 else None


def _matches_password(row: dict, password: str) -> bool:
    try:
        salt = bytes.fromhex(row["salt"])
        return secrets.compare_digest(_hash_password(password, salt), row["password_hash"])
    except (KeyError, TypeError, ValueError):
        return False


def _row_identity(row: dict) -> dict:
    username = row["_id"]
    role = row.get("role")
    if role not in {"admin", "user"}:
        # 只把无角色的历史配置管理员提升为 admin，其他历史账号归为 user。
        role = "admin" if "role" not in row and username == settings.auth_admin_user else "user"
        mongo.db().users.update_one({"_id": username, "role": row.get("role")}, {"$set": {"role": role}})
    must_change = row.get("must_change_password")
    if must_change is None:
        # 旧空密码账号同样进入首次设置流程，不能因缺少新字段绕过门禁。
        must_change = _matches_password(row, "")
        mongo.db().users.update_one(
            {"_id": username, "password_hash": row.get("password_hash"), "must_change_password": None},
            {"$set": {"must_change_password": must_change}},
        )
    departments = list(row.get("department_ids", ["operations"]))
    roles = row.get("department_roles", {"operations": "editor"} if "department_ids" not in row else {})
    roles = {key: value for key, value in roles.items()
             if key in departments and value in {"admin", "editor", "member"}}
    return {"username": username, "role": role, "must_change_password": bool(must_change),
            "display_name": row.get("display_name") or username, "password_version": _password_version(row),
            "department_ids": departments, "department_roles": roles}


# ---------- 账号体系 ----------

def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITER).hex()


def validate_registration(account: str, display_name: str, password: str = "") -> tuple[str, str]:
    """注册只设置用户名，普通账号在第一次登录后自行设置密码。"""
    account = account.strip().lower()
    display_name = unicodedata.normalize("NFKC", display_name.strip())
    if not _ACCOUNT.fullmatch(account):
        raise ValueError("账号须为 1–32 位英文字母、数字、下划线、点或短横线，并以英文字母开头")
    if not _DISPLAY_NAME.fullmatch(display_name):
        raise ValueError("用户名须为 1–32 位中文、字母、数字、下划线、点或短横线，并以中文、字母或数字开头")
    if password:
        raise ValueError("创建账号时无需填写密码，请由本人首次登录后设置")
    return account, display_name


def _login_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value.strip()).casefold()


def _row_login_names(row: dict) -> set[str]:
    return {_login_name(str(value)) for value in (row["_id"], row.get("display_name") or row["_id"])}


def _ensure_login_names_available(users, document: dict) -> None:
    # 共享唯一多键索引阻止新账号在 account / display_name 两个字段间并发抢占。
    users.create_index("login_names", unique=True, sparse=True)
    aliases = set(document["login_names"])
    # 旧文档尚无别名索引，必须检查其原有 ID 和显示名，不能抢占其登录名。
    for row in users.find({}):
        if aliases & _row_login_names(row):
            raise DuplicateKeyError("login name already exists")


def _user_document(username: str, password: str, role: str, display_name: str | None = None) -> dict:
    salt = secrets.token_bytes(16)
    return {"_id": username, "salt": salt.hex(), "password_hash": _hash_password(password, salt),
            "created_at": database.now_iso(), "role": role, "must_change_password": password == "",
            "password_version": 0, "display_name": display_name or username,
            "login_names": sorted({_login_name(username), _login_name(display_name or username)})}


def create_user(username: str, password: str = "", role: str = "user", *, display_name: str | None = None,
                department_ids: list[str] | None = None, department_roles: dict[str, str] | None = None) -> None:
    """仅创建账号，重名由数据库唯一主键拒绝，绝不重置已有密码。"""
    if role not in {"admin", "user"}:
        raise ValueError("无效的账号角色")
    users = mongo.db().users
    document = _user_document(username, password, role, display_name)
    document["department_ids"] = department_ids if department_ids is not None else ["operations"]
    document["department_roles"] = department_roles if department_roles is not None else {"operations": "editor"}
    _ensure_login_names_available(users, document)
    users.insert_one(document)


def authenticate(username: str, password: str) -> bool:
    """账号密码校验；账号不存在或密码错误一律 False。"""
    return authenticate_identity(username, password) is not None


def authenticate_identity(username: str, password: str) -> dict | None:
    """返回本次实际验证的密码版本，禁止验证后另读新版本签发旧密码会话。"""
    alias = _login_name(username)
    if not alias:
        return None
    # 查已索引别名与尚未迁移的旧账号；多个账号同名时拒绝登录，绝不猜测目标。
    candidates = [row for row in mongo.db().users.find(
        {"$or": [{"login_names": alias}, {"login_names": {"$exists": False}}]})
        if alias in _row_login_names(row)]
    row = candidates[0] if len(candidates) == 1 else None
    if len(candidates) > 1:
        # 旧规则允许 Admin 与 admin 两个不同 ID。只为旧文档的精确原始 ID
        # 保留登录能力；大小写混合别名或重名显示名仍拒绝，不猜测目标账号。
        exact_legacy = [candidate for candidate in candidates
                        if "login_names" not in candidate and candidate["_id"] == username.strip()]
        row = exact_legacy[0] if len(exact_legacy) == 1 else None
    if not row or _password_version(row) is None or not _matches_password(row, password):
        return None
    return _row_identity(row)


class PasswordChangeConflict(Exception):
    """密码在验证后已被另一个请求更新。"""


def change_password(username: str, current_password: str, new_password: str, expected_version: int) -> dict:
    """仅更新调用者账号；旧哈希和版本比较后原子写入，防止并发覆盖。"""
    if not 8 <= len(new_password) <= 128 or not new_password.strip():
        raise ValueError("新密码须为 8–128 位且不能全部为空格")
    users = mongo.db().users
    row = users.find_one({"_id": username})
    if row is None or _password_version(row) != expected_version:
        raise PasswordChangeConflict()
    if not _matches_password(row, current_password):
        raise ValueError("当前密码不正确")
    salt = secrets.token_bytes(16)
    new_hash = _hash_password(new_password, salt)
    result = users.update_one(
        {"_id": username, "password_hash": row["password_hash"], "salt": row["salt"],
         "password_version": row.get("password_version")},
        {"$set": {"salt": salt.hex(), "password_hash": new_hash, "must_change_password": False},
         "$inc": {"password_version": 1}},
    )
    if result.modified_count != 1:
        raise PasswordChangeConflict()
    # 清理本进程旧 token；其他进程即使仍留有 token，也会被数据库版本比较拒绝。
    with _lock:
        for token in [token for token, session in _sessions.items() if session.get("user") == username]:
            _sessions.pop(token, None)
    updated = {**row, "salt": salt.hex(), "password_hash": new_hash, "must_change_password": False,
               "password_version": expected_version + 1}
    return _row_identity(updated)


def ensure_admin_user(username: str, password: str) -> None:
    """幂等初始化管理员；已有密码不覆盖，生成的初始密码只写入本机文件。"""
    username = username.strip() or "admin"
    with _bootstrap_lock:
        users = mongo.db().users
        if users.find_one({"_id": username}):
            users.update_one({"_id": username}, {"$set": {"role": "admin"}})
            return
        generated = not password
        initial_password = password or secrets.token_urlsafe(24)
        document = _user_document(username, initial_password, "admin")
        try:
            _ensure_login_names_available(users, document)
            users.insert_one(document)
        except DuplicateKeyError:
            if users.find_one({"_id": username}):
                return  # 另一进程已完成初始化，不覆盖其密码。
            raise RuntimeError("初始管理员账号与已有登录名冲突，请配置不同的管理员账号") from None
        if not generated:
            return
        path = settings.index_dir / "bootstrap-admin.txt"
        created = False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"username: {username}\npassword: {initial_password}\n"
                             "此文件仅供首次登录，请妥善保管并在记录密码后删除。\n")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass  # Windows 等平台的权限能力依文件系统而定。
        except OSError:
            # 凭据无法安全落盘时撤销本次新建账号，避免留下无法登录的随机密码。
            users.delete_one({"_id": username, "password_hash": document["password_hash"]})
            if created:
                path.unlink(missing_ok=True)
            raise RuntimeError("无法写入初始化管理员凭据，请检查 data/index 权限或配置初始密码") from None


def count_users() -> int:
    return mongo.db().users.count_documents({})


def list_users() -> list[dict]:
    """全部账号概览（按创建时间排序），只暴露展示字段，不含哈希与盐。"""
    result = []
    for row in mongo.db().users.find():
        identity = _row_identity(row)
        identity["created_at"] = str(row.get("created_at") or "")
        result.append(identity)
    result.sort(key=lambda item: (item["created_at"], item["username"]))
    return result


def delete_user(username: str) -> None:
    """删除普通账号；管理员账号与不存在的账号一律拒绝。删除后其全部会话立即失效。"""
    users = mongo.db().users
    row = users.find_one({"_id": username})
    if row is None:
        raise ValueError("账号不存在或已被删除")
    if row.get("role") == "admin":
        raise ValueError("管理员账号不可删除")
    users.delete_one({"_id": username})
    collection = _session_collection()
    if collection is not None:
        collection.delete_many({"user": username})
        mongo.db().chat_sessions.delete_many({"user": username})
        database.bump_access_revision()
    with _lock:
        for token in [token for token, session in _sessions.items() if session.get("user") == username]:
            _sessions.pop(token, None)
