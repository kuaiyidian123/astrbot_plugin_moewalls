"""
astrbot_plugin_moewalls —— moewalls.com 动态壁纸搜索 / 下载插件

指令：
    /搜壁纸 关键词    搜索动态壁纸，返回带序号的预览图
    /序号            下载对应壁纸的完整视频并发送到聊天（例如 /1）

实现要点（基于对目标站点的实际抓包分析）：
    1. 搜索   GET https://moewalls.com/?s=<英文关键词>，解析结果里的 article 块，
             取出标题、详情页链接与缩略图。
    2. 详情   GET 详情页 HTML，从下载按钮上取出加密 token：
             <a id="moe-download" data-url="...">
    3. 下载   https://go.moewalls.com/download.php?video=<token> 直接返回 mp4 视频。

站点用户协议禁止滥用与批量采集，本插件只做轻量搜索与单次下载。
"""

import os
import re
import time
import html
import asyncio
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

# ============= AstrBot API =============
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api.message_components import Video
from astrbot.api import logger

# ============= 第三方依赖 =============
import aiohttp

try:
    from yarl import URL as YarlURL
except ImportError:  # yarl 是 aiohttp 的依赖，正常不会缺失
    YarlURL = None

# ============= 内部模块 =============
from .draw import draw_search_result_image

BASE_URL = "https://moewalls.com"
DOWNLOAD_PREFIX = "https://go.moewalls.com/download.php?video="

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

DEFAULT_CONFIG = {
    "max_results": 12,
    "translate_keyword": True,
    "show_size": True,
    "translate_provider": "",
    "search_cache_expire_minutes": 10,
    "max_video_size_mb": 200,
    "request_timeout": 30,
    "download_timeout": 300,
    "preview_cols": 4,
    "temp_cleanup_seconds": 180,
}

# 搜索结果里每个 article 块
_ARTICLE_RE = re.compile(
    r'<article\b[^>]*class="[^"]*post-\d+[^"]*"[^>]*>(.*?)</article>', re.S | re.I
)
# 标题与详情页链接
_TITLE_RE = re.compile(
    r'<h[1-6][^>]*class="[^"]*entry-title[^"]*"[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    re.S | re.I,
)
# 缩略图
_THUMB_RE = re.compile(
    r'<img[^>]*src="(https://moewalls\.com/wp-content/uploads/[^"]+?\.(?:jpg|jpeg|png|webp))"',
    re.I,
)
# 详情页里下载按钮上的加密 token
_TOKEN_RE = re.compile(r'id="moe-download"[\s\S]{0,300}?data-url="([^"]+)"', re.I)

# 关键词翻译用的系统提示词
_TRANSLATE_SYSTEM_PROMPT = (
    "You are a translation engine for a live wallpaper website. "
    "Translate the user's keyword into a short English search phrase suitable for "
    "searching animated wallpapers. Output ONLY the English phrase, at most 4 words, "
    "without quotes, punctuation or explanation."
)

# 预览图标题批量翻译用的系统提示词
_TRANSLATE_TITLE_PROMPT = (
    "你是翻译引擎。请把下面每一条英文壁纸标题翻译成简体中文，逐条输出，"
    "格式为「序号. 中文标题」，序号必须与输入完全一致；"
    "不要添加解释、不要合并或拆分条目、不要遗漏任何一条。"
)


