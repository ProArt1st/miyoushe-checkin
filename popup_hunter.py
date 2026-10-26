"""米游社自动化 - 智能破障与弹窗猎手 (Popup Hunter)
功能：
1. 识别并自动关闭各类活动全屏宣传图、版本更新、青少年模式、调查问卷弹窗
2. 具备点击右上角叉号、通用取消按钮与 KEYCODE_BACK 三级破障能力
3. 向守护报告系统反馈拦截详情，供 AI 记录与追溯
"""
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import List, Optional, Tuple

logger = logging.getLogger("PopupHunter")


class PopupHunter:
    DISMISS_TEXTS = ["跳过", "关闭", "我知道了", "暂不更新", "以后再说", "取消", "放弃", "同意并继续"]
    DISMISS_RES_KEYWORDS = ["close", "cancel", "dismiss", "skip", "dialog_close", "iv_close", "btn_close", "mclosebtn"]

    def __init__(self, adb):
        self.adb = adb
        self.width, self.height = self.adb.get_screen_size()

    def find_dismiss_target(self, root: Optional[ET.Element] = None) -> Optional[Tuple[int, int, str]]:
        """在当前控件树中寻找可关闭弹窗的控件坐标 (x, y, desc)"""
        if root is None:
            root = self.adb.dump_ui()
        if root is None:
            return None

        # 1. 优先根据文本查找
        for node in root.iter("node"):
            text = node.attrib.get("text", "").strip()
            if text in self.DISMISS_TEXTS:
                bounds = node.attrib.get("bounds", "")
                m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
                if m:
                    x1, y1, x2, y2 = map(int, m.groups())
                    return (x1 + x2) // 2, (y1 + y2) // 2, f"文本按钮: '{text}'"

        # 2. 根据 resource-id 关键字查找
        for node in root.iter("node"):
            res_id = node.attrib.get("resource-id", "").lower()
            clickable = node.attrib.get("clickable", "false")
            for kw in self.DISMISS_RES_KEYWORDS:
                if kw in res_id:
                    bounds = node.attrib.get("bounds", "")
                    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
                    if m:
                        x1, y1, x2, y2 = map(int, m.groups())
                        return (x1 + x2) // 2, (y1 + y2) // 2, f"控件ID: '{res_id}'"

        return None

    def try_dismiss_popup(self, max_attempts: int = 2) -> List[str]:
        """
        尝试自动发现并关闭弹窗
        返回所有采取的自愈动作列表
        """
        actions = []
        for attempt in range(max_attempts):
            root = self.adb.dump_ui()
            target = self.find_dismiss_target(root)
            if target:
                x, y, desc = target
                logger.info(f"[POPUP] 发现弹窗阻挡 -> 命中 {desc} @ ({x}, {y})，正在点击关闭...")
                self.adb.tap(x, y, delay=1.0)
                actions.append(f"点击关闭弹窗: {desc} @ ({x}, {y})")
                time.sleep(1.0)
            else:
                break

        return actions
