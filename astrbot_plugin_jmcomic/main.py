# -*- coding: utf-8 -*-
"""jm下载姬 - 禁漫本子下载插件

自包含依赖：jmcomic 源码内嵌在 vendor/，装上即用，卸载即清。
命令：
  搜本 <关键词> [页码]     搜索本子
  下本 <ID> [格式]         下载本子 (longimg/zip/pdf)
  排行 [周|月|日]          查看榜单
  我的任务                 查看任务
  取消 <任务号>            取消任务

授权式更新（2026-10-04 起，不再自动装）：
  敲任意命令会顺带查一次上游版本，有新版就在回复里提示一行
  jm更新                   下载新版到临时目录并静态审查（不装）
  jm安装                   看过审查报告后，确认安装
  jm跳过                   跳过这一版，等上游再出新版才重问
  jm放弃                   丢掉临时文件，vendor 一个字不动
"""

import asyncio
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import zipfile
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent
_DEPS_DIR = _PLUGIN_DIR / ".deps"
_VENDOR = _PLUGIN_DIR / "vendor"
# 更新下载源：官方源优先（版本最新），失败回退清华镜像（国内快，可能滞后）
_PIP_INDEX_URLS = ("https://pypi.org/simple/", "https://pypi.tuna.tsinghua.edu.cn/simple/")
# 插件私有依赖优先于系统包。curl-cffi 会安装在 .deps，绝不污染系统 pip 环境。
for _path in (str(_VENDOR), str(_DEPS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import Plain, Image, File
from astrbot.core.message.message_event_result import MessageChain

def _ensure_curl_cffi() -> tuple[bool, str]:
    """【2026-10-04 授权式改造】只检查，绝不安装。

    原实现在这里直接 pip install —— 等于插件第一次被加载就自动往 .deps 里写东西，
    不问任何人。现在改成纯检查：缺了就返回 False，插件照常加载但拒绝干活。
    要装必须由姐姐敲 jm依赖 走「列版本 → 选版本 → 下载审查 → 确认装」。
    """
    if (_DEPS_DIR / "curl_cffi").is_dir():
        importlib.invalidate_caches()
        return True, "ok"
    return False, "缺少私有 curl-cffi"

# 模块加载时只做检查，不安装。装由 jm依赖 授权触发。
_CURL_OK, _CURL_MSG = _ensure_curl_cffi()

try:
    import jmcomic
    JM_READY = True
except Exception as _jm_import_err:
    JM_READY = False
    JM_IMPORT_ERR = _jm_import_err
    logger.warning(f"[jm下载姬] jmcomic 导入失败，插件暂不可用: {_jm_import_err}")

_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
_PYPI_JSON_URLS = [
    "https://pypi.org/pypi/jmcomic/json",
    "https://pypi.tuna.tsinghua.edu.cn/pypi/jmcomic/json",
]
_FMT_ALIAS = {
    "longimg": "longimg", "长图": "longimg", "图": "longimg",
    "zip": "zip", "压缩包": "zip", "包": "zip",
    "pdf": "pdf", "文档": "pdf",
}

STATUS_TEXT = {
    "queued": "排队中",
    "downloading": "下载中",
    "converting": "转换中",
    "sending": "发送中",
    "done": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}


class JmComicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.config = config or {}
        self._tasks: dict[int, dict] = {}
        self._bg_tasks: set = set()
        self._next_tid = 1
        self._update_lock = threading.Lock()
        self._update_notice: str | None = None
        self._tid_lock = threading.Lock()
        self._startup_check_dep()

    # ---------------- 依赖自管理 ----------------

    def _startup_check_dep(self) -> None:
        """【2026-10-04 授权式改造】只报状态，绝不自动安装。

        原来这里是「没检测到就后台 pip install」，现在改成只打日志。
        缺依赖时插件暂停工作，命令回一句提示，要装得敲 jm依赖。
        """
        if self._curl_ok():
            logger.info("[jm下载姬] 私有 curl-cffi 已就绪")
            return
        logger.warning("[jm下载姬] 缺少私有 curl-cffi，插件暂停工作。"
                       "要装请敲 jm依赖，走授权流程")

    @staticmethod
    def _curl_ok() -> bool:
        return JM_READY

    # ---------------- 上游自动更新 ----------------

    @staticmethod
    def _get_latest_pypi_version(timeout: int = 5):
        """多源查询 jmcomic 最新版本：官方源优先，镜像兜底。全部失败返回 None。"""
        import urllib.request
        for url in _PYPI_JSON_URLS:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "jm-downloader/1.0"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                version = data.get("info", {}).get("version")
                if version:
                    return version
            except Exception as e:
                logger.debug(f"[jm下载姬] 上游版本源失败 {url}: {e}")
        return None

    @staticmethod
    def _vendor_version() -> str | None:
        """读取内嵌 jmcomic 的版本号。"""
        try:
            import jmcomic
            return getattr(jmcomic, "__version__", None)
        except Exception:
            return None

    @staticmethod
    def _is_newer(latest: str, current: str) -> bool:
        def to_tuple(v: str):
            nums = []
            for part in v.replace("v", "").split("."):
                m = re.search(r"\d+", part)
                nums.append(int(m.group()) if m else 0)
            return tuple(nums)
        try:
            return to_tuple(latest) > to_tuple(current)
        except Exception:
            return latest != current

    @staticmethod
    def _probe_version(tmp_dir) -> str | None:
        """子进程读临时目录 jmcomic 版本号（隔离环境，vendor 提供 common 兜底）。"""
        script = (
            "import sys; sys.path.insert(0, %r); sys.path.insert(0, %r); sys.path.insert(0, %r); "
            "import jmcomic; print(jmcomic.__version__)"
            % (str(_DEPS_DIR), str(_VENDOR), str(tmp_dir))
        )
        try:
            r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
            out = r.stdout.strip()
            return out.splitlines()[-1] if out else None
        except Exception:
            return None

    @staticmethod
    def _probe_usable(tmp_dir) -> bool:
        """子进程验证新版 jmcomic 能导入关键组件。"""
        script = (
            "import sys; sys.path.insert(0, %r); sys.path.insert(0, %r); sys.path.insert(0, %r); "
            "import jmcomic; from jmcomic import JmOption; print('OK')"
            % (str(_DEPS_DIR), str(_VENDOR), str(tmp_dir))
        )
        try:
            r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
            return r.returncode == 0 and "OK" in r.stdout
        except Exception:
            return False

    # ---------------- 授权式依赖管理（curl-cffi） ----------------

    _DEP_TMP = ".dep_tmp"

    @staticmethod
    def _fetch_pypi_versions(pkg: str = "curl-cffi", timeout: int = 15):
        """★只读★ 从 PyPI 拿版本列表和各自的发布时间。"""
        import urllib.request
        try:
            req = urllib.request.Request(f"https://pypi.org/pypi/{pkg}/json",
                                         headers={"User-Agent": "jm-downloader/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode("utf-8"))
            rel = {}
            for v, fs in d.get("releases", {}).items():
                if re.search(r"[a-zA-Z]", v):
                    continue
                ts = [f.get("upload_time") for f in fs if f.get("upload_time")]
                rel[v] = ts[0][:10] if ts else "?"
            return {"latest": d["info"]["version"],
                    "author": d["info"].get("author_email") or d["info"].get("author"),
                    "releases": rel}
        except Exception as e:
            logger.warning(f"[jm下载姬] 依赖版本查询失败: {e}")
            return None

    @staticmethod
    def _local_dep_version() -> str | None:
        for d in _DEPS_DIR.glob("curl_cffi-*.dist-info"):
            m = re.match(r"curl_cffi-(.+?)\.dist-info", d.name)
            if m:
                return m.group(1)
        return None

    @staticmethod
    def _sha256_of(p) -> str:
        import hashlib
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    @filter.command("jm依赖")
    async def cmd_jm_dep(self, event: AstrMessageEvent):
        """curl-cffi 的授权式入口：列版本 / 下载审查 / 确认装 / 放弃。"""
        if not self._check_access(event):
            yield event.plain_result(self._deny_text())
            return
        arg = self._strip_cmd(event.get_message_str(), "jm依赖").strip()

        # ---- 无参：列出版本，带发布时间 ----
        if not arg:
            local = self._local_dep_version()
            info = await asyncio.to_thread(self._fetch_pypi_versions)
            L = ["私有依赖 curl-cffi", f"  本机现在：{local or '没装'}"]
            if not info:
                L.append("  PyPI 连不上，过会儿再试。")
                yield event.plain_result("\n".join(L)); return
            rel = info["releases"]
            L.append(f"  PyPI 最新：{info['latest']}   作者：{info['author']}")
            L.append("")
            L.append("  可选版本（版本 / 发布时间）：")
            for v in sorted(rel, key=lambda x: [int(i) for i in x.split(".")])[-12:][::-1]:
                mark = "  ← 本机现在的" if v == local else ("  ← 最新" if v == info["latest"] else "")
                L.append(f"    {v:10s} {rel[v]}{mark}")
            L.append("")
            L.append("回 jm依赖 <版本号> 下载并审查，例如：jm依赖 0.16.3")
            yield event.plain_result("\n".join(L)); return

        # ---- 放弃 ----
        if arg in ("放弃", "丢弃"):
            await asyncio.to_thread(shutil.rmtree, _PLUGIN_DIR / self._DEP_TMP, True)
            yield event.plain_result("临时文件清掉了，.deps 没动。")
            return

        # ---- 装 ----
        if arg in ("装", "安装"):
            ok, msg = await asyncio.to_thread(self._install_dep)
            yield event.plain_result(("装好了：" if ok else "没装上：") + msg
                                     + "\n\n要生效得重载插件，这一步只能你来。")
            return

        # ---- 下载 + 审查 ----
        ver = arg
        info = await asyncio.to_thread(self._fetch_pypi_versions)
        if not info or ver not in info["releases"]:
            yield event.plain_result(f"PyPI 上没有 {ver} 这个版本。先敲 jm依赖 看列表。")
            return
        yield event.plain_result(f"开始下载 curl-cffi {ver} 到临时目录，下完发审查报告。")
        session = event.unified_msg_origin
        task = asyncio.create_task(self._dep_flow(ver, info, session))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _dep_flow(self, ver: str, info: dict, session: str) -> None:
        ok, msg = await asyncio.to_thread(self._download_dep_to_tmp, ver)
        if not ok:
            await self._notify(session, f"下载失败：{msg}\n.deps 没动。")
            return
        report = await asyncio.to_thread(self._audit_dep_tmp, ver, info)
        self._write_state(dep_reviewed=ver)
        await self._notify(session, f"下载完成，审查报告：\n\n{report}\n\n要装回 jm依赖 装，不要回 jm依赖 放弃。")

    def _download_dep_to_tmp(self, ver: str) -> tuple[bool, str]:
        """★不碰 .deps★ 下载指定版本到 .dep_tmp。"""
        if not self._update_lock.acquire(blocking=False):
            return False, "已有任务在进行"
        tmp = _PLUGIN_DIR / self._DEP_TMP
        try:
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True, exist_ok=True)
            # 源顺序：镜像优先。
            # 理由：版本号是钉死的（==ver），镜像上的同一个版本不会错；
            # 而官方源 pypi.org 的索引在本机常常慢到分钟级（2026-10-04 实测
            # 150 秒下不完一个 13.5MB 的 whl，镜像 0.7 秒）。所以先镜像，官方兜底。
            _dep_sources = (_PIP_INDEX_URLS[1], _PIP_INDEX_URLS[0])
            r = None
            for _src in _dep_sources:
                r = subprocess.run(
                    [sys.executable, "-m", "pip", "download", f"curl-cffi=={ver}",
                     "--no-deps", "--only-binary", ":all:",
                     "--retries", "2", "--timeout", "20",
                     "-d", str(tmp), "-i", _src],
                    timeout=120, check=False, capture_output=True, text=True)
                if r.returncode == 0:
                    break
            if r.returncode != 0:
                return False, f"pip download 失败：{(r.stderr or '')[-300:]}"
            whls = list(tmp.glob("curl_cffi-*.whl")) + list(tmp.glob("curl-cffi-*.tar.gz"))
            if not whls:
                return False, "下载目录里没有找到包文件"
            return True, f"{whls[0].name}"
        except Exception as e:
            return False, str(e)
        finally:
            self._update_lock.release()

    def _audit_dep_tmp(self, ver: str, info: dict) -> str:
        """审查下载来的 curl-cffi 包。实话实说：.so 里写了什么看不出来。"""
        tmp = _PLUGIN_DIR / self._DEP_TMP
        pkgs = list(tmp.glob("curl_cffi-*")) + list(tmp.glob("curl-cffi-*"))
        L = [f"curl-cffi {ver} 审查报告", ""]
        if not pkgs:
            return "\n".join(L + ["没找到下载下来的包文件。"])

        import zipfile, io
        p = pkgs[0]
        L.append(f"① 包文件：{p.name}   {p.stat().st_size/1024/1024:.1f} MB")
        L.append(f"   sha256 = {self._sha256_of(p)}")

        # 官方哈希比对（最硬的一条）
        try:
            import urllib.request
            req = urllib.request.Request("https://pypi.org/pypi/curl-cffi/json",
                                         headers={"User-Agent": "jm-downloader/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read().decode("utf-8"))
            official = [f["digests"]["sha256"] for f in d["releases"].get(ver, [])
                        if f.get("filename") == p.name]
            mine = self._sha256_of(p)
            if official:
                L.append("② 官方哈希比对：" + ("✅ 一致，没被篡改" if official[0] == mine else "❌ 不一致！别装"))
            else:
                L.append("② 官方哈希比对：这个文件名在 PyPI 上没匹配到，无法比对")
        except Exception as e:
            L.append(f"② 官方哈希比对：取不到（{type(e).__name__}）")

        # 包里有什么
        if p.suffix == ".whl":
            try:
                with zipfile.ZipFile(p) as z:
                    names = z.namelist()
                L.append("")
                L.append(f"③ 包内文件数：{len(names)}")
                hooks = [n for n in names if n.endswith(".pth") or n.endswith("setup.py")]
                sos = [n for n in names if n.endswith(".so")]
                L.append(f"   安装期钩子：{'、'.join(hooks) if hooks else '无'}")
                L.append(f"   二进制 .so：{len(sos)} 个")
                for s in sos[:5]:
                    L.append(f"     {s}")
                danger = [n for n in names if re.search(r"(backdoor|payload|steal|keylog|miner|/tmp/|/etc/)", n, re.I)]
                if danger:
                    L.append("   ⚠ 文件名可疑：" + "、".join(danger[:10]))
            except Exception as e:
                L.append(f"③ 读包内清单失败：{e}")
        else:
            L.append("")
            L.append("③ 是源码包，不是 whl，装的时候会现场编译")

        L.append("")
        L.append("⚠ 这个库的核心是编译好的 .so，里面写了什么读不出来。")
        L.append("   能确认的是：包名、版本、来源、哈希是否与官方一致、有没有夹带安装期钩子。")
        return "\n".join(L)

    def _install_dep(self) -> tuple[bool, str]:
        """★唯一允许写 .deps 的入口★ 把 .dep_tmp 里的东西装进 .deps。"""
        tmp = _PLUGIN_DIR / self._DEP_TMP
        st = self._read_state()
        ver = st.get("dep_reviewed")
        pkgs = list(tmp.glob("curl_cffi-*")) + list(tmp.glob("curl-cffi-*"))
        if not ver or not pkgs:
            return False, "还没下载审查过，先回 jm依赖 <版本号>"
        if ver not in pkgs[0].name:
            return False, f"审查过的是 {ver}，临时目录里却是 {pkgs[0].name}，已停手"
        try:
            _DEPS_DIR.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", str(pkgs[0]),
                 "-q", "--target", str(_DEPS_DIR)],
                timeout=600, check=False, capture_output=True, text=True)
            if r.returncode != 0:
                return False, f"pip install 失败：{(r.stderr or '')[-300:]}"
            importlib.invalidate_caches()
            got = self._local_dep_version()
            shutil.rmtree(tmp, ignore_errors=True)
            self._write_state(dep_reviewed=None)
            return True, f".deps 里的 curl-cffi 现在是 {got}"
        except Exception as e:
            return False, str(e)

    # ---------------- 授权式更新：状态与只读检测 ----------------

    @property
    def _state_file(self) -> Path:
        return _PLUGIN_DIR / ".update_state.json"

    _state_lock = threading.Lock()

    def _read_state(self) -> dict:
        """读更新状态。文件不存在 / 损坏 / 字段类型错，一律回默认值，绝不抛。"""
        default = {"installed": None, "skipped": None, "reviewed": None, "last_check": 0,
                   "audit_bad": 0, "dep_reviewed": None,
                   "_cached_latest": None, "_cached_jm_app": None}
        try:
            with open(self._state_file, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return default
            for k, v in default.items():
                if k not in data:
                    data[k] = v
            for k in ("installed", "skipped", "reviewed"):
                if data[k] is not None and not isinstance(data[k], str):
                    data[k] = None
            if not isinstance(data["last_check"], (int, float)):
                data["last_check"] = 0
            return data
        except Exception:
            return default

    def _write_state(self, **kw) -> dict:
        """合并写状态：加锁 + 先写临时文件再 os.replace 原子替换。"""
        with self._state_lock:
            st = self._read_state()
            st.update({k: v for k, v in kw.items() if k in st})
            tmp = self._state_file.with_suffix(".json.tmp")
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(st, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self._state_file)
            except Exception as e:
                logger.error(f"[jm下载姬] 更新状态写入失败: {e}")
            return st

    _CHECK_THROTTLE = 600  # 秒；常规命令最多 10 分钟真请求一次上游

    def _installed_version(self) -> str | None:
        """vendor 里实际生效的版本，优先信状态文件，其次问内存里的模块。"""
        st = self._read_state()
        if st.get("installed"):
            return st["installed"]
        return self._vendor_version()

    def _probe_upstream(self, force: bool = False) -> dict:
        """★只读★ 问上游要版本号。不下载、不安装、不改 vendor。

        返回 {"latest": str|None, "jm_app": str|None, "throttled": bool}
        """
        st = self._read_state()
        now = time.time()
        if (not force) and (now - float(st.get("last_check") or 0) < self._CHECK_THROTTLE):
            return {"latest": st.get("_cached_latest"), "jm_app": st.get("_cached_jm_app"),
                    "throttled": True}
        latest = self._get_latest_pypi_version()
        jm_app = self._probe_jm_app_version()
        self._write_state(last_check=now, _cached_latest=latest, _cached_jm_app=jm_app)
        return {"latest": latest, "jm_app": jm_app, "throttled": False}

    @staticmethod
    def _probe_jm_app_version(timeout: int = 8) -> str | None:
        """★只读★ 问禁漫服务器要 App 版本号（jm3_version）。

        走子进程隔离：关掉两个会改全局常量的开关，避免污染当前进程的 jmcomic。
        """
        script = (
            "import sys; sys.path.insert(0, %r); sys.path.insert(0, %r);"
            "import jmcomic;"
            "jmcomic.JmModuleConfig.FLAG_API_CLIENT_AUTO_UPDATE_DOMAIN = False;"
            "jmcomic.JmModuleConfig.FLAG_USE_VERSION_NEWER_IF_BEHIND = False;"
            "c = jmcomic.JmOption.default().new_jm_client();"
            "print(getattr(c.setting().model_data, 'jm3_version', ''))"
            % (str(_DEPS_DIR), str(_VENDOR))
        )
        try:
            r = subprocess.run([sys.executable, "-c", script],
                               capture_output=True, text=True, timeout=timeout)
            out = (r.stdout or "").strip().splitlines()
            return out[-1].strip() if out else None
        except Exception:
            return None

    _refresh_lock = threading.Lock()
    _refreshing = False

    def _pre_hook(self, event: AstrMessageEvent) -> str:
        """常规命令的前置钩子：只读缓存 + 踢一个后台刷新。绝不阻塞命令。

        第一次没缓存就不提示，后台查完写进状态文件，下一个命令就带上了。
        """
        try:
            self._kick_refresh()
            st = self._read_state()
            latest, cur = st.get("_cached_latest"), self._installed_version()
            if latest and cur and self._is_newer(latest, cur) and latest != st.get("skipped"):
                app = f" · 禁漫 App {st.get('_cached_jm_app')}" if st.get("_cached_jm_app") else ""
                return f"\n\n[上游有新版 {latest}（现 {cur}）{app}，回 jm更新 处理]"
            return ""
        except Exception as e:
            logger.warning(f"[jm下载姬] 前置检查异常: {e}")
            return ""

    def _kick_refresh(self) -> None:
        """缓存过期且当前没有刷新在跑时，起一个 daemon 线程去查，立刻返回。"""
        try:
            st = self._read_state()
            if time.time() - float(st.get("last_check") or 0) < self._CHECK_THROTTLE:
                return
            with JmComicPlugin._refresh_lock:
                if JmComicPlugin._refreshing:
                    return
                JmComicPlugin._refreshing = True
        except Exception as e:
            logger.warning(f"[jm下载姬] 触发刷新异常: {e}")
            return
        threading.Thread(target=self._refresh_bg, daemon=True).start()

    def _refresh_bg(self) -> None:
        """后台查上游版本号，只写状态文件，绝不碰 vendor。"""
        try:
            latest = self._get_latest_pypi_version()
            jm_app = self._probe_jm_app_version()
            self._write_state(last_check=time.time(),
                              _cached_latest=latest, _cached_jm_app=jm_app)
        except Exception as e:
            logger.warning(f"[jm下载姬] 后台刷新异常: {e}")
        finally:
            JmComicPlugin._refreshing = False

    # ---------------- 授权式更新：四个受理命令 ----------------

    def _gate(self, event) -> tuple[bool, str, str | None]:
        """非常规命令的守门人。返回 (是否放行, 文本, 上游版本)。

        没更新时固定回「暂无更新，请不要用非常规指令」；探测异常也按无更新处理。
        """
        try:
            info = self._probe_upstream(force=True)
        except Exception as e:
            logger.debug(f"[jm下载姬] 上游探测异常: {e}")
            return False, "暂无更新，请不要用非常规指令", None
        latest = info.get("latest")
        cur = self._installed_version()
        if not latest or not cur or not self._is_newer(latest, cur):
            return False, "暂无更新，请不要用非常规指令", None
        app = f" · 禁漫 App {info['jm_app']}" if info.get("jm_app") else ""
        return True, f"上游最新 {latest}（现装 {cur}）{app}", latest

    @filter.command("jm更新")
    async def cmd_jm_update(self, event: AstrMessageEvent):
        """同意更新：下载到临时目录 + 静态审查。全程不碰 vendor。"""
        if not self._check_access(event):
            yield event.plain_result(self._deny_text())
            return
        ok, msg, latest = self._gate(event)
        if not ok:
            yield event.plain_result(msg)
            return
        if self._read_state().get("reviewed") == latest and (_PLUGIN_DIR / ".update_tmp").is_dir():
            yield event.plain_result(f"{msg}\n这版已经下载审查过了，回 jm安装 装，或 jm放弃 丢掉。")
            return
        yield event.plain_result(f"{msg}\n开始下载到临时目录，下完发审查报告。")
        session = event.unified_msg_origin
        task = asyncio.create_task(self._update_flow(latest, session))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _update_flow(self, latest: str, session: str) -> None:
        ok, msg = await asyncio.to_thread(self._download_to_tmp, latest)
        if not ok:
            await self._notify(session, f"下载失败：{msg}\nvendor 没动，照常能用。")
            return
        report = await asyncio.to_thread(self._audit_tmp, latest)
        self._write_state(reviewed=latest)
        await self._notify(session, f"下载完成，审查报告：\n\n{report}\n\n要装回 jm安装，不要回 jm放弃。")

    @filter.command("jm跳过")
    async def cmd_jm_skip(self, event: AstrMessageEvent):
        """跳过这一版，等上游再出新版才重新问。"""
        if not self._check_access(event):
            yield event.plain_result(self._deny_text())
            return
        ok, msg, latest = self._gate(event)
        if not ok:
            yield event.plain_result(msg)
            return
        self._write_state(skipped=latest)
        yield event.plain_result(f"好，{latest} 这版跳过。上游再出新版我再问你。")

    @filter.command("jm放弃")
    async def cmd_jm_abandon(self, event: AstrMessageEvent):
        """丢掉临时文件，vendor 一个字不动。"""
        if not self._check_access(event):
            yield event.plain_result(self._deny_text())
            return
        ok, msg, latest = self._gate(event)
        if not ok:
            yield event.plain_result(msg)
            return
        await asyncio.to_thread(shutil.rmtree, _PLUGIN_DIR / ".update_tmp", True)
        self._write_state(reviewed=None)
        yield event.plain_result("临时文件清掉了，vendor 没动。")

    @filter.command("jm强制安装")
    async def cmd_jm_force_install(self, event: AstrMessageEvent):
        """审查报过可疑时，明知风险仍要装的通道。必须显式敲这个命令。"""
        if not self._check_access(event):
            yield event.plain_result(self._deny_text())
            return
        ok, msg, latest = self._gate(event)
        if not ok:
            yield event.plain_result(msg)
            return
        st = self._read_state()
        if st.get("reviewed") != latest:
            yield event.plain_result("这版还没下载审查过，先回 jm更新。")
            return
        if not st.get("audit_bad"):
            yield event.plain_result("这版审查是干净的，不用强制，回 jm安装 就行。")
            return
        yield event.plain_result("⚠ 你确认过了，强制安装。出问题自己兜着。")
        ok2, msg2 = await asyncio.to_thread(self._install_from_tmp, latest)
        yield event.plain_result(("装好了：" if ok2 else "没装上：") + msg2
                                 + "\n\n要生效得重载插件，这一步只能你来。")

    @filter.command("jm安装")
    async def cmd_jm_install(self, event: AstrMessageEvent):
        """确认安装：覆盖 vendor。这是唯一允许动 vendor 的入口。"""
        if not self._check_access(event):
            yield event.plain_result(self._deny_text())
            return
        ok, msg, latest = self._gate(event)
        if not ok:
            yield event.plain_result(msg)
            return
        st = self._read_state()
        if st.get("reviewed") != latest:
            yield event.plain_result("这版还没下载审查过，先回 jm更新。")
            return
        if st.get("audit_bad"):
            yield event.plain_result("🛑 这版审查出过可疑项，不给装。\n"
                                     "翻回上次的报告看看具体是什么。\n"
                                     "要是你确认没问题，回 jm强制安装。")
            return
        yield event.plain_result(f"{msg}\n开始安装。")
        ok2, msg2 = await asyncio.to_thread(self._install_from_tmp, latest)
        yield event.plain_result(("装好了：" if ok2 else "没装上：") + msg2
                                 + "\n\n要生效得重载插件，这一步只能你来。")

    # ---------------- 授权式更新：下载 / 审查 / 安装 ----------------

    _TMP_NAME = ".update_tmp"

    _DANGER = [
        (r"\beval\s*\(", "eval()"),
        (r"\bexec\s*\(", "exec()"),
        (r"__import__\s*\(", "__import__()"),
        (r"\bos\.system\s*\(", "os.system()"),
        (r"\bos\.popen\s*\(", "os.popen()"),
        (r"\bsubprocess\.", "subprocess 调用"),
        (r"\bpickle\.loads\s*\(", "pickle.loads()"),
        (r"\bmarshal\.loads\s*\(", "marshal.loads()"),
        (r"\bctypes\.", "ctypes"),
        (r"\bsocket\.", "socket"),
        (r"\bshutil\.rmtree\s*\(", "shutil.rmtree()"),
        (r"os\.environ", "读环境变量"),
        (r"base64\.b64decode", "base64 解码"),
        (r"~/\.ssh|/etc/passwd|/etc/shadow|id_rsa", "碰敏感路径"),
    ]

    def _download_to_tmp(self, latest: str) -> tuple[bool, str]:
        """★不碰 vendor★ 下载新版到 .update_tmp，只做完整性校验，不安装。"""
        if not self._update_lock.acquire(blocking=False):
            return False, "已有更新任务在进行"
        tmp_dir = _PLUGIN_DIR / self._TMP_NAME
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            tmp_dir.mkdir(parents=True, exist_ok=True)
            # 源顺序：镜像优先（理由同依赖下载那段，官方源索引常慢到分钟级）
            _lib_sources = (_PIP_INDEX_URLS[1], _PIP_INDEX_URLS[0])
            result = None
            for _idx in _lib_sources:
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "jmcomic", "-q",
                     "--target", str(tmp_dir), "--no-deps",
                     "--retries", "2", "--timeout", "20", "-i", _idx],
                    timeout=120, check=False)
                if result.returncode == 0:
                    break
            if result is None or result.returncode != 0:
                return False, f"pip 下载失败(码{result.returncode if result else '无'})"
            for _idx in _lib_sources:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "commonx", "-q",
                     "--target", str(tmp_dir), "--no-deps",
                     "--retries", "2", "--timeout", "20", "-i", _idx],
                    timeout=120, check=False)
            importlib.invalidate_caches()
            if not (tmp_dir / "jmcomic").is_dir():
                return False, "下载目录缺少 jmcomic 包"
            tmp_ver = self._probe_version(tmp_dir)
            if tmp_ver != latest:
                return False, f"版本校验不符(期望{latest},实际{tmp_ver or '未知'})"
            if not self._probe_usable(tmp_dir):
                return False, "新版导入自检失败"
            return True, f"已下载到 {tmp_dir}"
        except Exception as e:
            return False, str(e)
        finally:
            self._update_lock.release()

    @staticmethod
    def _iter_py(root: Path):
        for p in sorted(root.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            yield p

    def _audit_tmp(self, latest: str) -> str:
        """静态审查五样，返回给人看的纯文本报告。不执行任何下载来的代码。"""
        tmp = _PLUGIN_DIR / self._TMP_NAME
        new_root = tmp / "jmcomic"
        old_root = _VENDOR / "jmcomic"
        out = [f"新版 jmcomic {latest}", ""]

        # ① 文件清单差异
        new_set = {str(p.relative_to(new_root)) for p in self._iter_py(new_root)}
        old_set = {str(p.relative_to(old_root)) for p in self._iter_py(old_root)} if old_root.is_dir() else set()
        added, removed = sorted(new_set - old_set), sorted(old_set - new_set)
        out.append(f"① 文件清单：新 {len(new_set)} 个 / 旧 {len(old_set)} 个")
        if added:
            out.append("  新增：" + "、".join(added[:20]) + ("…" if len(added) > 20 else ""))
        if removed:
            out.append("  消失：" + "、".join(removed[:20]) + ("…" if len(removed) > 20 else ""))
        if not added and not removed:
            out.append("  文件组成没变")

        # ② 逐文件行数变化 → 找出被改动的
        changed = []
        for rel in sorted(new_set & old_set):
            try:
                a = (new_root / rel).read_text(encoding="utf-8", errors="ignore").count("\n")
                b = (old_root / rel).read_text(encoding="utf-8", errors="ignore").count("\n")
                if a != b:
                    changed.append(f"{rel} {b}→{a} 行({a-b:+d})")
            except Exception:
                pass
        out.append("")
        out.append(f"② 改动文件：{len(changed)} 个")
        for c in changed[:25]:
            out.append("  " + c)
        if not changed:
            out.append("  行数全部一致")

        # ③ 危险调用 + ④ 外联地址
        danger_hits, urls = [], set()
        for p in self._iter_py(new_root):
            try:
                src = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            rel = p.relative_to(new_root)
            for pat, name in self._DANGER:
                for mm in re.finditer(pat, src):
                    ln = src[:mm.start()].count("\n") + 1
                    danger_hits.append(f"{rel}:{ln} {name}")
            for u in re.findall(r"https?://[\w./\-?=&%:]+", src):
                urls.add(u)
        old_urls = set()
        for p in self._iter_py(old_root) if old_root.is_dir() else []:
            try:
                old_urls |= set(re.findall(r"https?://[\w./\-?=&%:]+", p.read_text(encoding="utf-8", errors="ignore")))
            except Exception:
                pass
        out.append("")
        out.append(f"③ 危险调用：{len(danger_hits)} 处")
        for d in danger_hits[:30]:
            out.append("  " + d)
        if len(danger_hits) > 30:
            out.append(f"  …还有 {len(danger_hits)-30} 处")
        if not danger_hits:
            out.append("  没扫到")
        new_urls = sorted(urls - old_urls)
        out.append("")
        out.append(f"④ 新增外联地址：{len(new_urls)} 个")
        for u in new_urls[:20]:
            out.append("  " + u)
        if not new_urls:
            out.append("  没有新地址")

        # ⑤ 安装期钩子
        hooks = [str(p.relative_to(tmp)) for p in tmp.rglob("*")
                 if p.suffix in (".pth",) or p.name in ("setup.py", "pyproject.toml")]
        out.append("")
        out.append(f"⑤ 安装期钩子：{'、'.join(hooks) if hooks else '无'}")
        if hooks:
            out.append("  ⚠ .pth 会在 Python 启动时执行，重点看")

        # ⑥ 来源
        try:
            import urllib.request
            req = urllib.request.Request(_PYPI_JSON_URLS[0], headers={"User-Agent": "jm-downloader/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                d = json.loads(resp.read().decode("utf-8"))
            i = d.get("info", {})
            ts = [f.get("upload_time") for f in d.get("releases", {}).get(latest, []) if f.get("upload_time")]
            out.append("")
            out.append("⑥ 来源核对")
            out.append(f"  作者 {i.get('author')} / {i.get('author_email')}")
            out.append(f"  主页 {i.get('home_page') or i.get('project_url')}")
            out.append(f"  该版上传时间 {ts[0] if ts else '未知'}")
        except Exception as e:
            out.append("")
            out.append(f"⑥ 来源核对：取不到（{type(e).__name__}）")

        # 结论：有可疑项就顶在报告最前面
        bad = []
        if danger_hits:
            bad.append(f"{len(danger_hits)} 处危险调用")
        if new_urls:
            bad.append(f"{len(new_urls)} 个新外联地址")
        if hooks:
            bad.append(f"{len(hooks)} 个安装期钩子")
        verdict = ("⚠ 审出可疑项：" + "、".join(bad) + "。别急着装，先把上面看完。") if bad \
            else "✅ 没扫到可疑项，可以装。"
        out.insert(0, verdict)
        out.insert(1, "")

        out.append("")
        out.append("以上是代码审查，不是杀毒。.so 二进制里写了什么看不出来。")
        self._write_state(audit_bad=1 if bad else 0)
        return "\n".join(out)

    def _install_from_tmp(self, latest: str) -> tuple[bool, str]:
        """★唯一允许动 vendor 的入口★ 覆盖前再核一次版本，防中途被换。"""
        if not self._update_lock.acquire(blocking=False):
            return False, "已有更新任务在进行"
        tmp_dir = _PLUGIN_DIR / self._TMP_NAME
        try:
            if not (tmp_dir / "jmcomic").is_dir():
                return False, "临时目录里没有 jmcomic，先回 jm更新"
            tmp_ver = self._probe_version(tmp_dir)
            if tmp_ver != latest:
                return False, f"审查过的版本和现在这份对不上(期望{latest},实际{tmp_ver or '未知'})，已停手"
            if not self._probe_usable(tmp_dir):
                return False, "新版导入自检失败，已停手"
            shutil.rmtree(str(_VENDOR / "jmcomic"), ignore_errors=True)
            shutil.move(str(tmp_dir / "jmcomic"), str(_VENDOR / "jmcomic"))
            if (tmp_dir / "common").is_dir():
                shutil.rmtree(str(_VENDOR / "common"), ignore_errors=True)
                shutil.move(str(tmp_dir / "common"), str(_VENDOR / "common"))
            for pc in _VENDOR.rglob("__pycache__"):
                shutil.rmtree(pc, ignore_errors=True)
            importlib.invalidate_caches()
            self._write_state(installed=latest, reviewed=None, skipped=None)
            return True, f"vendor 已更新到 {latest}"
        except Exception as e:
            return False, str(e)
        finally:
            self._update_lock.release()

    # ---------------- 配置读取（面板改动全部生效） ----------------

    def _cfg(self, key: str, default=None):
        try:
            return self.config.get(key, default)
        except Exception:
            try:
                return self.config[key]
            except Exception:
                return default

    def _get_tmp_dir(self) -> Path:
        d = str(self._cfg("tmp_dir", "/AstrBot/data/jmcomic_tmp")).strip()
        p = Path(d)
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ---------------- 权限 ----------------

    def _check_access(self, event: AstrMessageEvent) -> bool:
        mode = str(self._cfg("access_mode", "admin")).strip().lower()
        uid = str(event.get_sender_id())
        if mode == "all":
            return True
        if mode == "whitelist":
            wl = [str(x) for x in (self._cfg("whitelist", []) or [])]
            admins = [str(x) for x in (self._cfg("admin_ids", []) or [])]
            return uid in wl or uid in admins
        admins = [str(x) for x in (self._cfg("admin_ids", []) or [])]
        return uid in admins

    @staticmethod
    def _deny_text() -> str:
        return "你没有使用权限哦，找管理员开权限吧。"

    # ---------------- 辅助 ----------------

    @staticmethod
    def _strip_cmd(text: str, cmd: str) -> str:
        t = text.strip()
        for prefix in (cmd, "/" + cmd):
            if t.startswith(prefix):
                return t[len(prefix):].strip()
        return t

    def _send(self, session: str, chain: list) -> None:
        try:
            task = asyncio.create_task(self._send_async(session, chain))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        except RuntimeError:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(self._send_async(session, chain))
            loop.close()

    async def _send_async(self, session: str, chain: list) -> None:
        try:
            await self.context.send_message(session, MessageChain(chain=chain))
        except Exception as e:
            logger.error(f"[jm下载姬] 发送失败: {e}")

    async def _notify(self, session: str, text: str) -> None:
        await self._send_async(session, [Plain(text)])

    # ---------------- 任务系统 ----------------

    def _new_tid(self) -> int:
        with self._tid_lock:
            tid = self._next_tid
            self._next_tid += 1
            return tid

    def _user_active_cnt(self, uid: str) -> int:
        return sum(
            1 for t in self._tasks.values()
            if t["user_id"] == uid and t["status"] in ("queued", "downloading", "converting", "sending")
        )

    def _global_active_cnt(self) -> int:
        return sum(
            1 for t in self._tasks.values()
            if t["status"] in ("downloading", "converting", "sending")
        )

    # ---------------- 命令：搜本 ----------------

    @filter.command("搜本")
    async def cmd_search(self, event: AstrMessageEvent):
        if not self._check_access(event):
            yield event.plain_result(self._deny_text())
            return
        note = self._pre_hook(event) or self._update_notice
        if note:
            self._update_notice = None
            yield event.plain_result("🔔 " + note)
        arg = self._strip_cmd(event.get_message_str(), "搜本")
        parts = arg.split()
        if not parts:
            yield event.plain_result("用法：搜本 <关键词> [页码]。比如：搜本 无望菜志")
            return
        keyword = parts[0]
        page = 1
        if len(parts) > 1 and parts[1].isdigit():
            page = max(1, int(parts[1]))
        if not self._curl_ok():
            yield event.plain_result(
                "私有依赖没装，插件现在干不了活。\n"
                "敲 jm依赖 看可选版本，再敲 jm依赖 <版本号> 下载审查，最后 jm依赖 装。")
            return
        try:
            result = await asyncio.to_thread(
                self._do_search, keyword, page
            )
        except Exception as e:
            logger.error(f"[jm下载姬] 搜索失败: {e}\n{traceback.format_exc()}")
            yield event.plain_result(f"搜索失败了：{e}")
            return
        if not result or not result[0]:
            yield event.plain_result("啥也没搜到，换个关键词试试。")
            return
        lines, total_page = result
        head = f"搜「{keyword}」第{page}/{total_page}页，共{len(lines)}条：\n"
        yield event.plain_result(head + "\n".join(lines) + "\n回复「下本 ID」就能下载")

    @staticmethod
    def _do_search(keyword: str, page: int):
        client = jmcomic.JmOption.default().new_jm_client()
        sp = client.search_site(keyword, page=page)
        lines = []
        for aid, title in sp.iter_id_title():
            lines.append(f"{aid}｜{title[:45]}")
        return lines, max(1, sp.page_count)

    # ---------------- 命令：排行 ----------------

    @filter.command("排行")
    async def cmd_rank(self, event: AstrMessageEvent):
        if not self._check_access(event):
            yield event.plain_result(self._deny_text())
            return
        note = self._pre_hook(event) or self._update_notice
        if note:
            self._update_notice = None
            yield event.plain_result("🔔 " + note)
        arg = self._strip_cmd(event.get_message_str(), "排行")
        kind = "周"
        if arg:
            kind = "月" if "月" in arg else ("日" if "日" in arg else "周")
        if not self._curl_ok():
            yield event.plain_result(
                "私有依赖没装，插件现在干不了活。\n"
                "敲 jm依赖 看可选版本，再敲 jm依赖 <版本号> 下载审查，最后 jm依赖 装。")
            return
        try:
            lines = await asyncio.to_thread(self._do_rank, kind)
        except Exception as e:
            logger.error(f"[jm下载姬] 排行失败: {e}")
            yield event.plain_result(f"榜单拉取失败：{e}")
            return
        yield event.plain_result(f"{kind}排行 Top10：\n" + "\n".join(lines) + "\n回复「下本 ID」就能下载")

    @staticmethod
    def _do_rank(kind: str):
        client = jmcomic.JmOption.default().new_jm_client()
        if kind == "月":
            sp = client.month_ranking(1)
        elif kind == "日":
            sp = client.day_ranking(1)
        else:
            sp = client.week_ranking(1)
        lines = []
        for i, (aid, title) in enumerate(sp.iter_id_title(), 1):
            lines.append(f"{i}. {aid}｜{title[:40]}")
            if i >= 10:
                break
        return lines

    # ---------------- 命令：下本 ----------------

    @filter.command("下本")
    async def cmd_download(self, event: AstrMessageEvent):
        if not self._check_access(event):
            yield event.plain_result(self._deny_text())
            return
        note = self._pre_hook(event) or self._update_notice
        if note:
            self._update_notice = None
            yield event.plain_result("🔔 " + note)
        arg = self._strip_cmd(event.get_message_str(), "下本")
        parts = arg.split()
        if not parts or not parts[0].isdigit():
            yield event.plain_result("用法：下本 <ID> [格式]。比如：下本 243484 长图")
            return
        album_id = parts[0]
        fmt = _FMT_ALIAS.get(parts[1], None) if len(parts) > 1 else None
        if fmt is None:
            fmt = _FMT_ALIAS.get(str(self._cfg("default_format", "longimg")).strip(), "longimg")
        if not self._curl_ok():
            yield event.plain_result(
                "私有依赖没装，插件现在干不了活。\n"
                "敲 jm依赖 看可选版本，再敲 jm依赖 <版本号> 下载审查，最后 jm依赖 装。")
            return
        uid = str(event.get_sender_id())
        session = event.unified_msg_origin
        limit = max(1, int(self._cfg("per_user_limit", 1)))
        if self._user_active_cnt(uid) >= limit:
            yield event.plain_result(f"你手头任务满了（同时最多{limit}个），等前面的完成再来吧。")
            return
        tid = self._new_tid()
        task_dir = self._get_tmp_dir() / str(tid)
        try:
            task_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            yield event.plain_result(f"中转目录创建失败：{e}")
            return
        self._tasks[tid] = {
            "tid": tid, "user_id": uid, "session": session,
            "album_id": album_id, "fmt": fmt, "status": "queued",
            "title": "", "dir": str(task_dir), "error": "",
            "started": time.time(),
        }
        task = asyncio.create_task(self._run_task(tid))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        yield event.plain_result(f"任务{tid}已开始，ID：{album_id}，格式：{fmt}。下好发你。")

    # ---------------- 命令：我的任务 / 取消 ----------------

    @filter.command("我的任务")
    async def cmd_my_tasks(self, event: AstrMessageEvent):
        if not self._check_access(event):
            yield event.plain_result(self._deny_text())
            return
        note = self._pre_hook(event) or self._update_notice
        if note:
            self._update_notice = None
            yield event.plain_result("🔔 " + note)
        uid = str(event.get_sender_id())
        mine = [t for t in self._tasks.values() if t["user_id"] == uid]
        if not mine:
            yield event.plain_result("你还没有任务哦。")
            return
        lines = [f"任务{t['tid']}｜{STATUS_TEXT.get(t['status'], t['status'])}｜{t['album_id']}" for t in mine]
        yield event.plain_result("\n".join(lines))

    @filter.command("取消")
    async def cmd_cancel(self, event: AstrMessageEvent):
        if not self._check_access(event):
            yield event.plain_result(self._deny_text())
            return
        note = self._pre_hook(event) or self._update_notice
        if note:
            self._update_notice = None
            yield event.plain_result("🔔 " + note)
        arg = self._strip_cmd(event.get_message_str(), "取消")
        if not arg.isdigit():
            yield event.plain_result("用法：取消 <任务号>")
            return
        tid = int(arg)
        t = self._tasks.get(tid)
        if not t:
            yield event.plain_result("没有这个任务号。")
            return
        if str(event.get_sender_id()) != t["user_id"]:
            yield event.plain_result("只能取消自己的任务哦。")
            return
        if t["status"] in ("done", "failed", "cancelled"):
            yield event.plain_result(f"任务{tid}状态是{STATUS_TEXT.get(t['status'])}，没法取消了。")
            return
        if t["status"] == "queued":
            t["status"] = "cancelled"
            self._cleanup_task(t)
            yield event.plain_result(f"任务{tid}已取消。")
        else:
            t["status"] = "cancelled"
            yield event.plain_result(f"任务{tid}正在跑，已标记取消，完成后不再发送。")

    # ---------------- 任务执行核心 ----------------

    async def _run_task(self, tid: int) -> None:
        t = self._tasks.get(tid)
        if not t:
            return
        try:
            # 每次开跑都读一次配置，面板修改并发数无需重载插件即可生效。
            while self._global_active_cnt() >= max(1, int(self._cfg("max_concurrent", 3))):
                if t["status"] == "cancelled":
                    return
                await asyncio.sleep(0.2)
            if t["status"] == "cancelled":
                return
            t["status"] = "downloading"
            try:
                ok = await asyncio.to_thread(self._download_album, t)
            except Exception as e:
                t["status"] = "failed"
                t["error"] = str(e)
                logger.error(f"[jm下载姬] 任务{tid}下载异常: {e}\n{traceback.format_exc()}")
                await self._notify(t["session"], f"任务{tid}下载出错了：{e}")
                return
            if not ok:
                t["status"] = "failed"
                await self._notify(t["session"], f"任务{tid}下载失败：{t['error']}")
                return
            if t["status"] == "cancelled":
                return
            t["status"] = "converting"
            try:
                deliver = await asyncio.to_thread(self._convert, t)
            except Exception as e:
                t["status"] = "failed"
                t["error"] = str(e)
                logger.error(f"[jm下载姬] 任务{tid}转换异常: {e}\n{traceback.format_exc()}")
                await self._notify(t["session"], f"任务{tid}转换出错了：{e}")
                return
            if t["status"] == "cancelled":
                return
            t["status"] = "sending"
            try:
                await self._deliver(t, deliver)
                if t["status"] == "cancelled":
                    await self._notify(t["session"], f"任务{tid}已取消，文件就不发啦。")
                else:
                    t["status"] = "done"
            except Exception as e:
                t["status"] = "failed"
                t["error"] = str(e)
                logger.error(f"[jm下载姬] 任务{tid}发送异常: {e}\n{traceback.format_exc()}")
                await self._notify(t["session"], f"任务{tid}发送出错了：{e}")
        finally:
            self._cleanup_task(t)

    def _download_album(self, t: dict) -> bool:
        album_id = t["album_id"]
        try:
            option = jmcomic.create_option_by_str(f"""
dir_rule:
  base_dir: {t['dir']}
  rule: Bd_Aid
download:
  image:
    suffix: .jpg
""")
            try:
                client = option.new_jm_client()
                detail = client.get_album_detail(album_id)
                t["title"] = getattr(detail, "title", album_id)
            except Exception as e:
                logger.warning(f"[jm下载姬] 获取标题失败，用ID代替: {e}")
                t["title"] = album_id
            jmcomic.download_album(album_id, option=option)
            return True
        except Exception as e:
            t["error"] = str(e)
            return False

    def _convert(self, t: dict) -> dict:
        imgs = self._collect_imgs(t)
        if not imgs:
            raise RuntimeError("下载的图片是空的")
        fmt = t["fmt"]
        if fmt == "zip":
            return self._make_zip(t, imgs)
        if fmt == "pdf":
            return self._make_pdf(t, imgs)
        return self._make_longimg(t, imgs)

    @staticmethod
    def _collect_imgs(t: dict) -> list[str]:
        root = Path(t["dir"])
        found = []
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.lower() in _IMG_EXTS:
                found.append(str(p))
        found.sort()
        return found

    def _safe_name(self, t: dict) -> str:
        # 只保留 ASCII 字母数字与 -_，其余全部替换为 _，避免 file:// 路径特殊字符导致发送失败
        title = (t.get("title") or t["album_id"]).strip()
        title = re.sub(r"[^A-Za-z0-9_-]+", "_", title).strip("_")
        return (title or t["album_id"])[:60]

    def _make_zip(self, t: dict, imgs: list[str]) -> dict:
        name = self._safe_name(t)
        zpath = str(Path(t["dir"]) / f"{name}.zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as zf:
            for img in imgs:
                zf.write(img, os.path.basename(img))
        return {"kind": "file", "path": zpath, "name": f"{name}.zip"}

    def _make_pdf(self, t: dict, imgs: list[str]) -> dict:
        from PIL import Image
        name = self._safe_name(t)
        pdf_path = str(Path(t["dir"]) / f"{name}.pdf")
        pils = []
        try:
            for img in imgs:
                pils.append(Image.open(img).convert("RGB"))
            pils[0].save(pdf_path, "PDF", save_all=True, append_images=pils[1:])
        finally:
            for p in pils:
                try:
                    p.close()
                except Exception:
                    pass
        return {"kind": "file", "path": pdf_path, "name": f"{name}.pdf"}

    def _make_longimg(self, t: dict, imgs: list[str]) -> dict:
        from PIL import Image
        name = self._safe_name(t)
        width = 800
        max_height = 20_000  # QQ 对超高图片兼容性不稳定，自动切片保证交付成功。
        pages: list[list[tuple[str, int]]] = []
        current: list[tuple[str, int]] = []
        current_height = 0
        for p in imgs:
            with Image.open(p) as source:
                h = max(1, int(source.height * width / source.width))
            if current and current_height + h > max_height:
                pages.append(current)
                current = []
                current_height = 0
            current.append((p, h))
            current_height += h
        if current:
            pages.append(current)

        paths = []
        for index, page in enumerate(pages, 1):
            height = sum(h for _, h in page)
            canvas = Image.new("RGB", (width, height), (255, 255, 255))
            y = 0
            for p, h in page:
                with Image.open(p) as source:
                    image = source.convert("RGB").resize((width, h))
                canvas.paste(image, (0, y))
                image.close()
                y += h
            suffix = f"_第{index}段" if len(pages) > 1 else ""
            out_path = str(Path(t["dir"]) / f"{name}{suffix}.jpg")
            canvas.save(out_path, "JPEG", quality=85)
            canvas.close()
            paths.append(out_path)
        return {"kind": "images", "paths": paths, "name": f"{name}.jpg"}

    # ---------------- 交付 ----------------

    async def _deliver(self, t: dict, deliver: dict) -> None:
        if t["status"] == "cancelled":
            return
        name = deliver["name"]
        if deliver["kind"] == "images":
            paths = deliver["paths"]
            title_display = t.get("title") or t["album_id"]
            first = [Plain(f"任务{t['tid']}完成：{title_display}，共{len(paths)}段。")]
            first.extend(Image(file=item) for item in paths[:4])
            await self._send_async(t["session"], first)
            for i in range(4, len(paths), 4):
                batch = [Image(file=item) for item in paths[i:i + 4]]
                await self._send_async(t["session"], batch)
            return
        path = deliver["path"]
        # napcat 容器可访问路径保障：确保路径在 /AstrBot/data 下（软链打通）
        real = os.path.realpath(path)
        if not real.startswith("/AstrBot/data"):
            fallback_dir = Path("/AstrBot/data/jmcomic_tmp/deliver")
            fallback_dir.mkdir(parents=True, exist_ok=True)
            dest = fallback_dir / name
            shutil.copy2(path, dest)
            path = str(dest)
        await self._send_async(t["session"], [Plain(f"任务{t['tid']}完成：{name}"), File(name=name, file=path)])

    # ---------------- 清理 ----------------

    def _cleanup_task(self, t: dict) -> None:
        try:
            d = t.get("dir")
            if d and os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
        except Exception as e:
            logger.error(f"[jm下载姬] 清理任务目录失败: {e}")

    # ---------------- 生命周期 ----------------

    async def terminate(self) -> None:
        """【2026-10-04 改】不再清 .deps。

        原来这里会 rmtree(.deps)，本意是「卸载即净」。但 AstrBot 在**重载插件**
        时也会调 terminate()，而插件已经改成「缺依赖只报缺、不自动装」，
        结果每重载一次就把 40M 依赖删掉一次，插件直接变不可用，得重新走
        jm依赖 流程才能恢复。

        真正卸载插件时，插件目录会被整个删掉，.deps 就在目录里，自然跟着走，
        根本不需要单独 rm。所以这里只留一句日志。
        """
        logger.info("[jm下载姬] 插件卸载/重载。.deps 保留不清，真要清请手工删或走 jm依赖 流程")