@register(
    "astrbot_plugin_moewalls",
    "kuaiyidian123",
    "moewalls.com 动态壁纸搜索插件：/搜壁纸 关键词 返回带序号的预览图，回复 /序号 下载对应视频",
    "1.0.0",
    "https://github.com/kuaiyidian123/astrbot_plugin_moewalls",
)
class MoewallsPlugin(Star):
    """moewalls.com 动态壁纸插件"""

    def __init__(self, context: Context, config: Optional[dict] = None):
        super().__init__(context)
        self.config = {**DEFAULT_CONFIG, **(dict(config) if config else {})}

        # 数据目录：优先使用框架接口，失败时回退到约定路径
        try:
            data_dir = str(StarTools.get_data_dir("astrbot_plugin_moewalls"))
        except Exception as e:
            logger.warning(f"获取框架数据目录失败，回退到默认路径: {e}")
            data_dir = os.path.join(
                os.getcwd(), "data", "plugin_data", "astrbot_plugin_moewalls"
            )
        self.data_dir = data_dir
        self.temp_dir = os.path.join(data_dir, "temp")
        os.makedirs(self.temp_dir, exist_ok=True)

        # 搜索结果缓存：key -> {"items": [...], "expire": ts, "keyword": str}
        self.search_cache: Dict[str, Dict[str, Any]] = {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()

        logger.info("动态壁纸搜索插件初始化完成")

    async def terminate(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        logger.info("动态壁纸搜索插件已卸载")

    # ==================== HTTP 基础 ====================

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            return self._session

    async def _fetch_text(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        """请求网页并返回 HTML 文本"""
        session = await self._get_session()
        timeout = aiohttp.ClientTimeout(total=int(self.config["request_timeout"]))
        try:
            async with session.get(
                url, headers=HEADERS, timeout=timeout, allow_redirects=True
            ) as resp:
                if resp.status != 200:
                    return None, f"请求失败（HTTP {resp.status}）"
                return await resp.text(errors="ignore"), None
        except asyncio.TimeoutError:
            return None, "请求超时，请稍后重试"
        except Exception as e:
            logger.error(f"请求 {url} 失败: {e}")
            return None, f"请求出错：{e}"

    async def _fetch_bytes(
        self, url: str, max_bytes: int, referer: str = BASE_URL + "/"
    ) -> Optional[bytes]:
        """请求二进制内容（用于缩略图），超过上限返回 None"""
        session = await self._get_session()
        timeout = aiohttp.ClientTimeout(total=int(self.config["request_timeout"]))
        headers = dict(HEADERS)
        headers["Referer"] = referer
        try:
            async with session.get(url, headers=headers, timeout=timeout) as resp:
                if resp.status != 200:
                    return None
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(32 * 1024):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        return None
                return bytes(buf)
        except Exception as e:
            logger.warning(f"下载缩略图失败 {url}: {e}")
            return None

    # ==================== 关键词翻译 ====================

    async def _resolve_provider(self):
        """取用于翻译的对话模型：先按配置的 ID，失败回退当前默认模型"""
        provider_id = str(self.config.get("translate_provider") or "").strip()
        if provider_id:
            try:
                prov = self.context.get_provider_by_id(provider_id)
            except Exception as e:
                logger.warning(f"按 ID 获取对话模型失败: {e}")
                prov = None
            if prov is not None:
                return prov
            logger.warning(f"未找到对话模型「{provider_id}」，回退到默认模型")
        try:
            return await self.context.get_using_provider_async()
        except Exception as e:
            logger.warning(f"获取默认对话模型失败: {e}")
            return None

    async def _translate_keyword(self, keyword: str) -> str:
        """把中文关键词翻译成英文；不需要翻译或翻译失败时原样返回"""
        if not self.config.get("translate_keyword", True):
            return keyword
        # 纯 ASCII 关键词（含英文）无需翻译
        if not re.search(r"[\u4e00-\u9fff]", keyword):
            return keyword
        prov = await self._resolve_provider()
        if prov is None:
            logger.warning("没有可用的对话模型，使用原始关键词搜索")
            return keyword
        try:
            resp = await prov.text_chat(
                prompt=keyword, system_prompt=_TRANSLATE_SYSTEM_PROMPT
            )
            out = str(getattr(resp, "completion_text", "") or "").strip()
            out = out.strip("\"'“”‘’。.，,、\n")
            if out:
                return out
        except Exception as e:
            logger.warning(f"关键词翻译失败，使用原始关键词: {e}")
        return keyword

    # ==================== 搜索 ====================

    @staticmethod
    def _parse_search(html_text: str, limit: int) -> List[Dict[str, str]]:
        """从搜索页 HTML 中解析出壁纸列表"""
        items: List[Dict[str, str]] = []
        for block in _ARTICLE_RE.findall(html_text):
            m = _TITLE_RE.search(block)
            if not m:
                continue
            url = m.group(1).strip()
            title = html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
            if not url or not title:
                continue
            mi = _THUMB_RE.search(block)
            if not mi:
                continue
            items.append({"title": title, "url": url, "thumb": mi.group(1)})
            if len(items) >= limit:
                break
        return items

    async def _search(self, keyword: str, limit: int):
        """搜索壁纸，返回 (items, error)"""
        url = BASE_URL + "/?s=" + urllib.parse.quote(keyword)
        html_text, err = await self._fetch_text(url)
        if err:
            return None, err
        items = self._parse_search(html_text, limit)
        if not items:
            return None, "没有搜索到相关壁纸，换个关键词试试"
        return items, None

    # ==================== 详情 / 下载 ====================

    async def _resolve_download_url(self, detail_url: str):
        """请求详情页并拼出真实下载地址"""
        html_text, err = await self._fetch_text(detail_url)
        if err:
            return None, err
        m = _TOKEN_RE.search(html_text)
        if not m:
            return None, "没有在该壁纸页面找到下载地址"
        token = html.unescape(m.group(1)).strip()
        if not token:
            return None, "下载地址解析失败"
        return DOWNLOAD_PREFIX + token, None

    @staticmethod
    def _build_request_url(url: str):
        """构造请求用 URL。

        data-url 是已经做过 URL 编码的 token（含 %2F、%3D 等），aiohttp 默认会用
        yarl 重新解析，可能把 %2F 解码成 '/'、把 %26 当成参数分隔符，导致下载地址
        失效并拿到一个错误页。这里用 encoded=True 让 yarl 原样保留编码。
        """
        if YarlURL is None:
            return url
        try:
            return YarlURL(url, encoded=True)
        except Exception:
            return url

    @staticmethod
    def _looks_like_video(path: str) -> bool:
        """通过文件头判断是否为 mp4/webm/mkv，避免把错误页当成视频发送"""
        try:
            with open(path, "rb") as f:
                head = f.read(16)
        except Exception:
            return False
        if len(head) < 12:
            return False
        if head[4:8] == b"ftyp":                # mp4 / mov
            return True
        if head[:4] == b"\x1a\x45\xdf\xa3":     # webm / mkv
            return True
        return False

    async def _probe_size(self, url: str) -> Optional[int]:
        """用 HEAD 预检视频体积，失败返回 None"""
        session = await self._get_session()
        timeout = aiohttp.ClientTimeout(total=int(self.config["request_timeout"]))
        headers = dict(HEADERS)
        headers["Referer"] = BASE_URL + "/"
        try:
            async with session.head(
                self._build_request_url(url),
                headers=headers,
                timeout=timeout,
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    return None
                return int(resp.content_length) if resp.content_length else None
        except Exception as e:
            logger.warning(f"预检视频体积失败: {e}")
            return None

    async def _fetch_size(self, detail_url: str) -> Optional[int]:
        """获取某个壁纸的视频体积（字节）：详情页取 token 后 HEAD 预检，失败返回 None"""
        download_url, err = await self._resolve_download_url(detail_url)
        if err:
            return None
        return await self._probe_size(download_url)

    async def _download_video(
        self, url: str, filename: str, progress: Optional[asyncio.Queue] = None
    ):
        """流式下载壁纸视频，返回 (path, error)。

        progress 非空时，会在下载过程中把进度文案放进队列，由调用方转发给用户，
        避免大文件下载期间长时间没有任何反馈。
        """
        max_bytes = int(self.config["max_video_size_mb"]) * 1024 * 1024
        session = await self._get_session()
        timeout = aiohttp.ClientTimeout(total=int(self.config["download_timeout"]))
        headers = dict(HEADERS)
        headers["Referer"] = BASE_URL + "/"
        path = os.path.join(self.temp_dir, filename)
        written = 0
        last_report = 0
        try:
            async with session.get(
                self._build_request_url(url),
                headers=headers,
                timeout=timeout,
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    return None, f"下载失败（HTTP {resp.status}）"
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "text/html" in ctype or "json" in ctype:
                    return None, "下载地址已失效（返回的不是视频），请重新搜索后再试"
                if resp.content_length and resp.content_length > max_bytes:
                    size_mb = resp.content_length / 1048576
                    return None, (
                        f"视频体积 {size_mb:.1f}MB 超过上限"
                        f"（{self.config['max_video_size_mb']}MB），已取消下载"
                    )
                total = int(resp.content_length) if resp.content_length else 0
                # 有总大小时每 20% 报一次，否则每 8MB 报一次
                step = total // 5 if total else 8 * 1024 * 1024
                with open(path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(128 * 1024):
                        written += len(chunk)
                        if written > max_bytes:
                            self._remove_quietly(path)
                            return None, "视频体积超过上限，已取消下载"
                        f.write(chunk)
                        if progress is not None and written - last_report >= step:
                            last_report = written
                            if total:
                                await progress.put(
                                    f"⬇️ 下载中 {int(written * 100 / total)}%"
                                    f"（{written / 1048576:.1f}/{total / 1048576:.1f} MB）"
                                )
                            else:
                                await progress.put(f"⬇️ 下载中 {written / 1048576:.1f} MB...")
        except asyncio.TimeoutError:
            self._remove_quietly(path)
            logger.warning(f"下载壁纸视频超时（已下载 {written / 1048576:.1f}MB）")
            return None, "下载超时，请稍后重试（可在插件配置里调大「视频下载超时」）"
        except Exception as e:
            logger.error(f"下载视频失败: {e}")
            self._remove_quietly(path)
            return None, f"下载出错：{e}"
        finally:
            if progress is not None:
                try:
                    await progress.put(None)
                except Exception:
                    pass
        if written == 0:
            self._remove_quietly(path)
            return None, "下载到的文件为空"
        if not self._looks_like_video(path):
            self._remove_quietly(path)
            return None, "下载到的内容不是有效视频，请重新搜索后再试"
        logger.info(f"壁纸视频下载完成: {path}（{written / 1048576:.1f}MB）")
        return path, None

    # ==================== 工具 ====================

    @staticmethod
    def _remove_quietly(path: str):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception as e:
            logger.warning(f"删除临时文件失败 {path}: {e}")

    async def _cleanup_later(self, path: str, delay: int):
        """清理临时文件；delay <= 0 表示立即删除"""
        if delay > 0:
            await asyncio.sleep(delay)
        self._remove_quietly(path)

    def _purge_temp_dir(self):
        """启动时清空临时目录，避免上次运行残留的文件继续占用磁盘"""
        try:
            for name in os.listdir(self.temp_dir):
                path = os.path.join(self.temp_dir, name)
                if os.path.isfile(path):
                    self._remove_quietly(path)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"清理临时目录失败: {e}")

    @staticmethod
    def _cache_key(event: AstrMessageEvent) -> str:
        try:
            uid = event.get_sender_id()
        except Exception:
            uid = ""
        return f"{event.unified_msg_origin}:{uid}"

    @staticmethod
    def _strip_command(message_str: str, command: str) -> str:
        """去掉消息开头的命令名，取出参数部分"""
        text = (message_str or "").strip()
        if text.startswith("/"):
            text = text[1:]
        if text.startswith(command):
            text = text[len(command):]
        return text.strip()

    def _prune_cache(self):
        """清理过期缓存"""
        now = time.time()
        for key in [k for k, v in self.search_cache.items() if v["expire"] < now]:
            self.search_cache.pop(key, None)

    # ==================== 指令 ====================

    @filter.command("搜壁纸")
    async def cmd_search(self, event: AstrMessageEvent):
        """搜索动态壁纸"""
        keyword = self._strip_command(event.message_str, "搜壁纸")
        if not keyword:
            yield event.plain_result(
                "用法：/搜壁纸 关键词\n例如：/搜壁纸 火影忍者"
            )
            return

        yield event.plain_result(f"🔍 正在搜索「{keyword}」并获取壁纸信息，请稍候...")

        search_keyword = await self._translate_keyword(keyword)
        limit = int(self.config["max_results"])
        items, err = await self._search(search_keyword, limit)
        if err:
            yield event.plain_result(f"😔 {err}")
            return

        # 缓存搜索结果（按会话 + 用户隔离）
        self._prune_cache()
        self.search_cache[self._cache_key(event)] = {
            "items": items,
            "expire": time.time() + int(self.config["search_cache_expire_minutes"]) * 60,
            "keyword": keyword,
        }

        # 并发：批量翻译标题 + 下载缩略图 +（可选）探测每个壁纸的视频体积
        gather_list = [
            self._translate_titles([it["title"] for it in items]),
            asyncio.gather(
                *[self._fetch_bytes(it["thumb"], 4 * 1024 * 1024) for it in items]
            ),
        ]
        if self.config.get("show_size", True):
            gather_list.append(
                asyncio.gather(*[self._fetch_size(it["url"]) for it in items])
            )
        results = await asyncio.gather(*gather_list)
        cn_titles = results[0]
        thumbs = results[1]
        sizes = results[2] if len(results) > 2 else []

        rendered = []
        for i, data in enumerate(thumbs):
            if data:
                title = cn_titles[i] if i < len(cn_titles) else items[i]["title"]
                rendered.append(
                    {
                        "title": title,
                        "thumb": data,
                        "size": sizes[i] if i < len(sizes) else None,
                    }
                )

        if not rendered:
            yield event.plain_result("😔 缩略图下载失败，请稍后重试")
            return

        out_path = os.path.join(self.temp_dir, f"moewalls_search_{int(time.time())}.jpg")
        try:
            image_path = await asyncio.to_thread(
                draw_search_result_image,
                rendered,
                out_path,
                int(self.config["preview_cols"]),
                1,
            )
        except Exception as e:
            logger.error(f"生成预览图失败: {e}")
            image_path = None
        if not image_path:
            yield event.plain_result("😔 预览图生成失败，请稍后重试")
            return

        tip = f"共找到 {len(items)} 个结果，回复 /序号（1-{len(items)}）下载对应动态壁纸"
        if search_keyword != keyword:
            tip = f"（已翻译为「{search_keyword}」搜索）\n" + tip
        yield event.image_result(image_path)
        yield event.plain_result(tip)
        asyncio.create_task(self._cleanup_later(image_path, 120))

    @filter.command("壁纸帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """查看用法"""
        yield event.plain_result(
            "🖼️ 动态壁纸插件\n"
            "用法：/搜壁纸 关键词\n"
            "例如：/搜壁纸 火影忍者\n"
            "搜索后会返回带序号的壁纸预览图，回复 /序号 即可下载对应动态壁纸视频\n"
            "例如：/1 下载第 1 张壁纸"
        )

    @filter.regex(r"^/?(\d{1,3})$")
    async def cmd_select(self, event: AstrMessageEvent):
        """按序号下载壁纸"""
        m = re.match(r"^/?(\d{1,3})$", (event.message_str or "").strip())
        if not m:
            return
        index = int(m.group(1))

        self._prune_cache()
        entry = self.search_cache.get(self._cache_key(event))
        if not entry:
            yield event.plain_result("请先发送 /搜壁纸 关键词 进行搜索")
            return

        items = entry["items"]
        if index < 1 or index > len(items):
            yield event.plain_result(f"序号超出范围，请输入 1-{len(items)}")
            return

        item = items[index - 1]
        yield event.plain_result(f"🔗 正在获取「{item['title']}」的下载地址...")

        download_url, err = await self._resolve_download_url(item["url"])
        if err:
            yield event.plain_result(f"❌ {err}")
            return

        # 先预检体积：让用户对等待时间有预期，也提前拦住超大文件
        max_bytes = int(self.config["max_video_size_mb"]) * 1024 * 1024
        size = await self._probe_size(download_url)
        if size and size > max_bytes:
            yield event.plain_result(
                f"❌ 该壁纸约 {size / 1048576:.1f}MB，超过上限 "
                f"{self.config['max_video_size_mb']}MB，已取消下载"
            )
            return
        if size:
            yield event.plain_result(
                f"⬇️ 正在下载「{item['title']}」（约 {size / 1048576:.1f}MB），请稍候..."
            )
        else:
            yield event.plain_result(f"⬇️ 正在下载「{item['title']}」，请稍候...")

        filename = f"moewalls_{int(time.time())}_{index}.mp4"
        progress: asyncio.Queue = asyncio.Queue()
        task = asyncio.create_task(
            self._download_video(download_url, filename, progress)
        )
        # 边下载边把进度转发给用户，避免大文件下载期间「没反应」
        while True:
            try:
                tip = await asyncio.wait_for(progress.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if task.done():
                    break
                continue
            if tip is None:
                break
            yield event.plain_result(tip)
        try:
            path, err = await task
        except Exception as e:  # 理论上 _download_video 内部已兜底
            logger.error(f"下载任务异常: {e}")
            path, err = None, f"下载出错：{e}"
        if err:
            yield event.plain_result(f"❌ {err}")
            return

        yield event.plain_result(
            f"✅ 下载完成（{os.path.getsize(path) / 1048576:.1f}MB），正在发送视频..."
        )
        delay = int(self.config["temp_cleanup_seconds"])
        try:
            yield event.chain_result([Video(file=path)])
        finally:
            # 发送完成后清理；默认 0 = 立即删除，不在本地保留
            if delay <= 0:
                self._remove_quietly(path)
            else:
                asyncio.create_task(self._cleanup_later(path, delay))
