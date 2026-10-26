"""米游社自动化 - AI 守护极速签到系统
核心机制：
1. 【动态 3 次自适应重试】：
   - WebView 渲染与状态判别：0.5s 快速探测，网络快时 0.6 秒秒过，网络慢时最多重试 3 次自适应缓冲。
   - 签到卡片核验：触控后动态轮询至多 3 次，特征出现立即收工，杜绝任何死板等待。
   - 频道与图标检索：优先高速缓存，未命中时 3 次弹性探测，兼顾极速与网络抖动容错。
2. 【首页坐标单次预热缓存】：
   - 首页单次 Dump 提取所有频道 Tab 坐标，消除每个游戏重复调用 uiautomator dump 的开销。
3. 【延迟按需调优】：
   - 延时完全贴合手机物理动画（0.3s~0.5s）。
4. 【按需破障与自愈守护】：
   - 遇到异常时自动唤醒 PopupHunter 深入排查并保存诊断报告。
"""
import os
import sys

# 自动自举虚拟环境：若当前未处于本项目的 .venv 环境中，无缝自动切换至 .venv 执行
_venv_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv")
_venv_py = (
    os.path.join(_venv_dir, "Scripts", "python.exe")
    if sys.platform == "win32"
    else os.path.join(_venv_dir, "bin", "python")
)
if os.path.exists(_venv_py) and os.path.abspath(sys.prefix) != os.path.abspath(_venv_dir):
    if sys.argv and sys.argv[0] != "-c":
        os.execv(_venv_py, [_venv_py] + sys.argv)

import datetime
import logging
import re
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple
import numpy as np
from PIL import Image

from adb_client import ADBClient
from popup_hunter import PopupHunter
from reporter import DiagnosticReporter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("MiyousheGuardian")


