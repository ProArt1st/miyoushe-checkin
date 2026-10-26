"""米游社自动化 - AI 守护诊断报告生成器 (Diagnostic Reporter)
功能：
1. 现场冻结与快照归档（截屏 + UI 树 XML + 硬件状态）
2. 根因智能诊断（自动分析弹窗阻挡、改版缺失、风控拦截、网络异常）
3. 生成规范的 Markdown 诊断报告与终端高亮汇总
"""
import datetime
import os
import re
import time
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple


class DiagnosticReporter:
    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
        self.reports_dir = os.path.join(self.base_dir, "reports")
        self.snapshots_dir = os.path.join(self.reports_dir, "snapshots")
        os.makedirs(self.snapshots_dir, exist_ok=True)
        self.session_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.incidents: List[Dict] = []
        self.execution_summary: Dict = {}

    def get_device_diagnostics(self, adb) -> Dict[str, str]:
        """采集设备底层诊断数据"""
        diag = {
            "battery": "未知",
            "wakefulness": "未知",
            "focused_window": "未知",
            "screen_size": f"{adb.width} × {adb.height}" if hasattr(adb, "width") else "未知"
        }
        try:
            bat_out = adb.run_cmd(["shell", "dumpsys", "battery"])
            for line in bat_out.splitlines():
                if "level:" in line.lower():
                    diag["battery"] = line.split(":")[-1].strip() + "%"
                    break

            wake_out = adb.run_cmd(["shell", "dumpsys", "power"])
            for line in wake_out.splitlines():
                if "mWakefulness=" in line:
                    diag["wakefulness"] = line.split("=")[-1].strip()
                    break

            win_out = adb.run_cmd(["shell", "dumpsys", "window"])
            for line in win_out.splitlines():
                if "mCurrentFocus=" in line and "Window{" in line:
                    diag["focused_window"] = line.split("Window{")[-1].split("}")[0].strip()
                    break
        except Exception as e:
            diag["error"] = str(e)

        return diag

    def analyze_ui_for_anomalies(self, root: Optional[ET.Element], dev_diag: Optional[Dict] = None) -> Dict[str, any]:
        """分析设备状态与 UI 树是否存在锁屏、弹窗、风控或异常视图"""
        dev_diag = dev_diag or {}
        wake = dev_diag.get("wakefulness", "")
        focus = dev_diag.get("focused_window", "")

        # 检查是否处于锁屏或休眠
        if wake == "Asleep" or "Keyguard" in focus or "NotificationShade" in focus:
            return {
                "has_anomaly": True,
                "type": "DEVICE_LOCKED_OR_ASLEEP",
                "detail": f"设备处于休眠或锁屏状态 (屏幕: {wake}, 当前窗口: {focus})，导致米游社未在前台显示",
                "suggestions": [
                    "守护者已配置 'wm dismiss-keyguard' 自动破锁",
                    "建议在手机【开发者选项】中开启【充电时屏幕不休眠】"
                ]
            }

        # 检查是否应用被切出前台
        if focus and "com.mihoyo.hyperion" not in focus:
            return {
                "has_anomaly": True,
                "type": "APP_NOT_IN_FOREGROUND",
                "detail": f"米游社未处于前台焦点，当前前台应用为: {focus}",
                "suggestions": [
                    "守护者已配置前台自检与自动拉起 (monkey -p com.mihoyo.hyperion ...)",
                    "请避免在自动化执行期间手动切换到其他 App"
                ]
            }

        if root is None:
            return {
                "has_anomaly": True,
                "type": "UI_DUMP_FAILED",
                "detail": "无法获取当前画面的 UI 控件树（可能处于高帧率动效中或设备响应超时）",
                "suggestions": ["已自动配置关闭信息流视频播放，重试即可恢复。"]
            }

        # 检查是否有风控/验证码关键词
        security_keywords = ["安全验证", "拖动滑块", "点击倒立", "异常访问", "验证码"]
        popup_keywords = ["版本更新", "更新提示", "活动说明", "温馨提示", "青少年模式", "用户协议", "隐私政策"]
        dismiss_keywords = ["跳过", "我知道了", "暂不更新", "以后再说", "取消", "关闭"]

        found_security = []
        found_popups = []
        found_dismissers = []

        for node in root.iter():
            text = node.attrib.get("text", "")
            desc = node.attrib.get("content-desc", "")
            res_id = node.attrib.get("resource-id", "")
            combined = f"{text} {desc} {res_id}"

            for kw in security_keywords:
                if kw in combined and kw not in found_security:
                    found_security.append(kw)

            for kw in popup_keywords:
                if kw in combined and kw not in found_popups:
                    found_popups.append(kw)

            for kw in dismiss_keywords:
                if kw in combined and kw not in found_dismissers:
                    bounds = node.attrib.get("bounds", "")
                    found_dismissers.append(f"{kw} @ {bounds}")

        if found_security:
            return {
                "has_anomaly": True,
                "type": "SECURITY_VERIFICATION",
                "detail": f"检测到触发安全风控/滑块验证: {', '.join(found_security)}",
                "suggestions": ["米游社触发了人机验证，请人工在手机上滑动完成，或暂停今日自动化以防黑号。"]
            }

        if found_popups:
            return {
                "has_anomaly": True,
                "type": "POPUP_OVERLAY",
                "detail": f"检测到全屏/模态弹窗遮挡界面: {', '.join(found_popups)}",
                "dismissers": found_dismissers,
                "suggestions": [
                    f"发现可关闭按钮: {', '.join(found_dismissers) if found_dismissers else '未发现直接文字按钮，尝试按 Back 键或定位右上角 × 叉号'}",
                    "AI 守护已尝试自动关闭弹窗。"
                ]
            }

        return {
            "has_anomaly": False,
            "type": "NORMAL",
            "detail": "UI 树未发现明显遮罩或安全拦截，可能是控件选择器或坐标漂移导致。"
        }

    def record_incident(
        self,
        game_name: str,
        phase: str,
        error_msg: str,
        adb,
        root: Optional[ET.Element] = None,
        recovery_actions: Optional[List[str]] = None
    ) -> Dict:
        """记录一起故障现场并生成快照切片"""
        timestamp_str = datetime.datetime.now().strftime("%H%M%S")
        prefix = f"{self.session_id}_{game_name}_{phase}_{timestamp_str}"
        img_name = f"{prefix}.png"
        xml_name = f"{prefix}.xml"

        img_path = os.path.join(self.snapshots_dir, img_name)
        xml_path = os.path.join(self.snapshots_dir, xml_name)

        # 1. 保存现场截图
        try:
            adb.screencap(img_path)
        except Exception:
            img_path = "截图失败"

        # 2. 保存 UI 树 XML
        if root is not None:
            try:
                tree = ET.ElementTree(root)
                tree.write(xml_path, encoding="utf-8", xml_declaration=True)
            except Exception:
                xml_path = "XML保存失败"
        else:
            xml_path = "无可用UI树"

        # 3. 分析设备与异常状态
        dev_diag = self.get_device_diagnostics(adb)
        ui_analysis = self.analyze_ui_for_anomalies(root, dev_diag=dev_diag)

        incident = {
            "game_name": game_name,
            "phase": phase,
            "error_msg": error_msg,
            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "img_path": img_path,
            "xml_path": xml_path,
            "device_diag": dev_diag,
            "ui_analysis": ui_analysis,
            "recovery_actions": recovery_actions or []
        }
        self.incidents.append(incident)
        return incident

    def generate_markdown_report(self, results: Dict[str, bool], total_elapsed: float) -> str:
        """生成格式规范的 Markdown 诊断报告文件"""
        report_filename = f"report_{self.session_id}.md"
        report_path = os.path.join(self.reports_dir, report_filename)

        all_passed = all(results.values()) if results else False
        status_text = "[PASS] 全部成功" if all_passed else "[FAIL] 存在异常"

        lines = [
            f"# 米游社签到 - 执行与诊断报告",
            f"",
            f"- 批次: `{self.session_id}`",
            f"- 时间: `{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`",
            f"- 耗时: `{total_elapsed:.1f}s`",
            f"- 状态: {status_text}",
            f"",
            f"---",
            f"",
            f"## 任务状态汇总",
            f"",
            f"| 游戏名称 | 执行结果 | 异常数 | 状态说明 |",
            f"| :--- | :--- | :--- | :--- |"
        ]

        for game, passed in results.items():
            inc_count = len([inc for inc in self.incidents if inc["game_name"] == game])
            stat_text = "PASS" if passed else "FAIL"
            desc_text = "正常流转" if inc_count == 0 else f"触发 {inc_count} 次自愈分析"
            lines.append(f"| **{game}** | `{stat_text}` | {inc_count} | {desc_text} |")

        if self.incidents:
            lines.extend([
                f"",
                f"---",
                f"",
                f"## 故障切片与现场诊断",
                f""
            ])

            for idx, inc in enumerate(self.incidents, 1):
                ui_anom = inc["ui_analysis"]
                dev = inc["device_diag"]
                lines.extend([
                    f"### 故障 #{idx}: 【{inc['game_name']}】 (阶段: `{inc['phase']}`)",
                    f"- 时间: `{inc['time']}`",
                    f"- 摘要: `{inc['error_msg']}`",
                    f"- 设备: 电量 `{dev.get('battery')}` | 屏幕 `{dev.get('wakefulness')}` | 前台窗口: `{dev.get('focused_window')}`",
                    f"- 根因: **[{ui_anom.get('type')}]** {ui_anom.get('detail')}",
                    f"- 现场截图: [{os.path.basename(inc['img_path'])}](file://{inc['img_path']})",
                    f"- 控件树快照: [{os.path.basename(inc['xml_path'])}](file://{inc['xml_path']})",
                    f""
                ])

                if inc["recovery_actions"]:
                    lines.append(f"**已执行自愈操作:**")
                    for act in inc["recovery_actions"]:
                        lines.append(f"- {act}")
                    lines.append(f"")

                if ui_anom.get("suggestions"):
                    lines.append(f"**诊断建议:**")
                    for sug in ui_anom["suggestions"]:
                        lines.append(f"- {sug}")
                    lines.append(f"")

        lines.extend([
            f"---",
            f"*(本报告由自动化系统生成)*"
        ])

        report_content = "\n".join(lines)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_content)

        return report_path

    def cleanup_old_reports(self, days: int = 7) -> int:
        """
        清理超出指定天数（默认7天）的历史执行报告与现场快照文件
        保留 .gitkeep
        返回被清理的文件总数
        """
        cutoff_time = time.time() - (days * 86400)
        deleted_count = 0

        # 1. 扫描 reports 根目录下的 .md 报告
        if os.path.exists(self.reports_dir):
            for fname in os.listdir(self.reports_dir):
                if fname == ".gitkeep":
                    continue
                fpath = os.path.join(self.reports_dir, fname)
                if os.path.isfile(fpath) and fname.startswith("report_") and fname.endswith(".md"):
                    try:
                        if os.path.getmtime(fpath) < cutoff_time:
                            os.remove(fpath)
                            deleted_count += 1
                    except Exception:
                        pass

        # 2. 扫描 snapshots 目录下的所有快照 (.png, .xml)
        if os.path.exists(self.snapshots_dir):
            for fname in os.listdir(self.snapshots_dir):
                if fname == ".gitkeep":
                    continue
                fpath = os.path.join(self.snapshots_dir, fname)
                if os.path.isfile(fpath):
                    try:
                        if os.path.getmtime(fpath) < cutoff_time:
                            os.remove(fpath)
                            deleted_count += 1
                    except Exception:
                        pass

        return deleted_count
