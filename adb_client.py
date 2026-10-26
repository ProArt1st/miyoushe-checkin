"""ADB 操作封装模块
提供点击、滑动、按键、设备分辨率自适应、UI 控件动态查找与截屏功能。
"""
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from typing import Optional, Tuple


class ADBClient:
    def __init__(self, device: Optional[str] = None):
        self.device = device or self._detect_single_device()
        self._width: Optional[int] = None
        self._height: Optional[int] = None

    def _detect_single_device(self) -> Optional[str]:
        """自动检测当前连接的 ADB 设备（若只有一个则自动使用）"""
        try:
            res = subprocess.run(["adb", "devices"], stdout=subprocess.PIPE, text=True, timeout=5)
            lines = [l.strip().split() for l in res.stdout.strip().split("\n")[1:] if l.strip()]
            devices = [l[0] for l in lines if len(l) >= 2 and l[1] == "device"]
            if len(devices) == 1:
                return devices[0]
            return None
        except Exception:
            return None

    def _base_cmd(self) -> list:
        if self.device:
            return ["adb", "-s", self.device]
        return ["adb"]

    def run_cmd(self, args: list, timeout: int = 15) -> str:
        """运行 adb 命令并返回文本输出"""
        cmd = self._base_cmd() + args
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        if res.returncode != 0:
            raise RuntimeError(f"ADB 命令执行失败: {' '.join(cmd)}\n错误: {res.stderr}")
        return res.stdout

    def get_screen_size(self) -> Tuple[int, int]:
        """获取当前设备的实际显示分辨率 (Width, Height)，自适应 Override 和 Physical"""
        if self._width and self._height:
            return self._width, self._height

        out = self.run_cmd(["shell", "wm", "size"])
        # 优先读取 Override size（用户或系统设置的生效分辨率）
        override = re.search(r"Override size:\s*(\d+)x(\d+)", out)
        if override:
            self._width, self._height = int(override.group(1)), int(override.group(2))
        else:
            phys = re.search(r"Physical size:\s*(\d+)x(\d+)", out)
            if phys:
                self._width, self._height = int(phys.group(1)), int(phys.group(2))
            else:
                self._width, self._height = 720, 1280  # 保底默认

        return self._width, self._height

    def tap(self, x: int, y: int, delay: float = 0.5):
        """模拟点击屏幕物理像素坐标"""
        self.run_cmd(["shell", "input", "tap", str(x), str(y)])
        if delay > 0:
            time.sleep(delay)

    def tap_ratio(self, rx: float, ry: float, delay: float = 0.5):
        """按屏幕比例点击 (0.0 ~ 1.0)，自动换算为当前设备分辨率下的像素坐标"""
        w, h = self.get_screen_size()
        x = int(w * rx)
        y = int(h * ry)
        self.tap(x, y, delay=delay)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 200, delay: float = 0.5):
        """模拟滑动"""
        self.run_cmd(["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms)])
        if delay > 0:
            time.sleep(delay)

    def swipe_ratio(self, rx1: float, ry1: float, rx2: float, ry2: float, duration_ms: int = 200, delay: float = 0.5):
        """按屏幕比例滑动 (0.0 ~ 1.0)"""
        w, h = self.get_screen_size()
        self.swipe(int(w * rx1), int(h * ry1), int(w * rx2), int(h * ry2), duration_ms=duration_ms, delay=delay)

    def keyevent(self, keycode: int, delay: float = 0.5):
        """模拟按键（例如 4 为返回键 KEYCODE_BACK）"""
        self.run_cmd(["shell", "input", "keyevent", str(keycode)])
        if delay > 0:
            time.sleep(delay)

    def screencap(self, save_path: str):
        """截图保存到本地路径"""
        cmd = self._base_cmd() + ["exec-out", "screencap", "-p"]
        with open(save_path, "wb") as f:
            res = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE)
            if res.returncode != 0:
                raise RuntimeError(f"截图失败: {res.stderr.decode('utf-8', errors='ignore')}")

    def dump_ui(self) -> Optional[ET.Element]:
        """抓取当前屏幕 UI 控件树 XML"""
        try:
            self.run_cmd(["shell", "uiautomator", "dump", "--compressed", "/data/local/tmp/dump.xml"], timeout=10)
            xml_text = self.run_cmd(["shell", "cat", "/data/local/tmp/dump.xml"], timeout=10)
            return ET.fromstring(xml_text)
        except Exception:
            return None

    def find_node_rect(self, target_text: Optional[str] = None, resource_id: Optional[str] = None, root: Optional[ET.Element] = None) -> Optional[Tuple[int, int, int, int]]:
        """在 UI 控件树中查找控件并返回矩形区域 (x1, y1, x2, y2)"""
        if root is None:
            root = self.dump_ui()
        if root is None:
            return None

        for node in root.iter("node"):
            matched = True
            if target_text and target_text not in node.attrib.get("text", ""):
                matched = False
            if resource_id and resource_id not in node.attrib.get("resource-id", ""):
                matched = False

            if matched:
                bounds = node.attrib.get("bounds", "")
                m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
                if m:
                    return tuple(map(int, m.groups()))  # x1, y1, x2, y2
        return None

    def find_node(self, target_text: Optional[str] = None, resource_id: Optional[str] = None, root: Optional[ET.Element] = None) -> Optional[Tuple[int, int]]:
        """在 UI 控件树中查找控件并返回中心坐标 (center_x, center_y)"""
        rect = self.find_node_rect(target_text=target_text, resource_id=resource_id, root=root)
        if rect:
            x1, y1, x2, y2 = rect
            return (x1 + x2) // 2, (y1 + y2) // 2
        return None

    def click_text(self, target_text: str, delay: float = 1.0) -> bool:
        """根据文本动态查找控件并点击中心点"""
        pos = self.find_node(target_text=target_text)
        if pos:
            self.tap(pos[0], pos[1], delay=delay)
            return True
        return False