class MiyousheCheckin:
    # H5 签到卡片相对屏幕宽度的自适应比例模型 (rx, ry_w)
    # 移动端 Web 响应式栅格按宽度等比缩放，彻底杜绝 16:9 / 20:9 / 2K 屏的垂直拉伸错位
    CARD_RATIOS_W = {
        "原神": (0.176, 0.833),
        "崩坏：星穹铁道": (0.176, 0.967),
        "绝区零": (0.176, 0.833),
    }

    def __init__(self, adb: Optional[ADBClient] = None):
        self.adb = adb or ADBClient()
        self.width, self.height = self.adb.get_screen_size()
        self.hunter = PopupHunter(self.adb)
        self.reporter = DiagnosticReporter()
        self.tab_cache: Dict[str, Tuple[int, int]] = {}
        self.icon_cache: Dict[str, Tuple[int, int]] = {}
        self.tab_y = int(self.height * 0.088)  # 默认比例，首次扫描时自动校准为该设备真实的物理 Y 轴中心
        in_venv = os.path.exists(_venv_py) and os.path.abspath(sys.prefix) == os.path.abspath(_venv_dir)
        env_tag = "(.venv 虚拟环境)" if in_venv else "(系统全局环境)"
        logger.info(f"[INIT] 自动化驱动就绪 | 设备: {self.adb.device} | 分辨率: {self.width}x{self.height} | 环境: {env_tag}")
        self.wake_device()

    def is_hyperion_focused(self) -> bool:
        """检查米游社是否真正处于当前前台焦点"""
        try:
            focus_out = self.adb.run_cmd(["shell", "dumpsys", "window"])
            for line in focus_out.splitlines():
                if "mCurrentFocus=" in line and "Window{" in line:
                    return "com.mihoyo.hyperion" in line
        except Exception:
            pass
        return False

    def activate_hyperion(self):
        """将米游社强行拉至前台并等待主界面就绪"""
        logger.info("米游社未处于前台，正在拉至前台...")
        self.adb.run_cmd(["shell", "monkey", "-p", "com.mihoyo.hyperion", "-c", "android.intent.category.LAUNCHER", "1"])
        for _ in range(8):
            time.sleep(0.8)
            if self.is_hyperion_focused():
                logger.info("米游社主界面已进入前台就绪。")
                time.sleep(1.0)
                return True
        return False

    def wake_device(self):
        """确保设备屏幕亮起、解锁并置顶米游社"""
        try:
            res = self.adb.run_cmd(["shell", "dumpsys", "power"])
            win = self.adb.run_cmd(["shell", "dumpsys", "window"])
            if "mWakefulness=Awake" not in res or "isKeyguardShowing=true" in win:
                logger.info("设备处于休眠或锁屏状态，正在唤醒屏幕并解锁...")
                self.adb.run_cmd(["shell", "input", "keyevent", "224"])
                self.adb.run_cmd(["shell", "wm", "dismiss-keyguard"])
                time.sleep(0.4)

            if not self.is_hyperion_focused():
                self.activate_hyperion()
        except Exception as e:
            logger.warning(f"屏幕状态自检异常: {e}")

    def build_home_cache(self, root: Optional[ET.Element] = None):
        """一次性动态提取首页各频道 Tab 的真实 Bounds 中心点与 Tab 栏高度"""
        if root is None:
            root = self.adb.dump_ui()
        if root is None:
            return

        for node in root.iter("node"):
            text = node.attrib.get("text", "")
            res_id = node.attrib.get("resource-id", "")
            bounds = node.attrib.get("bounds", "")
            if "mHomeTabItemTvTitle" in res_id and text:
                m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
                if m:
                    x1, y1, x2, y2 = map(int, m.groups())
                    center_pos = ((x1 + x2) // 2, (y1 + y2) // 2)
                    self.tab_cache[text] = center_pos
                    self.tab_y = center_pos[1]  # 自动校准该设备真实的频道栏 Y 轴中心

        logger.info(f"[CACHE] 捕获首页频道坐标 (Y={self.tab_y}): {list(self.tab_cache.keys())}")

    def reset_tab_scroll(self):
        """平滑滑动将顶栏频道复位至最左侧起点（确保原神、崩铁、绝区零均处于初始视野）"""
        for _ in range(2):
            self.adb.swipe(
                int(self.width * 0.20), self.tab_y,
                int(self.width * 0.85), self.tab_y,
                duration_ms=250, delay=0.3
            )
        self.tab_cache.clear()

    def ensure_home(self):
        """确保停留在米游社首页并预热坐标缓存"""
        logger.info("确认处于首页并预热频道缓存...")
        self.wake_device()
        root = self.adb.dump_ui()

        # 仅在非首页时尝试点击首页
        home_pos = self.adb.find_node(target_text="首页", resource_id="mHomeTabRadioBtnTvName", root=root)
        if not home_pos:
            home_pos = self.adb.find_node(target_text="回顶部", resource_id="mHomeTabRadioBtnTvName", root=root)

        if home_pos:
            self.adb.tap(home_pos[0], home_pos[1], delay=0.4)
        else:
            self.adb.tap_ratio(0.10, 0.95, delay=0.4)

        # 确保顶栏复位至最左侧起点并预热缓存
        self.reset_tab_scroll()
        root = self.adb.dump_ui()
        self.build_home_cache(root)

    def switch_game_tab(self, game_name: str, max_attempts: int = 3) -> bool:
        """动态切换游戏频道 Tab（优先高速缓存，最多 3 次自适应重试）"""
        target_pos = self.tab_cache.get(game_name)
        if target_pos:
            logger.info(f"[TAB] 命中频道缓存: 【{game_name}】 -> 点击 {target_pos}")
            self.adb.tap(target_pos[0], target_pos[1], delay=0.4)
            return True

        # 若未缓存，启动动态查找（自适应至多 3 次）
        for attempt in range(max_attempts):
            logger.info(f"动态扫描频道: 【{game_name}】 (尝试 {attempt+1}/{max_attempts})")
            root = self.adb.dump_ui()
            pos = self.adb.find_node(target_text=game_name, resource_id="mHomeTabItemTvTitle", root=root)
            if pos:
                self.tab_cache[game_name] = pos
                self.adb.tap(pos[0], pos[1], delay=0.4)
                return True
            # 未找到时尝试清除可能的弹窗阻挡
            self.hunter.try_dismiss_popup()
            time.sleep(0.3)

        # 若仍未找到，横向滑动翻找（原神在最左侧，向右滑；其他可能在右侧，向左滑）
        logger.info(f"当前视野未见 【{game_name}】，滑动翻找...")
        if game_name == "原神":
            self.reset_tab_scroll()
        else:
            for _ in range(2):
                self.adb.swipe(
                    int(self.width * 0.75), self.tab_y,
                    int(self.width * 0.25), self.tab_y,
                    duration_ms=200, delay=0.4
                )
            self.tab_cache.clear()

        root = self.adb.dump_ui()
        self.build_home_cache(root)
        pos = self.adb.find_node(target_text=game_name, resource_id="mHomeTabItemTvTitle", root=root)
        if pos:
            self.tab_cache[game_name] = pos
            self.adb.tap(pos[0], pos[1], delay=0.4)
            return True

        logger.error(f"[ERROR] 3次尝试未能找到 【{game_name}】 频道")
        self.reporter.record_incident(
            game_name=game_name,
            phase="SWITCH_TAB",
            error_msg=f"未能定位到顶栏游戏频道 【{game_name}】",
            adb=self.adb
        )
        return False

    def enter_checkin_page(self, game_name: str, max_attempts: int = 3) -> bool:
        """动态自适应进入“签到福利”入口（优先设备专属缓存，未命中时动态扫描）"""
        target_pos = self.icon_cache.get(game_name)
        if target_pos:
            logger.info(f"[PORTAL] 命中入口缓存: {target_pos}，进入...")
            self.adb.tap(target_pos[0], target_pos[1], delay=1.5)
            return True

        # 动态自适应探测当前设备上的真实坐标（至多 3 次）
        for attempt in range(max_attempts):
            root = self.adb.dump_ui()
            pos = self.adb.find_node(target_text="签到福利", resource_id="navigatorItemTv", root=root)
            if not pos:
                pos = self.adb.find_node(target_text="签到福利", root=root)
            if pos:
                self.icon_cache[game_name] = pos
                logger.info(f"[PORTAL] 动态捕获【{game_name}】入口坐标: {pos}，进入...")
                self.adb.tap(pos[0], pos[1], delay=1.5)
                return True
            self.hunter.try_dismiss_popup()
            time.sleep(0.3)

        logger.error("[ERROR] 3次探测未在当前页面找到【签到福利】图标")
        self.reporter.record_incident(
            game_name=game_name,
            phase="ENTER_PORTAL",
            error_msg="未在金刚区发现【签到福利】图标",
            adb=self.adb
        )
        return False

    def get_card_pos(self, game_name: str, card_idx: int) -> Tuple[int, int]:
        """计算指定卡片索引 (0-indexed, 0=第1天, 1=第2天...) 的自适应中心坐标"""
        base_ry = 0.967 if game_name == "崩坏：星穹铁道" else 0.833
        col = card_idx % 4
        row = card_idx // 4
        cx = int(self.width * (0.176 + col * 0.216))
        cy = int(self.width * (base_ry + row * 0.286))
        return cx, cy

    def inspect_checkin_status(self, game_name: str, screencap_path: Optional[str] = None) -> Tuple[str, int, int]:
        """
        全月多天网格动态扫描：自适应定位今日待签卡片或核验已签状态
        返回 (status, target_x, target_y)
        status: 'NOT_SIGNED' (需要点击), 'ALREADY_SIGNED' (今日已签), 'UNKNOWN' (仍在渲染)
        """
        if screencap_path is None:
            screencap_path = os.path.join(tempfile.gettempdir(), "checkin_status.png")

        try:
            self.adb.screencap(screencap_path)
        except Exception as e:
            logger.warning(f"获取页面截图异常: {e}")
            return "UNKNOWN", 0, 0

        if not os.path.exists(screencap_path):
            return "UNKNOWN", 0, 0

        try:
            img = Image.open(screencap_path).convert("RGB")
            w, h = img.size
            half_box = int(w * 0.09)
            base_ry = 0.967 if game_name == "崩坏：星穹铁道" else 0.833

            # 采样顶部检查骨架屏/转场是否完成 (纯白无方差白屏、纯黑或超低方差转场)
            sample_area = np.array(img.crop((int(w * 0.1), int(w * base_ry), int(w * 0.9), int(w * base_ry) + 180)))
            if (np.mean(sample_area) > 250 and np.std(sample_area) < 10) or np.mean(sample_area) < 28 or np.std(sample_area) < 15:
                return "UNKNOWN", 0, 0

            # 顺次扫描视野内可见的卡片 (第1天至第8天)
            for idx in range(8):
                cx, cy = self.get_card_pos(game_name, idx)
                crop = img.crop((cx - half_box, cy - half_box, cx + half_box, cy + half_box))
                arr = np.array(crop)
                r, g, b = arr[:,:,0].astype(int), arr[:,:,1].astype(int), arr[:,:,2].astype(int)
                mean_val = np.mean(arr)

                if game_name == "原神":
                    green = np.sum((g > 120) & (g > r + 20) & (g > b + 40))
                    if green > 400:
                        continue  # 该天已盖对勾，顺延检查下一张

                    # 发现第一张未签卡片，检查是否今日已经累计签到完成
                    try:
                        missed_crop = img.crop((int(w * 0.60), int(w * 0.58), int(w * 0.85), int(w * 0.68)))
                        m_arr = np.array(missed_crop)
                        mr, mg, mb = m_arr[:,:,0].astype(int), m_arr[:,:,1].astype(int), m_arr[:,:,2].astype(int)
                        # 过滤橙棕色数字像素
                        orange_mask = (mr > 160) & (mg > 90) & (mg < 150) & (mb < 80)
                        if np.sum(orange_mask) > 100:
                            binary = np.ones_like(mr, dtype=np.uint8) * 255
                            binary[orange_mask] = 0
                            clean_missed_path = os.path.join(tempfile.gettempdir(), "clean_missed.png")
                            Image.fromarray(binary).save(clean_missed_path)
                            out = subprocess.run(
                                ["tesseract", clean_missed_path, "stdout", "--psm", "7", "-c", "tessedit_char_whitelist=0123456789"],
                                capture_output=True, text=True, timeout=2
                            )
                            missed_str = out.stdout.strip()
                            if missed_str.isdigit():
                                missed_days = int(missed_str)
                                today_day = datetime.datetime.now().day
                                if idx + missed_days >= today_day:
                                    return "ALREADY_SIGNED", cx, cy
                    except Exception:
                        pass

                    return "NOT_SIGNED", cx, cy

                elif game_name == "崩坏：星穹铁道":
                    stamp = np.sum((r > 190) & (g > 110) & (g < 185) & (b < 80))
                    if stamp > 1000:
                        continue  # 该天已盖“已领取”印章，顺延检查下一张
                    # 检查此卡片是否有今日活跃高亮金色光晕
                    glow = np.sum((r > 240) & (g > 190) & (b > 110) & (b < 190))
                    if glow > 5000:
                        return "NOT_SIGNED", cx, cy
                    else:
                        # 无印章且无光晕，说明今日任务已签过，此卡是明日锁定的卡片
                        return "ALREADY_SIGNED", cx, cy

                elif game_name == "绝区零":
                    # 亮橙色代表今日待签卡片 (#f39800)
                    orange = np.sum((r > 165) & (g > 80) & (b < 90) & (r - b > 50))
                    if orange > 1500:
                        return "NOT_SIGNED", cx, cy
                    # 检查是否为已签卡片 (暗色遮罩置灰)
                    if orange < 200 and mean_val < 125:
                        continue  # 该天已签，顺延检查下一张
                    # 既无橙色待签也非暗色已签，进入后续锁定卡片 -> 今日任务已完成
                    return "ALREADY_SIGNED", cx, cy

            # 默认保底
            first_cx, first_cy = self.get_card_pos(game_name, 0)
            return "ALREADY_SIGNED", first_cx, first_cy

        except Exception as e:
            logger.warning(f"视觉分析异常: {e}")
            return "UNKNOWN", 0, 0

    def wait_for_checkin_ready(self, game_name: str, max_attempts: int = 4, interval: float = 0.6) -> Tuple[str, int, int]:
        """
        动态自适应轮询：等待 WebView 渲染就绪并精确识别状态与今日卡片坐标
        返回 (status, target_x, target_y)
        """
        for attempt in range(max_attempts):
            status, tx, ty = self.inspect_checkin_status(game_name)
            if status in ("ALREADY_SIGNED", "NOT_SIGNED"):
                if attempt > 0:
                    logger.info(f"第 {attempt+1} 次自适应探测命中状态: {status}")
                return status, tx, ty
            logger.info(f"页面仍在加载渲染中 (探测 {attempt+1}/{max_attempts})，等待缓冲...")
            time.sleep(interval)
        return "UNKNOWN", 0, 0

    def wait_for_verification(self, game_name: str, max_attempts: int = 4, interval: float = 0.5) -> bool:
        """点击卡片后，动态轮询核验签到成功标记"""
        for _ in range(max_attempts):
            status, _, _ = self.inspect_checkin_status(game_name)
            if status == "ALREADY_SIGNED":
                return True
            time.sleep(interval)
        return False

    def execute_sign_in(self, game_name: str, force_click: bool = False) -> bool:
        """在 WebView 签到页面中执行动态自适应签到"""
        logger.info(f"[STATUS] 探测 【{game_name}】 签到状态...")
        status, target_x, target_y = self.wait_for_checkin_ready(game_name, max_attempts=4, interval=0.6)

        if status == "ALREADY_SIGNED" and not force_click:
            logger.info(f"[SKIP] 【{game_name}】 今日已完成签到，安全跳过。")
            return True

        if status == "ALREADY_SIGNED" and force_click:
            logger.info(f"[SIM] 模拟未签到测试，执行真实卡片点击交互...")

        if status == "UNKNOWN":
            logger.warning(f"[WARN] 【{game_name}】 多次探测未识别状态，尝试兜底点击。")
            target_x, target_y = self.get_card_pos(game_name, 0)
            self.reporter.record_incident(
                game_name=game_name,
                phase="VISUAL_CHECK",
                error_msg="多次探测未识别到签到卡片状态（可能网络加载慢或页面白屏）",
                adb=self.adb
            )

        if target_x == 0 or target_y == 0:
            target_x, target_y = self.get_card_pos(game_name, 0)

        # 点击今日签到卡片（自适应网格模型）
        logger.info(f"[TAP] 点击 【{game_name}】 今日卡片 -> ({target_x}, {target_y})")
        self.adb.swipe(target_x, target_y, target_x, target_y, duration_ms=120, delay=0.3)

        # 动态 3 次核验
        verified = self.wait_for_verification(game_name, max_attempts=3, interval=0.4)
        if verified:
            logger.info(f"[SUCCESS] 【{game_name}】 签到成功并核验通过。")
        else:
            logger.info(f"[DONE] 【{game_name}】 签到指令已发送完毕。")
        return True

    def back_to_home(self):
        """极速返回主页"""
        self.adb.keyevent(4, delay=0.4)

    def checkin_single_game(self, game_name: str, force_click: bool = False) -> bool:
        """执行单个游戏的完整签到生命周期"""
        logger.info(f"[START] 开始: 【{game_name}】")
        try:
            if not self.switch_game_tab(game_name):
                return False

            if not self.enter_checkin_page(game_name):
                return False

            success = self.execute_sign_in(game_name, force_click=force_click)
            self.back_to_home()
            return success
        except Exception as e:
            logger.error(f"[ERROR] 【{game_name}】 异常: {e}", exc_info=True)
            self.reporter.record_incident(
                game_name=game_name,
                phase="EXCEPTION",
                error_msg=f"未捕获异常: {str(e)}",
                adb=self.adb
            )
            self.adb.keyevent(4, delay=0.5)
            return False

    def checkin_all(self, games: Optional[List[str]] = None, force_click: bool = False) -> Dict[str, bool]:
        """批量执行所有指定游戏的签到任务并生成诊断报告"""
        if games is None:
            games = ["原神", "崩坏：星穹铁道", "绝区零"]

        start_time = time.time()
        self.ensure_home()

        results = {}
        for game in games:
            res = self.checkin_single_game(game, force_click=force_click)
            results[game] = res

        elapsed = time.time() - start_time
        report_path = self.reporter.generate_markdown_report(results, elapsed)

        # 若全部运行完全正常，自动清理 7 天前的历史报告与快照数据
        all_passed = all(results.values()) if results else False
        if all_passed:
            cleaned = self.reporter.cleanup_old_reports(days=7)
            if cleaned > 0:
                logger.info(f"[CLEANUP] 已自动清理 7 天前旧报告与快照共 {cleaned} 个文件。")

        logger.info("=" * 50)
        logger.info(f"[SUMMARY] 全部签到任务执行完毕，总耗时: {elapsed:.1f} 秒")
        for g, status in results.items():
            stat_str = "PASS" if status else "FAIL"
            logger.info(f"  * {g:<10}: {stat_str}")
        logger.info("=" * 50)
        logger.info(f"[REPORT] 诊断报告: file://{report_path}")

        return results


if __name__ == "__main__":
    import sys
    force = "--force-click" in sys.argv or "--simulate" in sys.argv
    bot = MiyousheCheckin()
    bot.checkin_all(force_click=force)
